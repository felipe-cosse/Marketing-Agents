"""Pure command-only lifecycle for one persisted run step."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Never

from marketing_agents.domain.entities.runtime import RunStep
from marketing_agents.domain.enums import Effect, StepState
from marketing_agents.domain.validation import require_id, require_utc


class StepLifecycleCommand(StrEnum):
    INITIALIZE = "initialize"
    MARK_READY = "mark_ready"
    WAIT_FOR_APPROVAL = "wait_for_approval"
    RELEASE_APPROVAL = "release_approval"
    START = "start"
    START_RESERVED_WRITE = "start_reserved_write"
    SUCCEED = "succeed"
    FAIL = "fail"
    REJECT = "reject"
    CANCEL = "cancel"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class NoStepTransitionContext:
    pass


@dataclass(frozen=True, slots=True)
class StepTerminalContext:
    reason_code: str

    def __post_init__(self) -> None:
        require_id(self.reason_code, "step terminal reason code")


type StepTransitionContext = NoStepTransitionContext | StepTerminalContext


@dataclass(frozen=True, slots=True)
class StepStateTransition:
    step_id: str
    run_id: str
    sequence: int
    command: StepLifecycleCommand
    previous_state: StepState | None
    new_state: StepState
    reason_code: str
    occurred_at: datetime
    expected_version: int
    resulting_version: int

    def __post_init__(self) -> None:
        require_id(self.step_id, "step transition step ID")
        require_id(self.run_id, "step transition run ID")
        require_id(self.reason_code, "step transition reason code")
        require_utc(self.occurred_at, "step transition time")
        if type(self.command) is not StepLifecycleCommand:
            raise ValueError("step transition command must use the exact enum")
        if self.previous_state is not None and type(self.previous_state) is not StepState:
            raise ValueError("step previous state must use the exact enum")
        if type(self.new_state) is not StepState:
            raise ValueError("step new state must use the exact enum")
        if (
            self.sequence != self.resulting_version
            or self.resulting_version != self.expected_version + 1
            or self.sequence < 1
        ):
            raise ValueError("step transition sequence and versions must be contiguous")
        if self.sequence == 1:
            if (
                self.command is not StepLifecycleCommand.INITIALIZE
                or self.previous_state is not None
                or self.new_state is not StepState.PENDING
                or self.expected_version != 0
            ):
                raise ValueError("initial step transition must initialize pending version one")
        elif (
            self.command is StepLifecycleCommand.INITIALIZE
            or self.previous_state is None
            or self.previous_state is self.new_state
        ):
            raise ValueError("subsequent step transition must change an existing state")
        else:
            allowed_edges = {
                StepLifecycleCommand.MARK_READY: {(StepState.PENDING, StepState.READY)},
                StepLifecycleCommand.WAIT_FOR_APPROVAL: {
                    (StepState.PENDING, StepState.AWAITING_APPROVAL)
                },
                StepLifecycleCommand.RELEASE_APPROVAL: {
                    (StepState.AWAITING_APPROVAL, StepState.READY)
                },
                StepLifecycleCommand.START: {(StepState.READY, StepState.EXECUTING)},
                StepLifecycleCommand.START_RESERVED_WRITE: {(StepState.READY, StepState.EXECUTING)},
                StepLifecycleCommand.SUCCEED: {(StepState.EXECUTING, StepState.SUCCEEDED)},
                StepLifecycleCommand.FAIL: {
                    (StepState.READY, StepState.FAILED),
                    (StepState.EXECUTING, StepState.FAILED),
                },
                StepLifecycleCommand.REJECT: {(StepState.AWAITING_APPROVAL, StepState.REJECTED)},
                StepLifecycleCommand.CANCEL: {
                    (StepState.PENDING, StepState.CANCELLED),
                    (StepState.READY, StepState.CANCELLED),
                    (StepState.AWAITING_APPROVAL, StepState.CANCELLED),
                },
                StepLifecycleCommand.SKIP: {
                    (StepState.PENDING, StepState.SKIPPED),
                    (StepState.READY, StepState.SKIPPED),
                    (StepState.AWAITING_APPROVAL, StepState.SKIPPED),
                },
            }
            if (self.previous_state, self.new_state) not in allowed_edges.get(self.command, set()):
                raise ValueError("step transition command and states are inconsistent")


@dataclass(frozen=True, slots=True)
class StepTransitionResult:
    step: RunStep
    transition: StepStateTransition

    def __post_init__(self) -> None:
        if type(self.step) is not RunStep or type(self.transition) is not StepStateTransition:
            raise ValueError("step transition result requires exact immutable contracts")
        self.transition.__post_init__()
        if (
            self.step.id != self.transition.step_id
            or self.step.run_id != self.transition.run_id
            or self.step.state is not self.transition.new_state
            or self.step.version != self.transition.resulting_version
            or self.step.updated_at != self.transition.occurred_at
        ):
            raise ValueError("step transition result does not match its updated step")
        terminal = self.step.state in _TERMINAL_STATES
        if terminal != (self.step.terminal_reason_code == self.transition.reason_code):
            raise ValueError("step transition result terminal reason is inconsistent")
        if (
            self.transition.command
            in {
                StepLifecycleCommand.MARK_READY,
                StepLifecycleCommand.START,
            }
            and self.step.effect is not Effect.READ
        ):
            raise ValueError("ordinary ready and start transitions are read-only")
        if (
            self.transition.command
            in {
                StepLifecycleCommand.WAIT_FOR_APPROVAL,
                StepLifecycleCommand.RELEASE_APPROVAL,
                StepLifecycleCommand.START_RESERVED_WRITE,
                StepLifecycleCommand.REJECT,
            }
            and self.step.effect is not Effect.WRITE
        ):
            raise ValueError("approval transitions are write-only")


class StepTransitionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_TERMINAL_STATES = frozenset(
    {
        StepState.SUCCEEDED,
        StepState.FAILED,
        StepState.REJECTED,
        StepState.CANCELLED,
        StepState.SKIPPED,
    }
)


def initial_pending_transition(step: RunStep) -> StepStateTransition:
    if step.state is not StepState.PENDING or step.version != 1:
        raise ValueError("initial step must be pending at version one")
    if step.created_at != step.updated_at:
        raise ValueError("initial step update time must equal its creation time")
    return StepStateTransition(
        step_id=step.id,
        run_id=step.run_id,
        sequence=1,
        command=StepLifecycleCommand.INITIALIZE,
        previous_state=None,
        new_state=StepState.PENDING,
        reason_code="plan_step_recorded",
        occurred_at=step.created_at,
        expected_version=0,
        resulting_version=1,
    )


def _reject(code: str, message: str) -> Never:
    raise StepTransitionError(code, message)


def transition_step(
    step: RunStep,
    command: StepLifecycleCommand,
    context: StepTransitionContext,
    occurred_at: datetime,
) -> StepTransitionResult:
    """Apply one legal step command; callers cannot nominate the target state."""

    require_utc(occurred_at, "step transition time")
    if occurred_at < step.updated_at:
        _reject("non_monotonic_time", "step transition time is stale")
    if step.state in _TERMINAL_STATES:
        _reject("terminal_state_immutable", "terminal step state is immutable")

    next_state: StepState
    reason_code: str
    if command is StepLifecycleCommand.MARK_READY:
        if (
            step.state is not StepState.PENDING
            or step.effect.value != "read"
            or not isinstance(context, NoStepTransitionContext)
        ):
            _reject("invalid_transition", "step cannot become ready")
        next_state, reason_code = StepState.READY, "step_dependencies_satisfied"
    elif command is StepLifecycleCommand.WAIT_FOR_APPROVAL:
        if (
            step.state is not StepState.PENDING
            or step.effect.value != "write"
            or not isinstance(context, NoStepTransitionContext)
        ):
            _reject("invalid_transition", "step cannot await approval")
        next_state, reason_code = StepState.AWAITING_APPROVAL, "step_approval_required"
    elif command is StepLifecycleCommand.RELEASE_APPROVAL:
        if (
            step.state is not StepState.AWAITING_APPROVAL
            or step.effect.value != "write"
            or not isinstance(context, NoStepTransitionContext)
        ):
            _reject("invalid_transition", "write step approval cannot release")
        next_state, reason_code = StepState.READY, "approval_barrier_released"
    elif command is StepLifecycleCommand.START:
        if (
            step.state is not StepState.READY
            or step.effect.value != "read"
            or not isinstance(context, NoStepTransitionContext)
        ):
            _reject("invalid_transition", "step cannot start")
        next_state, reason_code = StepState.EXECUTING, "step_execution_started"
    elif command is StepLifecycleCommand.START_RESERVED_WRITE:
        if (
            step.state is not StepState.READY
            or step.effect.value != "write"
            or not isinstance(context, NoStepTransitionContext)
        ):
            _reject("invalid_transition", "reserved write step cannot start")
        next_state, reason_code = StepState.EXECUTING, "reserved_write_started"
    elif command is StepLifecycleCommand.SUCCEED:
        if step.state is not StepState.EXECUTING or not isinstance(
            context, NoStepTransitionContext
        ):
            _reject("invalid_transition", "step cannot succeed")
        next_state, reason_code = StepState.SUCCEEDED, "step_succeeded"
    elif command is StepLifecycleCommand.FAIL:
        if step.state not in {StepState.READY, StepState.EXECUTING} or not isinstance(
            context, StepTerminalContext
        ):
            _reject("invalid_transition", "step cannot fail")
        next_state, reason_code = StepState.FAILED, context.reason_code
    elif command is StepLifecycleCommand.REJECT:
        if step.state is not StepState.AWAITING_APPROVAL or not isinstance(
            context, StepTerminalContext
        ):
            _reject("invalid_transition", "step cannot be rejected")
        next_state, reason_code = StepState.REJECTED, context.reason_code
    elif command is StepLifecycleCommand.CANCEL:
        if step.state not in {
            StepState.PENDING,
            StepState.READY,
            StepState.AWAITING_APPROVAL,
        } or not isinstance(context, StepTerminalContext):
            _reject("invalid_transition", "step cannot be cancelled")
        next_state, reason_code = StepState.CANCELLED, context.reason_code
    elif command is StepLifecycleCommand.SKIP:
        if step.state not in {
            StepState.PENDING,
            StepState.READY,
            StepState.AWAITING_APPROVAL,
        } or not isinstance(context, StepTerminalContext):
            _reject("invalid_transition", "step cannot be skipped")
        next_state, reason_code = StepState.SKIPPED, context.reason_code
    else:
        _reject("invalid_transition", "step lifecycle command is not legal here")

    terminal_reason = reason_code if next_state in _TERMINAL_STATES else None
    updated = replace(
        step,
        state=next_state,
        updated_at=occurred_at,
        version=step.version + 1,
        terminal_reason_code=terminal_reason,
    )
    transition = StepStateTransition(
        step_id=step.id,
        run_id=step.run_id,
        sequence=updated.version,
        command=command,
        previous_state=step.state,
        new_state=next_state,
        reason_code=reason_code,
        occurred_at=occurred_at,
        expected_version=step.version,
        resulting_version=updated.version,
    )
    return StepTransitionResult(updated, transition)
