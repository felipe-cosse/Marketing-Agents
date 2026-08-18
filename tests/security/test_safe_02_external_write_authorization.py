"""SAFE-02: every external mutation requires one exact sealed authorization."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from marketing_agents.application.policies.write_authorization import (
    ApprovalReservation,
    AuthorizedExternalWrite,
    WriteAuthorizationError,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    canonical_action_hash,
    semantic_action_hash,
)
from marketing_agents.domain.canonical_json import CanonicalJsonError, canonical_json_bytes
from marketing_agents.infrastructure.catalog import compile_catalog

IDEMPOTENCY_KEY = "idem-safe-02-0000000001"
ROOT = Path(__file__).resolve().parents[2]


def _action(action_type: str = "newsletter.subscribe") -> CanonicalExternalAction:
    semantic = SemanticExternalAction(
        template_id="tpl.email.newsletter.newsletter-subscriber",
        instance_id="inst.email.newsletter.newsletter-subscriber.01",
        action_type=action_type,
        capability_id="newsletter.subscribe",
        connector_family="newsletter-email",
        binding_id="mock.newsletter.default",
        destination="list:newsletter",
        payload_schema_id="schema:newsletter-subscribe:v1",
        minimized_payload={"contact_ref": "contact:1"},
    )
    return CanonicalExternalAction(
        action_id="action:1",
        authorization_set_id="authorization-set:1",
        run_id="run:1",
        plan_hash="a" * 64,
        proposal_revision=1,
        step_id="step:write",
        step_key="write",
        template_id=semantic.template_id,
        instance_id=semantic.instance_id,
        action_type=action_type,
        capability_id=semantic.capability_id,
        connector_family=semantic.connector_family,
        binding_id=semantic.binding_id,
        destination=semantic.destination,
        payload_schema_id=semantic.payload_schema_id,
        minimized_payload=semantic.minimized_payload,
        semantic_action_hash=semantic_action_hash(semantic),
    )


def _reservation(action: CanonicalExternalAction) -> ApprovalReservation:
    return ApprovalReservation(
        reservation_id="reservation:1",
        authorization_set_id=action.authorization_set_id,
        state="dispatch_reserved",
        action_id=action.action_id,
        action_hash=canonical_action_hash(action),
        capability_id=action.capability_id,
        binding_id=action.binding_id,
        approval_request_id="approval-request:1",
        approval_decision_id="approval-decision:1",
        idempotency_key=IDEMPOTENCY_KEY,
        reserved_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    "action_type",
    [
        "social.publish",
        "email.send",
        "calendar.enroll",
        "newsletter.unsubscribe",
        "crm.upsert",
        "cms.update",
        "community.send",
        "fulfillment.create",
    ],
)
def test_safe_02_mutation_families_have_zero_calls_without_exact_proof(
    action_type: str,
) -> None:
    calls: list[AuthorizedExternalWrite] = []

    class RecordingConnector:
        async def execute(self, write: AuthorizedExternalWrite) -> ConnectorWriteResult:
            if not isinstance(write, AuthorizedExternalWrite):
                raise TypeError("sealed authorization required")
            calls.append(write)
            return ConnectorWriteResult("receipt:1", "succeeded", {})

    action = _action(action_type)
    bad_reservation = _reservation(action).model_copy(update={"action_hash": "0" * 64})
    with pytest.raises(WriteAuthorizationError, match="does not match"):
        WriteAuthorizationGuard().authorize(action, bad_reservation, IDEMPOTENCY_KEY)
    assert calls == []
    with pytest.raises(TypeError, match="sealed authorization"):
        asyncio.run(RecordingConnector().execute(action))  # type: ignore[arg-type]
    assert calls == []


def test_safe_02_exact_reserved_action_produces_one_connector_call() -> None:
    calls: list[AuthorizedExternalWrite] = []

    class RecordingConnector:
        async def execute(self, write: AuthorizedExternalWrite) -> ConnectorWriteResult:
            calls.append(write)
            return ConnectorWriteResult("receipt:1", "succeeded", {"mock": True})

    action = _action()
    write = WriteAuthorizationGuard().authorize(action, _reservation(action), IDEMPOTENCY_KEY)
    result = asyncio.run(RecordingConnector().execute(write))
    assert result.receipt_id == "receipt:1"
    assert calls == [write]
    assert write.action_hash == canonical_action_hash(action)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("run_id", "run:changed"),
        ("plan_hash", "b" * 64),
        ("proposal_revision", 2),
        ("step_id", "step:changed"),
        ("step_key", "changed"),
        ("template_id", "tpl.changed"),
        ("instance_id", "inst.changed"),
        ("action_type", "newsletter.unsubscribe"),
        ("capability_id", "newsletter.unsubscribe"),
        ("connector_family", "crm"),
        ("binding_id", "mock.changed"),
        ("destination", "list:changed"),
        ("payload_schema_id", "schema:changed"),
        ("minimized_payload", {"contact_ref": "contact:changed"}),
        ("semantic_action_hash", "0" * 64),
    ],
)
def test_safe_02_tampering_any_canonical_action_field_invalidates_approval(
    field: str, changed: object
) -> None:
    action = _action()
    tampered = action.model_copy(update={field: changed})
    with pytest.raises(WriteAuthorizationError):
        WriteAuthorizationGuard().authorize(tampered, _reservation(action), IDEMPOTENCY_KEY)


@pytest.mark.parametrize(
    ("field", "changed", "code"),
    [
        ("action_id", "action:changed", "approval_action_mismatch"),
        ("authorization_set_id", "set:changed", "authorization_set_mismatch"),
        ("capability_id", "newsletter.unsubscribe", "approval_capability_mismatch"),
        ("binding_id", "mock.changed", "approval_binding_mismatch"),
        ("idempotency_key", "idem-changed-0000000000", "idempotency_key_mismatch"),
    ],
)
def test_safe_02_reservation_scope_mismatch_fails_closed(
    field: str, changed: str, code: str
) -> None:
    action = _action()
    reservation = _reservation(action).model_copy(update={field: changed})
    with pytest.raises(WriteAuthorizationError) as captured:
        WriteAuthorizationGuard().authorize(action, reservation, IDEMPOTENCY_KEY)
    assert captured.value.code == code


def test_safe_02_authorized_write_cannot_be_forged_and_catalog_writes_are_guarded() -> None:
    action = _action()
    with pytest.raises(WriteAuthorizationError, match="write guard"):
        AuthorizedExternalWrite(
            action=action,
            action_hash=canonical_action_hash(action),
            reservation_id="reservation:fake",
            approval_request_id="request:fake",
            approval_decision_id="decision:fake",
            idempotency_key=IDEMPOTENCY_KEY,
            _seal=object(),
        )

    catalog = compile_catalog(ROOT / "catalog" / "v1")
    capability_by_id = {item.id: item for item in catalog.tool_capabilities}
    policy_by_id = {item.id: item for item in catalog.approval_policies}
    for template in catalog.templates:
        writes = [
            capability_by_id[item]
            for item in template.allowed_tool_capability_ids
            if capability_by_id[item].effect == "write"
        ]
        if writes:
            assert policy_by_id[template.approval_policy_id].kind == "human_external_write"
            assert template.retry_policy.max_attempts == 1

    assert canonical_json_bytes({"b": 1, "a": "e\u0301"}) == canonical_json_bytes(
        {"a": "é", "b": 1}
    )
    with pytest.raises(CanonicalJsonError):
        canonical_json_bytes({"bad": float("nan")})
