"""Atomic execution activation for sealed read-only plans."""

from __future__ import annotations

from dataclasses import dataclass

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run, RunStep
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.run_lifecycle import (
    NoRunTransitionContext,
    RunLifecycleCommand,
    transition_run,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    transition_step,
)

from .audit_events import AuditEventFactory


class ExecutionActivationError(RuntimeError):
    def __init__(self, code: str, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class ExecutionActivationResult:
    run: Run
    steps: tuple[RunStep, ...]
    activated: bool


class ExecutionActivationService:
    """Start the Run deadline only when a direct plan truly becomes executable."""

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies

    async def activate(
        self,
        run_id: str,
        *,
        audit_context: AuditContext,
    ) -> ExecutionActivationResult:
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(run_id)
            if run is None:
                raise ExecutionActivationError("run_not_found", "Run does not exist", run_id=run_id)
            plan = await unit_of_work.run_steps.get_plan(run_id)
            steps = await unit_of_work.run_steps.validate_plan_for_execution(run_id)
            control = await unit_of_work.execution_control.get(run_id)
            if (
                plan is None
                or plan.approval_required
                or run.approval_required is not False
                or control is None
                or control.policy_hash != plan.plan_hash
            ):
                raise ExecutionActivationError(
                    "execution_control_invalid",
                    "direct activation lacks one exact sealed read execution plan",
                    run_id=run_id,
                )
            if run.state is RunState.EXECUTING:
                history = await unit_of_work.runs.list_transitions(run_id)
                activations = tuple(
                    transition
                    for transition in history
                    if transition.command is RunLifecycleCommand.ACTIVATE_PLAN
                )
                if (
                    len(activations) != 1
                    or control.started_at != activations[0].occurred_at
                    or control.deadline_at is None
                    or any(
                        step.state is StepState.PENDING and not step.dependency_keys
                        for step in steps
                    )
                ):
                    raise ExecutionActivationError(
                        "execution_activation_replay_invalid",
                        "executing Run lacks its exact activation and root-step witnesses",
                        run_id=run_id,
                    )
                return ExecutionActivationResult(run, steps, activated=False)
            if run.state is not RunState.PLANNED or control.started_at is not None:
                raise ExecutionActivationError(
                    "run_not_planned",
                    "only an unstarted planned Run can activate",
                    run_id=run_id,
                )
            if any(step.state is not StepState.PENDING for step in steps):
                raise ExecutionActivationError(
                    "execution_activation_step_drift",
                    "unstarted direct plan steps must remain pending",
                    run_id=run_id,
                )

            activated_at = self._dependencies.utc_now()
            run_result = transition_run(
                run,
                RunLifecycleCommand.ACTIVATE_PLAN,
                NoRunTransitionContext(),
                activated_at,
            )
            applied_run = await unit_of_work.runs.apply_transition(
                expected_version=run.version,
                expected_state=RunState.PLANNED,
                result=run_result,
            )
            if not applied_run:
                raise ExecutionActivationError(
                    "execution_activation_conflict",
                    "Run changed before direct activation committed",
                    run_id=run_id,
                )
            root_results = tuple(
                transition_step(
                    step,
                    StepLifecycleCommand.MARK_READY,
                    NoStepTransitionContext(),
                    activated_at,
                )
                for step in steps
                if not step.dependency_keys
            )
            if not root_results:
                raise ExecutionActivationError(
                    "execution_root_missing",
                    "direct execution plan has no dependency root",
                    run_id=run_id,
                )
            for result in root_results:
                applied_step = await unit_of_work.run_steps.apply_transition(
                    expected_run_version=run_result.run.version,
                    expected_run_state=RunState.EXECUTING,
                    expected_version=result.transition.expected_version,
                    expected_state=StepState.PENDING,
                    result=result,
                )
                if not applied_step:
                    raise ExecutionActivationError(
                        "execution_activation_conflict",
                        "root step changed before direct activation committed",
                        run_id=run_id,
                    )
            try:
                await unit_of_work.execution_control.start_execution(
                    run_id=run_id,
                    expected_control_version=control.version,
                    started_at=activated_at,
                )
            except RuntimeError as exc:
                raise ExecutionActivationError(
                    getattr(exc, "code", "execution_start_conflict"),
                    "Run deadline could not start with direct activation",
                    run_id=run_id,
                ) from exc
            factory = AuditEventFactory(audit_context)
            await unit_of_work.audits.append_many(
                (
                    factory.run_transition(run_result.run, run_result.transition),
                    *(
                        factory.step_transition(result.step, result.transition)
                        for result in root_results
                    ),
                )
            )
            await unit_of_work.commit()
            ready_by_id = {result.step.id: result.step for result in root_results}
            return ExecutionActivationResult(
                run_result.run,
                tuple(ready_by_id.get(step.id, step) for step in steps),
                activated=True,
            )


__all__ = [
    "ExecutionActivationError",
    "ExecutionActivationResult",
    "ExecutionActivationService",
]
