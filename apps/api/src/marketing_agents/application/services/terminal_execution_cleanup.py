"""Atomic terminal-plan cleanup shared by controlled READ and WRITE executors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.approval import AuthorizationSetStatus
from marketing_agents.domain.audit import (
    TERMINAL_RUNTIME_CONTROL_DENIAL_CODES,
    AuditContext,
    AuditEventDraft,
    RunTerminalFailureOrigin,
    normalize_audit_reason_code,
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
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.domain.step_lifecycle import (
    StepLifecycleCommand,
    StepTerminalContext,
    StepTransitionResult,
    transition_step,
)
from marketing_agents.domain.validation import require_digest, require_id, require_utc

from .audit_events import AuditEventFactory
from .run_cancellation import _executing_read_disposition


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
    """Close all unstarted work and fail one executing plan atomically."""

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
        require_id(denial_code, "terminal-cleanup denial code")
        if denial_code not in TERMINAL_RUNTIME_CONTROL_DENIAL_CODES:
            raise ValueError("terminal cleanup requires a terminal runtime-control denial code")
        return await self.fail_execution_in_uow(
            unit_of_work,
            run_id=run_id,
            failed_step_id=denied_step_id,
            plan_hash=plan_hash,
            failure_code=denial_code,
            sibling_reason_code="runtime_control_denied",
            _failure_origin=RunTerminalFailureOrigin.RUNTIME_CONTROL,
            occurred_at=occurred_at,
            audit_context=audit_context,
        )

    async def fail_execution_in_uow(
        self,
        unit_of_work: UnitOfWork,
        *,
        run_id: str,
        failed_step_id: str,
        plan_hash: str,
        failure_code: str,
        occurred_at: datetime,
        audit_context: AuditContext,
        focal_action_id: str | None = None,
        sibling_reason_code: str = "parent_run_failed",
        _failure_origin: RunTerminalFailureOrigin = RunTerminalFailureOrigin.ORDINARY_EXECUTION,
    ) -> TerminalExecutionCleanupResult:
        """Fail one step/Run and close only siblings with no external call in flight."""

        require_id(run_id, "terminal-cleanup Run ID")
        require_id(failed_step_id, "terminal-cleanup failed step ID")
        require_digest(plan_hash, "terminal-cleanup plan hash")
        require_id(failure_code, "terminal-cleanup failure code")
        require_id(sibling_reason_code, "terminal-cleanup sibling reason code")
        require_utc(occurred_at, "terminal-cleanup time")
        if focal_action_id is not None:
            require_id(focal_action_id, "terminal-cleanup focal action ID")
        if type(_failure_origin) is not RunTerminalFailureOrigin:
            raise TypeError("terminal cleanup requires an exact failure-origin witness")
        if normalize_audit_reason_code(failure_code) != failure_code:
            raise ValueError("terminal cleanup requires a safe terminal failure code")
        if normalize_audit_reason_code(sibling_reason_code) != sibling_reason_code:
            raise ValueError("terminal cleanup requires a safe sibling reason code")
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
        denied_step = next((step for step in steps if step.id == failed_step_id), None)
        if denied_step is None or denied_step.state not in {StepState.READY, StepState.EXECUTING}:
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_step_conflict",
                "terminal runtime cleanup lost the denied step",
            )
        control = await unit_of_work.execution_control.get(run_id)
        if control is None or control.policy_hash != plan_hash:
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_control_invalid",
                "terminal runtime cleanup lacks the exact sealed execution control",
            )

        write_steps = tuple(step for step in steps if step.effect is Effect.WRITE)
        actions: tuple[ExternalAction, ...] = ()
        if not write_steps:
            if run.approval_required or focal_action_id is not None:
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
            if focal_action_id is not None and (
                (focal_action := action_by_id.get(focal_action_id)) is None
                or focal_action.step_id != failed_step_id
            ):
                raise TerminalExecutionCleanupError(
                    "terminal_cleanup_focal_action_invalid",
                    "terminal runtime cleanup lost its exact focal WRITE action",
                )
        else:
            raise TerminalExecutionCleanupError(
                "terminal_cleanup_action_set_invalid",
                "direct runtime cleanup found sealed WRITE steps without authorization",
            )

        cancelled_pairs: list[tuple[ExternalAction, ExternalAction]] = []
        for action in actions:
            if (
                action.id == focal_action_id
                or action.state
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
                reason_code=sibling_reason_code,
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
            StepTerminalContext(failure_code),
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

        failed_pre_call_results: list[StepTransitionResult] = []
        for step in steps:
            if step.id == denied_step.id or step.state is not StepState.EXECUTING:
                continue
            close_without_external_call = step.id in cancelled_step_ids
            if step.effect is Effect.READ:
                if step.runtime_policy.attempt_kind is AttemptKind.NO_CALL:
                    close_without_external_call = True
                else:
                    try:
                        disposition = await _executing_read_disposition(
                            unit_of_work,
                            run_id=run_id,
                            plan_hash=plan_hash,
                            control_version=control.version,
                            step=step,
                        )
                    except (RuntimeError, ValueError) as exc:
                        raise TerminalExecutionCleanupError(
                            "terminal_cleanup_attempt_lineage_invalid",
                            "executing READ sibling lacks exact attempt lineage",
                        ) from exc
                    close_without_external_call = disposition == "retry_backoff"
            if close_without_external_call:
                failed_pre_call_results.append(
                    transition_step(
                        step,
                        StepLifecycleCommand.FAIL,
                        StepTerminalContext(sibling_reason_code),
                        occurred_at,
                    )
                )
        failed_pre_call_transitions = tuple(failed_pre_call_results)
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
                    "non-call sibling step changed before terminal cleanup committed",
                )

        skipped_transitions = tuple(
            transition_step(
                step,
                StepLifecycleCommand.SKIP,
                StepTerminalContext(sibling_reason_code),
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
            FailureContext(RunFailurePhase.EXECUTION, failure_code),
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
            factory.run_transition(
                run_transition.run,
                run_transition.transition,
                terminal_failure_origin=_failure_origin,
            ),
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
