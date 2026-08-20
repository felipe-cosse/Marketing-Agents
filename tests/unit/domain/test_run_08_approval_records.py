"""RUN-08: canonical durable approval request, decision, renewal, and use contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    semantic_action_hash,
)
from marketing_agents.domain.approval import (
    ApprovalBindingError,
    ApprovalDecision,
    ApprovalPolicySnapshot,
    ApprovalUse,
    ProposedExternalAction,
    StoredActionApprovalRequest,
    approval_redaction_schema,
    assert_decision_binds_request,
    request_approval,
    safe_approval_destination,
)
from marketing_agents.domain.enums import ApprovalDecisionKind, ApprovalStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _action() -> CanonicalExternalAction:
    semantic = SemanticExternalAction(
        template_id="template.email",
        instance_id="instance.email.1",
        action_type="email.send",
        capability_id="cap.email.send",
        connector_family="email",
        binding_id="binding.email.primary",
        destination="recipient-sha256-v1:" + "a" * 64,
        payload_schema_id="schema.email.send.v1",
        minimized_payload={"recipient": "person@example.invalid", "subject": "Hello"},
    )
    return CanonicalExternalAction(
        action_id="action.1",
        authorization_set_id="authorization-set.1",
        run_id="run.1",
        plan_hash="b" * 64,
        proposal_revision=1,
        step_id="step.send",
        step_key="send",
        template_id=semantic.template_id,
        instance_id=semantic.instance_id,
        action_type=semantic.action_type,
        capability_id=semantic.capability_id,
        connector_family=semantic.connector_family,
        binding_id=semantic.binding_id,
        destination=semantic.destination,
        payload_schema_id=semantic.payload_schema_id,
        minimized_payload=semantic.minimized_payload,
        semantic_action_hash=semantic_action_hash(semantic),
    )


def _policy(*, allow_self_approval: bool = False) -> ApprovalPolicySnapshot:
    return ApprovalPolicySnapshot(
        policy_id="policy.external-write",
        required_roles=frozenset({"role.approver"}),
        required_scopes=frozenset({"scope.external-write"}),
        expires_after_seconds=900,
        allow_self_approval=allow_self_approval,
    )


def _request():
    action = _action()
    proposal = ProposedExternalAction.create(
        action,
        redacted_destination=safe_approval_destination(action.binding_id),
        payload_schema=approval_redaction_schema(("/recipient",)),
    )
    return request_approval(
        request_id="approval-request.1",
        proposed_action=proposal,
        policy=_policy(),
        requested_by="principal.requester",
        requested_at=NOW,
    )


def _decision(*, kind: ApprovalDecisionKind = ApprovalDecisionKind.APPROVE) -> ApprovalDecision:
    request = _request()
    return ApprovalDecision(
        id="approval-decision.1",
        request_id=request.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        step_id=request.step_id,
        step_key=request.step_key,
        actor_id="principal.approver",
        authentication_method="local_session",
        correlation_id="correlation.approval.1",
        decision=kind,
        authority_roles=frozenset({"role.approver"}),
        authority_scopes=frozenset({"scope.external-write"}),
        reason_code=(
            "approval_granted" if kind is ApprovalDecisionKind.APPROVE else "approval_rejected"
        ),
        decided_at=NOW + timedelta(minutes=1),
    )


def test_run_08_request_ttl_policy_and_projection_are_exact() -> None:
    request = _request()
    assert request.expires_at == NOW + timedelta(seconds=900)
    assert request.redacted_projection["payload"] == {
        "recipient": "[REDACTED]",
        "subject": "Hello",
    }
    assert "person@example.invalid" not in repr(request)
    with pytest.raises(ValueError, match="policy TTL"):
        replace(request, expires_at=request.expires_at + timedelta(seconds=1))
    with pytest.raises(ValueError, match="exact boolean"):
        replace(_policy(), allow_self_approval="false")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        replace(_policy(), expires_after_seconds=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"[Pp]ointer"):
        approval_redaction_schema(("recipient",))
    forged_projection = dict(request.redacted_projection)
    forged_projection["destination"] = 7
    with pytest.raises(ApprovalBindingError, match="destination summary"):
        replace(
            request,
            redacted_destination=7,  # type: ignore[arg-type]
            redacted_projection=forged_projection,
        )


def test_run_08_decision_binds_exact_request_policy_and_actor_separation() -> None:
    request = _request()
    decision = _decision()
    assert_decision_binds_request(decision, request)
    with pytest.raises(ApprovalBindingError, match="forbids"):
        assert_decision_binds_request(
            replace(decision, actor_id=request.requested_by),
            request,
        )
    with pytest.raises(ApprovalBindingError, match="authority"):
        assert_decision_binds_request(
            replace(decision, authority_scopes=frozenset()),
            request,
        )


def test_run_08_lifecycle_allows_only_exact_expiry_renewal_and_single_use() -> None:
    request = _request()
    decision = _decision()
    approved = StoredActionApprovalRequest(
        request=request,
        status=ApprovalStatus.APPROVED,
        version=2,
        updated_at=decision.decided_at,
        decision=decision,
    )
    use = ApprovalUse(
        id="approval-use.1",
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
        reservation_id="reservation.1",
        used_at=NOW + timedelta(minutes=2),
    )
    consumed = StoredActionApprovalRequest(
        request=request,
        status=ApprovalStatus.CONSUMED,
        version=3,
        updated_at=use.used_at,
        decision=decision,
        use=use,
    )
    assert consumed.use == use

    expired_at = request.expires_at
    renewed_at = expired_at + timedelta(seconds=1)
    renewed = StoredActionApprovalRequest(
        request=request,
        status=ApprovalStatus.EXPIRED,
        version=4,
        updated_at=renewed_at,
        decision=decision,
        expired_at=expired_at,
        replacement_request_id="approval-request.2",
        renewed_at=renewed_at,
    )
    assert renewed.replacement_request_id == "approval-request.2"
    with pytest.raises(ValueError, match="contradictory"):
        replace(approved, status=ApprovalStatus.EXPIRED, expired_at=expired_at)


def test_run_08_full_set_supersession_is_a_new_epoch_not_a_leaf_revision() -> None:
    with pytest.raises(ValueError, match="ORCH-08 authorization-set head"):
        StoredActionApprovalRequest(
            request=_request(),
            status=ApprovalStatus.SUPERSEDED,
            version=2,
            updated_at=NOW + timedelta(minutes=1),
        )
