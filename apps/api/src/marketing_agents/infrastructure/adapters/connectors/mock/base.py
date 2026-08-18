"""Shared deterministic rendering and exact-write validation for connector mocks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, JsonValue

from marketing_agents.application.policies.write_authorization import AuthorizedExternalWrite
from marketing_agents.application.ports.connector_families import (
    MockConnectorRecord,
    RecordsPayload,
)
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorObservation,
    ConnectorPortError,
    ConnectorReadRequest,
    ConnectorWriteResult,
)
from marketing_agents.domain.action_hash import canonical_action_hash
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.infrastructure.adapters.connectors.registry import (
    ConnectorOperationRegistration,
)

MOCK_CONNECTOR_DOMAIN = b"marketing-agents:mock-connector:v1\x00"


@dataclass(frozen=True, slots=True)
class _StoredMockReceipt:
    action_hash: str
    result: ConnectorWriteResult


class InMemoryMockReceiptLedger:
    """Injected process-local ledger; RUN-05 later supplies durable persistence."""

    __slots__ = ("_receipts", "_side_effect_count")

    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str], _StoredMockReceipt] = {}
        self._side_effect_count = 0

    @property
    def side_effect_count(self) -> int:
        return self._side_effect_count

    def record(
        self,
        *,
        binding_id: str,
        idempotency_key: str,
        action_hash: str,
        capability_id: str,
    ) -> ConnectorWriteResult:
        key = (binding_id, idempotency_key)
        existing = self._receipts.get(key)
        if existing is not None:
            if existing.action_hash != action_hash:
                raise ConnectorPortError(
                    "idempotency_conflict",
                    "mock connector idempotency key is bound to another exact action",
                )
            return existing.result

        receipt_digest = hashlib.sha256(
            MOCK_CONNECTOR_DOMAIN
            + canonical_json_bytes(
                {
                    "binding_id": binding_id,
                    "idempotency_key": idempotency_key,
                    "action_hash": action_hash,
                    "capability_id": capability_id,
                }
            )
        ).hexdigest()
        result = ConnectorWriteResult(
            receipt_id=f"mock-receipt:{receipt_digest[:32]}",
            status="mock_succeeded",
            safe_metadata={
                "mode": "mock",
                "external_side_effect": False,
                "capability_id": capability_id,
            },
        )
        self._receipts[key] = _StoredMockReceipt(action_hash=action_hash, result=result)
        self._side_effect_count += 1
        return result


def build_read_observation[ParametersT: BaseModel, PayloadT: RecordsPayload](
    registration: ConnectorOperationRegistration,
    request: ConnectorReadRequest[ParametersT],
    payload_type: type[PayloadT],
    resource_ids: tuple[str, ...],
) -> ConnectorObservation[PayloadT]:
    metadata = registration.metadata
    if not metadata.enabled:
        raise ConnectorPortError("operation_disabled", "connector operation is disabled")
    if request.capability_id != metadata.capability_id:
        raise ConnectorPortError("capability_mismatch", "connector capability does not match")
    if request.context.requested_timeout_seconds > metadata.default_timeout_seconds:
        raise ConnectorPortError(
            "invalid_request", "connector timeout exceeds the registered operation bound"
        )
    expected_binding = f"mock.{metadata.connector_family}.default"
    if request.context.binding_id != expected_binding:
        raise ConnectorPortError("binding_mismatch", "connector binding does not match mock family")

    fixture_projection: dict[str, JsonValue] = {
        "capability_id": metadata.capability_id,
        "binding_id": request.context.binding_id,
        "parameters": cast(JsonValue, request.parameters.model_dump(mode="json")),
    }
    fixture_digest = hashlib.sha256(
        MOCK_CONNECTOR_DOMAIN + canonical_json_bytes(fixture_projection)
    ).hexdigest()
    records = tuple(
        MockConnectorRecord(
            resource_id=resource_id,
            attributes={
                "fixture": hashlib.sha256(
                    fixture_digest.encode("ascii") + resource_id.encode("utf-8")
                ).hexdigest()[:16],
                "mock": True,
                "operation": metadata.capability_id,
            },
        )
        for resource_id in resource_ids
    )
    payload = payload_type(records=records)
    return ConnectorObservation[PayloadT](
        capability_id=metadata.capability_id,
        binding_id=request.context.binding_id,
        observation_id=f"mock-observation:{fixture_digest[:32]}",
        payload=payload,
        provenance_ids=request.context.provenance_ids,
        classification=metadata.data_classification,
    )


def execute_mock_write[CommandT: BaseModel](
    registration: ConnectorOperationRegistration,
    request: AuthorizedConnectorCommand[CommandT],
    ledger: InMemoryMockReceiptLedger,
) -> ConnectorWriteResult:
    metadata = registration.metadata
    if not metadata.enabled:
        raise ConnectorPortError("operation_disabled", "connector operation is disabled")
    if not isinstance(request.authorization, AuthorizedExternalWrite):
        raise ConnectorPortError(
            "authorization_mismatch", "sealed connector authorization is required"
        )

    authorization = request.authorization
    action = authorization.action
    checks = (
        (action.capability_id == metadata.capability_id, "capability_mismatch"),
        (action.connector_family == metadata.connector_family, "capability_mismatch"),
        (
            action.binding_id == f"mock.{metadata.connector_family}.default",
            "binding_mismatch",
        ),
        (action.payload_schema_id == metadata.request_schema_id, "schema_mismatch"),
        (authorization.action_hash == canonical_action_hash(action), "authorization_mismatch"),
        (
            canonical_json_bytes(request.command.model_dump(mode="json"))
            == canonical_json_bytes(action.minimized_payload),
            "authorization_mismatch",
        ),
    )
    for valid, code in checks:
        if not valid:
            raise ConnectorPortError(code, "connector command does not match its exact proof")

    return ledger.record(
        binding_id=action.binding_id,
        idempotency_key=authorization.idempotency_key,
        action_hash=authorization.action_hash,
        capability_id=metadata.capability_id,
    )
