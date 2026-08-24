"""ORCH-06/ORCH-08: release, call-start, and runtime cancellation audits."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    semantic_action_hash,
)
from marketing_agents.domain.approval import (
    ApprovalDecision,
    ApprovalPolicySnapshot,
    ApprovalUse,
    ProposedExternalAction,
    StoredActionApprovalRequest,
    approval_redaction_schema,
    request_approval,
    safe_approval_destination,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import (
    ActionReservationSnapshot,
    DeliveryContractSnapshot,
    DispatchLease,
    ExternalAction,
)
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    Effect,
    ExternalActionState,
    StepState,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    transition_step,
)

from tests.unit.domain.test_orch_09_audit_contracts import _step

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _factory() -> AuditEventFactory:
    return AuditEventFactory(
        AuditContext.system(
            "service.approval-boundary",
            correlation_id="correlation.orch-08.audit",
        )
    )


def _approved_member() -> tuple[ExternalAction, StoredActionApprovalRequest]:
    semantic = SemanticExternalAction(
        template_id="template.orch-08.publish",
        instance_id="instance.orch-08.publish",
        action_type="newsletter.publish",
        capability_id="cap.newsletter.publish",
        connector_family="newsletter",
        binding_id="binding.newsletter.primary",
        destination="recipient-sha256-v1:" + ("a" * 64),
        payload_schema_id="schema.newsletter.publish.v1",
        minimized_payload={"recipient": "person@example.invalid", "subject": "Update"},
    )
    envelope = CanonicalExternalAction(
        action_id="action.orch-08.publish",
        authorization_set_id="authorization-set.orch-08.1",
        run_id="run.orch-08.audit",
        plan_hash="b" * 64,
        proposal_revision=1,
        step_id="step.orch-08.publish",
        step_key="publish",
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
    proposal = ProposedExternalAction.create(
        envelope,
        redacted_destination=safe_approval_destination(envelope.binding_id),
        payload_schema=approval_redaction_schema(("/recipient",)),
    )
    policy = ApprovalPolicySnapshot(
        policy_id="policy.orch-08.human-write",
        required_roles=frozenset({"role.approver"}),
        required_scopes=frozenset({"scope.external-write"}),
        expires_after_seconds=900,
        allow_self_approval=False,
    )
    action = ExternalAction.proposed(
        proposal,
        policy,
        DeliveryContractSnapshot(
            capability_id=envelope.capability_id,
            connector_family=envelope.connector_family,
            binding_id=envelope.binding_id,
            binding_configuration_revision=1,
            request_schema_id=envelope.payload_schema_id,
            idempotency_support="required",
            timeout_seconds=30,
        ),
        NOW,
    )
    awaiting = replace(
        action,
        state=ExternalActionState.AWAITING_APPROVAL,
        version=2,
    )
    request = request_approval(
        request_id="approval-request.orch-08.publish",
        proposed_action=proposal,
        policy=policy,
        requested_by="principal.orch-08.requester",
        requested_at=NOW,
    )
    decision = ApprovalDecision(
        id="approval-decision.orch-08.publish",
        request_id=request.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        step_id=request.step_id,
        step_key=request.step_key,
        actor_id="principal.orch-08.approver",
        authentication_method="local_fixed",
        correlation_id="correlation.orch-08.decision",
        decision=ApprovalDecisionKind.APPROVE,
        authority_roles=frozenset({"approver", "role.approver"}),
        authority_scopes=frozenset({"approvals:decide", "scope.external-write"}),
        reason_code="approval_granted",
        decided_at=NOW + timedelta(seconds=1),
    )
    approved = replace(
        awaiting,
        state=ExternalActionState.APPROVED,
        version=3,
        updated_at=decision.decided_at,
    )
    stored = StoredActionApprovalRequest(
        request=request,
        status=ApprovalStatus.APPROVED,
        version=2,
        updated_at=decision.decided_at,
        decision=decision,
    )
    return approved, stored


def _released_member() -> tuple[
    ExternalAction,
    ExternalAction,
    StoredActionApprovalRequest,
    StoredActionApprovalRequest,
]:
    approved, previous_request = _approved_member()
    released_at = NOW + timedelta(seconds=2)
    decision = previous_request.decision
    assert decision is not None
    use = ApprovalUse(
        id="approval-use.orch-08.publish",
        request_id=previous_request.request.id,
        decision_id=decision.id,
        action_id=approved.id,
        action_hash=approved.action_hash,
        authorization_set_id=approved.envelope.authorization_set_id,
        run_id=approved.run_id,
        plan_hash=approved.envelope.plan_hash,
        proposal_revision=approved.envelope.proposal_revision,
        step_id=approved.step_id,
        step_key=approved.envelope.step_key,
        reservation_id="reservation.orch-08.publish",
        used_at=released_at,
    )
    reservation = ActionReservationSnapshot(
        reservation_id=use.reservation_id,
        authorization_set_id=approved.envelope.authorization_set_id,
        approval_request_id=previous_request.request.id,
        approval_decision_id=decision.id,
        action_hash=approved.action_hash,
        capability_id=approved.envelope.capability_id,
        binding_id=approved.envelope.binding_id,
        idempotency_key=approved.idempotency_key,
        reserved_at=released_at,
    )
    reserved = replace(
        approved,
        state=ExternalActionState.DISPATCH_RESERVED,
        version=approved.version + 1,
        updated_at=released_at,
        reservation=reservation,
    )
    consumed = StoredActionApprovalRequest(
        request=previous_request.request,
        status=ApprovalStatus.CONSUMED,
        version=3,
        updated_at=released_at,
        decision=decision,
        use=use,
    )
    return approved, reserved, previous_request, consumed


def test_orch_08_release_member_audits_bind_use_reservation_and_decision() -> None:
    approved, reserved, previous_request, consumed = _released_member()
    factory = _factory()

    action_event = factory.action_dispatch_reserved(approved, reserved, consumed)
    approval_event = factory.approval_consumed(previous_request, consumed, reserved)

    assert action_event.event_type == "action.dispatch_reserved"
    assert action_event.approval_request_id == consumed.request.id
    assert action_event.approval_decision_id == consumed.decision.id  # type: ignore[union-attr]
    assert action_event.safe_metadata.values == {
        "approval_use_id": consumed.use.id,  # type: ignore[union-attr]
        "approval_set_id": reserved.envelope.authorization_set_id,
        "idempotency_support": "required",
        "reservation_id": reserved.reservation.reservation_id,  # type: ignore[union-attr]
    }
    assert approval_event.event_type == "approval.consumed"
    assert approval_event.previous_state == "approved"
    assert approval_event.new_state == "consumed"
    assert approval_event.mutation_version == 3

    reservation = reserved.reservation
    assert reservation is not None
    tampered = replace(
        reserved,
        reservation=replace(
            reservation,
            reservation_id="reservation.orch-08.tampered",
        ),
    )
    with pytest.raises(ValueError, match="exact consumed approval"):
        factory.action_dispatch_reserved(approved, tampered, consumed)


@pytest.mark.parametrize(
    ("approved_source", "supersession_reason", "action_reason"),
    (
        (False, "approval_set_rejected", "sibling_approval_rejected"),
        (True, "run_cancelled", "operator_cancelled"),
    ),
)
def test_orch_08_closed_sibling_audits_bind_optional_decision_exactly(
    approved_source: bool,
    supersession_reason: str,
    action_reason: str,
) -> None:
    approved, approved_request = _approved_member()
    if approved_source:
        previous_action = approved
        previous_request = approved_request
    else:
        previous_action = replace(
            approved,
            state=ExternalActionState.AWAITING_APPROVAL,
            version=2,
            updated_at=NOW,
        )
        previous_request = StoredActionApprovalRequest.created(approved_request.request)
    closed_at = NOW + timedelta(seconds=2)
    cancelled = replace(
        previous_action,
        state=ExternalActionState.CANCELLED,
        version=previous_action.version + 1,
        updated_at=closed_at,
        terminal_reason_code=action_reason,
    )
    superseded = StoredActionApprovalRequest(
        request=previous_request.request,
        status=ApprovalStatus.SUPERSEDED,
        version=previous_request.version + 1,
        updated_at=closed_at,
        decision=previous_request.decision,
        superseded_at=closed_at,
        superseded_reason_code=supersession_reason,
    )

    action_event = _factory().action_cancelled(previous_action, cancelled, superseded)
    approval_event = _factory().approval_superseded(
        previous_request,
        superseded,
        cancelled,
    )

    expected_decision_id = (
        None if previous_request.decision is None else previous_request.decision.id
    )
    assert action_event.event_type == "action.cancelled"
    assert action_event.approval_decision_id == expected_decision_id
    assert action_event.safe_metadata.values["closure_reason"] == action_reason
    assert action_event.safe_metadata.values["approval_status"] == "superseded"
    assert approval_event.event_type == "approval.superseded"
    assert approval_event.approval_decision_id == expected_decision_id
    assert approval_event.safe_metadata.values["supersession_reason"] == supersession_reason


def test_orch_08_cancel_after_expiry_audits_only_the_action_mutation() -> None:
    approved, approved_request = _approved_member()
    expired_at = approved_request.request.expires_at
    awaiting = replace(
        approved,
        state=ExternalActionState.AWAITING_APPROVAL,
        version=approved.version + 1,
        updated_at=expired_at,
    )
    expired = StoredActionApprovalRequest(
        request=approved_request.request,
        status=ApprovalStatus.EXPIRED,
        version=approved_request.version + 1,
        updated_at=expired_at,
        decision=approved_request.decision,
        expired_at=expired_at,
    )
    cancelled_at = expired_at + timedelta(seconds=1)
    cancelled = replace(
        awaiting,
        state=ExternalActionState.CANCELLED,
        version=awaiting.version + 1,
        updated_at=cancelled_at,
        terminal_reason_code="operator_cancelled",
    )

    event = _factory().action_cancelled(awaiting, cancelled, expired)

    assert event.event_type == "action.cancelled"
    assert event.approval_request_id == expired.request.id
    assert event.approval_decision_id == expired.decision.id  # type: ignore[union-attr]
    assert event.safe_metadata.values["approval_status"] == "expired"
    assert event.reason_code == "operator_cancelled"


def test_orch_08_dispatch_claim_and_call_start_audits_reject_delivery_drift() -> None:
    _, reserved, _, _ = _released_member()
    claimed_at = NOW + timedelta(seconds=3)
    lease = DispatchLease(
        owner="worker.orch-08.dispatch",
        attempt_number=1,
        claimed_at=claimed_at,
        expires_at=claimed_at + timedelta(seconds=30),
    )
    claimed = replace(
        reserved,
        state=ExternalActionState.DISPATCHING,
        version=reserved.version + 1,
        updated_at=claimed_at,
        delivery_attempt_count=1,
        lease=lease,
    )
    started_at = claimed_at + timedelta(seconds=1)
    marked = replace(
        claimed,
        version=claimed.version + 1,
        updated_at=started_at,
        call_started_at=started_at,
        call_deadline_at=started_at + timedelta(seconds=10),
    )
    factory = _factory()

    claim_event = factory.action_dispatch_claimed(reserved, claimed)
    start_event = factory.action_call_started(claimed, marked)

    assert claim_event.event_type == "action.dispatch_claimed"
    assert claim_event.action_attempt_number == 1
    assert start_event.event_type == "action.call_started"
    assert start_event.previous_state == start_event.new_state == "dispatching"
    assert start_event.occurred_at == started_at

    drifted = replace(marked, delivery_attempt_limit=3)
    with pytest.raises(ValueError, match="exact open dispatch"):
        factory.action_call_started(claimed, drifted)


@pytest.mark.parametrize("claimed_before_call", (False, True))
def test_orch_06_runtime_cancel_audit_binds_released_pre_call_state(
    claimed_before_call: bool,
) -> None:
    _, reserved, _, _ = _released_member()
    previous = reserved
    if claimed_before_call:
        claimed_at = NOW + timedelta(seconds=3)
        previous = replace(
            reserved,
            state=ExternalActionState.DISPATCHING,
            version=reserved.version + 1,
            updated_at=claimed_at,
            delivery_attempt_count=1,
            lease=DispatchLease(
                owner="worker.orch-06.cancel",
                attempt_number=1,
                claimed_at=claimed_at,
                expires_at=claimed_at + timedelta(seconds=30),
            ),
        )
    cancelled_at = NOW + timedelta(seconds=4)
    cancelled = replace(
        previous,
        state=ExternalActionState.CANCELLED,
        version=previous.version + 1,
        updated_at=cancelled_at,
        lease=None,
        terminal_reason_code="operator_cancelled",
    )

    event = _factory().action_runtime_cancelled(previous, cancelled)

    reservation = reserved.reservation
    assert reservation is not None
    assert event.previous_state == previous.state.value
    assert event.new_state == ExternalActionState.CANCELLED.value
    assert event.action_attempt_number == (1 if claimed_before_call else None)
    assert event.approval_request_id == reservation.approval_request_id
    assert event.approval_decision_id == reservation.approval_decision_id
    assert event.safe_metadata.values["approval_status"] == "released"
    assert event.safe_metadata.values["closure_reason"] == "operator_cancelled"

    with pytest.raises(ValueError, match="pre-call released work"):
        _factory().action_runtime_cancelled(
            previous,
            replace(cancelled, reservation=None),
        )


def test_orch_08_write_release_and_first_call_step_commands_are_auditable() -> None:
    awaiting = _step(Effect.WRITE, StepState.AWAITING_APPROVAL, version=2)
    released = transition_step(
        awaiting,
        StepLifecycleCommand.RELEASE_APPROVAL,
        NoStepTransitionContext(),
        awaiting.updated_at + timedelta(seconds=1),
    )
    started = transition_step(
        released.step,
        StepLifecycleCommand.START_RESERVED_WRITE,
        NoStepTransitionContext(),
        released.step.updated_at + timedelta(seconds=1),
    )

    release_event = _factory().step_transition(released.step, released.transition)
    start_event = _factory().step_transition(started.step, started.transition)

    assert release_event.safe_metadata.values["command"] == "release_approval"
    assert release_event.reason_code == "approval_barrier_released"
    assert start_event.safe_metadata.values["command"] == "start_reserved_write"
    assert start_event.reason_code == "reserved_write_started"
