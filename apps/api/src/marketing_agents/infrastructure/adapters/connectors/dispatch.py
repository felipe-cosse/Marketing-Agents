"""Registry-backed typed gateway for exact authorized mock writes."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, cast

from pydantic import ValidationError

from marketing_agents.application.policies.write_authorization import (
    AuthorizedExternalWrite,
)
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorPortError,
    ConnectorWriteResult,
)
from marketing_agents.application.ports.external_writes import (
    ConnectorDeliveryContract,
    ConnectorDeliveryFailure,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import Effect

from .mock.families import MockConnectorBundle
from .registry import ConnectorBundleConfigurationError, ConnectorOperationRegistry

_SAFE_CONNECTOR_CODES = frozenset(
    {
        "authorization_mismatch",
        "binding_mismatch",
        "capability_mismatch",
        "idempotency_conflict",
        "invalid_request",
        "operation_disabled",
        "schema_mismatch",
    }
)


class RegistryConnectorWriteGateway:
    """Resolve only declared write operations and rebuild their typed command."""

    def __init__(
        self,
        registry: ConnectorOperationRegistry,
        bundle: MockConnectorBundle,
        *,
        binding_configuration_revisions: Mapping[str, int],
    ) -> None:
        if not bundle.ledger.durable:
            raise ConnectorBundleConfigurationError(
                "external-write dispatch requires a durable connector receipt ledger"
            )
        self._registry = registry
        self._bundle = bundle
        self._binding_revisions = MappingProxyType(dict(binding_configuration_revisions))

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        resolution_failure: ConnectorDeliveryFailure | None = None
        try:
            registration = self._registry.resolve(action.envelope.capability_id)
            revision = self._binding_revisions[action.connector_binding_id]
        except (ConnectorBundleConfigurationError, KeyError):
            resolution_failure = ConnectorDeliveryFailure(
                "delivery_contract_unavailable",
                "connector delivery contract is unavailable",
                request_may_have_left_process=False,
            )
        if resolution_failure is not None:
            raise resolution_failure from None
        metadata = registration.metadata
        if metadata.effect is not Effect.WRITE:
            raise ConnectorDeliveryFailure(
                "delivery_effect_mismatch",
                "read connector operations cannot dispatch an external write",
                request_may_have_left_process=False,
            )
        contract = ConnectorDeliveryContract(
            capability_id=metadata.capability_id,
            connector_family=metadata.connector_family,
            binding_id=action.connector_binding_id,
            binding_configuration_revision=revision,
            request_schema_id=metadata.request_schema_id,
            idempotency_support=cast(
                Literal["required", "supported", "unavailable"],
                metadata.idempotency_support,
            ),
            timeout_seconds=metadata.default_timeout_seconds,
        )
        expected = action.delivery_contract
        if (
            contract.capability_id != expected.capability_id
            or contract.connector_family != expected.connector_family
            or contract.binding_id != expected.binding_id
            or contract.binding_configuration_revision != expected.binding_configuration_revision
            or contract.request_schema_id != expected.request_schema_id
            or contract.idempotency_support != expected.idempotency_support
            or contract.timeout_seconds != expected.timeout_seconds
        ):
            raise ConnectorDeliveryFailure(
                "delivery_contract_drift",
                "current connector contract differs from the persisted plan snapshot",
                request_may_have_left_process=False,
            )
        return contract

    async def execute(self, authorization: AuthorizedExternalWrite) -> ConnectorWriteResult:
        action = authorization.action
        mapped_failure: ConnectorDeliveryFailure | None = None
        try:
            registration = self._registry.resolve(action.capability_id)
            command = registration.request_type.model_validate_json(
                canonical_json_bytes(action.minimized_payload), strict=True
            )
            connector = getattr(self._bundle, action.connector_family)
            method = getattr(connector, registration.method_name)
            return cast(
                ConnectorWriteResult,
                await method(
                    AuthorizedConnectorCommand(
                        authorization=authorization,
                        command=command,
                    )
                ),
            )
        except (ConnectorBundleConfigurationError, ConnectorPortError, ValidationError) as exc:
            code = getattr(exc, "code", "connector_request_rejected")
            mapped_failure = ConnectorDeliveryFailure(
                str(code) if code in _SAFE_CONNECTOR_CODES else "connector_request_rejected",
                "connector rejected the exact authorized request",
                request_may_have_left_process=False,
            )
        except Exception:
            mapped_failure = ConnectorDeliveryFailure(
                "connector_delivery_uncertain",
                "connector delivery failed after invocation began",
                request_may_have_left_process=True,
            )
        if mapped_failure is None:  # pragma: no cover - successful path returns above
            raise AssertionError("connector failure mapping lost its classified result")
        raise mapped_failure from None
