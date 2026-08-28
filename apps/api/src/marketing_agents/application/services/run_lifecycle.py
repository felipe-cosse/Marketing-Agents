"""Transactional primary-Run receipt and command-only lifecycle advancement."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.run_lifecycle import (
    CompletionContext,
    RunLifecycleCommand,
    RunStateTransition,
    RunTransitionContext,
    RunTransitionError,
    RunTransitionEvidence,
    RunTransitionResult,
    initial_received_transition,
    transition_run,
)

from .audit_events import AuditEventFactory

_REJECTION_RETRY_DELAYS_SECONDS = (0.01, 0.02)


class RunLifecycleServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
        current_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id
        self.current_version = current_version


class RunAdvanceDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CAS_LOST = "cas_lost"


@dataclass(frozen=True, slots=True)
class RunAdvanceAttempt:
    """Caller-owned-UoW outcome; rejected witnesses must be committed by the caller."""

    disposition: RunAdvanceDisposition
    result: RunTransitionResult | None = None
    error: RunTransitionError | RunLifecycleServiceError | None = None
    rejection_reason_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.disposition) is not RunAdvanceDisposition:
            raise ValueError("Run advance disposition must use the exact enum")
        if self.disposition is RunAdvanceDisposition.ACCEPTED:
            if (
                self.result is None
                or self.error is not None
                or self.rejection_reason_code is not None
            ):
                raise ValueError("accepted Run advance must contain only its transition result")
        elif self.result is not None or self.error is None or self.rejection_reason_code is None:
            raise ValueError("rejected or CAS-lost Run advance requires its typed rejection")


@dataclass(frozen=True, slots=True)
class ReceiveRunRequest:
    work_item: WorkItem
    catalog_hash: str


@dataclass(frozen=True, slots=True)
class ReceiveRunResult:
    run: Run
    created: bool
    initial_transition: RunStateTransition | None


class RunLifecycleService:
    """Persist each accepted transition and its redacted audit witness atomically."""

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies

    async def receive(
        self,
        request: ReceiveRunRequest,
        *,
        audit_context: AuditContext,
    ) -> ReceiveRunResult:
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self.receive_in_uow(
                unit_of_work,
                request,
                audit_context=audit_context,
            )
            await unit_of_work.commit()
            return result

    async def receive_in_uow(
        self,
        unit_of_work: UnitOfWork,
        request: ReceiveRunRequest,
        *,
        audit_context: AuditContext,
    ) -> ReceiveRunResult:
        now = self._dependencies.utc_now()
        candidate = Run(
            id=self._dependencies.new_id("run"),
            work_item_id=request.work_item.id,
            state=RunState.RECEIVED,
            catalog_hash=request.catalog_hash,
            configuration_revision=request.work_item.configuration_revision,
            created_at=now,
            version=1,
            updated_at=now,
        )
        initial = initial_received_transition(candidate)
        stored = await unit_of_work.runs.add_received_or_get(candidate, initial)
        factory = AuditEventFactory(audit_context)
        initial_snapshot = _initial_run_snapshot(stored.run)
        expected_event = factory.run_transition(
            initial_snapshot,
            initial_received_transition(initial_snapshot),
        )
        if stored.inserted:
            await unit_of_work.audits.append(expected_event)
            return ReceiveRunResult(stored.run, created=True, initial_transition=initial)
        existing = await unit_of_work.audits.get_mutation_event("run", stored.run.id, 1)
        _require_initial_run_event(existing, initial_snapshot)
        return ReceiveRunResult(stored.run, created=False, initial_transition=None)

    async def advance(
        self,
        run_id: str,
        expected_version: int,
        command: RunLifecycleCommand,
        context: RunTransitionContext,
        *,
        audit_context: AuditContext,
    ) -> RunTransitionResult:
        async with self._dependencies.unit_of_work() as unit_of_work:
            attempt = await self.attempt_advance_in_uow(
                unit_of_work,
                run_id,
                expected_version,
                command,
                context,
                audit_context=audit_context,
            )
            if attempt.disposition is not RunAdvanceDisposition.CAS_LOST:
                await unit_of_work.commit()
        if attempt.disposition is RunAdvanceDisposition.ACCEPTED:
            if attempt.result is None:  # pragma: no cover - dataclass invariant
                raise AssertionError("accepted Run advance lost its result")
            return attempt.result
        if attempt.disposition is RunAdvanceDisposition.CAS_LOST:
            await self._persist_rejection(
                run_id,
                expected_version,
                command,
                attempt.rejection_reason_code or "stale_run_version",
                audit_context,
            )
        if attempt.error is None:  # pragma: no cover - dataclass invariant
            raise AssertionError("rejected Run advance lost its error")
        raise attempt.error

    async def attempt_advance_in_uow(
        self,
        unit_of_work: UnitOfWork,
        run_id: str,
        expected_version: int,
        command: RunLifecycleCommand,
        context: RunTransitionContext,
        *,
        audit_context: AuditContext,
    ) -> RunAdvanceAttempt:
        current = await unit_of_work.runs.get(run_id)
        if current is None:
            raise RunLifecycleServiceError("run_not_found", "run does not exist", run_id=run_id)
        approval_boundary_command = command in {
            RunLifecycleCommand.RELEASE_APPROVED_PLAN,
            RunLifecycleCommand.REJECT_APPROVAL,
        } or (
            current.approval_required is True
            and command
            in {
                RunLifecycleCommand.ACTIVATE_PLAN,
                RunLifecycleCommand.CANCEL,
            }
        )
        if approval_boundary_command:
            error = RunLifecycleServiceError(
                "approval_boundary_service_required",
                "write-plan lifecycle commands require persisted approval-boundary composition",
                run_id=run_id,
                current_version=current.version,
            )
            return await self._rejected_attempt_in_uow(
                unit_of_work,
                current=current,
                expected_version=expected_version,
                command=command,
                reason_code="invalid_transition",
                occurred_at=self._dependencies.utc_now(),
                audit_context=audit_context,
                error=error,
            )
        if (
            current.approval_required is False
            and command in {RunLifecycleCommand.ACTIVATE_PLAN, RunLifecycleCommand.CANCEL}
            and await unit_of_work.execution_control.get(current.id) is not None
        ):
            error = RunLifecycleServiceError(
                "execution_control_service_required",
                "controlled activation and cancellation require runtime-control composition",
                run_id=run_id,
                current_version=current.version,
            )
            return await self._rejected_attempt_in_uow(
                unit_of_work,
                current=current,
                expected_version=expected_version,
                command=command,
                reason_code="invalid_transition",
                occurred_at=self._dependencies.utc_now(),
                audit_context=audit_context,
                error=error,
            )
        if command is RunLifecycleCommand.RECORD_PLAN:
            error = RunLifecycleServiceError(
                "record_plan_requires_snapshot",
                "record-plan transitions require atomic plan persistence",
                run_id=run_id,
                current_version=current.version,
            )
            return await self._rejected_attempt_in_uow(
                unit_of_work,
                current=current,
                expected_version=expected_version,
                command=command,
                reason_code="invalid_transition",
                occurred_at=self._dependencies.utc_now(),
                audit_context=audit_context,
                error=error,
            )
        if current.version != expected_version:
            error = RunLifecycleServiceError(
                "stale_run_version",
                "run changed before the lifecycle command was applied",
                run_id=run_id,
                current_version=current.version,
            )
            return await self._rejected_attempt_in_uow(
                unit_of_work,
                current=current,
                expected_version=expected_version,
                command=command,
                reason_code=error.code,
                occurred_at=self._dependencies.utc_now(),
                audit_context=audit_context,
                error=error,
            )
        occurred_at = self._dependencies.utc_now()
        try:
            if command in {RunLifecycleCommand.ACTIVATE_PLAN, RunLifecycleCommand.COMPLETE}:
                plan = await unit_of_work.run_steps.get_plan(current.id)
                steps = await unit_of_work.run_steps.validate_plan_for_execution(current.id)
                if plan is None or plan.approval_required != current.approval_required:
                    raise RunLifecycleServiceError(
                        "plan_snapshot_invalid",
                        "Run approval disposition differs from its persisted plan",
                        run_id=current.id,
                        current_version=current.version,
                    )
                if command is RunLifecycleCommand.COMPLETE and isinstance(
                    context, CompletionContext
                ):
                    succeeded = sum(step.state is StepState.SUCCEEDED for step in steps)
                    failed = sum(step.state is StepState.FAILED for step in steps)
                    persisted_context = CompletionContext(
                        total_step_count=len(steps),
                        succeeded_step_count=succeeded,
                        failed_step_count=failed,
                        unfinished_step_count=len(steps) - succeeded - failed,
                    )
                    if context != persisted_context:
                        raise RunTransitionError(
                            "execution_incomplete",
                            "completion counts do not match persisted Run steps",
                            RunTransitionEvidence(
                                accepted=False,
                                run_id=current.id,
                                command=command,
                                previous_state=current.state,
                                requested_state=RunState.COMPLETED,
                                reason_code="execution_incomplete",
                                expected_version=current.version,
                                occurred_at=occurred_at,
                            ),
                        )
                    context = persisted_context
            result = transition_run(current, command, context, occurred_at)
        except RunTransitionError as exc:
            return await self._rejected_attempt_in_uow(
                unit_of_work,
                current=current,
                expected_version=expected_version,
                command=command,
                reason_code=exc.code,
                occurred_at=exc.audit_evidence.occurred_at,
                audit_context=audit_context,
                error=exc,
            )
        except RuntimeError as exc:
            if isinstance(exc, RunLifecycleServiceError):
                raise
            raise RunLifecycleServiceError(
                "plan_snapshot_invalid",
                "Run execution plan is missing or corrupt",
                run_id=current.id,
                current_version=current.version,
            ) from exc
        applied = await unit_of_work.runs.apply_transition(
            expected_version=expected_version,
            expected_state=current.state,
            result=result,
        )
        if not applied:
            error = RunLifecycleServiceError(
                "stale_run_version",
                "run changed concurrently before the lifecycle command was persisted",
                run_id=run_id,
            )
            return RunAdvanceAttempt(
                RunAdvanceDisposition.CAS_LOST,
                error=error,
                rejection_reason_code=error.code,
            )
        await unit_of_work.audits.append(
            AuditEventFactory(audit_context).run_transition(
                result.run,
                result.transition,
            )
        )
        return RunAdvanceAttempt(RunAdvanceDisposition.ACCEPTED, result=result)

    async def _rejected_attempt_in_uow(
        self,
        unit_of_work: UnitOfWork,
        *,
        current: Run,
        expected_version: int,
        command: RunLifecycleCommand,
        reason_code: str,
        occurred_at: datetime,
        audit_context: AuditContext,
        error: RunTransitionError | RunLifecycleServiceError,
    ) -> RunAdvanceAttempt:
        fenced = await unit_of_work.runs.fence(
            run_id=current.id,
            expected_version=current.version,
            expected_state=current.state,
        )
        if not fenced:
            return RunAdvanceAttempt(
                RunAdvanceDisposition.CAS_LOST,
                error=error,
                rejection_reason_code=reason_code,
            )
        try:
            await self._append_rejection_in_uow(
                unit_of_work,
                current=current,
                expected_version=expected_version,
                command=command,
                reason_code=reason_code,
                occurred_at=occurred_at,
                audit_context=audit_context,
            )
        except RuntimeError as exc:
            if getattr(exc, "code", None) not in {
                "audit_sequence_busy",
                "audit_append_conflict",
            }:
                raise
            return RunAdvanceAttempt(
                RunAdvanceDisposition.CAS_LOST,
                error=error,
                rejection_reason_code=reason_code,
            )
        return RunAdvanceAttempt(
            RunAdvanceDisposition.REJECTED,
            error=error,
            rejection_reason_code=reason_code,
        )

    async def _persist_rejection(
        self,
        run_id: str,
        expected_version: int,
        command: RunLifecycleCommand,
        reason_code: str,
        audit_context: AuditContext,
    ) -> None:
        for retry_index in range(len(_REJECTION_RETRY_DELAYS_SECONDS) + 1):
            try:
                async with self._dependencies.unit_of_work() as unit_of_work:
                    current = await unit_of_work.runs.get(run_id)
                    if current is None:
                        return
                    fenced = await unit_of_work.runs.fence(
                        run_id=current.id,
                        expected_version=current.version,
                        expected_state=current.state,
                    )
                    if fenced:
                        await self._append_rejection_in_uow(
                            unit_of_work,
                            current=current,
                            expected_version=expected_version,
                            command=command,
                            reason_code=reason_code,
                            occurred_at=self._dependencies.utc_now(),
                            audit_context=audit_context,
                        )
                        await unit_of_work.commit()
                        return
            except RuntimeError as exc:
                if getattr(exc, "code", None) not in {
                    "audit_sequence_busy",
                    "audit_append_conflict",
                } or retry_index == len(_REJECTION_RETRY_DELAYS_SECONDS):
                    raise
            if retry_index < len(_REJECTION_RETRY_DELAYS_SECONDS):
                # Let the winning transaction commit after this UoW has released its snapshot.
                await asyncio.sleep(_REJECTION_RETRY_DELAYS_SECONDS[retry_index])
        raise RunLifecycleServiceError(
            "audit_rejection_race",
            "rejected Run attempt could not acquire a stable observation fence",
            run_id=run_id,
        )

    async def _append_rejection_in_uow(
        self,
        unit_of_work: UnitOfWork,
        *,
        current: Run,
        expected_version: int,
        command: RunLifecycleCommand,
        reason_code: str,
        occurred_at: datetime,
        audit_context: AuditContext,
    ) -> None:
        factory = AuditEventFactory(audit_context)
        attempt_id = factory.run_attempt_id(current.id, command)
        existing = await unit_of_work.audits.get_attempt_event(current.id, attempt_id)
        if existing is not None:
            _require_same_rejected_attempt(
                existing,
                expected_version=expected_version,
                current=current,
                command=command,
                reason_code=reason_code,
                audit_context=audit_context,
            )
            return
        event = factory.run_transition_rejected(
            current,
            command=command,
            caller_expected_version=expected_version,
            reason_code=reason_code,
            occurred_at=occurred_at,
        )
        await unit_of_work.audits.append(event)

    async def history(self, run_id: str) -> tuple[RunStateTransition, ...]:
        async with self._dependencies.unit_of_work() as unit_of_work:
            return await unit_of_work.runs.list_transitions(run_id)


def _initial_run_snapshot(run: Run) -> Run:
    return replace(
        run,
        state=RunState.RECEIVED,
        updated_at=run.created_at,
        approval_required=None,
        terminal_reason_code=None,
        version=1,
    )


def _require_initial_run_event(existing: AuditEvent | None, run: Run) -> None:
    if existing is None:
        raise RunLifecycleServiceError(
            "run_audit_witness_missing",
            "persisted Run mutation lacks its exact audit witness",
            run_id=run.id,
        )
    draft = existing.draft
    expected_catalog = (
        run.catalog_hash
        if run.catalog_hash.startswith("catalog-sha256-v1:")
        else "catalog-sha256-v1:" + run.catalog_hash
    )
    if (
        draft.event_type != "run.received"
        or existing.run_sequence != 1
        or draft.run_id != run.id
        or draft.aggregate_id != run.id
        or draft.mutation_version != 1
        or draft.transition_sequence != 1
        or draft.occurred_at != run.created_at
        or draft.previous_state is not None
        or draft.new_state != RunState.RECEIVED.value
        or draft.reason_code != "work_admitted"
        or dict(draft.safe_metadata.values)
        != {"command": "receive", "catalog_content_hash": expected_catalog}
    ):
        raise RunLifecycleServiceError(
            "run_audit_witness_mismatch",
            "persisted Run initial audit witness does not match its authoritative snapshot",
            run_id=run.id,
        )


def _require_same_rejected_attempt(
    existing: AuditEvent,
    *,
    expected_version: int,
    current: Run,
    command: RunLifecycleCommand,
    reason_code: str,
    audit_context: AuditContext,
) -> None:
    draft = existing.draft
    expected_reason = (
        reason_code
        if reason_code
        in {
            "approval_barrier_incomplete",
            "approval_rejection_mismatch",
            "execution_incomplete",
            "failure_phase_mismatch",
            "invalid_cancellation_effects",
            "invalid_transition",
            "non_monotonic_time",
            "stale_run_version",
            "terminal_state_immutable",
        }
        else "unclassified_failure"
    )
    if (
        draft.actor_id != audit_context.actor_id
        or draft.actor_source is not audit_context.actor_source
        or draft.auth_method != audit_context.auth_method
        or draft.correlation_id != audit_context.correlation_id
        or draft.attempted_command != command.value
        or draft.expected_version != expected_version
        or draft.observed_version != current.version
        or draft.observed_state != current.state.value
        or draft.requested_state != _requested_state_for_run(current, command)
        or draft.reason_code != expected_reason
    ):
        raise RunLifecycleServiceError(
            "audit_attempt_identity_conflict",
            "rejected Run attempt identity maps to different observed facts",
            run_id=current.id,
            current_version=current.version,
        )


def _requested_state_for_run(run: Run, command: RunLifecycleCommand) -> str | None:
    if command is RunLifecycleCommand.ACTIVATE_PLAN:
        return (
            RunState.AWAITING_APPROVAL.value if run.approval_required else RunState.EXECUTING.value
        )
    return {
        RunLifecycleCommand.RECEIVE: RunState.RECEIVED.value,
        RunLifecycleCommand.MARK_VALIDATED: RunState.VALIDATED.value,
        RunLifecycleCommand.RECORD_PLAN: RunState.PLANNED.value,
        RunLifecycleCommand.RELEASE_APPROVED_PLAN: RunState.EXECUTING.value,
        RunLifecycleCommand.REJECT_APPROVAL: RunState.REJECTED.value,
        RunLifecycleCommand.COMPLETE: RunState.COMPLETED.value,
        RunLifecycleCommand.FAIL: RunState.FAILED.value,
        RunLifecycleCommand.CANCEL: RunState.CANCELLED.value,
    }[command]
