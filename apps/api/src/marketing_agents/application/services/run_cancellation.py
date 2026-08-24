"""Atomic cancellation fence for runtime-controlled read plans."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run, RunStep
from marketing_agents.domain.enums import Effect, RunState, StepState
from marketing_agents.domain.execution_control import AttemptOutcome
from marketing_agents.domain.run_lifecycle import (
    CancellationContext,
    RunLifecycleCommand,
    transition_run,
)
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.domain.step_lifecycle import (
    StepLifecycleCommand,
    StepTerminalContext,
    StepTransitionResult,
    transition_step,
)

from .audit_events import AuditEventFactory

_CANCEL_ACTOR_DOMAIN = b"marketing-agents:execution-cancel-actor:v1\x00"


class RunCancellationServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class RunCancellationResult:
    run: Run
    cancelled_steps: tuple[RunStep, ...]
    preserved_executing_step_ids: tuple[str, ...]


async def _executing_read_disposition(
    unit_of_work: UnitOfWork,
    *,
    run_id: str,
    plan_hash: str,
    control_version: int,
    step: RunStep,
) -> str:
    """Distinguish an open READ call from durable retry backoff, fail closed otherwise."""

    policy = step.runtime_policy
    if (
        step.run_id != run_id
        or step.plan_hash != plan_hash
        or step.effect is not Effect.READ
        or step.state is not StepState.EXECUTING
        or policy.attempt_kind not in {AttemptKind.MODEL, AttemptKind.TOOL}
        or step.version < 2
    ):
        raise ValueError("executing READ step binding is invalid")
    attempts = await unit_of_work.execution_control.list_attempts(
        step.id,
        policy.operation_key,
    )
    if not attempts or len(attempts) > policy.retry.max_attempts:
        raise ValueError("executing READ step attempt lineage is incomplete")

    previous_source_control_version = 0
    previous_retry_not_before = None
    for index, attempt in enumerate(attempts, start=1):
        expected_source_step_version = step.version - 1 if index == 1 else step.version
        if (
            attempt.attempt_number != index
            or attempt.run_id != run_id
            or attempt.step_id != step.id
            or attempt.operation_key != policy.operation_key
            or attempt.policy_hash != plan_hash
            or attempt.kind is not policy.attempt_kind
            or attempt.source_step_version != expected_source_step_version
            or attempt.source_control_version <= previous_source_control_version
            or attempt.source_control_version >= control_version
        ):
            raise ValueError("executing READ step attempt lineage is contradictory")
        if index == 1:
            if attempt.eligible_at != attempt.reserved_at:
                raise ValueError("first READ attempt has invalid eligibility")
        elif (
            previous_retry_not_before is None
            or attempt.eligible_at != previous_retry_not_before
            or attempt.reserved_at < previous_retry_not_before
        ):
            raise ValueError("retried READ attempt lacks exact prior authority")

        previous_source_control_version = attempt.source_control_version
        if index < len(attempts):
            if (
                attempt.outcome is not AttemptOutcome.TRANSIENT_FAILURE
                or attempt.retry_not_before is None
                or attempt.terminal_reason_code is not None
            ):
                raise ValueError("superseded READ attempt lacks retry authority")
            previous_retry_not_before = attempt.retry_not_before

    latest = attempts[-1]
    if latest.outcome is None:
        return "open"
    if (
        latest.outcome is AttemptOutcome.TRANSIENT_FAILURE
        and latest.retry_not_before is not None
        and latest.terminal_reason_code is None
    ):
        return "retry_backoff"
    raise ValueError("executing READ step has no open call or retry authority")


class RunCancellationService:
    """Fence future calls before cancelling queued work and the parent Run."""

    _MAX_STALE_CONTROL_RETRIES = 3

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies

    async def request(
        self,
        run_id: str,
        *,
        audit_context: AuditContext,
    ) -> RunCancellationResult:
        for retry_index in range(self._MAX_STALE_CONTROL_RETRIES):
            try:
                return await self._request_once(
                    run_id,
                    audit_context=audit_context,
                )
            except RunCancellationServiceError as exc:
                if (
                    exc.code != "stale_execution_control"
                    or retry_index == self._MAX_STALE_CONTROL_RETRIES - 1
                ):
                    raise
        raise AssertionError("bounded cancellation retry exhausted without an outcome")

    async def _request_once(
        self,
        run_id: str,
        *,
        audit_context: AuditContext,
    ) -> RunCancellationResult:
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(run_id)
            if run is None:
                raise RunCancellationServiceError(
                    "run_not_found", "Run does not exist", run_id=run_id
                )
            if run.state in {
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.REJECTED,
                RunState.CANCELLED,
            }:
                raise RunCancellationServiceError(
                    "terminal_state_immutable",
                    "terminal Run cannot accept another cancellation",
                    run_id=run_id,
                )
            plan = await unit_of_work.run_steps.get_plan(run_id)
            steps = await unit_of_work.run_steps.validate_plan_for_execution(run_id)
            if plan is None or plan.approval_required or run.approval_required is not False:
                raise RunCancellationServiceError(
                    "approval_boundary_service_required",
                    "write plans cancel through their approval boundary",
                    run_id=run_id,
                )
            control = await unit_of_work.execution_control.get(run_id)
            if control is None or control.policy_hash != plan.plan_hash:
                raise RunCancellationServiceError(
                    "execution_control_invalid",
                    "Run cancellation lacks its exact sealed execution control",
                    run_id=run_id,
                )
            occurred_at = self._dependencies.utc_now()
            actor_digest = hashlib.sha256(
                _CANCEL_ACTOR_DOMAIN + audit_context.actor_id.encode("utf-8")
            ).hexdigest()
            try:
                await unit_of_work.execution_control.request_cancel(
                    run_id=run_id,
                    expected_control_version=control.version,
                    actor_digest=actor_digest,
                    requested_at=occurred_at,
                )
            except RuntimeError as exc:
                raise RunCancellationServiceError(
                    getattr(exc, "code", "cancellation_conflict"),
                    "Run cancellation fence could not be persisted",
                    run_id=run_id,
                ) from exc

            preserved_executing_step_ids: list[str] = []
            retry_backoff_results: list[StepTransitionResult] = []
            for step in steps:
                if step.state is not StepState.EXECUTING:
                    continue
                try:
                    disposition = await _executing_read_disposition(
                        unit_of_work,
                        run_id=run_id,
                        plan_hash=plan.plan_hash,
                        control_version=control.version,
                        step=step,
                    )
                except (RuntimeError, ValueError) as exc:
                    raise RunCancellationServiceError(
                        "execution_attempt_lineage_invalid",
                        "executing READ step lacks exact attempt lineage",
                        run_id=run_id,
                    ) from exc
                if disposition == "open":
                    preserved_executing_step_ids.append(step.id)
                    continue
                retry_backoff_results.append(
                    transition_step(
                        step,
                        StepLifecycleCommand.FAIL,
                        StepTerminalContext("run_cancelled"),
                        occurred_at,
                    )
                )

            cancellable = tuple(
                step for step in steps if step.state in {StepState.PENDING, StepState.READY}
            )
            step_results = (
                *retry_backoff_results,
                *(
                    transition_step(
                        step,
                        StepLifecycleCommand.CANCEL,
                        StepTerminalContext("run_cancelled"),
                        occurred_at,
                    )
                    for step in cancellable
                ),
            )
            for result in step_results:
                applied = await unit_of_work.run_steps.apply_transition(
                    expected_run_version=run.version,
                    expected_run_state=run.state,
                    expected_version=result.transition.expected_version,
                    expected_state=result.transition.previous_state or StepState.PENDING,
                    result=result,
                )
                if not applied:
                    raise RunCancellationServiceError(
                        "cancellation_conflict",
                        "queued step changed before cancellation committed",
                        run_id=run_id,
                    )
            run_result = transition_run(
                run,
                RunLifecycleCommand.CANCEL,
                CancellationContext(reason_code="operator_cancelled"),
                occurred_at,
            )
            applied_run = await unit_of_work.runs.apply_transition(
                expected_version=run.version,
                expected_state=run.state,
                result=run_result,
            )
            if not applied_run:
                raise RunCancellationServiceError(
                    "cancellation_conflict",
                    "Run changed before cancellation committed",
                    run_id=run_id,
                )
            factory = AuditEventFactory(audit_context)
            await unit_of_work.audits.append_many(
                (
                    *(
                        factory.step_transition(result.step, result.transition)
                        for result in step_results
                    ),
                    factory.run_transition(run_result.run, run_result.transition),
                )
            )
            await unit_of_work.commit()
            return RunCancellationResult(
                run=run_result.run,
                cancelled_steps=tuple(
                    result.step
                    for result in step_results
                    if result.step.state is StepState.CANCELLED
                ),
                preserved_executing_step_ids=tuple(preserved_executing_step_ids),
            )


__all__ = [
    "RunCancellationResult",
    "RunCancellationService",
    "RunCancellationServiceError",
]
