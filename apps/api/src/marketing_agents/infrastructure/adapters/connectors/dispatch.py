"""Registry-backed typed adapters for exact connector reads and writes."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from marketing_agents.application.policies.write_authorization import (
    AuthorizedExternalWrite,
)
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorCallContext,
    ConnectorObservation,
    ConnectorPortError,
    ConnectorWriteResult,
)
from marketing_agents.application.ports.external_writes import (
    ConnectorDeliveryContract,
    ConnectorDeliveryFailure,
)
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterContract,
    ReadAdapterPermanentError,
    ReadAdapterRequest,
    ReadAdapterResult,
    ReadAdapterTransientError,
)
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import Effect
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.domain.schema_hash import canonical_schema_hash

from .mock.families import MockConnectorBundle
from .registry import (
    ConnectorBundleConfigurationError,
    ConnectorOperationRegistration,
    ConnectorOperationRegistry,
)

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

_SAFE_READ_CONNECTOR_CODES = frozenset(
    {
        "binding_mismatch",
        "capability_mismatch",
        "invalid_request",
        "operation_disabled",
        "schema_mismatch",
    }
)


class RegistryConnectorReadAdapter:
    """Resolve one sealed external READ through the immutable connector registry."""

    def __init__(
        self,
        registry: ConnectorOperationRegistry,
        bundle: MockConnectorBundle,
        *,
        binding_configuration_revisions: Mapping[str, int],
    ) -> None:
        if bundle.registry is not registry:
            raise ConnectorBundleConfigurationError(
                "READ adapter registry must be the bundle's exact registry"
            )
        revisions = dict(binding_configuration_revisions)
        for binding_id, revision in revisions.items():
            if (
                not binding_id
                or binding_id != binding_id.strip()
                or type(revision) is not int
                or revision < 1
            ):
                raise ConnectorBundleConfigurationError(
                    "READ adapter binding revisions must be normalized positive values"
                )
        self._registry = registry
        self._bundle = bundle
        self._binding_revisions = MappingProxyType(revisions)

    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        """Project current registry and binding facts for pre-reservation comparison."""

        if type(operation) is not OperationExecutionPolicy:
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid",
                "connector READ contract requires an exact operation policy",
            )
        if operation.connector_family == "model" or operation.binding_id is None:
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid",
                "registry connector adapter accepts only external READ operations",
            )
        try:
            registration = self._registry.resolve(operation.capability_id)
            revision = self._binding_revisions[operation.binding_id]
        except (ConnectorBundleConfigurationError, KeyError):
            raise ReadAdapterPermanentError(
                "adapter_contract_unavailable",
                "connector READ contract is unavailable",
            ) from None

        metadata = registration.metadata
        if metadata.effect is not Effect.READ:
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid",
                "write connector operations cannot execute as controlled READs",
            )
        result_type = registration.result_type
        if not isinstance(result_type, type) or not issubclass(result_type, BaseModel):
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid",
                "registered connector output schema is unavailable",
            )
        try:
            return ReadAdapterContract(
                policy_hash=operation.policy_hash,
                step_id=operation.step_id,
                operation_key=operation.operation_key,
                attempt_kind=AttemptKind.TOOL,
                selected_instance_id=operation.selected_instance_id,
                configuration_revision=revision,
                capability_id=metadata.capability_id,
                connector_family=metadata.connector_family,
                binding_id=operation.binding_id,
                binding_configuration_revision=revision,
                request_schema_id=metadata.request_schema_id,
                result_schema_id=metadata.result_schema_id,
                result_schema_hash=canonical_schema_hash(result_type.model_json_schema()),
                request_redaction_fields=metadata.request_redaction_fields,
                result_redaction_fields=metadata.result_redaction_fields,
                data_classification=metadata.data_classification,
                connector_timeout_seconds=metadata.default_timeout_seconds,
                effective_timeout_seconds=min(
                    operation.step_timeout_seconds,
                    metadata.default_timeout_seconds,
                ),
                max_input_bytes=operation.max_input_bytes,
                max_input_field_bytes=operation.max_input_field_bytes,
                max_output_bytes=operation.max_output_bytes,
                max_model_output_tokens=operation.max_model_output_tokens,
            )
        except ValueError:
            raise ReadAdapterPermanentError(
                "adapter_contract_drift",
                "current connector READ contract is incompatible with the sealed operation",
            ) from None

    def output_contract_for(
        self,
        operation: OperationExecutionPolicy,
    ) -> RuntimeOutputContract:
        """Expose the registered Pydantic result schema independently of call output."""

        contract = self.contract_for(operation)
        try:
            registration = self._registry.resolve(operation.capability_id)
            result_type = registration.result_type
            if not isinstance(result_type, type) or not issubclass(result_type, BaseModel):
                raise TypeError("registered READ result type is not a model")
            schema = result_type.model_json_schema()
            return RuntimeOutputContract(
                schema_id=contract.result_schema_id,
                schema_version="v1",
                schema=schema,
                classification=contract.data_classification,
                provider_kind="connector",
                provider_mode="mock",
                provider_name=contract.connector_family,
                provider_version=result_type.__name__,
            )
        except (ConnectorBundleConfigurationError, TypeError, ValueError):
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid",
                "registered connector output schema is unavailable",
            ) from None

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        """Strictly rebuild, invoke, and revalidate one registered connector READ."""

        if type(request) is not ReadAdapterRequest:
            raise ReadAdapterPermanentError(
                "connector_request_rejected",
                "connector READ requires an exact adapter request",
            )
        registration = self._registration_for_request(request)
        metadata = registration.metadata
        try:
            context = ConnectorCallContext(
                binding_id=cast(str, request.binding_id),
                run_id=request.run_id,
                step_id=request.step_id,
                correlation_id=request.correlation_id,
                deadline=request.call_deadline_at,
                provenance_ids=request.provenance_ids,
                requested_timeout_seconds=request.requested_timeout_seconds,
            )
            typed_request = registration.request_type.model_validate_json(
                canonical_json_bytes(
                    {
                        "capability_id": request.capability_id,
                        "context": context.model_dump(mode="json"),
                        "parameters": request.input_payload,
                    }
                ),
                strict=True,
            )
            connector = getattr(self._bundle, metadata.connector_family)
            method = getattr(connector, registration.method_name)
        except (AttributeError, ConnectorBundleConfigurationError):
            raise ReadAdapterPermanentError(
                "adapter_contract_unavailable",
                "registered connector READ implementation is unavailable",
            ) from None
        except (TypeError, ValueError, ValidationError):
            raise ReadAdapterPermanentError(
                "connector_request_rejected",
                "connector READ request does not match its registered schema",
            ) from None

        try:
            observation = await method(typed_request)
        except ConnectorPortError as exc:
            code = (
                exc.code if exc.code in _SAFE_READ_CONNECTOR_CODES else "connector_request_rejected"
            )
            raise ReadAdapterPermanentError(
                code,
                "connector rejected the exact controlled READ request",
            ) from None
        except Exception:
            raise ReadAdapterTransientError(
                "connector_read_unavailable",
                "connector READ failed without returning an observation",
            ) from None

        try:
            validated = self._validate_observation(request, registration.result_type, observation)
            return ReadAdapterResult.from_request(
                request,
                observation_id=validated.observation_id,
                provenance_ids=validated.provenance_ids,
                output_payload=validated.payload.model_dump(mode="json"),
            )
        except ReadAdapterPermanentError:
            raise
        except (TypeError, ValueError, ValidationError):
            raise ReadAdapterPermanentError(
                "connector_result_rejected",
                "connector READ result does not match its registered contract",
            ) from None

    def _registration_for_request(
        self,
        request: ReadAdapterRequest,
    ) -> ConnectorOperationRegistration:
        contract = request.contract
        if contract.connector_family == "model" or contract.binding_id is None:
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid",
                "registry connector adapter accepts only external READ requests",
            )
        try:
            registration = self._registry.resolve(contract.capability_id)
            revision = self._binding_revisions[contract.binding_id]
        except (ConnectorBundleConfigurationError, KeyError):
            raise ReadAdapterPermanentError(
                "adapter_contract_unavailable",
                "connector READ contract is unavailable",
            ) from None
        metadata = registration.metadata
        current = (
            metadata.effect,
            metadata.capability_id,
            metadata.connector_family,
            revision,
            metadata.request_schema_id,
            metadata.result_schema_id,
            metadata.request_redaction_fields,
            metadata.result_redaction_fields,
            metadata.data_classification,
            metadata.default_timeout_seconds,
        )
        expected = (
            Effect.READ,
            contract.capability_id,
            contract.connector_family,
            contract.binding_configuration_revision,
            contract.request_schema_id,
            contract.result_schema_id,
            contract.request_redaction_fields,
            contract.result_redaction_fields,
            contract.data_classification,
            contract.connector_timeout_seconds,
        )
        if current != expected:
            raise ReadAdapterPermanentError(
                "adapter_contract_drift",
                "current connector READ contract differs from the reserved request",
            )
        return registration

    @staticmethod
    def _validate_observation(
        request: ReadAdapterRequest,
        result_type: type[BaseModel] | type[ConnectorWriteResult],
        observation: object,
    ) -> ConnectorObservation[BaseModel]:
        if not isinstance(result_type, type) or not issubclass(result_type, BaseModel):
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid",
                "registered READ result type is not a Pydantic model",
            )
        if not isinstance(observation, ConnectorObservation):
            raise ReadAdapterPermanentError(
                "connector_result_rejected",
                "connector did not return a typed observation",
            )
        observation_type = TypeAdapter(ConnectorObservation[result_type])  # type: ignore[valid-type]
        validated = cast(
            ConnectorObservation[BaseModel],
            observation_type.validate_json(
                canonical_json_bytes(observation.model_dump(mode="json")),
                strict=True,
            ),
        )
        if (
            validated.trust_class != "untrusted_tool_result"
            or validated.capability_id != request.capability_id
            or validated.binding_id != request.binding_id
            or validated.classification is not request.data_classification
            or not set(request.provenance_ids).issubset(validated.provenance_ids)
            or len(validated.provenance_ids) != len(set(validated.provenance_ids))
        ):
            raise ReadAdapterPermanentError(
                "connector_result_mismatch",
                "connector observation identity differs from the exact READ request",
            )
        return validated


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
