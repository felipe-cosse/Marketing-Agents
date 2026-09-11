"""DEL-07: replay and cancellation reject corrupt port facts without committing.

The fixtures seal valid domain records and real audit drafts. History validation
is a separately tested precondition, mocked here to isolate boundary replay.
Explicit corruption simulates an invalid typed repository response; it does not
claim ordinary domain constructors permit such records.
"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from marketing_agents.application.ports.repositories import (
    ApprovalRepositoryConflict,
    CurrentAuthorizationSet,
)
from marketing_agents.application.services import approval_boundaries as boundary
from marketing_agents.application.services.approval_records import ApprovalRecordServiceError
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.approval import (
    ApprovalUse,
    AuthorizationSetStatus,
)
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.entities import ActionReservationSnapshot, DispatchLease
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    Effect,
    ExternalActionState,
    RunState,
    StepState,
)
from marketing_agents.domain.run_lifecycle import (
    ApprovalBarrierContext,
    CancellationContext,
    RunLifecycleCommand,
    transition_run,
)
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepTerminalContext,
    transition_step,
)

from tests.unit.application.test_del_07_boundary_snapshot_guards import _boundary_fixture
from tests.unit.application.test_orch_06_controlled_read_executor_boundaries import _step
from tests.unit.domain.test_run_08_approval_records import NOW, _decision


def _corrupt(record: Any, **changes: Any) -> None:
    for key, value in changes.items():
        object.__setattr__(record, key, value)


def _store_events(fixture: SimpleNamespace, *drafts: Any) -> None:
    for draft in drafts:
        sequence = len(fixture.events) + 1
        fixture.events[(draft.aggregate_type, draft.aggregate_id, draft.mutation_version)] = (
            AuditEvent(draft, sequence, sequence, sequence)
        )


def _base() -> SimpleNamespace:
    fixture = _boundary_fixture()
    fixture.step = replace(fixture.step, state=StepState.AWAITING_APPROVAL)
    fixture.service._require_complete_history = AsyncMock()
    fixture.events = {}
    fixture.uow.audits.get_mutation_event = AsyncMock(
        side_effect=lambda kind, identity, version: fixture.events.get((kind, identity, version))
    )
    fixture.uow.run_steps.list_transitions = AsyncMock(return_value=())
    fixture.uow.run_steps.apply_transition = AsyncMock(return_value=True)
    fixture.uow.runs.list_transitions = AsyncMock(return_value=())
    fixture.uow.runs.apply_transition = AsyncMock(return_value=True)
    fixture.control = SimpleNamespace(
        policy_hash=fixture.request.plan_hash,
        version=1,
        started_at=None,
        deadline_at=None,
        cancel_requested_at=None,
    )
    fixture.uow.execution_control = SimpleNamespace(
        get=AsyncMock(return_value=fixture.control),
        request_cancel=AsyncMock(),
        start_execution=AsyncMock(),
    )
    fixture.snapshot = boundary._BoundarySnapshot(
        fixture.selection,
        fixture.run,
        (fixture.stored,),
        (fixture.stored,),
        (fixture.action,),
        (fixture.step,),
        (fixture.step,),
    )
    fixture.service._load_current = AsyncMock(side_effect=lambda *_args: fixture.snapshot)
    fixture.clock.now.return_value = NOW + timedelta(seconds=3)
    return fixture


def _approved_fixture() -> SimpleNamespace:
    fixture = _base()
    decision = replace(
        _decision(),
        authentication_method="local_fixed",
        authority_roles=fixture.request.policy.required_roles | {"approver"},
        authority_scopes=fixture.request.policy.required_scopes | {"approvals:decide"},
        decided_at=NOW + timedelta(seconds=1),
    )
    approved = replace(
        fixture.stored,
        status=ApprovalStatus.APPROVED,
        version=2,
        updated_at=decision.decided_at,
        decision=decision,
    )
    action = replace(
        fixture.action,
        state=ExternalActionState.APPROVED,
        version=3,
        updated_at=decision.decided_at,
    )
    factory = AuditEventFactory(
        AuditContext.authenticated_user(
            decision.actor_id,
            authentication_method=decision.authentication_method,
            correlation_id=decision.correlation_id,
        )
    )
    _store_events(
        fixture,
        factory.action_decided(fixture.action, action, approved),
        factory.approval_decided(fixture.stored, approved, action),
    )
    fixture.approved, fixture.approved_action = approved, action
    fixture.snapshot = replace(fixture.snapshot, requests=(approved,), actions=(action,))
    return fixture


def _released_fixture() -> SimpleNamespace:
    fixture = _approved_fixture()
    released_at = NOW + timedelta(seconds=2)
    request, decision = fixture.request, fixture.approved.decision
    run_result = transition_run(
        fixture.run,
        RunLifecycleCommand.RELEASE_APPROVED_PLAN,
        ApprovalBarrierContext(
            (request.action_hash,),
            (request.action_hash,),
            (request.action_hash,),
            {request.action_hash: request.expires_at},
        ),
        released_at,
    )
    step_result = transition_step(
        fixture.step, StepLifecycleCommand.RELEASE_APPROVAL, NoStepTransitionContext(), released_at
    )
    use = ApprovalUse(
        id="use.del07",
        request_id=request.id,
        decision_id=decision.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        step_id=request.step_id,
        step_key=request.step_key,
        reservation_id="reservation.del07",
        used_at=released_at,
    )
    reservation = ActionReservationSnapshot(
        reservation_id=use.reservation_id,
        authorization_set_id=request.authorization_set_id,
        approval_request_id=request.id,
        approval_decision_id=decision.id,
        action_hash=request.action_hash,
        capability_id=request.capability_id,
        binding_id=request.binding_id,
        idempotency_key=fixture.action.idempotency_key,
        reserved_at=released_at,
    )
    consumed = replace(
        fixture.approved, status=ApprovalStatus.CONSUMED, version=3, updated_at=released_at, use=use
    )
    action = replace(
        fixture.approved_action,
        state=ExternalActionState.DISPATCH_RESERVED,
        version=4,
        updated_at=released_at,
        reservation=reservation,
    )
    authorization_set = replace(
        fixture.selection.authorization_set,
        status=AuthorizationSetStatus.RELEASED,
        version=2,
        updated_at=released_at,
        released_at=released_at,
        released_run_version=run_result.run.version,
        release_hash="c" * 64,
        terminal_reason_code="approval_barrier_satisfied",
    )
    factory = AuditEventFactory(fixture.context)
    _store_events(
        fixture,
        factory.action_dispatch_reserved(fixture.approved_action, action, consumed),
        factory.approval_consumed(fixture.approved, consumed, action),
        factory.run_transition(run_result.run, run_result.transition),
        factory.step_transition(step_result.step, step_result.transition),
    )
    fixture.release_transition = run_result.transition
    fixture.step_release = step_result.transition
    fixture.control.started_at = released_at
    fixture.control.deadline_at = released_at + timedelta(minutes=1)
    fixture.uow.runs.list_transitions.return_value = (run_result.transition,)
    fixture.uow.run_steps.list_transitions.return_value = (step_result.transition,)
    fixture.snapshot = replace(
        fixture.snapshot,
        selection=CurrentAuthorizationSet(fixture.selection.head, authorization_set),
        run=run_result.run,
        requests=(consumed,),
        actions=(action,),
        member_steps=(step_result.step,),
        plan_steps=(step_result.step,),
    )
    cancelled = replace(
        action,
        state=ExternalActionState.CANCELLED,
        version=5,
        updated_at=NOW + timedelta(seconds=3),
        terminal_reason_code="operator_cancelled",
    )
    fixture.uow.external_actions.cancel_unstarted_after_release = AsyncMock(return_value=cancelled)
    return fixture


def _cancelled_released_fixture() -> SimpleNamespace:
    fixture = _released_fixture()
    occurred_at = NOW + timedelta(seconds=3)
    snapshot = fixture.snapshot
    run_result = transition_run(
        snapshot.run,
        RunLifecycleCommand.CANCEL,
        CancellationContext("operator_cancelled"),
        occurred_at,
    )
    step_result = transition_step(
        snapshot.plan_steps[0],
        StepLifecycleCommand.CANCEL,
        StepTerminalContext("run_cancelled"),
        occurred_at,
    )
    cancelled = fixture.uow.external_actions.cancel_unstarted_after_release.return_value
    factory = AuditEventFactory(fixture.context)
    _store_events(
        fixture,
        factory.run_transition(run_result.run, run_result.transition),
        factory.step_transition(step_result.step, step_result.transition),
        factory.action_runtime_cancelled(snapshot.actions[0], cancelled),
    )
    fixture.cancellation = run_result.transition
    fixture.control.cancel_requested_at = occurred_at
    fixture.uow.runs.list_transitions.return_value += (run_result.transition,)
    fixture.uow.run_steps.list_transitions.return_value += (step_result.transition,)
    fixture.snapshot = replace(
        snapshot,
        run=run_result.run,
        actions=(cancelled,),
        member_steps=(step_result.step,),
        plan_steps=(step_result.step,),
    )
    return fixture


@pytest.mark.asyncio
async def test_del_07_valid_released_and_post_release_cancelled_replay_are_read_only() -> None:
    for factory in (_released_fixture, _cancelled_released_fixture):
        fixture = factory()
        await fixture.service._require_released_replay(fixture.uow, fixture.snapshot)
        fixture.uow.commit.assert_not_awaited()
        fixture.uow.audits.append_many.assert_not_awaited()


@pytest.mark.parametrize(
    "fault, code",
    [
        ("release_fields", "authorization_release_replay_mismatch"),
        ("control", "authorization_release_control_mismatch"),
        ("run_history", "authorization_release_transition_missing"),
        ("run_transition", "authorization_release_transition_mismatch"),
        ("member_use", "authorization_release_member_mismatch"),
        ("action_audit", "authorization_release_action_audit_missing"),
        ("action_version", "authorization_release_action_audit_mismatch"),
        ("step_history", "authorization_release_step_audit_missing"),
    ],
)
@pytest.mark.asyncio
async def test_del_07_released_replay_requires_every_exact_witness(fault: str, code: str) -> None:
    fixture = _released_fixture()
    snapshot = fixture.snapshot
    if fault == "release_fields":
        _corrupt(snapshot.selection.authorization_set, released_at=None)
    elif fault == "control":
        fixture.uow.execution_control.get.return_value = None
    elif fault == "run_history":
        fixture.uow.runs.list_transitions.return_value = ()
    elif fault == "run_transition":
        _corrupt(fixture.release_transition, reason_code="wrong.reason")
    elif fault == "member_use":
        _corrupt(snapshot.requests[0], use=None)
    elif fault == "action_audit":
        del fixture.events[("external_action", fixture.action.id, 4)]
    elif fault == "action_version":
        _corrupt(
            fixture.events[("external_action", fixture.action.id, 4)].draft, mutation_version=None
        )
    else:
        fixture.uow.run_steps.list_transitions.return_value = ()
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service._require_released_replay(fixture.uow, snapshot)
    assert failure.value.code == code
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()


@pytest.mark.parametrize(
    "fault, code",
    [
        ("cancel_transition", "post_release_cancel_audit_missing"),
        ("cancel_control", "post_release_cancel_control_missing"),
        ("queued_step", "post_release_cancel_step_incomplete"),
        ("cancel_step_history", "post_release_cancel_step_audit_missing"),
        ("queued_action", "post_release_cancel_action_incomplete"),
        ("cancel_action_history", "post_release_cancel_action_audit_missing"),
        ("cancel_action_reservation", "post_release_cancel_action_mismatch"),
        ("cancel_action_attempt", "post_release_cancel_action_attempt_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_del_07_cancelled_release_cannot_replay_without_its_fence_and_terminal_facts(
    fault: str, code: str
) -> None:
    fixture = _cancelled_released_fixture()
    snapshot = fixture.snapshot
    if fault == "cancel_transition":
        fixture.uow.runs.list_transitions.return_value = (fixture.release_transition,)
    elif fault == "cancel_control":
        fixture.control.cancel_requested_at = None
    elif fault == "queued_step":
        _corrupt(snapshot.plan_steps[0], state=StepState.READY)
    elif fault == "cancel_step_history":
        fixture.uow.run_steps.list_transitions.return_value = (fixture.step_release,)
    elif fault == "queued_action":
        _corrupt(snapshot.actions[0], state=ExternalActionState.DISPATCH_RESERVED)
    elif fault == "cancel_action_history":
        del fixture.events[("external_action", fixture.action.id, 5)]
    elif fault == "cancel_action_reservation":
        _corrupt(snapshot.actions[0], reservation=None)
    else:
        _corrupt(
            fixture.events[("external_action", fixture.action.id, 5)].draft, action_attempt_number=2
        )
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service._require_released_replay(fixture.uow, snapshot)
    assert failure.value.code == code
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "fault, code",
    [
        ("run", "released_run_not_cancellable"),
        ("control", "execution_control_invalid"),
        ("action_cas", "action_cancellation_conflict"),
        ("duplicate_step", "released_action_step_binding_invalid"),
        ("unbound_write", "released_action_step_binding_invalid"),
        ("unstarted_write", "released_action_step_binding_invalid"),
        ("step_cas", "step_cancellation_conflict"),
        ("run_cas", "run_transition_conflict"),
    ],
)
@pytest.mark.asyncio
async def test_del_07_post_release_cancel_failures_cannot_commit(fault: str, code: str) -> None:
    fixture = _released_fixture()
    snapshot = fixture.snapshot
    if fault == "run":
        snapshot = replace(snapshot, run=fixture.run)
    elif fault == "control":
        fixture.uow.execution_control.get.return_value = None
    elif fault == "action_cas":
        fixture.uow.external_actions.cancel_unstarted_after_release.return_value = None
    elif fault == "duplicate_step":
        snapshot = replace(snapshot, actions=snapshot.actions * 2)
    elif fault in {"unbound_write", "unstarted_write"}:
        executing = replace(snapshot.plan_steps[0], state=StepState.EXECUTING)
        snapshot = replace(
            snapshot,
            plan_steps=(executing,),
            actions=() if fault == "unbound_write" else (fixture.approved_action,),
        )
    elif fault == "step_cas":
        fixture.uow.run_steps.apply_transition.return_value = False
    else:
        fixture.uow.runs.apply_transition.return_value = False
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service._cancel_released_in_uow(
            fixture.uow, snapshot, audit_context=fixture.context
        )
    assert failure.value.code == code
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()


@pytest.mark.parametrize("disposition", ["retry_backoff", "running", "corrupt"])
@pytest.mark.asyncio
async def test_del_07_read_retry_cancellation_requires_exact_attempt_lineage(
    disposition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _released_fixture()
    read = replace(
        _step(effect=Effect.READ, kind=AttemptKind.MODEL),
        id="step.read",
        run_id=fixture.run.id,
        plan_hash=fixture.request.plan_hash,
        created_at=NOW,
        updated_at=NOW,
        state=StepState.EXECUTING,
    )
    fixture.snapshot = replace(fixture.snapshot, plan_steps=(*fixture.snapshot.plan_steps, read))
    classify = AsyncMock(return_value=disposition)
    if disposition == "corrupt":
        classify.side_effect = ValueError("private provider diagnostic")
    monkeypatch.setattr(boundary, "_executing_read_disposition", classify)
    if disposition == "corrupt":
        with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
            await fixture.service._cancel_released_in_uow(
                fixture.uow, fixture.snapshot, audit_context=fixture.context
            )
        assert failure.value.code == "execution_attempt_lineage_invalid"
        assert "private" not in str(failure.value)
        fixture.uow.commit.assert_not_awaited()
    else:
        result = await fixture.service._cancel_released_in_uow(
            fixture.uow, fixture.snapshot, audit_context=fixture.context
        )
        assert result.disposition is boundary.ApprovalBoundaryDisposition.CANCELLED
        assert fixture.uow.run_steps.apply_transition.await_count == (
            2 if disposition == "retry_backoff" else 1
        )
        fixture.uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_del_07_unstarted_executing_write_is_failed_when_its_action_is_cancelled() -> None:
    fixture = _released_fixture()
    step = replace(fixture.snapshot.plan_steps[0], state=StepState.EXECUTING)
    snapshot = replace(fixture.snapshot, plan_steps=(step,))
    result = await fixture.service._cancel_released_in_uow(
        fixture.uow, snapshot, audit_context=fixture.context
    )
    assert result.disposition is boundary.ApprovalBoundaryDisposition.CANCELLED
    assert (
        fixture.uow.run_steps.apply_transition.await_args.kwargs["result"].step.state
        is StepState.FAILED
    )


@pytest.mark.asyncio
async def test_del_07_disabled_retry_budget_fails_closed_without_calling_repository() -> None:
    fixture = _base()
    fixture.service._MAX_STALE_CONTROL_RETRIES = 0
    with pytest.raises(AssertionError, match="bounded boundary cancellation"):
        await fixture.service.cancel(fixture.run.id, audit_context=fixture.context)
    fixture.factory.assert_not_called()


def _closed_fixture() -> SimpleNamespace:
    fixture = _base()
    closed_at = NOW + timedelta(seconds=3)
    run_result = transition_run(
        fixture.run,
        RunLifecycleCommand.CANCEL,
        CancellationContext("operator_cancelled"),
        closed_at,
    )
    step_result = transition_step(
        fixture.step,
        StepLifecycleCommand.CANCEL,
        StepTerminalContext("operator_cancelled"),
        closed_at,
    )
    superseded = replace(
        fixture.stored,
        status=ApprovalStatus.SUPERSEDED,
        version=2,
        updated_at=closed_at,
        superseded_at=closed_at,
        superseded_reason_code="run_cancelled",
    )
    action = replace(
        fixture.action,
        state=ExternalActionState.CANCELLED,
        version=3,
        updated_at=closed_at,
        terminal_reason_code="operator_cancelled",
    )
    closed = replace(
        fixture.selection.authorization_set,
        status=AuthorizationSetStatus.CANCELLED,
        version=2,
        updated_at=closed_at,
        terminal_reason_code="operator_cancelled",
    )
    factory = AuditEventFactory(fixture.context)
    _store_events(
        fixture,
        factory.run_transition(run_result.run, run_result.transition),
        factory.step_transition(step_result.step, step_result.transition),
        factory.action_cancelled(fixture.action, action, superseded),
        factory.approval_superseded(fixture.stored, superseded, action),
    )
    fixture.control.cancel_requested_at = closed_at
    fixture.uow.runs.list_transitions.return_value = (run_result.transition,)
    fixture.uow.run_steps.list_transitions.return_value = (step_result.transition,)
    fixture.snapshot = replace(
        fixture.snapshot,
        selection=CurrentAuthorizationSet(fixture.selection.head, closed),
        run=run_result.run,
        requests=(superseded,),
        actions=(action,),
        member_steps=(step_result.step,),
        plan_steps=(step_result.step,),
    )
    return fixture


@pytest.mark.asyncio
async def test_del_07_valid_closed_replay_is_read_only() -> None:
    fixture = _closed_fixture()
    await fixture.service._require_closed_replay(fixture.uow, fixture.snapshot)
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()


@pytest.mark.parametrize(
    "fault, code",
    [
        ("control", "authorization_close_control_mismatch"),
        ("parent", "authorization_close_replay_mismatch"),
        ("run_history", "authorization_close_transition_missing"),
        ("step", "authorization_close_step_mismatch"),
        ("step_history", "authorization_close_step_audit_missing"),
        ("step_reason", "authorization_close_step_audit_mismatch"),
        ("action", "authorization_close_action_mismatch"),
        ("request", "authorization_close_request_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_del_07_closed_replay_requires_complete_terminal_evidence(
    fault: str, code: str
) -> None:
    fixture = _closed_fixture()
    if fault == "control":
        fixture.control.cancel_requested_at = None
    elif fault == "parent":
        fixture.snapshot = replace(fixture.snapshot, run=fixture.run)
    elif fault == "run_history":
        fixture.uow.runs.list_transitions.return_value = ()
    elif fault == "step":
        _corrupt(fixture.snapshot.plan_steps[0], state=StepState.EXECUTING)
    elif fault == "step_history":
        fixture.uow.run_steps.list_transitions.return_value = ()
    elif fault == "step_reason":
        _corrupt(fixture.uow.run_steps.list_transitions.return_value[0], reason_code="other.reason")
    elif fault == "action":
        _corrupt(fixture.snapshot.actions[0], state=ExternalActionState.AWAITING_APPROVAL)
    else:
        # Leave its audited terminal action intact, but corrupt the request leaf
        # and corresponding projected status so the terminal-status guard decides.
        _corrupt(fixture.snapshot.requests[0], status=ApprovalStatus.PENDING)
        event = fixture.events[("external_action", fixture.action.id, 3)]
        values = dict(event.draft.safe_metadata.values)
        values["approval_status"] = "pending"
        _corrupt(event.draft.safe_metadata, values=values)
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service._require_closed_replay(fixture.uow, fixture.snapshot)
    assert failure.value.code == code
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "fault, code",
    [
        ("terminal", "authorization_set_already_terminal"),
        ("run", "run_not_awaiting_approval"),
        ("control", "execution_control_invalid"),
        ("fence", "cancellation_conflict"),
        ("close_cas", "stale_set"),
    ],
)
@pytest.mark.asyncio
async def test_del_07_pre_release_cancellation_faults_do_not_commit(fault: str, code: str) -> None:
    fixture = _closed_fixture() if fault == "terminal" else _base()
    if fault == "run":
        fixture.snapshot = replace(
            fixture.snapshot, run=replace(fixture.run, state=RunState.EXECUTING)
        )
    elif fault == "control":
        fixture.uow.execution_control.get.return_value = None
    elif fault == "fence":
        fixture.uow.execution_control.request_cancel.side_effect = RuntimeError(
            "private diagnostics"
        )
    elif fault == "close_cas":
        fixture.uow.approvals.close_current_set = AsyncMock(
            side_effect=ApprovalRepositoryConflict("stale_set", "private diagnostics")
        )
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service._cancel_once(fixture.run.id, audit_context=fixture.context)
    assert failure.value.code == code
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()


@pytest.mark.parametrize(
    "fault, code",
    [
        ("missing_decision", "approval_decision_missing"),
        ("release_cas", "stale_set"),
        ("control", "execution_control_invalid"),
        ("start", "execution_start_conflict"),
    ],
)
@pytest.mark.asyncio
async def test_del_07_release_requires_decision_cas_and_atomic_execution_start(
    fault: str, code: str
) -> None:
    fixture = _approved_fixture()
    fixture.uow.approvals.release_current_set = AsyncMock()
    if fault == "missing_decision":
        _corrupt(fixture.approved, decision=None)
    elif fault == "release_cas":
        fixture.uow.approvals.release_current_set.side_effect = ApprovalRepositoryConflict(
            "stale_set", "private diagnostics"
        )
    elif fault == "control":
        fixture.uow.execution_control.get.return_value = None
    else:
        fixture.uow.execution_control.start_execution.side_effect = RuntimeError(
            "private diagnostics"
        )
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service._release_in_uow(
            fixture.uow,
            fixture.snapshot,
            released_at=NOW + timedelta(seconds=3),
            audit_context=fixture.context,
        )
    assert failure.value.code == code
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_expiry_persistence_fault_never_returns_a_successful_boundary() -> None:
    fixture = _base()
    fixture.clock.now.return_value = fixture.request.expires_at
    fixture.service._records.mark_expired_in_uow = AsyncMock(
        side_effect=ApprovalRecordServiceError("stale_expiry", "private diagnostics")
    )
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service.evaluate_in_uow(
            fixture.uow, fixture.run.id, audit_context=fixture.context
        )
    assert failure.value.code == "stale_expiry"
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_expiry_replay_binds_awaiting_action_and_expiration_time() -> None:
    fixture = _base()
    expired = replace(
        fixture.stored,
        status=ApprovalStatus.EXPIRED,
        version=2,
        updated_at=fixture.request.expires_at,
        expired_at=fixture.request.expires_at,
    )
    fixture.snapshot = replace(
        fixture.snapshot,
        requests=(expired,),
        actions=(replace(fixture.action, state=ExternalActionState.APPROVED),),
    )
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service.evaluate_in_uow(
            fixture.uow, fixture.run.id, audit_context=fixture.context
        )
    assert failure.value.code == "authorization_expiry_action_mismatch"
    _corrupt(expired, expired_at=None)
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as missing:
        await fixture.service._require_expiry_audit(
            fixture.uow, expired, fixture.action, action_version=2
        )
    assert missing.value.code == "authorization_expiry_mismatch"


@pytest.mark.parametrize("kind", ["run", "step"])
@pytest.mark.asyncio
async def test_del_07_transition_audit_sequence_is_independently_bound(kind: str) -> None:
    fixture = _released_fixture()
    transition = fixture.release_transition if kind == "run" else fixture.step_release
    identity = fixture.run.id if kind == "run" else fixture.step.id
    event = fixture.events[(kind, identity, transition.resulting_version)]
    _corrupt(event.draft, transition_sequence=999)
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        if kind == "run":
            await fixture.service._require_run_transition_audit(
                fixture.uow, transition, run_id=identity
            )
        else:
            await fixture.service._require_step_transition_audit(
                fixture.uow, transition, step=fixture.step
            )
    assert failure.value.code == f"authorization_{kind}_audit_mismatch"


@pytest.mark.parametrize(
    "fault, code",
    [
        ("authority", "approval_authority_snapshot_mismatch"),
        ("missing", "approval_decision_audit_missing"),
        ("mismatch", "approval_decision_audit_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_del_07_release_rechecks_human_authority_and_decision_audits(
    fault: str, code: str
) -> None:
    fixture = _approved_fixture()
    if fault == "authority":
        _corrupt(fixture.approved.decision, authentication_method="anonymous")
    elif fault == "missing":
        del fixture.events[("external_action", fixture.action.id, 3)]
    else:
        _corrupt(
            fixture.events[("external_action", fixture.action.id, 3)].draft,
            correlation_id="other.correlation",
        )
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service._require_release_decision_witnesses(
            fixture.uow, fixture.approved, fixture.approved_action
        )
    assert failure.value.code == code


@pytest.mark.parametrize("fault", ["missing", "mismatch"])
def test_del_07_audit_event_binding_cannot_be_omitted_or_substituted(fault: str) -> None:
    fixture = _released_fixture()
    event = fixture.events[("run", fixture.run.id, fixture.release_transition.resulting_version)]
    fields = (
        "event_type",
        "run_id",
        "aggregate_id",
        "mutation_version",
        "occurred_at",
        "previous_state",
        "new_state",
        "reason_code",
    )
    expected = {field: getattr(event, field) for field in fields}
    if fault == "mismatch":
        expected["reason_code"] = "wrong.reason"
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        fixture.service._require_bound_event(None if fault == "missing" else event, **expected)
    assert failure.value.code == f"authorization_boundary_audit_{fault}"


@pytest.mark.asyncio
async def test_del_07_rejection_cas_conflict_cannot_commit_or_append_success_audit() -> None:
    fixture = _approved_fixture()
    decision = replace(
        fixture.approved.decision,
        decision=ApprovalDecisionKind.REJECT,
        reason_code="approval_rejected",
    )
    rejected = replace(fixture.approved, status=ApprovalStatus.REJECTED, decision=decision)
    action = replace(
        fixture.approved_action,
        state=ExternalActionState.REJECTED,
        terminal_reason_code="approval_rejected",
    )
    fixture.snapshot = replace(fixture.snapshot, requests=(rejected,), actions=(action,))
    fixture.uow.approvals.close_current_set = AsyncMock(
        side_effect=ApprovalRepositoryConflict("stale_set", "private persistence diagnostic")
    )
    with pytest.raises(boundary.ApprovalBoundaryServiceError) as failure:
        await fixture.service._reject_in_uow(
            fixture.uow, fixture.snapshot, audit_context=fixture.context
        )
    assert failure.value.code == "stale_set"
    assert "private" not in str(failure.value)
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_cancelled_parent_replay_does_not_claim_inflight_action_was_cancelled() -> (
    None
):
    fixture = _cancelled_released_fixture()
    started_at = NOW + timedelta(seconds=2, microseconds=500_000)
    ready = replace(
        fixture.snapshot.member_steps[0],
        state=StepState.READY,
        version=2,
        updated_at=NOW + timedelta(seconds=2),
        terminal_reason_code=None,
    )
    started = transition_step(
        ready, StepLifecycleCommand.START_RESERVED_WRITE, NoStepTransitionContext(), started_at
    )
    action = replace(
        fixture.snapshot.actions[0],
        state=ExternalActionState.DISPATCHING,
        version=5,
        updated_at=started_at,
        terminal_reason_code=None,
        delivery_attempt_count=1,
        lease=DispatchLease("worker.del07", 1, started_at, started_at + timedelta(seconds=60)),
        call_started_at=started_at,
        call_deadline_at=started_at + timedelta(seconds=30),
    )
    fixture.snapshot = replace(
        fixture.snapshot,
        actions=(action,),
        member_steps=(started.step,),
        plan_steps=(started.step,),
    )
    fixture.uow.run_steps.list_transitions.return_value = (fixture.step_release, started.transition)
    del fixture.events[("external_action", fixture.action.id, 5)]
    await fixture.service._require_released_replay(fixture.uow, fixture.snapshot)
    assert action.state is ExternalActionState.DISPATCHING
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.external_actions.cancel_unstarted_after_release.assert_not_awaited()
