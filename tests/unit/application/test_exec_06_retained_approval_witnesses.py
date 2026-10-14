"""EXEC-06: recorded retention preserves stable approval history failures."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from marketing_agents.application.services.approval_records import (
    ApprovalRecordServiceError,
    _require_decision_audits,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.approval import StoredActionApprovalRequest
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.enums import ApprovalStatus, ExternalActionState

from tests.unit.application.test_del_07_approval_decision_faults import _decision_fixture
from tests.unit.domain.test_run_08_approval_records import _decision


def _factory(stored: StoredActionApprovalRequest) -> AuditEventFactory:
    decision = stored.decision
    assert decision is not None
    return AuditEventFactory(
        AuditContext.authenticated_user(
            decision.actor_id,
            authentication_method=decision.authentication_method,
            correlation_id=decision.correlation_id,
        )
    )


def _history_fixture() -> SimpleNamespace:
    fixture = _decision_fixture()
    decision = replace(
        _decision(),
        authentication_method="local_fixed",
        authority_roles=fixture.request.policy.required_roles | {"approver"},
        authority_scopes=fixture.request.policy.required_scopes | {"approvals:decide"},
    )
    fixture.decided = replace(
        fixture.stored,
        status=ApprovalStatus.APPROVED,
        version=2,
        updated_at=decision.decided_at,
        decision=decision,
    )
    fixture.decided_action = replace(
        fixture.action,
        state=ExternalActionState.APPROVED,
        version=3,
        updated_at=decision.decided_at,
    )
    factory = _factory(fixture.decided)
    drafts = (
        factory.action_decided(fixture.action, fixture.decided_action, fixture.decided),
        factory.approval_decided(fixture.stored, fixture.decided, fixture.decided_action),
    )
    fixture.events = {
        (draft.aggregate_type, draft.aggregate_id, draft.mutation_version): AuditEvent(
            draft, sequence, sequence, sequence
        )
        for sequence, draft in enumerate(drafts, start=1)
    }
    fixture.uow.audits.get_mutation_event = AsyncMock(
        side_effect=lambda kind, identity, version: fixture.events[(kind, identity, version)]
    )
    return fixture


@pytest.mark.asyncio
async def test_exec_06_recorded_approval_history_accepts_exact_decision_witnesses() -> None:
    fixture = _history_fixture()
    await _require_decision_audits(
        fixture.uow, fixture.decided, fixture.decided_action, action_version=3
    )
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()
    fixture.uow.approvals.record_decision.assert_not_awaited()


@pytest.mark.parametrize("witness", ("action", "approval"))
@pytest.mark.parametrize("fault", ("tampered", "malformed", "nonauthoritative"))
@pytest.mark.asyncio
async def test_exec_06_recorded_approval_history_preserves_safe_conflict_errors(
    witness: str, fault: str
) -> None:
    fixture = _history_fixture()
    key = (
        ("external_action", fixture.action.id, 3)
        if witness == "action"
        else ("approval_request", fixture.request.id, 2)
    )
    event = fixture.events[key]
    if fault == "tampered":
        # Simulate a corrupt repository bypassing the sealed record constructor.
        object.__setattr__(event.draft, "correlation_id", "correlation.changed")
    elif fault == "malformed":
        object.__setattr__(event, "draft", object())
    else:
        alternate = replace(
            fixture.decided,
            decision=replace(fixture.decided.decision, correlation_id="correlation.changed"),
        )
        factory = _factory(alternate)
        draft = (
            factory.action_decided(fixture.action, fixture.decided_action, alternate)
            if witness == "action"
            else factory.approval_decided(fixture.stored, alternate, fixture.decided_action)
        )
        draft.verify_integrity()
        fixture.events[key] = replace(event, draft=draft)

    with pytest.raises(ApprovalRecordServiceError) as failure:
        await _require_decision_audits(
            fixture.uow, fixture.decided, fixture.decided_action, action_version=3
        )
    assert failure.value.code == "approval_audit_conflict"
    assert str(failure.value) == (
        "stored approval decision audits differ from their exact lifecycle"
    )
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()
    fixture.uow.approvals.record_decision.assert_not_awaited()
