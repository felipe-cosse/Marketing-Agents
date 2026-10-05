"""AC-08: every registered write handler requires exact sealed authorization.

Synthetic trusted reservations isolate the inner connector proof contract here;
the real-composition acceptance journey independently proves persisted human
approval and durable dispatcher release."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from marketing_agents.application.policies.write_authorization import (
    ApprovalReservation,
    AuthorizedExternalWrite,
    WriteAuthorizationError,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorPortError,
)
from marketing_agents.config import Settings
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    canonical_action_hash,
    semantic_action_hash,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import Effect
from marketing_agents.infrastructure.adapters.connectors.mock.families import build_connector_bundle
from marketing_agents.infrastructure.adapters.connectors.registry import DISABLED_V1_CAPABILITIES
from marketing_agents.infrastructure.catalog import compile_catalog

CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog" / "v1"
WRITE_PAYLOADS = {
    "cap.newsletter.subscribe": {"contact_ref": "contact:ac08", "list_ref": "list:ac08"},
    "cap.newsletter.unsubscribe": {"contact_ref": "contact:ac08", "list_ref": "list:ac08"},
    "cap.email.send-message": {
        "contact_ref": "contact:ac08",
        "subject": "Mock",
        "body": "Not sent",
    },
    "cap.crm.upsert-contact": {"contact_ref": "contact:ac08", "fields": {"demo": True}},
    "cap.events.enroll-attendee": {"attendee_ref": "attendee:ac08", "session_ref": "session:ac08"},
    "cap.messaging.send-message": {"recipient_refs": ["member:ac08"], "body": "Mock message"},
    "cap.messaging.share-material": {
        "recipient_refs": ["member:ac08"],
        "artifact_id": "artifact:ac08",
    },
    "cap.spreadsheet.update-rows": {
        "document_ref": "document:ac08",
        "range_a1": "A1:A1",
        "rows": [{"label": "Mock"}],
    },
}
DISABLED_WRITES = {"cap.email.send-message", "cap.spreadsheet.update-rows"}


def sealed_contract_proof(operation, command):
    """Exercise the genuine guard; a synthetic reservation is valid only in this unit scope."""
    metadata = operation.metadata
    semantic = SemanticExternalAction(
        template_id="tpl.ac08.contract-only",
        instance_id="inst.ac08.contract-only.01",
        action_type=metadata.capability_id.removeprefix("cap."),
        capability_id=metadata.capability_id,
        connector_family=metadata.connector_family,
        binding_id=f"mock.{metadata.connector_family}.default",
        destination="mock-destination:ac08-contract-only",
        payload_schema_id=metadata.request_schema_id,
        minimized_payload=command.model_dump(mode="json"),
    )
    action = CanonicalExternalAction(
        action_id=f"action:ac08:{metadata.capability_id}",
        authorization_set_id="authorization-set:ac08-contract-only",
        run_id="run:ac08-contract-only",
        plan_hash="a" * 64,
        proposal_revision=1,
        step_id="step:ac08-contract-only",
        step_key="contract-only",
        template_id=semantic.template_id,
        instance_id=semantic.instance_id,
        action_type=semantic.action_type,
        capability_id=semantic.capability_id,
        connector_family=semantic.connector_family,
        binding_id=semantic.binding_id,
        destination=semantic.destination,
        payload_schema_id=semantic.payload_schema_id,
        # Revalidate JSON values, not the semantic model's deeply frozen internals.
        minimized_payload=command.model_dump(mode="json"),
        semantic_action_hash=semantic_action_hash(semantic),
    )
    key = f"idem-ac08-contract-only-{metadata.capability_id}"
    reservation = ApprovalReservation(
        reservation_id=f"reservation:ac08:{metadata.capability_id}",
        authorization_set_id=action.authorization_set_id,
        state="dispatch_reserved",
        action_id=action.action_id,
        action_hash=canonical_action_hash(action),
        capability_id=action.capability_id,
        binding_id=action.binding_id,
        approval_request_id=f"request:ac08:{metadata.capability_id}",
        approval_decision_id=f"decision:ac08:{metadata.capability_id}",
        idempotency_key=key,
        reserved_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
    )
    return WriteAuthorizationGuard().authorize(action, reservation, key)


@pytest.mark.parametrize("capability_id", tuple(WRITE_PAYLOADS))
@pytest.mark.asyncio
async def test_ac_08_every_registered_write_handler_requires_sealed_proof(
    capability_id: str,
) -> None:
    catalog = compile_catalog(CATALOG_PATH)
    bundle = build_connector_bundle(Settings(_env_file=None), catalog)
    registered_writes = {
        item.metadata.capability_id: item
        for item in bundle.registry.operations
        if item.metadata.effect is Effect.WRITE
    }
    assert (
        set(registered_writes)
        == set(WRITE_PAYLOADS)
        == {item.id for item in catalog.tool_capabilities if item.effect == "write"}
    )
    assert (
        {capability for capability, item in registered_writes.items() if not item.metadata.enabled}
        == DISABLED_WRITES
        == set(DISABLED_V1_CAPABILITIES)
    )
    operation = registered_writes[capability_id]
    metadata = operation.metadata
    command = operation.request_type.model_validate_json(
        canonical_json_bytes(WRITE_PAYLOADS[capability_id]), strict=True
    )
    assert type(command) is operation.request_type
    # Resolve the actual shipped method, including deliberately disabled methods,
    # so no registered declaration is skipped because a binding filters it out.
    connector = getattr(bundle, metadata.connector_family)
    handler = getattr(connector, operation.method_name)
    proof = sealed_contract_proof(operation, command)

    with pytest.raises(WriteAuthorizationError) as forged:
        AuthorizedExternalWrite(
            action=proof.action,
            action_hash=proof.action_hash,
            reservation_id=proof.reservation_id,
            approval_request_id=proof.approval_request_id,
            approval_decision_id=proof.approval_decision_id,
            idempotency_key=proof.idempotency_key,
            _seal=object(),
        )
    assert forged.value.code == "invalid_authorization_seal"
    for unsealed in (None, object(), proof.action):
        with pytest.raises(ConnectorPortError) as denied:
            await handler(
                AuthorizedConnectorCommand(
                    authorization=unsealed,  # type: ignore[arg-type]
                    command=command,
                )
            )
        assert denied.value.code == (
            "authorization_mismatch" if metadata.enabled else "operation_disabled"
        )
        assert bundle.ledger.side_effect_count == 0

    # A genuine seal must not authorize even a schema-valid command with one
    # changed field. Keep the proof fixed and revalidate the altered typed DTO.
    changed_payload = command.model_dump(mode="json")
    changed_field = next(key for key, value in changed_payload.items() if isinstance(value, str))
    changed_payload[changed_field] += "-changed"
    changed_command = operation.request_type.model_validate_json(
        canonical_json_bytes(changed_payload), strict=True
    )
    assert type(changed_command) is operation.request_type and changed_command != command
    with pytest.raises(ConnectorPortError) as mismatch:
        await handler(AuthorizedConnectorCommand(authorization=proof, command=changed_command))
    assert mismatch.value.code == (
        "authorization_mismatch" if metadata.enabled else "operation_disabled"
    )
    assert bundle.ledger.side_effect_count == 0

    binding = bundle.binding_registry.resolve(proof.action.binding_id)
    exact = AuthorizedConnectorCommand(authorization=proof, command=command)
    if not metadata.enabled:
        assert capability_id not in binding.handlers
        with pytest.raises(ConnectorPortError) as disabled:
            await handler(exact)
        assert disabled.value.code == "operation_disabled"
        assert bundle.ledger.side_effect_count == 0
    else:
        assert binding.handlers[capability_id] == handler
        result = await handler(exact)
        assert result.status == "mock_succeeded"
        assert result.safe_metadata["capability_id"] == capability_id
        assert result.safe_metadata["provider_name"] == metadata.connector_family
        assert result.safe_metadata["external_side_effect"] is False
        assert bundle.ledger.side_effect_count == 1
