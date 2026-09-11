"""DEL-07: decision repository faults cannot commit authority or audit success."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.repositories import (
    ApprovalDecisionInsertResult,
    ApprovalRepositoryConflict,
    CurrentAuthorizationSet,
    ExternalActionRepositoryConflict,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services.approval_boundaries import ApprovalBoundaryService
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionService,
    ApprovalDecisionServiceError,
)
from marketing_agents.domain.approval import (
    ApprovalDecision,
    AuthorizationSet,
    AuthorizationSetHead,
    AuthorizationSetMember,
    AuthorizationSetStatus,
    ProposedExternalAction,
    StoredActionApprovalRequest,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import DeliveryContractSnapshot, ExternalAction
from marketing_agents.domain.enums import ApprovalStatus, ExternalActionState, RunState

from tests.unit.application.test_run_10_authorized_approval_actor import (
    _command,
    _full_principal,
)
from tests.unit.domain.test_run_08_approval_records import NOW, _action, _request


def _decision_fixture() -> SimpleNamespace:
    """Valid domain facts; asynchronous ports are controllable fault boundaries."""
    request = _request()
    stored = StoredActionApprovalRequest.created(request)
    proposal = ProposedExternalAction(_action(), request.action_hash, request.redacted_projection)
    action = replace(
        ExternalAction.proposed(
            proposal,
            request.policy,
            DeliveryContractSnapshot(
                request.capability_id,
                request.connector_family,
                request.binding_id,
                1,
                proposal.envelope.payload_schema_id,
                "required",
                30,
            ),
            NOW,
        ),
        state=ExternalActionState.AWAITING_APPROVAL,
        version=2,
    )
    member = AuthorizationSetMember(
        request.authorization_set_id,
        1,
        request.run_id,
        request.plan_hash,
        request.proposal_revision,
        request.action_id,
        request.action_hash,
        request.step_id,
        request.step_key,
    )
    authorization_set = AuthorizationSet.open(
        authorization_set_id=request.authorization_set_id,
        members=(member,),
        opened_at=NOW,
    )
    head = AuthorizationSetHead(
        request.run_id,
        authorization_set.id,
        request.plan_hash,
        request.proposal_revision,
        authorization_set.membership_hash,
        1,
        NOW,
    )
    selection = CurrentAuthorizationSet(head, authorization_set)
    uow = Mock(spec=UnitOfWork)
    uow.approvals = SimpleNamespace(
        get=AsyncMock(return_value=stored),
        list_current_set=AsyncMock(return_value=(stored,)),
        get_current_authorization_set=AsyncMock(return_value=selection),
        record_decision=AsyncMock(),
    )
    uow.external_actions = SimpleNamespace(get=AsyncMock(return_value=action))
    uow.audits = SimpleNamespace(append_many=AsyncMock())
    uow.runs = SimpleNamespace(get=AsyncMock(return_value=None))
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.commit = AsyncMock()
    factory = Mock(return_value=uow)
    clock = Mock()
    clock.now.return_value = NOW + timedelta(seconds=1)
    ids = Mock()
    ids.new.side_effect = lambda namespace: f"{namespace}.del07"
    dependencies = OrchestrationDependencies(clock, ids, factory)

    async def record_decision(
        *, decision: ApprovalDecision, **_: Any
    ) -> ApprovalDecisionInsertResult:
        decided = replace(
            stored,
            status=ApprovalStatus.APPROVED,
            version=2,
            updated_at=decision.decided_at,
            decision=decision,
        )
        uow.external_actions.get.return_value = replace(
            action,
            state=ExternalActionState.APPROVED,
            version=3,
            updated_at=decision.decided_at,
        )
        return ApprovalDecisionInsertResult(decided, True)

    uow.approvals.record_decision.side_effect = record_decision
    return SimpleNamespace(
        request=request,
        stored=stored,
        action=action,
        selection=selection,
        uow=uow,
        clock=clock,
        ids=ids,
        factory=factory,
        service=ApprovalDecisionService(dependencies),
    )


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    (
        ("request_corrupt", "approval_record_corrupt"),
        ("request_missing", "approval_request_missing"),
        ("generation", "approval_generation_conflict"),
        ("pending_version", "approval_decision_conflict"),
        ("expired", "approval_expired"),
        ("client_hash", "approval_hash_mismatch"),
        ("set_corrupt", "approval_record_corrupt"),
        ("head_corrupt", "approval_record_corrupt"),
        ("head_missing", "approval_generation_conflict"),
        ("head_closed", "approval_generation_conflict"),
        ("head_identity", "approval_generation_conflict"),
        ("head_plan", "approval_generation_conflict"),
        ("head_revision", "approval_generation_conflict"),
        ("leaf_missing", "approval_generation_conflict"),
        ("leaf_duplicate", "approval_generation_conflict"),
        ("leaf_changed", "approval_generation_conflict"),
        ("action_corrupt", "approval_action_corrupt"),
        ("action_missing", "approval_action_missing"),
        ("action_state", "approval_action_conflict"),
        ("request_binding", "approval_action_conflict"),
        ("decision_cas", "approval_decision_conflict"),
        ("decision_replay", "approval_decision_conflict"),
        ("decided_action_corrupt", "approval_action_corrupt"),
        ("decided_action_missing", "approval_action_missing"),
    ),
)
@pytest.mark.asyncio
async def test_del_07_repository_faults_never_commit_or_emit_success_audits(
    fault: str, expected_code: str
) -> None:
    fixture = _decision_fixture()
    uow = fixture.uow
    approvals = uow.approvals
    command = _command()
    conflict = ApprovalRepositoryConflict("private-storage-detail", "private storage diagnostic")
    action_conflict = ExternalActionRepositoryConflict(
        "private-action-detail", "private action diagnostic"
    )
    if fault == "request_corrupt":
        approvals.get.side_effect = conflict
    elif fault == "request_missing":
        approvals.get.return_value = None
    elif fault == "generation":
        command = replace(command, expected_generation=command.expected_generation + 1)
    elif fault == "pending_version":
        object.__setattr__(fixture.stored, "version", 2)
    elif fault == "expired":
        fixture.clock.now.return_value = fixture.request.expires_at
    elif fault == "client_hash":
        command = replace(command, expected_action_hash="0" * 64)
    elif fault == "set_corrupt":
        approvals.list_current_set.side_effect = conflict
    elif fault == "head_corrupt":
        approvals.get_current_authorization_set.side_effect = conflict
    elif fault == "head_missing":
        approvals.get_current_authorization_set.return_value = None
    elif fault.startswith("head_"):
        # Simulate a repository violating its hydration contract; each stale
        # identity must still be denied by the decision service's own guard.
        field, value = {
            "head_closed": ("status", AuthorizationSetStatus.REJECTED),
            "head_identity": ("id", "authorization-set.other"),
            "head_plan": ("plan_hash", "a" * 64),
            "head_revision": ("proposal_revision", 2),
        }[fault]
        object.__setattr__(fixture.selection.authorization_set, field, value)
    elif fault == "leaf_missing":
        approvals.list_current_set.return_value = ()
    elif fault == "leaf_duplicate":
        approvals.list_current_set.return_value = (fixture.stored, fixture.stored)
    elif fault == "leaf_changed":
        approvals.list_current_set.return_value = (
            StoredActionApprovalRequest.created(replace(fixture.request, requested_by="other")),
        )
    elif fault == "action_corrupt":
        uow.external_actions.get.side_effect = action_conflict
    elif fault == "action_missing":
        uow.external_actions.get.return_value = None
    elif fault == "action_state":
        uow.external_actions.get.return_value = replace(
            fixture.action, state=ExternalActionState.APPROVED
        )
    elif fault == "request_binding":
        object.__setattr__(fixture.request, "binding_id", "binding.changed")
    elif fault == "decision_cas":
        approvals.record_decision.side_effect = conflict
    elif fault == "decision_replay":
        approvals.record_decision.side_effect = None
        approvals.record_decision.return_value = ApprovalDecisionInsertResult(fixture.stored, False)
    elif fault == "decided_action_corrupt":
        uow.external_actions.get.side_effect = (fixture.action, action_conflict)
    else:
        uow.external_actions.get.side_effect = (fixture.action, None)

    with pytest.raises(ApprovalDecisionServiceError) as failure:
        await fixture.service.decide_in_uow(uow, command, principal=_full_principal())
    assert failure.value.code == expected_code
    assert "private" not in str(failure.value)
    uow.commit.assert_not_awaited()
    uow.audits.append_many.assert_not_awaited()
    if not fault.startswith("decision_") and not fault.startswith("decided_action_"):
        approvals.record_decision.assert_not_awaited()
        fixture.ids.new.assert_not_called()


@pytest.mark.parametrize("parent_state", (None, RunState.EXECUTING))
@pytest.mark.asyncio
async def test_del_07_decision_wrapper_does_not_commit_without_actionable_parent(
    parent_state: RunState | None,
) -> None:
    fixture = _decision_fixture()
    if parent_state is not None:
        fixture.uow.runs.get.return_value = SimpleNamespace(state=parent_state)
    with pytest.raises(ApprovalDecisionServiceError) as failure:
        await fixture.service.decide(_command(), principal=_full_principal())
    assert failure.value.code == (
        "approval_run_missing" if parent_state is None else "approval_run_not_actionable"
    )
    fixture.uow.commit.assert_not_awaited()
    assert fixture.uow.__aexit__.await_args.args[0] is ApprovalDecisionServiceError


@pytest.mark.asyncio
async def test_del_07_decision_wrong_command_type_is_denied_before_opening_a_transaction() -> None:
    fixture = _decision_fixture()
    with pytest.raises(ApprovalDecisionServiceError) as failure:
        await fixture.service.decide(
            cast(ApprovalDecisionCommand, object()), principal=_full_principal()
        )
    assert failure.value.code == "approval_command_invalid"
    fixture.factory.assert_not_called()
    fixture.ids.new.assert_not_called()
    fixture.clock.now.assert_not_called()


@pytest.mark.parametrize("composed", (False, True))
@pytest.mark.asyncio
async def test_del_07_decision_commits_only_after_optional_composed_boundary(
    composed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _decision_fixture()
    fixture.uow.runs.get.return_value = SimpleNamespace(
        state=RunState.AWAITING_APPROVAL if composed else RunState.RECEIVED,
        approval_required=True if composed else None,
    )
    evaluate = AsyncMock()
    monkeypatch.setattr(ApprovalBoundaryService, "evaluate_in_uow", evaluate)
    result = await fixture.service.decide(_command(), principal=_full_principal())
    assert result.request.status is ApprovalStatus.APPROVED
    fixture.uow.commit.assert_awaited_once()
    if composed:
        evaluate.assert_awaited_once()
        assert evaluate.await_args.args == (fixture.uow, fixture.action.run_id)
        assert evaluate.await_args.kwargs["audit_context"] == AuditContext.authenticated_user(
            result.decision.actor_id,
            authentication_method=result.decision.authentication_method,
            correlation_id=result.decision.correlation_id,
        )
    else:
        evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_composed_boundary_failure_prevents_decision_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _decision_fixture()
    fixture.uow.runs.get.return_value = SimpleNamespace(
        state=RunState.AWAITING_APPROVAL, approval_required=True
    )
    monkeypatch.setattr(
        ApprovalBoundaryService,
        "evaluate_in_uow",
        AsyncMock(side_effect=RuntimeError("injected boundary persistence failure")),
    )
    with pytest.raises(RuntimeError, match="injected boundary persistence failure"):
        await fixture.service.decide(_command(), principal=_full_principal())
    fixture.uow.commit.assert_not_awaited()
    assert fixture.uow.__aexit__.await_args.args[0] is RuntimeError
