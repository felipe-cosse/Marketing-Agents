"""Audited optimistic-CAS service for persisted RunStep state transitions."""

from __future__ import annotations

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import Effect, RunState, StepState
from marketing_agents.domain.step_lifecycle import (
    StepLifecycleCommand,
    StepTransitionContext,
    StepTransitionResult,
    transition_step,
)

from .audit_events import AuditEventFactory


class RunStepLifecycleServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        step_id: str,
        current_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.step_id = step_id
        self.current_version = current_version


class RunStepLifecycleService:
    """Persist one accepted step transition and audit event in the same UoW."""

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies

    async def advance(
        self,
        step_id: str,
        expected_version: int,
        command: StepLifecycleCommand,
        context: StepTransitionContext,
        *,
        audit_context: AuditContext,
    ) -> StepTransitionResult:
        for retry_index in range(3):
            try:
                async with self._dependencies.unit_of_work() as unit_of_work:
                    result = await self.advance_in_uow(
                        unit_of_work,
                        step_id,
                        expected_version,
                        command,
                        context,
                        audit_context=audit_context,
                    )
                    await unit_of_work.commit()
                    return result
            except RunStepLifecycleServiceError as exc:
                if exc.code != "stale_step_version" or retry_index == 2:
                    raise
        raise AssertionError("bounded step transition retry exhausted without an outcome")

    async def advance_in_uow(
        self,
        unit_of_work: UnitOfWork,
        step_id: str,
        expected_version: int,
        command: StepLifecycleCommand,
        context: StepTransitionContext,
        *,
        audit_context: AuditContext,
    ) -> StepTransitionResult:
        current = await unit_of_work.run_steps.get(step_id)
        if current is None:
            raise RunStepLifecycleServiceError(
                "step_not_found", "Run step does not exist", step_id=step_id
            )
        if current.version != expected_version:
            raise RunStepLifecycleServiceError(
                "stale_step_version",
                "Run step changed before its lifecycle command was applied",
                step_id=step_id,
                current_version=current.version,
            )
        if current.effect is Effect.WRITE and command in {
            StepLifecycleCommand.WAIT_FOR_APPROVAL,
            StepLifecycleCommand.RELEASE_APPROVAL,
            StepLifecycleCommand.START,
            StepLifecycleCommand.START_RESERVED_WRITE,
            StepLifecycleCommand.REJECT,
            StepLifecycleCommand.CANCEL,
            StepLifecycleCommand.SKIP,
        }:
            raise RunStepLifecycleServiceError(
                "approval_boundary_service_required",
                "write-step lifecycle commands require persisted approval-boundary composition",
                step_id=step_id,
                current_version=current.version,
            )
        parent = await unit_of_work.runs.get(current.run_id)
        if parent is None:
            raise RunStepLifecycleServiceError(
                "parent_run_missing",
                "Run step lacks its authoritative parent Run",
                step_id=step_id,
            )
        allowed_parent_states = {
            StepLifecycleCommand.WAIT_FOR_APPROVAL: {
                RunState.PLANNED,
                RunState.AWAITING_APPROVAL,
            },
            StepLifecycleCommand.REJECT: {
                RunState.AWAITING_APPROVAL,
            },
            StepLifecycleCommand.CANCEL: {
                RunState.EXECUTING,
            },
        }.get(command, {RunState.EXECUTING})
        if parent.state not in allowed_parent_states:
            raise RunStepLifecycleServiceError(
                "parent_run_not_executable",
                "parent Run state does not authorize this step transition",
                step_id=step_id,
                current_version=current.version,
            )
        try:
            steps = await unit_of_work.run_steps.validate_plan_for_execution(current.run_id)
        except RuntimeError as exc:
            raise RunStepLifecycleServiceError(
                "step_plan_snapshot_invalid",
                "persisted Run plan cannot authorize a step mutation",
                step_id=step_id,
                current_version=current.version,
            ) from exc
        validated_current = next(
            (step for step in steps if step.id == current.id),
            None,
        )
        if validated_current != current:
            raise RunStepLifecycleServiceError(
                "step_plan_snapshot_mismatch",
                "Run step differs from its complete persisted plan snapshot",
                step_id=step_id,
                current_version=current.version,
            )
        if command is StepLifecycleCommand.MARK_READY:
            state_by_key = {step.key: step.state for step in steps}
            if any(
                state_by_key.get(dependency_key) is not StepState.SUCCEEDED
                for dependency_key in current.dependency_keys
            ):
                raise RunStepLifecycleServiceError(
                    "step_dependency_barrier_incomplete",
                    "all dependency steps must have succeeded before this step is ready",
                    step_id=step_id,
                    current_version=current.version,
                )
        result = transition_step(
            current,
            command,
            context,
            self._dependencies.utc_now(),
        )
        applied = await unit_of_work.run_steps.apply_transition(
            expected_run_version=parent.version,
            expected_run_state=parent.state,
            expected_version=expected_version,
            expected_state=current.state,
            result=result,
        )
        if not applied:
            raise RunStepLifecycleServiceError(
                "stale_step_version",
                "Run step changed concurrently before its transition committed",
                step_id=step_id,
            )
        await unit_of_work.audits.append(
            AuditEventFactory(audit_context).step_transition(
                result.step,
                result.transition,
            )
        )
        return result
