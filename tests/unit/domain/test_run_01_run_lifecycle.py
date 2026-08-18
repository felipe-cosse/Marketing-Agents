"""RUN-01: exhaustive pure lifecycle transitions and ADR-0004 evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.domain.entities import Run
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.run_lifecycle import (
    ApprovalBarrierContext,
    ApprovalRejectionContext,
    CancellationContext,
    CompletionContext,
    FailureContext,
    NoRunTransitionContext,
    PlanDispositionContext,
    RunFailurePhase,
    RunLifecycleCommand,
    RunTransitionContext,
    RunTransitionError,
    initial_received_transition,
    transition_run,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _run(
    state: RunState = RunState.RECEIVED,
    *,
    approval_required: bool | None = None,
    version: int = 1,
    updated_at: datetime = NOW,
) -> Run:
    if state in {RunState.AWAITING_APPROVAL, RunState.REJECTED}:
        approval_required = True
    elif state in {RunState.PLANNED, RunState.EXECUTING, RunState.COMPLETED}:
        approval_required = False if approval_required is None else approval_required
    terminal_reason = (
        "terminal_test_reason"
        if state
        in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.REJECTED,
            RunState.CANCELLED,
        }
        else None
    )
    return Run(
        id="run.0001",
        work_item_id="work.0001",
        state=state,
        catalog_hash="c" * 64,
        configuration_revision=2,
        created_at=NOW,
        version=version,
        updated_at=updated_at,
        approval_required=approval_required,
        terminal_reason_code=terminal_reason,
    )


def _advance(
    run: Run,
    command: RunLifecycleCommand,
    context: RunTransitionContext,
    seconds: int,
) -> Run:
    return transition_run(run, command, context, NOW + timedelta(seconds=seconds)).run


def test_run_01_read_only_path_is_contiguous_and_completes() -> None:
    run = _run()
    initial = initial_received_transition(run)

    assert (initial.sequence, initial.expected_version, initial.resulting_version) == (1, 0, 1)
    assert initial.previous_state is None
    assert initial.new_state is RunState.RECEIVED

    run = _advance(run, RunLifecycleCommand.MARK_VALIDATED, NoRunTransitionContext(), 1)
    run = _advance(run, RunLifecycleCommand.RECORD_PLAN, PlanDispositionContext(False), 2)
    assert run.state is RunState.PLANNED
    assert run.approval_required is False

    activated = transition_run(
        run,
        RunLifecycleCommand.ACTIVATE_PLAN,
        NoRunTransitionContext(),
        NOW + timedelta(seconds=3),
    )
    assert activated.run.state is RunState.EXECUTING
    assert activated.transition.reason_code == "read_only_plan_released"
    assert activated.transition.sequence == activated.run.version == 4
    assert activated.transition.expected_version == 3

    completed = transition_run(
        activated.run,
        RunLifecycleCommand.COMPLETE,
        CompletionContext(2, 2, 0, 0),
        NOW + timedelta(seconds=4),
    )
    assert completed.run.state is RunState.COMPLETED
    assert completed.run.terminal_reason_code == "execution_completed"
    assert completed.run.version == 5


def test_run_01_write_path_requires_exact_current_unexpired_approval() -> None:
    run = _run()
    run = _advance(run, RunLifecycleCommand.MARK_VALIDATED, NoRunTransitionContext(), 1)
    run = _advance(run, RunLifecycleCommand.RECORD_PLAN, PlanDispositionContext(True), 2)
    run = _advance(run, RunLifecycleCommand.ACTIVATE_PLAN, NoRunTransitionContext(), 3)
    assert run.state is RunState.AWAITING_APPROVAL
    assert run.approval_required is True

    invalid = ApprovalBarrierContext(
        required_action_hashes=(HASH_A, HASH_B),
        current_action_hashes=(HASH_A,),
        approved_action_hashes=(HASH_A, HASH_B),
        expires_at_by_hash={
            HASH_A: NOW + timedelta(minutes=5),
            HASH_B: NOW + timedelta(minutes=5),
        },
    )
    with pytest.raises(RunTransitionError) as rejected:
        transition_run(
            run,
            RunLifecycleCommand.RELEASE_APPROVED_PLAN,
            invalid,
            NOW + timedelta(seconds=4),
        )
    assert rejected.value.code == "approval_barrier_incomplete"
    assert rejected.value.audit_evidence.accepted is False
    assert HASH_A not in repr(rejected.value.audit_evidence)
    assert HASH_B not in repr(rejected.value.audit_evidence)

    boundary_expired = ApprovalBarrierContext(
        required_action_hashes=(HASH_A,),
        current_action_hashes=(HASH_A,),
        approved_action_hashes=(HASH_A,),
        expires_at_by_hash={HASH_A: NOW + timedelta(seconds=4)},
    )
    with pytest.raises(RunTransitionError) as expired:
        transition_run(
            run,
            RunLifecycleCommand.RELEASE_APPROVED_PLAN,
            boundary_expired,
            NOW + timedelta(seconds=4),
        )
    assert expired.value.code == "approval_barrier_incomplete"

    valid = ApprovalBarrierContext(
        required_action_hashes=(HASH_A, HASH_B),
        current_action_hashes=(HASH_B, HASH_A),
        approved_action_hashes=(HASH_A, HASH_B),
        expires_at_by_hash={
            HASH_A: NOW + timedelta(minutes=5),
            HASH_B: NOW + timedelta(minutes=5),
        },
    )
    released = transition_run(
        run,
        RunLifecycleCommand.RELEASE_APPROVED_PLAN,
        valid,
        NOW + timedelta(seconds=4),
    )
    assert released.run.state is RunState.EXECUTING
    assert released.transition.reason_code == "approval_barrier_satisfied"

    rejected_run = transition_run(
        run,
        RunLifecycleCommand.REJECT_APPROVAL,
        ApprovalRejectionContext((HASH_A, HASH_B), (HASH_B,)),
        NOW + timedelta(seconds=4),
    )
    assert rejected_run.run.state is RunState.REJECTED
    assert rejected_run.run.terminal_reason_code == "approval_rejected"


def _context_for(state: RunState, command: RunLifecycleCommand) -> RunTransitionContext:
    if command in {RunLifecycleCommand.RECEIVE, RunLifecycleCommand.MARK_VALIDATED}:
        return NoRunTransitionContext()
    if command is RunLifecycleCommand.RECORD_PLAN:
        return PlanDispositionContext(False)
    if command is RunLifecycleCommand.ACTIVATE_PLAN:
        return NoRunTransitionContext()
    if command is RunLifecycleCommand.RELEASE_APPROVED_PLAN:
        return ApprovalBarrierContext(
            (HASH_A,),
            (HASH_A,),
            (HASH_A,),
            {HASH_A: NOW + timedelta(minutes=5)},
        )
    if command is RunLifecycleCommand.REJECT_APPROVAL:
        return ApprovalRejectionContext((HASH_A,), (HASH_A,))
    if command is RunLifecycleCommand.COMPLETE:
        return CompletionContext(1, 1, 0, 0)
    if command is RunLifecycleCommand.FAIL:
        phase = {
            RunState.RECEIVED: RunFailurePhase.VALIDATION,
            RunState.VALIDATED: RunFailurePhase.PLANNING,
            RunState.PLANNED: RunFailurePhase.PLANNING,
            RunState.AWAITING_APPROVAL: RunFailurePhase.APPROVAL_PROCESSING,
            RunState.EXECUTING: RunFailurePhase.EXECUTION,
        }.get(state, RunFailurePhase.EXECUTION)
        return FailureContext(phase, "phase_failed")
    return CancellationContext("operator_cancelled")


_LEGAL_COMMANDS = {
    RunState.RECEIVED: {
        RunLifecycleCommand.MARK_VALIDATED,
        RunLifecycleCommand.FAIL,
        RunLifecycleCommand.CANCEL,
    },
    RunState.VALIDATED: {
        RunLifecycleCommand.RECORD_PLAN,
        RunLifecycleCommand.FAIL,
        RunLifecycleCommand.CANCEL,
    },
    RunState.PLANNED: {
        RunLifecycleCommand.ACTIVATE_PLAN,
        RunLifecycleCommand.FAIL,
        RunLifecycleCommand.CANCEL,
    },
    RunState.AWAITING_APPROVAL: {
        RunLifecycleCommand.RELEASE_APPROVED_PLAN,
        RunLifecycleCommand.REJECT_APPROVAL,
        RunLifecycleCommand.FAIL,
        RunLifecycleCommand.CANCEL,
    },
    RunState.EXECUTING: {
        RunLifecycleCommand.COMPLETE,
        RunLifecycleCommand.FAIL,
        RunLifecycleCommand.CANCEL,
    },
    RunState.COMPLETED: set(),
    RunState.FAILED: set(),
    RunState.REJECTED: set(),
    RunState.CANCELLED: set(),
}


@pytest.mark.parametrize("state", tuple(RunState))
@pytest.mark.parametrize("command", tuple(RunLifecycleCommand))
def test_run_01_every_state_command_pair_is_explicit(
    state: RunState,
    command: RunLifecycleCommand,
) -> None:
    run = _run(state, version=7)
    context = _context_for(state, command)

    if command in _LEGAL_COMMANDS[state]:
        result = transition_run(run, command, context, NOW + timedelta(seconds=1))
        assert result.transition.previous_state is state
        assert result.transition.sequence == 8
        assert result.run.version == 8
        assert result.audit_evidence.accepted is True
    else:
        with pytest.raises(RunTransitionError) as rejected:
            transition_run(run, command, context, NOW + timedelta(seconds=1))
        expected_code = (
            "terminal_state_immutable" if not _LEGAL_COMMANDS[state] else "invalid_transition"
        )
        assert rejected.value.code == expected_code
        assert rejected.value.audit_evidence.accepted is False


def test_run_01_failures_are_phase_bound_and_completion_is_fail_closed() -> None:
    with pytest.raises(RunTransitionError) as mismatch:
        transition_run(
            _run(RunState.VALIDATED, version=2),
            RunLifecycleCommand.FAIL,
            FailureContext(RunFailurePhase.EXECUTION, "connector_failed"),
            NOW + timedelta(seconds=1),
        )
    assert mismatch.value.code == "failure_phase_mismatch"

    with pytest.raises(RunTransitionError) as incomplete:
        transition_run(
            _run(RunState.EXECUTING, version=4),
            RunLifecycleCommand.COMPLETE,
            CompletionContext(3, 2, 1, 0),
            NOW + timedelta(seconds=1),
        )
    assert incomplete.value.code == "execution_incomplete"


def test_run_01_cancellation_reports_effects_only_after_execution_started() -> None:
    with pytest.raises(RunTransitionError) as pre_execution:
        transition_run(
            _run(RunState.PLANNED, version=3),
            RunLifecycleCommand.CANCEL,
            CancellationContext("operator_cancelled", completed_effect_count=1),
            NOW + timedelta(seconds=1),
        )
    assert pre_execution.value.code == "invalid_cancellation_effects"

    cancelled = transition_run(
        _run(RunState.EXECUTING, version=4),
        RunLifecycleCommand.CANCEL,
        CancellationContext(
            "operator_cancelled",
            completed_effect_count=2,
            outcome_unknown_effect_count=1,
        ),
        NOW + timedelta(seconds=1),
    )
    assert cancelled.run.state is RunState.CANCELLED
    assert cancelled.transition.completed_effect_count == 2
    assert cancelled.transition.outcome_unknown_effect_count == 1
    assert cancelled.audit_evidence.completed_effect_count == 2
    assert cancelled.audit_evidence.outcome_unknown_effect_count == 1


def test_run_01_rejects_stale_time_without_mutating_the_run() -> None:
    run = _run(updated_at=NOW + timedelta(seconds=2))
    with pytest.raises(RunTransitionError) as stale:
        transition_run(
            run,
            RunLifecycleCommand.MARK_VALIDATED,
            NoRunTransitionContext(),
            NOW + timedelta(seconds=1),
        )
    assert stale.value.code == "non_monotonic_time"
    assert run.state is RunState.RECEIVED
    assert run.version == 1
