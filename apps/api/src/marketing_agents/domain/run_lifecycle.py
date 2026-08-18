"""Pure ADR-0004 run lifecycle with typed commands and safe evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Never

from marketing_agents.domain.entities import Run
from marketing_agents.domain.entities._validation import (
    require_digest,
    require_id,
    require_utc,
)
from marketing_agents.domain.enums import RunState


class RunLifecycleCommand(StrEnum):
    RECEIVE = "receive"
    MARK_VALIDATED = "mark_validated"
    RECORD_PLAN = "record_plan"
    ACTIVATE_PLAN = "activate_plan"
    RELEASE_APPROVED_PLAN = "release_approved_plan"
    REJECT_APPROVAL = "reject_approval"
    COMPLETE = "complete"
    FAIL = "fail"
    CANCEL = "cancel"


class RunFailurePhase(StrEnum):
    VALIDATION = "validation"
    PLANNING = "planning"
    APPROVAL_PROCESSING = "approval_processing"
    EXECUTION = "execution"


@dataclass(frozen=True, slots=True)
class NoRunTransitionContext:
    pass


@dataclass(frozen=True, slots=True)
class PlanDispositionContext:
    contains_write_actions: bool

    def __post_init__(self) -> None:
        if not isinstance(self.contains_write_actions, bool):
            raise ValueError("plan write disposition must be boolean")


@dataclass(frozen=True, slots=True)
class ApprovalBarrierContext:
    required_action_hashes: tuple[str, ...]
    current_action_hashes: tuple[str, ...]
    approved_action_hashes: tuple[str, ...]
    expires_at_by_hash: Mapping[str, datetime]

    def __post_init__(self) -> None:
        for values, field_name in (
            (self.required_action_hashes, "required action hashes"),
            (self.current_action_hashes, "current action hashes"),
            (self.approved_action_hashes, "approved action hashes"),
        ):
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
            for value in values:
                require_digest(value, field_name)
        for action_hash, expires_at in self.expires_at_by_hash.items():
            require_digest(action_hash, "approval expiry action hash")
            require_utc(expires_at, "approval expiry")
        object.__setattr__(
            self,
            "expires_at_by_hash",
            MappingProxyType(dict(self.expires_at_by_hash)),
        )


@dataclass(frozen=True, slots=True)
class ApprovalRejectionContext:
    required_action_hashes: tuple[str, ...]
    rejected_action_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        for values, field_name in (
            (self.required_action_hashes, "required action hashes"),
            (self.rejected_action_hashes, "rejected action hashes"),
        ):
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
            for value in values:
                require_digest(value, field_name)


@dataclass(frozen=True, slots=True)
class CompletionContext:
    total_step_count: int
    succeeded_step_count: int
    failed_step_count: int
    unfinished_step_count: int

    def __post_init__(self) -> None:
        values = (
            self.total_step_count,
            self.succeeded_step_count,
            self.failed_step_count,
            self.unfinished_step_count,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values
        ):
            raise ValueError("completion counts must be nonnegative integers")
        if self.total_step_count < 1:
            raise ValueError("completion requires at least one planned step")
        if self.total_step_count != sum(values[1:]):
            raise ValueError("completion counts must sum to the total step count")


@dataclass(frozen=True, slots=True)
class FailureContext:
    phase: RunFailurePhase
    failure_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.phase, RunFailurePhase):
            raise ValueError("run failure phase is invalid")
        require_id(self.failure_code, "run failure code")


@dataclass(frozen=True, slots=True)
class CancellationContext:
    reason_code: str
    completed_effect_count: int = 0
    outcome_unknown_effect_count: int = 0

    def __post_init__(self) -> None:
        require_id(self.reason_code, "cancellation reason code")
        for value in (self.completed_effect_count, self.outcome_unknown_effect_count):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("cancellation effect counts must be nonnegative integers")


type RunTransitionContext = (
    NoRunTransitionContext
    | PlanDispositionContext
    | ApprovalBarrierContext
    | ApprovalRejectionContext
    | CompletionContext
    | FailureContext
    | CancellationContext
)


@dataclass(frozen=True, slots=True)
class RunStateTransition:
    run_id: str
    sequence: int
    command: RunLifecycleCommand
    previous_state: RunState | None
    new_state: RunState
    reason_code: str
    occurred_at: datetime
    expected_version: int
    resulting_version: int
    completed_effect_count: int = 0
    outcome_unknown_effect_count: int = 0

    def __post_init__(self) -> None:
        require_id(self.run_id, "transition run ID")
        require_id(self.reason_code, "transition reason code")
        require_utc(self.occurred_at, "transition time")
        if (
            self.sequence != self.resulting_version
            or self.resulting_version != self.expected_version + 1
        ):
            raise ValueError("transition sequence and versions must be contiguous")
        if self.sequence < 1:
            raise ValueError("transition sequence must be positive")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (self.completed_effect_count, self.outcome_unknown_effect_count)
        ):
            raise ValueError("transition effect counts must be nonnegative integers")
        if self.sequence == 1:
            if (
                self.command is not RunLifecycleCommand.RECEIVE
                or self.previous_state is not None
                or self.new_state is not RunState.RECEIVED
                or self.expected_version != 0
            ):
                raise ValueError("initial transition must receive version one")
        elif (
            self.command is RunLifecycleCommand.RECEIVE
            or self.previous_state is None
            or self.previous_state is self.new_state
        ):
            raise ValueError("subsequent transition must change an existing state")
        if self.command is not RunLifecycleCommand.CANCEL and (
            self.completed_effect_count or self.outcome_unknown_effect_count
        ):
            raise ValueError("only cancellation records external-effect counts")


@dataclass(frozen=True, slots=True)
class RunTransitionEvidence:
    accepted: bool
    run_id: str
    command: RunLifecycleCommand
    previous_state: RunState
    requested_state: RunState | None
    reason_code: str
    expected_version: int
    occurred_at: datetime
    completed_effect_count: int = 0
    outcome_unknown_effect_count: int = 0


@dataclass(frozen=True, slots=True)
class RunTransitionResult:
    run: Run
    transition: RunStateTransition
    audit_evidence: RunTransitionEvidence


class RunTransitionError(ValueError):
    def __init__(self, code: str, message: str, audit_evidence: RunTransitionEvidence) -> None:
        super().__init__(message)
        self.code = code
        self.audit_evidence = audit_evidence


_TERMINAL_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.REJECTED, RunState.CANCELLED}
)


def initial_received_transition(run: Run) -> RunStateTransition:
    if run.state is not RunState.RECEIVED or run.version != 1 or run.updated_at != run.created_at:
        raise ValueError("initial run must be received at version one and creation time")
    return RunStateTransition(
        run_id=run.id,
        sequence=1,
        command=RunLifecycleCommand.RECEIVE,
        previous_state=None,
        new_state=RunState.RECEIVED,
        reason_code="work_admitted",
        occurred_at=run.created_at,
        expected_version=0,
        resulting_version=1,
    )


def _requested_state(run: Run, command: RunLifecycleCommand) -> RunState | None:
    if command is RunLifecycleCommand.MARK_VALIDATED:
        return RunState.VALIDATED
    if command is RunLifecycleCommand.RECORD_PLAN:
        return RunState.PLANNED
    if command is RunLifecycleCommand.ACTIVATE_PLAN:
        return RunState.AWAITING_APPROVAL if run.approval_required else RunState.EXECUTING
    if command is RunLifecycleCommand.RELEASE_APPROVED_PLAN:
        return RunState.EXECUTING
    if command is RunLifecycleCommand.REJECT_APPROVAL:
        return RunState.REJECTED
    if command is RunLifecycleCommand.COMPLETE:
        return RunState.COMPLETED
    if command is RunLifecycleCommand.FAIL:
        return RunState.FAILED
    if command is RunLifecycleCommand.CANCEL:
        return RunState.CANCELLED
    return None


def _evidence(
    run: Run,
    command: RunLifecycleCommand,
    occurred_at: datetime,
    *,
    accepted: bool,
    reason_code: str,
    context: RunTransitionContext,
) -> RunTransitionEvidence:
    completed = context.completed_effect_count if isinstance(context, CancellationContext) else 0
    unknown = (
        context.outcome_unknown_effect_count if isinstance(context, CancellationContext) else 0
    )
    return RunTransitionEvidence(
        accepted=accepted,
        run_id=run.id,
        command=command,
        previous_state=run.state,
        requested_state=_requested_state(run, command),
        reason_code=reason_code,
        expected_version=run.version,
        occurred_at=occurred_at,
        completed_effect_count=completed,
        outcome_unknown_effect_count=unknown,
    )


def _reject(
    run: Run,
    command: RunLifecycleCommand,
    context: RunTransitionContext,
    occurred_at: datetime,
    code: str,
    message: str,
) -> Never:
    raise RunTransitionError(
        code,
        message,
        _evidence(
            run,
            command,
            occurred_at,
            accepted=False,
            reason_code=code,
            context=context,
        ),
    )


def transition_run(
    run: Run,
    command: RunLifecycleCommand,
    context: RunTransitionContext,
    occurred_at: datetime,
) -> RunTransitionResult:
    """Apply one legal command; callers cannot nominate an arbitrary target state."""

    require_utc(occurred_at, "transition time")
    if occurred_at < run.updated_at:
        _reject(
            run,
            command,
            context,
            occurred_at,
            "non_monotonic_time",
            "transition time is stale",
        )
    if run.state in _TERMINAL_STATES:
        _reject(
            run,
            command,
            context,
            occurred_at,
            "terminal_state_immutable",
            "terminal run state is immutable",
        )

    next_state: RunState
    reason_code: str
    approval_required = run.approval_required
    terminal_reason: str | None = None

    if command is RunLifecycleCommand.MARK_VALIDATED:
        if run.state is not RunState.RECEIVED or not isinstance(context, NoRunTransitionContext):
            _reject(run, command, context, occurred_at, "invalid_transition", "run cannot validate")
        next_state, reason_code = RunState.VALIDATED, "input_validated"
    elif command is RunLifecycleCommand.RECORD_PLAN:
        if run.state is not RunState.VALIDATED or not isinstance(context, PlanDispositionContext):
            _reject(run, command, context, occurred_at, "invalid_transition", "run cannot plan")
        next_state, reason_code = RunState.PLANNED, "plan_recorded"
        approval_required = context.contains_write_actions
    elif command is RunLifecycleCommand.ACTIVATE_PLAN:
        if run.state is not RunState.PLANNED or not isinstance(context, NoRunTransitionContext):
            _reject(
                run,
                command,
                context,
                occurred_at,
                "invalid_transition",
                "plan cannot activate",
            )
        if run.approval_required:
            next_state, reason_code = RunState.AWAITING_APPROVAL, "write_plan_requires_approval"
        else:
            next_state, reason_code = RunState.EXECUTING, "read_only_plan_released"
    elif command is RunLifecycleCommand.RELEASE_APPROVED_PLAN:
        if run.state is not RunState.AWAITING_APPROVAL or not isinstance(
            context, ApprovalBarrierContext
        ):
            _reject(
                run, command, context, occurred_at, "invalid_transition", "approval cannot release"
            )
        required = set(context.required_action_hashes)
        if (
            required != set(context.current_action_hashes)
            or required != set(context.approved_action_hashes)
            or required != set(context.expires_at_by_hash)
            or any(expiry <= occurred_at for expiry in context.expires_at_by_hash.values())
        ):
            _reject(
                run,
                command,
                context,
                occurred_at,
                "approval_barrier_incomplete",
                "complete current exact-action approvals are required",
            )
        next_state, reason_code = RunState.EXECUTING, "approval_barrier_satisfied"
    elif command is RunLifecycleCommand.REJECT_APPROVAL:
        if run.state is not RunState.AWAITING_APPROVAL or not isinstance(
            context, ApprovalRejectionContext
        ):
            _reject(
                run, command, context, occurred_at, "invalid_transition", "approval cannot reject"
            )
        if not set(context.rejected_action_hashes).issubset(context.required_action_hashes):
            _reject(
                run,
                command,
                context,
                occurred_at,
                "approval_rejection_mismatch",
                "rejected action must belong to the required set",
            )
        next_state, reason_code = RunState.REJECTED, "approval_rejected"
        terminal_reason = reason_code
    elif command is RunLifecycleCommand.COMPLETE:
        if run.state is not RunState.EXECUTING or not isinstance(context, CompletionContext):
            _reject(run, command, context, occurred_at, "invalid_transition", "run cannot complete")
        if context.failed_step_count or context.unfinished_step_count:
            _reject(
                run,
                command,
                context,
                occurred_at,
                "execution_incomplete",
                "all required steps must succeed before completion",
            )
        next_state, reason_code = RunState.COMPLETED, "execution_completed"
        terminal_reason = reason_code
    elif command is RunLifecycleCommand.FAIL:
        if not isinstance(context, FailureContext):
            _reject(
                run,
                command,
                context,
                occurred_at,
                "invalid_transition",
                "failure context invalid",
            )
        allowed_phase = {
            RunState.RECEIVED: RunFailurePhase.VALIDATION,
            RunState.VALIDATED: RunFailurePhase.PLANNING,
            RunState.PLANNED: RunFailurePhase.PLANNING,
            RunState.AWAITING_APPROVAL: RunFailurePhase.APPROVAL_PROCESSING,
            RunState.EXECUTING: RunFailurePhase.EXECUTION,
        }.get(run.state)
        if allowed_phase is not context.phase:
            _reject(
                run,
                command,
                context,
                occurred_at,
                "failure_phase_mismatch",
                "failure phase does not match current processing state",
            )
        next_state, reason_code = RunState.FAILED, context.failure_code
        terminal_reason = reason_code
    elif command is RunLifecycleCommand.CANCEL:
        if not isinstance(context, CancellationContext):
            _reject(
                run,
                command,
                context,
                occurred_at,
                "invalid_transition",
                "cancellation context invalid",
            )
        if run.state is not RunState.EXECUTING and (
            context.completed_effect_count or context.outcome_unknown_effect_count
        ):
            _reject(
                run,
                command,
                context,
                occurred_at,
                "invalid_cancellation_effects",
                "pre-execution cancellation cannot report external effects",
            )
        next_state, reason_code = RunState.CANCELLED, context.reason_code
        terminal_reason = reason_code
    else:
        _reject(run, command, context, occurred_at, "invalid_transition", "command is not public")

    updated = replace(
        run,
        state=next_state,
        updated_at=occurred_at,
        version=run.version + 1,
        approval_required=approval_required,
        terminal_reason_code=terminal_reason,
    )
    completed = context.completed_effect_count if isinstance(context, CancellationContext) else 0
    unknown = (
        context.outcome_unknown_effect_count if isinstance(context, CancellationContext) else 0
    )
    transition = RunStateTransition(
        run_id=run.id,
        sequence=updated.version,
        command=command,
        previous_state=run.state,
        new_state=next_state,
        reason_code=reason_code,
        occurred_at=occurred_at,
        expected_version=run.version,
        resulting_version=updated.version,
        completed_effect_count=completed,
        outcome_unknown_effect_count=unknown,
    )
    evidence = _evidence(
        run,
        command,
        occurred_at,
        accepted=True,
        reason_code=reason_code,
        context=context,
    )
    return RunTransitionResult(updated, transition, evidence)
