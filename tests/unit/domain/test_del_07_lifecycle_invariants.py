"""DEL-07: reject malformed lifecycle facts before they can become audit evidence."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from marketing_agents.domain.enums import Effect, RunState, StepState
from marketing_agents.domain.run_lifecycle import (
    ApprovalBarrierContext,
    ApprovalRejectionContext,
    CancellationContext,
    CompletionContext,
    FailureContext,
    NoRunTransitionContext,
    PlanDispositionContext,
    RunLifecycleCommand,
    RunTransitionError,
    initial_received_transition,
    transition_run,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepTransitionError,
    initial_pending_transition,
    transition_step,
)

from tests.unit.domain.test_orch_09_audit_contracts import NOW as STEP_NOW
from tests.unit.domain.test_orch_09_audit_contracts import _step
from tests.unit.domain.test_run_01_run_lifecycle import HASH_A, NOW, _run


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_del_07_plan_disposition_requires_a_boolean(value: Any) -> None:
    with pytest.raises(ValueError, match="boolean"):
        PlanDispositionContext(value)


@pytest.mark.parametrize(
    "field", ["required_action_hashes", "current_action_hashes", "approved_action_hashes"]
)
@pytest.mark.parametrize("hashes", [(), (HASH_A, HASH_A)])
def test_del_07_barrier_rejects_empty_or_duplicate_members(
    field: str, hashes: tuple[str, ...]
) -> None:
    values: dict[str, Any] = dict(
        required_action_hashes=(HASH_A,),
        current_action_hashes=(HASH_A,),
        approved_action_hashes=(HASH_A,),
        expires_at_by_hash={HASH_A: NOW},
    )
    values[field] = hashes
    with pytest.raises(ValueError, match=r"empty|unique"):
        ApprovalBarrierContext(**values)


@pytest.mark.parametrize("field", ["required_action_hashes", "rejected_action_hashes"])
@pytest.mark.parametrize("hashes", [(), (HASH_A, HASH_A)])
def test_del_07_rejection_rejects_empty_or_duplicate_members(
    field: str, hashes: tuple[str, ...]
) -> None:
    values = dict(required_action_hashes=(HASH_A,), rejected_action_hashes=(HASH_A,))
    values[field] = hashes
    with pytest.raises(ValueError, match=r"empty|unique"):
        ApprovalRejectionContext(**values)


@pytest.mark.parametrize(
    "counts", [(1, True, 0, 0), (1, -1, 2, 0), (1, 1.0, 0, 0), (0, 0, 0, 0), (2, 1, 0, 0)]
)
def test_del_07_completion_requires_a_positive_consistent_census(counts: Any) -> None:
    with pytest.raises(ValueError, match=r"counts|at least one"):
        CompletionContext(*counts)


def test_del_07_failure_phase_is_an_enum_not_an_untrusted_string() -> None:
    with pytest.raises(ValueError, match="phase"):
        FailureContext("execution", "failure.test")  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["completed_effect_count", "outcome_unknown_effect_count"])
@pytest.mark.parametrize("value", [True, -1, 0.5])
def test_del_07_cancellation_census_rejects_non_counts(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match="counts"):
        CancellationContext("cancelled.test", **{field: value})


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"command": "receive"}, "exact enum"),
        ({"previous_state": "received"}, "exact enum"),
        ({"new_state": "received"}, "exact enum"),
        ({"sequence": 2}, "contiguous"),
        ({"sequence": 0, "resulting_version": 0, "expected_version": -1}, "positive"),
        ({"completed_effect_count": True}, "counts"),
        ({"command": RunLifecycleCommand.MARK_VALIDATED}, "initial transition"),
        ({"sequence": 2, "resulting_version": 2, "expected_version": 1}, "existing state"),
        ({"completed_effect_count": 1}, "only cancellation"),
    ],
)
def test_del_07_run_transition_fact_rejects_invalid_evidence(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(initial_received_transition(_run()), **changes)


def test_del_07_run_transition_fact_rejects_an_inconsistent_command_edge() -> None:
    result = transition_run(
        _run(), RunLifecycleCommand.MARK_VALIDATED, NoRunTransitionContext(), NOW
    )
    with pytest.raises(ValueError, match="inconsistent"):
        replace(result.transition, command=RunLifecycleCommand.COMPLETE)


@pytest.mark.parametrize(
    "changes",
    [{"state": RunState.VALIDATED}, {"version": 2}, {"updated_at": NOW + timedelta(seconds=1)}],
)
def test_del_07_initial_run_fact_requires_pristine_state(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="initial run"):
        initial_received_transition(replace(_run(), **changes))


def test_del_07_run_result_binds_exact_types_run_audit_and_terminal_reason() -> None:
    result = transition_run(
        _run(), RunLifecycleCommand.CANCEL, CancellationContext("cancel.test"), NOW
    )
    for field in ("run", "transition", "audit_evidence"):
        with pytest.raises(ValueError, match="exact immutable"):
            replace(result, **{field: object()})
    with pytest.raises(ValueError, match="updated Run"):
        replace(result, run=replace(result.run, id="run.other"))
    with pytest.raises(ValueError, match="audit evidence"):
        replace(result, audit_evidence=replace(result.audit_evidence, accepted=False))
    with pytest.raises(ValueError, match="terminal reason"):
        replace(result, run=replace(result.run, terminal_reason_code="other.reason"))


def test_del_07_run_result_rejects_lost_approval_disposition() -> None:
    read_plan = _run(RunState.PLANNED, approval_required=False)
    activated = transition_run(
        read_plan, RunLifecycleCommand.ACTIVATE_PLAN, NoRunTransitionContext(), NOW
    )
    with pytest.raises(ValueError, match="approval disposition"):
        replace(activated, run=replace(activated.run, approval_required=True))
    write_plan = _run(RunState.AWAITING_APPROVAL)
    approved = transition_run(
        write_plan,
        RunLifecycleCommand.RELEASE_APPROVED_PLAN,
        ApprovalBarrierContext(
            (HASH_A,), (HASH_A,), (HASH_A,), {HASH_A: NOW + timedelta(minutes=1)}
        ),
        NOW,
    )
    with pytest.raises(ValueError, match="write-bearing"):
        replace(approved, run=replace(approved.run, approval_required=False))


@pytest.mark.parametrize("command", [RunLifecycleCommand.FAIL, RunLifecycleCommand.CANCEL])
def test_del_07_run_terminal_commands_require_their_own_context(
    command: RunLifecycleCommand,
) -> None:
    with pytest.raises(RunTransitionError) as rejected:
        transition_run(_run(), command, NoRunTransitionContext(), NOW)
    assert rejected.value.code == "invalid_transition"
    assert rejected.value.audit_evidence.accepted is False


def test_del_07_run_rejection_must_name_an_action_in_the_required_set() -> None:
    with pytest.raises(RunTransitionError) as rejected:
        transition_run(
            _run(RunState.AWAITING_APPROVAL),
            RunLifecycleCommand.REJECT_APPROVAL,
            ApprovalRejectionContext((HASH_A,), ("b" * 64,)),
            NOW,
        )
    assert rejected.value.code == "approval_rejection_mismatch"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"command": "initialize"}, "exact enum"),
        ({"previous_state": "pending"}, "exact enum"),
        ({"new_state": "pending"}, "exact enum"),
        ({"sequence": 2}, "contiguous"),
        ({"command": StepLifecycleCommand.MARK_READY}, "initial step transition"),
        ({"sequence": 2, "resulting_version": 2, "expected_version": 1}, "existing state"),
    ],
)
def test_del_07_step_transition_fact_rejects_invalid_evidence(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(initial_pending_transition(_step(Effect.READ, StepState.PENDING)), **changes)


@pytest.mark.parametrize(
    "changes",
    [{"state": StepState.READY}, {"version": 2}, {"updated_at": STEP_NOW + timedelta(seconds=1)}],
)
def test_del_07_initial_step_fact_requires_pristine_state(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="initial step"):
        initial_pending_transition(replace(_step(Effect.READ, StepState.PENDING), **changes))


def test_del_07_step_rejects_stale_time_and_unrecognized_command() -> None:
    step = _step(Effect.READ, StepState.READY, version=2)
    with pytest.raises(StepTransitionError) as stale:
        transition_step(step, StepLifecycleCommand.START, NoStepTransitionContext(), STEP_NOW)
    assert stale.value.code == "non_monotonic_time"
    with pytest.raises(StepTransitionError) as invalid:
        transition_step(
            step, StepLifecycleCommand.INITIALIZE, NoStepTransitionContext(), step.updated_at
        )
    assert invalid.value.code == "invalid_transition"


def test_del_07_step_result_binds_exact_types_identity_and_terminal_reason() -> None:
    result = transition_step(
        _step(Effect.READ, StepState.EXECUTING),
        StepLifecycleCommand.SUCCEED,
        NoStepTransitionContext(),
        STEP_NOW,
    )
    for field in ("step", "transition"):
        with pytest.raises(ValueError, match="exact immutable"):
            replace(result, **{field: object()})
    with pytest.raises(ValueError, match="updated step"):
        replace(result, step=replace(result.step, run_id="run.other"))
    with pytest.raises(ValueError, match="terminal reason"):
        replace(result, step=replace(result.step, terminal_reason_code="other.reason"))


def test_del_07_read_step_cannot_borrow_a_write_approval_transition() -> None:
    result = transition_step(
        _step(Effect.READ, StepState.READY),
        StepLifecycleCommand.START,
        NoStepTransitionContext(),
        STEP_NOW,
    )
    with pytest.raises(ValueError, match="write-only"):
        replace(
            result,
            transition=replace(
                result.transition, command=StepLifecycleCommand.START_RESERVED_WRITE
            ),
        )
