"""Atomic cancellation fence for runtime-controlled read plans."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run, RunStep
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.run_lifecycle import (
    CancellationContext,
    RunLifecycleCommand,
    transition_run,
)
from marketing_agents.domain.step_lifecycle import (
    StepLifecycleCommand,
    StepTerminalContext,
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

            cancellable = tuple(
                step for step in steps if step.state in {StepState.PENDING, StepState.READY}
            )
            step_results = tuple(
                transition_step(
                    step,
                    StepLifecycleCommand.CANCEL,
                    StepTerminalContext("run_cancelled"),
                    occurred_at,
                )
                for step in cancellable
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
                cancelled_steps=tuple(result.step for result in step_results),
                preserved_executing_step_ids=tuple(
                    step.id for step in steps if step.state is StepState.EXECUTING
                ),
            )


__all__ = [
    "RunCancellationResult",
    "RunCancellationService",
    "RunCancellationServiceError",
]
