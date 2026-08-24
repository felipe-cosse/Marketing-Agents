"""Short-transaction executor for durable generic READ operations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import ceil
from typing import Any

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.read_adapter import (
    ReadAdapter,
    ReadAdapterCancelledError,
    ReadAdapterContract,
    ReadAdapterError,
    ReadAdapterPermanentError,
    ReadAdapterRequest,
    ReadAdapterResult,
    ReadAdapterTransientError,
)
from marketing_agents.application.ports.repositories import (
    AttemptReservationResult,
    ExecutionControlRepositoryConflict,
)
from marketing_agents.domain.audit import (
    RUNTIME_CONTROL_DENIAL_CODES,
    TERMINAL_RUNTIME_CONTROL_DENIAL_CODES,
    AuditContext,
    AuditEventDraft,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import RunStep
from marketing_agents.domain.enums import Effect, RunState, StepState
from marketing_agents.domain.execution_control import (
    AttemptCompletionCommand,
    AttemptOutcome,
    AttemptReservationCommand,
    ExecutionAttempt,
    ExpiredAttemptRecoveryCommand,
    OperationExecutionPolicy,
)
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    canonical_payload_size_bytes,
    effective_call_timeout_seconds,
    payload_fields_within_byte_limit,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepTerminalContext,
    StepTransitionContext,
    transition_step,
)
from marketing_agents.domain.validation import frozen_json_mapping, require_id

from .audit_events import AuditEventFactory
from .terminal_execution_cleanup import TerminalExecutionCleanupService


class ReadExecutionClassification(StrEnum):
    SUCCEEDED = "succeeded"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ControlledReadExecutorError(RuntimeError):
    """Stable fail-closed error; adapter/provider details are never retained."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        step_id: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        require_id(code, "controlled READ error code")
        require_id(step_id, "controlled READ error step ID")
        super().__init__(message)
        self.code = code
        self.step_id = step_id
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class ControlledReadCommand:
    """Caller input contains no retry, deadline, counter, or operation authority."""

    step_id: str
    input_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_id(self.step_id, "controlled READ step ID")
        object.__setattr__(
            self,
            "input_payload",
            frozen_json_mapping(self.input_payload, "controlled READ input payload"),
        )


@dataclass(frozen=True, slots=True)
class ControlledReadResult:
    classification: ReadExecutionClassification
    attempt: ExecutionAttempt
    step: RunStep
    output: ReadAdapterResult | None
    retry_not_before: datetime | None
    cancellation_observed_after_return: bool

    def __post_init__(self) -> None:
        if type(self.classification) is not ReadExecutionClassification:
            raise ValueError("READ result classification must use the exact enum")
        if type(self.attempt) is not ExecutionAttempt or self.attempt.outcome is None:
            raise ValueError("READ result requires one completed durable attempt")
        if (
            type(self.step) is not RunStep
            or self.step.id != self.attempt.step_id
            or self.step.run_id != self.attempt.run_id
        ):
            raise ValueError("READ result step must bind its durable attempt")
        if type(self.cancellation_observed_after_return) is not bool:
            raise ValueError("READ result cancellation observation must be boolean")
        if self.retry_not_before != self.attempt.retry_not_before:
            raise ValueError("READ result retry time must match its durable attempt")
        expected_outcome = _attempt_outcome(self.classification)
        if self.attempt.outcome is not expected_outcome:
            raise ValueError("READ result classification differs from its durable outcome")
        if self.classification is ReadExecutionClassification.SUCCEEDED:
            if (
                type(self.output) is not ReadAdapterResult
                or self.attempt.outcome is not AttemptOutcome.SUCCEEDED
                or self.retry_not_before is not None
                or self.step.state is not StepState.SUCCEEDED
            ):
                raise ValueError("successful READ result is internally inconsistent")
            return
        if self.output is not None:
            raise ValueError("failed READ result cannot retain an adapter output")
        if self.retry_not_before is not None:
            if (
                self.classification
                not in {
                    ReadExecutionClassification.TRANSIENT_FAILURE,
                    ReadExecutionClassification.TIMED_OUT,
                }
                or self.attempt.outcome is not AttemptOutcome.TRANSIENT_FAILURE
                or self.step.state is not StepState.EXECUTING
            ):
                raise ValueError("retryable READ result is internally inconsistent")
        elif self.step.state is not StepState.FAILED:
            raise ValueError("terminal failed READ result must fail its step")

    @property
    def retryable(self) -> bool:
        return self.retry_not_before is not None


@dataclass(frozen=True, slots=True)
class _ReservedRead:
    reservation: AttemptReservationResult
    operation: OperationExecutionPolicy
    request: ReadAdapterRequest


@dataclass(frozen=True, slots=True)
class _RecoveredRead:
    result: ControlledReadResult


class _CompletionConflict(RuntimeError):
    pass


class ControlledReadExecutor:
    """Reserve, call outside transactions, then durably complete one READ attempt."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        adapter: ReadAdapter,
    ) -> None:
        self._dependencies = dependencies
        self._adapter = adapter

    async def execute(
        self,
        command: ControlledReadCommand,
        *,
        audit_context: AuditContext,
    ) -> ControlledReadResult:
        if type(command) is not ControlledReadCommand:
            raise TypeError("controlled READ execution requires an exact command")
        audit_context.verify_integrity()
        try:
            reservation = await self._reserve(command, audit_context=audit_context)
        except ControlledReadExecutorError as exc:
            await self._record_runtime_control_denial(
                command.step_id,
                exc,
                audit_context=audit_context,
            )
            raise
        if isinstance(reservation, _RecoveredRead):
            return reservation.result
        reserved = reservation

        classification: ReadExecutionClassification
        output: ReadAdapterResult | None = None
        runtime_denial_code: str | None = None
        timeout_seconds = (
            reserved.reservation.attempt.call_deadline_at - self._dependencies.utc_now()
        ).total_seconds()
        try:
            if timeout_seconds <= 0:
                classification = ReadExecutionClassification.TIMED_OUT
            else:
                loop = asyncio.get_running_loop()
                monotonic_deadline = loop.time() + timeout_seconds
                async with asyncio.timeout(timeout_seconds):
                    candidate = await self._adapter.execute(reserved.request)
                    returned_at = self._dependencies.utc_now()
                    returned_monotonic = loop.time()
                if (
                    returned_at >= reserved.request.call_deadline_at
                    or returned_monotonic >= monotonic_deadline
                ):
                    classification = ReadExecutionClassification.TIMED_OUT
                elif not _result_binds_request(candidate, reserved.request):
                    classification = ReadExecutionClassification.PERMANENT_FAILURE
                else:
                    runtime_denial_code = _result_runtime_denial_code(
                        candidate,
                        reserved.operation,
                    )
                    if runtime_denial_code is None:
                        classification = ReadExecutionClassification.SUCCEEDED
                        output = candidate
                    else:
                        classification = ReadExecutionClassification.PERMANENT_FAILURE
        except ReadAdapterTransientError:
            classification = ReadExecutionClassification.TRANSIENT_FAILURE
        except ReadAdapterPermanentError:
            classification = ReadExecutionClassification.PERMANENT_FAILURE
        except ReadAdapterCancelledError:
            classification = ReadExecutionClassification.CANCELLED
        except TimeoutError:
            classification = ReadExecutionClassification.TIMED_OUT
        except asyncio.CancelledError:
            completion = asyncio.create_task(
                self._complete(
                    reserved,
                    ReadExecutionClassification.CANCELLED,
                    None,
                    runtime_denial_code=None,
                    audit_context=audit_context,
                )
            )
            await asyncio.shield(completion)
            raise
        except Exception:
            classification = ReadExecutionClassification.PERMANENT_FAILURE

        try:
            return await self._complete(
                reserved,
                classification,
                output,
                runtime_denial_code=runtime_denial_code,
                audit_context=audit_context,
            )
        except ControlledReadExecutorError as exc:
            await self._record_runtime_control_denial(
                command.step_id,
                exc,
                audit_context=audit_context,
            )
            raise

    async def _record_runtime_control_denial(
        self,
        step_id: str,
        error: ControlledReadExecutorError,
        *,
        audit_context: AuditContext,
    ) -> None:
        """Append a redacted witness only after the denied transaction rolls back."""

        if error.code not in RUNTIME_CONTROL_DENIAL_CODES:
            return
        occurred_at = self._dependencies.utc_now()
        async with self._dependencies.unit_of_work() as unit_of_work:
            step = await unit_of_work.run_steps.get(step_id)
            if step is None:
                return
            event = AuditEventFactory(audit_context).runtime_control_denied(
                run_id=step.run_id,
                step_id=step.id,
                operation_key=step.runtime_policy.operation_key,
                denial_code=error.code,
                retry_after_seconds=_safe_audit_retry_after(error.retry_after_seconds),
                occurred_at=occurred_at,
            )
            existing = await unit_of_work.audits.get(event.id)
            if existing is not None:
                metadata = existing.safe_metadata.values
                retry_after_seconds = metadata.get("retry_after_seconds")
                if (
                    existing.event_type != event.event_type
                    or existing.aggregate_id != event.aggregate_id
                    or existing.run_id != event.run_id
                    or existing.step_id != event.step_id
                    or existing.action_id is not None
                    or frozenset(metadata)
                    not in {
                        frozenset({"denial_code", "operation_key"}),
                        frozenset({"denial_code", "operation_key", "retry_after_seconds"}),
                    }
                    or metadata.get("denial_code") != error.code
                    or metadata.get("operation_key") != step.runtime_policy.operation_key
                    or (
                        retry_after_seconds is not None
                        and (
                            type(retry_after_seconds) is not int
                            or not 1 <= retry_after_seconds <= 3_600
                        )
                    )
                ):
                    raise RuntimeError("runtime-control denial audit identity is corrupt")
            transition_events: list[AuditEventDraft] = []
            run = await unit_of_work.runs.get(step.run_id)
            if (
                error.code in TERMINAL_RUNTIME_CONTROL_DENIAL_CODES
                and run is not None
                and run.state is RunState.EXECUTING
            ):
                cleanup = await TerminalExecutionCleanupService().fail_runtime_control_in_uow(
                    unit_of_work,
                    run_id=run.id,
                    denied_step_id=step.id,
                    plan_hash=step.plan_hash,
                    denial_code=error.code,
                    occurred_at=occurred_at,
                    audit_context=audit_context,
                )
                transition_events.extend(cleanup.audit_events)
            events = (
                *((event,) if existing is None else ()),
                *transition_events,
            )
            if events:
                await unit_of_work.audits.append_many(events)
            await unit_of_work.commit()

    async def _reserve(
        self,
        command: ControlledReadCommand,
        *,
        audit_context: AuditContext,
    ) -> _ReservedRead | _RecoveredRead:
        try:
            async with self._dependencies.unit_of_work() as unit_of_work:
                step = await unit_of_work.run_steps.get(command.step_id)
                if step is None:
                    raise ControlledReadExecutorError(
                        "step_not_found",
                        "READ step does not exist",
                        step_id=command.step_id,
                    )
                if step.effect is not Effect.READ:
                    raise ControlledReadExecutorError(
                        "read_step_required",
                        "controlled READ executor rejects WRITE steps",
                        step_id=step.id,
                    )
                if step.runtime_policy.attempt_kind is AttemptKind.NO_CALL:
                    raise ControlledReadExecutorError(
                        "adapter_call_not_allowed",
                        "no-call step cannot reserve a READ adapter attempt",
                        step_id=step.id,
                    )
                operation_key = step.runtime_policy.operation_key
                operation = await unit_of_work.execution_control.get_operation(
                    step.id, operation_key
                )
                control = await unit_of_work.execution_control.get(step.run_id)
                run = await unit_of_work.runs.get(step.run_id)
                if (
                    operation is None
                    or control is None
                    or run is None
                    or run.state
                    not in {
                        RunState.EXECUTING,
                        RunState.CANCELLED,
                        RunState.FAILED,
                        RunState.REJECTED,
                    }
                    or not _operation_binds_step(operation, step)
                ):
                    raise ControlledReadExecutorError(
                        "execution_policy_invalid",
                        "READ step lacks its exact immutable operation policy",
                        step_id=step.id,
                    )
                if control.policy_hash != step.plan_hash or control.started_at is None:
                    raise ControlledReadExecutorError(
                        "execution_not_started",
                        "READ execution control has not been activated",
                        step_id=step.id,
                    )
                reserved_at = self._dependencies.utc_now()
                attempts = await unit_of_work.execution_control.list_attempts(
                    step.id,
                    operation_key,
                )
                if attempts and attempts[-1].outcome is None:
                    open_attempt = attempts[-1]
                    if reserved_at < open_attempt.call_deadline_at:
                        raise ControlledReadExecutorError(
                            "attempt_in_progress",
                            "prior READ attempt retains its bounded call authority",
                            step_id=step.id,
                            retry_after_seconds=_bounded_retry_after_seconds(
                                open_attempt.call_deadline_at,
                                reserved_at,
                            ),
                        )
                    cancellation_observed = (
                        control.cancel_requested_at is not None or run.state is RunState.CANCELLED
                    )
                    completed = await unit_of_work.execution_control.recover_expired_attempt(
                        ExpiredAttemptRecoveryCommand(
                            attempt_id=open_attempt.id,
                            expected_attempt_version=open_attempt.version,
                            expected_call_deadline_at=open_attempt.call_deadline_at,
                            recovered_at=reserved_at,
                        )
                    )
                    recovered_step = step
                    if completed.retry_not_before is None:
                        transition = transition_step(
                            step,
                            StepLifecycleCommand.FAIL,
                            StepTerminalContext(
                                "run_cancelled"
                                if cancellation_observed
                                else completed.terminal_reason_code or "retry_deadline_exceeded"
                            ),
                            reserved_at,
                        )
                        applied = await unit_of_work.run_steps.apply_transition(
                            expected_run_version=run.version,
                            expected_run_state=run.state,
                            expected_version=step.version,
                            expected_state=StepState.EXECUTING,
                            result=transition,
                        )
                        if not applied:
                            raise ControlledReadExecutorError(
                                "stale_recovery_conflict",
                                "expired READ attempt lost its step recovery fence",
                                step_id=step.id,
                            )
                        await unit_of_work.audits.append(
                            AuditEventFactory(audit_context).step_transition(
                                transition.step,
                                transition.transition,
                            )
                        )
                        recovered_step = transition.step
                    await unit_of_work.commit()
                    return _RecoveredRead(
                        ControlledReadResult(
                            classification=(
                                ReadExecutionClassification.CANCELLED
                                if completed.attempt.outcome is AttemptOutcome.CANCELLED
                                else (
                                    ReadExecutionClassification.PERMANENT_FAILURE
                                    if completed.attempt.outcome is AttemptOutcome.PERMANENT_FAILURE
                                    else ReadExecutionClassification.TIMED_OUT
                                )
                            ),
                            attempt=completed.attempt,
                            step=recovered_step,
                            output=None,
                            retry_not_before=completed.retry_not_before,
                            cancellation_observed_after_return=cancellation_observed,
                        )
                    )
                if run.state is not RunState.EXECUTING or control.cancel_requested_at is not None:
                    raise ControlledReadExecutorError(
                        (
                            "run_cancelled"
                            if control.cancel_requested_at is not None
                            or run.state is RunState.CANCELLED
                            else "run_terminal"
                        ),
                        "terminal Run cannot reserve another READ attempt",
                        step_id=step.id,
                    )
                if not payload_fields_within_byte_limit(
                    command.input_payload,
                    operation.max_input_field_bytes,
                ):
                    raise ControlledReadExecutorError(
                        "input_field_too_large",
                        "READ input contains a field outside its sealed byte limit",
                        step_id=step.id,
                    )
                if canonical_payload_size_bytes(command.input_payload) > operation.max_input_bytes:
                    raise ControlledReadExecutorError(
                        "input_payload_too_large",
                        "READ input exceeds its sealed canonical byte limit",
                        step_id=step.id,
                    )
                try:
                    validated_steps = await unit_of_work.run_steps.validate_plan_for_execution(
                        run.id
                    )
                except RuntimeError as exc:
                    raise ControlledReadExecutorError(
                        "execution_plan_invalid",
                        "READ execution cannot validate its sealed plan",
                        step_id=step.id,
                    ) from exc
                if next((item for item in validated_steps if item.id == step.id), None) != step:
                    raise ControlledReadExecutorError(
                        "execution_plan_mismatch",
                        "READ step differs from its sealed execution plan",
                        step_id=step.id,
                    )
                try:
                    expected_contract = ReadAdapterContract.from_operation(operation)
                    declared_contract = self._adapter.contract_for(operation)
                except ReadAdapterError as exc:
                    safe_code = (
                        exc.code
                        if exc.code
                        in {
                            "adapter_contract_drift",
                            "adapter_contract_invalid",
                            "adapter_contract_unavailable",
                        }
                        else "adapter_contract_unavailable"
                    )
                    raise ControlledReadExecutorError(
                        safe_code,
                        "READ adapter rejected the sealed operation contract",
                        step_id=step.id,
                    ) from exc
                except (TypeError, ValueError) as exc:
                    raise ControlledReadExecutorError(
                        "adapter_contract_invalid",
                        "READ adapter contract could not be validated",
                        step_id=step.id,
                    ) from exc
                if (
                    type(declared_contract) is not ReadAdapterContract
                    or declared_contract != expected_contract
                ):
                    raise ControlledReadExecutorError(
                        "adapter_contract_drift",
                        "READ adapter contract differs from sealed execution policy",
                        step_id=step.id,
                    )
                reservation = await unit_of_work.execution_control.reserve_attempt(
                    AttemptReservationCommand(
                        attempt_id=self._dependencies.new_id("execution-attempt"),
                        run_id=step.run_id,
                        step_id=step.id,
                        operation_key=operation_key,
                        expected_control_version=control.version,
                        expected_step_version=step.version,
                        reserved_at=reserved_at,
                    )
                )
                attempt = reservation.attempt
                if attempt.outcome is not None:
                    raise ControlledReadExecutorError(
                        "attempt_replay_not_callable",
                        "completed attempt cannot authorize another adapter call",
                        step_id=step.id,
                    )
                if attempt.attempt_number == 1:
                    if step.state is not StepState.READY:
                        raise ControlledReadExecutorError(
                            "first_attempt_step_invalid",
                            "first READ attempt requires one READY step",
                            step_id=step.id,
                        )
                    transition = transition_step(
                        step,
                        StepLifecycleCommand.START,
                        NoStepTransitionContext(),
                        reserved_at,
                    )
                    applied = await unit_of_work.run_steps.apply_transition(
                        expected_run_version=run.version,
                        expected_run_state=RunState.EXECUTING,
                        expected_version=step.version,
                        expected_state=StepState.READY,
                        result=transition,
                    )
                    if not applied:
                        raise ControlledReadExecutorError(
                            "step_start_conflict",
                            "READ step changed before its controlled attempt committed",
                            step_id=step.id,
                        )
                    await unit_of_work.audits.append(
                        AuditEventFactory(audit_context).step_transition(
                            transition.step,
                            transition.transition,
                        )
                    )
                elif step.state is not StepState.EXECUTING:
                    raise ControlledReadExecutorError(
                        "retry_step_invalid",
                        "READ retry requires its already executing step",
                        step_id=step.id,
                    )
                await unit_of_work.commit()
                request = ReadAdapterRequest(
                    attempt_id=attempt.id,
                    run_id=attempt.run_id,
                    step_id=attempt.step_id,
                    operation_key=attempt.operation_key,
                    policy_hash=attempt.policy_hash,
                    attempt_number=attempt.attempt_number,
                    call_deadline_at=attempt.call_deadline_at,
                    correlation_id=audit_context.correlation_id,
                    requested_timeout_seconds=operation.step_timeout_seconds,
                    provenance_ids=(f"work-item:{run.work_item_id}",),
                    input_classification=DataClassification.INTERNAL,
                    contract=expected_contract,
                    input_payload=command.input_payload,
                )
                return _ReservedRead(reservation, operation, request)
        except ControlledReadExecutorError:
            raise
        except ExecutionControlRepositoryConflict as exc:
            raise ControlledReadExecutorError(
                getattr(exc, "code", "reservation_conflict"),
                "READ attempt could not be reserved",
                step_id=command.step_id,
                retry_after_seconds=getattr(exc, "retry_after_seconds", None),
            ) from exc
        except RuntimeError as exc:
            raise ControlledReadExecutorError(
                "reservation_conflict",
                "READ attempt reservation did not commit",
                step_id=command.step_id,
            ) from exc

    async def _complete(
        self,
        reserved: _ReservedRead,
        classification: ReadExecutionClassification,
        output: ReadAdapterResult | None,
        *,
        runtime_denial_code: str | None,
        audit_context: AuditContext,
    ) -> ControlledReadResult:
        for retry_index in range(3):
            try:
                return await self._complete_once(
                    reserved,
                    classification,
                    output,
                    runtime_denial_code=runtime_denial_code,
                    audit_context=audit_context,
                )
            except _CompletionConflict as exc:
                if retry_index == 2:
                    raise ControlledReadExecutorError(
                        "completion_conflict",
                        "READ completion lost its cancellation or step fence",
                        step_id=reserved.request.step_id,
                    ) from exc
        raise AssertionError("bounded READ completion retry exhausted without an outcome")

    async def _complete_once(
        self,
        reserved: _ReservedRead,
        classification: ReadExecutionClassification,
        output: ReadAdapterResult | None,
        *,
        runtime_denial_code: str | None,
        audit_context: AuditContext,
    ) -> ControlledReadResult:
        try:
            async with self._dependencies.unit_of_work() as unit_of_work:
                attempt = await unit_of_work.execution_control.get_attempt(
                    reserved.reservation.attempt.id
                )
                step = await unit_of_work.run_steps.get(reserved.request.step_id)
                control = await unit_of_work.execution_control.get(reserved.request.run_id)
                run = await unit_of_work.runs.get(reserved.request.run_id)
                if (
                    attempt is None
                    or attempt.outcome is not None
                    or step is None
                    or control is None
                    or run is None
                    or step.state is not StepState.EXECUTING
                    or step.run_id != run.id
                    or attempt.step_id != step.id
                    or attempt.policy_hash != control.policy_hash
                    or control.policy_hash != step.plan_hash
                    or run.state
                    not in {
                        RunState.EXECUTING,
                        RunState.CANCELLED,
                        RunState.FAILED,
                        RunState.REJECTED,
                    }
                ):
                    raise ControlledReadExecutorError(
                        "completion_state_invalid",
                        "READ completion lacks its exact persisted attempt and step",
                        step_id=reserved.request.step_id,
                    )
                try:
                    validated_steps = await unit_of_work.run_steps.validate_plan_for_execution(
                        run.id
                    )
                except RuntimeError as exc:
                    raise ControlledReadExecutorError(
                        "completion_plan_invalid",
                        "READ completion cannot validate its sealed plan",
                        step_id=step.id,
                    ) from exc
                if next((item for item in validated_steps if item.id == step.id), None) != step:
                    raise ControlledReadExecutorError(
                        "completion_plan_mismatch",
                        "READ completion step differs from its sealed plan",
                        step_id=step.id,
                    )

                cancellation_observed = (
                    control.cancel_requested_at is not None or run.state is RunState.CANCELLED
                )
                terminal_parent = run.state in {RunState.FAILED, RunState.REJECTED}
                effective_classification = classification
                effective_runtime_denial_code = runtime_denial_code
                if (
                    cancellation_observed
                    and classification is not ReadExecutionClassification.SUCCEEDED
                ):
                    effective_classification = ReadExecutionClassification.CANCELLED
                    effective_runtime_denial_code = None
                elif (
                    terminal_parent and classification is not ReadExecutionClassification.SUCCEEDED
                ):
                    effective_classification = ReadExecutionClassification.PERMANENT_FAILURE
                    effective_runtime_denial_code = None
                outcome = _attempt_outcome(effective_classification)
                completed_at = self._dependencies.utc_now()
                completed = await unit_of_work.execution_control.complete_attempt(
                    AttemptCompletionCommand(
                        attempt_id=attempt.id,
                        outcome=outcome,
                        expected_control_version=control.version,
                        completed_at=completed_at,
                    )
                )
                updated_step = step
                if completed.retry_not_before is None:
                    if effective_runtime_denial_code is not None:
                        cleanup = (
                            await TerminalExecutionCleanupService().fail_runtime_control_in_uow(
                                unit_of_work,
                                run_id=run.id,
                                denied_step_id=step.id,
                                plan_hash=step.plan_hash,
                                denial_code=effective_runtime_denial_code,
                                occurred_at=completed_at,
                                audit_context=audit_context,
                            )
                        )
                        denial_event = AuditEventFactory(audit_context).runtime_control_denied(
                            run_id=run.id,
                            step_id=step.id,
                            operation_key=reserved.operation.operation_key,
                            denial_code=effective_runtime_denial_code,
                            occurred_at=completed_at,
                        )
                        await unit_of_work.audits.append_many((denial_event, *cleanup.audit_events))
                        updated_step = cleanup.denied_step
                    elif effective_classification is ReadExecutionClassification.SUCCEEDED:
                        command = StepLifecycleCommand.SUCCEED
                        context: StepTransitionContext = NoStepTransitionContext()
                    else:
                        command = StepLifecycleCommand.FAIL
                        context = StepTerminalContext(
                            "run_cancelled"
                            if cancellation_observed
                            else completed.terminal_reason_code or "unclassified_failure"
                        )
                    if effective_runtime_denial_code is None:
                        transition = transition_step(step, command, context, completed_at)
                        applied = await unit_of_work.run_steps.apply_transition(
                            expected_run_version=run.version,
                            expected_run_state=run.state,
                            expected_version=step.version,
                            expected_state=StepState.EXECUTING,
                            result=transition,
                        )
                        if not applied:
                            raise _CompletionConflict("parent Run or READ step changed")
                        await unit_of_work.audits.append(
                            AuditEventFactory(audit_context).step_transition(
                                transition.step, transition.transition
                            )
                        )
                        updated_step = transition.step
                await unit_of_work.commit()
                return ControlledReadResult(
                    classification=effective_classification,
                    attempt=completed.attempt,
                    step=updated_step,
                    output=(
                        output
                        if effective_classification is ReadExecutionClassification.SUCCEEDED
                        else None
                    ),
                    retry_not_before=completed.retry_not_before,
                    cancellation_observed_after_return=cancellation_observed,
                )
        except (_CompletionConflict, ControlledReadExecutorError):
            raise
        except ExecutionControlRepositoryConflict as exc:
            if getattr(exc, "code", None) in {
                "stale_execution_control",
                "stale_parent_run",
            }:
                raise _CompletionConflict("READ completion lost its cancellation fence") from exc
            raise ControlledReadExecutorError(
                getattr(exc, "code", "completion_conflict"),
                "READ attempt could not be completed",
                step_id=reserved.request.step_id,
                retry_after_seconds=getattr(exc, "retry_after_seconds", None),
            ) from exc
        except RuntimeError as exc:
            raise ControlledReadExecutorError(
                "completion_conflict",
                "READ attempt completion did not commit",
                step_id=reserved.request.step_id,
            ) from exc


def _operation_binds_step(operation: OperationExecutionPolicy, step: RunStep) -> bool:
    policy = step.runtime_policy
    return (
        type(operation) is OperationExecutionPolicy
        and operation.run_id == step.run_id
        and operation.step_id == step.id
        and operation.operation_key == policy.operation_key
        and operation.kind is policy.attempt_kind
        and operation.capability_id == step.capability_id
        and operation.selected_instance_id == step.selected_instance_id
        and operation.configuration_revision == step.configuration_revision
        and operation.connector_family == step.connector_family
        and operation.binding_id == step.binding_id
        and operation.binding_configuration_revision == step.binding_configuration_revision
        and operation.request_schema_id == step.request_schema_id
        and operation.result_schema_id == step.result_schema_id
        and operation.request_redaction_fields == step.request_redaction_fields
        and operation.result_redaction_fields == step.result_redaction_fields
        and operation.data_classification is step.data_classification
        and operation.connector_timeout_seconds == step.timeout_seconds
        and operation.policy_hash == step.plan_hash
        and operation.max_attempts == policy.retry.max_attempts
        and operation.retry_backoff is policy.retry.backoff
        and operation.step_timeout_seconds
        == effective_call_timeout_seconds(policy, step.timeout_seconds)
        and operation.max_input_bytes == policy.budget.max_input_bytes
        and operation.max_input_field_bytes == policy.budget.max_input_field_bytes
        and operation.max_output_bytes == policy.budget.max_output_bytes
        and operation.max_model_output_tokens == policy.budget.max_model_output_tokens
        and operation.rate_limit_scope is policy.rate_limit.scope
        and operation.rate_limit_key == policy.rate_limit.key
        and operation.rate_window_max_calls == policy.rate_limit.max_calls
        and operation.rate_window_seconds == policy.rate_limit.window_seconds
    )


def _attempt_outcome(classification: ReadExecutionClassification) -> AttemptOutcome:
    if classification is ReadExecutionClassification.SUCCEEDED:
        return AttemptOutcome.SUCCEEDED
    if classification in {
        ReadExecutionClassification.TRANSIENT_FAILURE,
        ReadExecutionClassification.TIMED_OUT,
    }:
        return AttemptOutcome.TRANSIENT_FAILURE
    if classification is ReadExecutionClassification.PERMANENT_FAILURE:
        return AttemptOutcome.PERMANENT_FAILURE
    return AttemptOutcome.CANCELLED


def _safe_audit_retry_after(value: int | None) -> int | None:
    if type(value) is int and 1 <= value <= 3_600:
        return value
    return None


def _result_binds_request(result: ReadAdapterResult, request: ReadAdapterRequest) -> bool:
    return (
        type(result) is ReadAdapterResult
        and result.attempt_id == request.attempt_id
        and result.run_id == request.run_id
        and result.step_id == request.step_id
        and result.operation_key == request.operation_key
        and result.policy_hash == request.policy_hash
        and result.attempt_number == request.attempt_number
        and result.contract == request.contract
        and result.provenance_ids == request.provenance_ids
        and result.classification is request.contract.data_classification
    )


def _result_runtime_denial_code(
    result: ReadAdapterResult,
    operation: OperationExecutionPolicy,
) -> str | None:
    """Validate untrusted result budgets without retaining result content or sizes."""

    if canonical_payload_size_bytes(result.output_payload) > operation.max_output_bytes:
        return "output_payload_too_large"
    tokens = result.model_output_tokens
    if operation.kind is AttemptKind.MODEL:
        if type(tokens) is not int or tokens < 0:
            return "model_output_tokens_invalid"
        if tokens > operation.max_model_output_tokens:
            return "model_output_tokens_exceeded"
    elif tokens is not None:
        return "model_output_tokens_invalid"
    return None


def _bounded_retry_after_seconds(eligible_at: datetime, now: datetime) -> int:
    return min(max(ceil((eligible_at - now).total_seconds()), 1), 3_600)


__all__ = [
    "ControlledReadCommand",
    "ControlledReadExecutor",
    "ControlledReadExecutorError",
    "ControlledReadResult",
    "ReadExecutionClassification",
]
