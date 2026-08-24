"""Atomic terminal-plan cleanup shared by controlled READ and WRITE executors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.approval import AuthorizationSetStatus
from marketing_agents.domain.audit import (
    RUNTIME_CONTROL_DENIAL_CODES,
    AuditContext,
    AuditEventDraft,
)
from marketing_agents.domain.entities import ExternalAction, Run, RunStep
from marketing_agents.domain.enums import Effect, ExternalActionState, RunState, StepState
from marketing_agents.domain.run_lifecycle import (
    FailureContext,
    RunFailurePhase,
    RunLifecycleCommand,
    RunTransitionResult,
    transition_run,
)
from marketing_agents.domain.step_lifecycle import (
    StepLifecycleCommand,
    StepTerminalContext,
    StepTransitionResult,
    transition_step,
)
from marketing_agents.domain.validation import require_digest, require_id, require_utc

from .audit_events import AuditEventFactory


class TerminalExecutionCleanupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TerminalExecutionCleanupResult:
    run: Run
    denied_step: RunStep
    cancelled_actions: tuple[ExternalAction, ...]
    skipped_steps: tuple[RunStep, ...]
    audit_events: tuple[AuditEventDraft, ...]
    run_transition: RunTransitionResult
    denied_step_transition: StepTransitionResult
    failed_pre_call_step_transitions: tuple[StepTransitionResult, ...]
    skipped_step_transitions: tuple[StepTransitionResult, ...]


class TerminalExecutionCleanupService:
    """Close all unstarted work and fail one runtime-denied executing plan atomically."""

    async def fail_runtime_control_in_uow(
        self,
        unit_of_work: UnitOfWork,
        *,
        run_id: str,
        denied_step_id: str,
        plan_hash: str,
        denial_code: str,
        occurred_at: datetime,
        audit_context: AuditContext,
    ) -> TerminalExecutionCleanupResult:
        require_id(run_id, "terminal-cleanup Run ID")
        require_id(denied_step_id, "terminal-cleanup denied step ID")
        require_digest(plan_hash, "terminal-cleanup plan hash")
        require_id(denial_code, "terminal-cleanup denial code")
        require_utc(occurred_at, "terminal-cleanup time")
        if denial_code not in RUNTIME_CONTROL_DENIAL_CODES:
            raise ValueError("terminal cleanup requires a runtime-control denial code")
        run = await unit_of_work.runs.get(run_id)
        if run is None or run.state is not RunState.EXECUTING:
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_run_conflict",
                "terminal runtime cleanup requires the exact executing Run",
            )
        try:
            steps = await unit_of_work.run_steps.validate_plan_for_execution(run_id)
        except RuntimeError as exc:
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_plan_invalid",
                "terminal runtime cleanup cannot validate the sealed plan",
            ) from exc
        denied_step = next((step for step in steps if step.id == denied_step_id), None)
        if denied_step is None or denied_step.state not in {StepState.READY, StepState.EXECUTING}:
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_step_conflict",
                "terminal runtime cleanup lost the denied step",
            )

        write_steps = tuple(step for step in steps if step.effect is Effect.WRITE)
        actions: tuple[ExternalAction, ...] = ()
        if not write_steps:
            if run.approval_required:
                raise TerminalExecutionCleanupError(
                    "terminal_cleanup_action_set_invalid",
                    "approval-required runtime cleanup has no sealed WRITE steps",
                )
        elif run.approval_required:
            actions = await unit_of_work.external_actions.list_run_plan(
                run_id,
                plan_hash,
            )
            selection = await unit_of_work.approvals.get_current_authorization_set(run_id)
            if (
                selection is None
                or selection.authorization_set.status is not AuthorizationSetStatus.RELEASED
                or selection.authorization_set.plan_hash != plan_hash
            ):
                raise TerminalExecutionCleanupError(
                    "terminal_cleanup_authorization_invalid",
                    "terminal runtime cleanup lacks the exact released authorization set",
                )
            members = selection.authorization_set.members
            member_by_action = {member.action_id: member for member in members}
            action_by_id = {action.id: action for action in actions}
            if (
                len(member_by_action) != len(members)
                or len(action_by_id) != len(actions)
                or set(member_by_action) != set(action_by_id)
                or {member.step_id for member in members} != {step.id for step in write_steps}
                or any(
                    action.run_id != run_id
                    or action.envelope.plan_hash != plan_hash
                    or action.envelope.proposal_revision
                    != selection.authorization_set.proposal_revision
                    or action.action_hash != member_by_action[action.id].action_hash
                    or action.step_id != member_by_action[action.id].step_id
                    or action.envelope.step_key != member_by_action[action.id].step_key
                    for action in actions
                )
            ):
                raise TerminalExecutionCleanupError(
                    "terminal_cleanup_action_set_invalid",
                    "terminal runtime cleanup action rows do not cover the released WRITE set",
                )
        else:
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_action_set_invalid",
                "direct runtime cleanup found sealed WRITE steps without authorization",
            )

        cancelled_pairs: list[tuple[ExternalAction, ExternalAction]] = []
        for action in actions:
            if (
                action.state
                not in {
                    ExternalActionState.DISPATCH_RESERVED,
                    ExternalActionState.DISPATCHING,
                }
                or action.call_started_at is not None
            ):
                continue
            cancelled = await unit_of_work.external_actions.cancel_unstarted_after_release(
                action_id=action.id,
                run_id=run_id,
                plan_hash=plan_hash,
                expected_version=action.version,
                occurred_at=occurred_at,
                reason_code="runtime_control_denied",
            )
            if cancelled is None:
                raise TerminalExecutionCleanupError(
                    "terminal_cleanup_action_conflict",
                    "pre-call action changed before terminal cleanup committed",
                )
            cancelled_pairs.append((action, cancelled))
        cancelled_step_ids = {previous.step_id for previous, _ in cancelled_pairs}

        denied_transition = transition_step(
            denied_step,
            StepLifecycleCommand.FAIL,
            StepTerminalContext(denial_code),
            occurred_at,
        )
        if not await unit_of_work.run_steps.apply_transition(
            expected_run_version=run.version,
            expected_run_state=RunState.EXECUTING,
            expected_version=denied_step.version,
            expected_state=denied_step.state,
            result=denied_transition,
        ):
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_step_conflict",
                "denied step changed before terminal cleanup committed",
            )

        failed_pre_call_transitions = tuple(
            transition_step(
                step,
                StepLifecycleCommand.FAIL,
                StepTerminalContext("runtime_control_denied"),
                occurred_at,
            )
            for step in steps
            if step.id != denied_step.id
            and step.id in cancelled_step_ids
            and step.state is StepState.EXECUTING
        )
        for transition in failed_pre_call_transitions:
            if not await unit_of_work.run_steps.apply_transition(
                expected_run_version=run.version,
                expected_run_state=RunState.EXECUTING,
                expected_version=transition.transition.expected_version,
                expected_state=StepState.EXECUTING,
                result=transition,
            ):
                raise TerminalExecutionCleanupError(
                    "terminal_cleanup_step_conflict",
                    "pre-call WRITE step changed before terminal cleanup committed",
                )

        skipped_transitions = tuple(
            transition_step(
                step,
                StepLifecycleCommand.SKIP,
                StepTerminalContext("runtime_control_denied"),
                occurred_at,
            )
            for step in steps
            if step.id != denied_step.id
            and step.state in {StepState.PENDING, StepState.READY, StepState.AWAITING_APPROVAL}
        )
        for transition in skipped_transitions:
            previous_state = transition.transition.previous_state
            if previous_state is None:  # pragma: no cover - transition domain invariant
                raise AssertionError("terminal cleanup skip lost its previous state")
            if not await unit_of_work.run_steps.apply_transition(
                expected_run_version=run.version,
                expected_run_state=RunState.EXECUTING,
                expected_version=transition.transition.expected_version,
                expected_state=previous_state,
                result=transition,
            ):
                raise TerminalExecutionCleanupError(
                    "terminal_cleanup_step_conflict",
                    "queued step changed before terminal cleanup committed",
                )

        run_transition = transition_run(
            run,
            RunLifecycleCommand.FAIL,
            FailureContext(RunFailurePhase.EXECUTION, denial_code),
            occurred_at,
        )
        if not await unit_of_work.runs.apply_transition(
            expected_version=run.version,
            expected_state=RunState.EXECUTING,
            result=run_transition,
        ):
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_run_conflict",
                "Run changed before terminal cleanup committed",
            )

        factory = AuditEventFactory(audit_context)
        audit_events = (
            *(
                factory.action_runtime_cancelled(previous, cancelled)
                for previous, cancelled in cancelled_pairs
            ),
            factory.step_transition(
                denied_transition.step,
                denied_transition.transition,
            ),
            *(
                factory.step_transition(transition.step, transition.transition)
                for transition in failed_pre_call_transitions
            ),
            *(
                factory.step_transition(transition.step, transition.transition)
                for transition in skipped_transitions
            ),
            factory.run_transition(run_transition.run, run_transition.transition),
        )
        return TerminalExecutionCleanupResult(
            run=run_transition.run,
            denied_step=denied_transition.step,
            cancelled_actions=tuple(cancelled for _, cancelled in cancelled_pairs),
            skipped_steps=tuple(transition.step for transition in skipped_transitions),
            audit_events=audit_events,
            run_transition=run_transition,
            denied_step_transition=denied_transition,
            failed_pre_call_step_transitions=failed_pre_call_transitions,
            skipped_step_transitions=skipped_transitions,
        )


__all__ = [
    "TerminalExecutionCleanupError",
    "TerminalExecutionCleanupResult",
    "TerminalExecutionCleanupService",
]
