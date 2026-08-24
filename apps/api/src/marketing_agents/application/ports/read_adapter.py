"""Typed, provider-independent port for one controlled READ operation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.domain.validation import (
    frozen_json_mapping,
    require_digest,
    require_id,
    require_json_pointers,
    require_utc,
)


def _require_provenance_ids(values: tuple[str, ...], name: str) -> None:
    if type(values) is not tuple or not values or len(values) > 64:
        raise ValueError(f"{name} must be a nonempty immutable tuple of at most 64 IDs")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")
    for value in values:
        require_id(value, name)


@dataclass(frozen=True, slots=True)
class ReadAdapterContract:
    """Adapter-declared identity matched to a sealed operation before reservation."""

    policy_hash: str
    step_id: str
    operation_key: str
    attempt_kind: AttemptKind
    selected_instance_id: str
    configuration_revision: int
    capability_id: str
    connector_family: str
    binding_id: str | None
    binding_configuration_revision: int | None
    request_schema_id: str
    result_schema_id: str
    request_redaction_fields: tuple[str, ...]
    result_redaction_fields: tuple[str, ...]
    data_classification: DataClassification
    connector_timeout_seconds: int | None
    effective_timeout_seconds: int
    max_input_bytes: int
    max_input_field_bytes: int
    max_output_bytes: int
    max_model_output_tokens: int

    def __post_init__(self) -> None:
        require_digest(self.policy_hash, "READ adapter contract policy hash")
        for value, name in (
            (self.step_id, "READ adapter contract step ID"),
            (self.operation_key, "READ adapter contract operation key"),
            (self.selected_instance_id, "READ adapter contract selected instance ID"),
            (self.capability_id, "READ adapter contract capability ID"),
            (self.connector_family, "READ adapter contract connector family"),
            (self.request_schema_id, "READ adapter contract request schema ID"),
            (self.result_schema_id, "READ adapter contract result schema ID"),
        ):
            require_id(value, name)
        if type(self.attempt_kind) is not AttemptKind or self.attempt_kind not in {
            AttemptKind.MODEL,
            AttemptKind.TOOL,
        }:
            raise ValueError("READ adapter contract kind must be model or tool")
        if type(self.data_classification) is not DataClassification:
            raise ValueError("READ adapter contract classification must use the exact enum")
        if type(self.configuration_revision) is not int or self.configuration_revision < 1:
            raise ValueError("READ adapter contract configuration revision must be positive")
        require_json_pointers(
            self.request_redaction_fields,
            "READ adapter contract request redaction fields",
        )
        require_json_pointers(
            self.result_redaction_fields,
            "READ adapter contract result redaction fields",
        )
        if (
            type(self.effective_timeout_seconds) is not int
            or not 1 <= self.effective_timeout_seconds <= 120
        ):
            raise ValueError("READ adapter effective timeout must be from 1 through 120 seconds")
        for limit_value, limit_name, maximum in (
            (self.max_input_bytes, "input byte limit", 1_048_576),
            (self.max_input_field_bytes, "input field byte limit", 262_144),
            (self.max_output_bytes, "output byte limit", 4_194_304),
            (self.max_model_output_tokens, "model output token limit", 32_768),
        ):
            if type(limit_value) is not int or not 1 <= limit_value <= maximum:
                raise ValueError(f"READ adapter {limit_name} is outside its global bound")
        if self.max_input_field_bytes > self.max_input_bytes:
            raise ValueError("READ adapter input field limit exceeds total input bytes")
        if self.connector_family == "model":
            if (
                self.attempt_kind is not AttemptKind.MODEL
                or self.binding_id is not None
                or self.binding_configuration_revision is not None
                or self.request_redaction_fields
                or self.result_redaction_fields
                or self.connector_timeout_seconds is not None
                or self.data_classification is not DataClassification.INTERNAL
            ):
                raise ValueError("model adapter contract retains connector-only metadata")
        else:
            if self.attempt_kind is not AttemptKind.TOOL:
                raise ValueError("external READ adapter contracts must consume tool attempts")
            if self.binding_id is None:
                raise ValueError("external READ adapter contract requires a binding ID")
            require_id(self.binding_id, "READ adapter contract binding ID")
            if (
                self.binding_configuration_revision != self.configuration_revision
                or self.connector_timeout_seconds is None
                or type(self.connector_timeout_seconds) is not int
                or not 1 <= self.connector_timeout_seconds <= 120
                or self.effective_timeout_seconds > self.connector_timeout_seconds
            ):
                raise ValueError(
                    "external READ adapter contract requires its exact binding and timeout"
                )

    @classmethod
    def from_operation(cls, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        """Project the one authoritative adapter contract from durable policy."""

        if type(operation) is not OperationExecutionPolicy:
            raise TypeError("READ adapter contract requires an exact operation policy")
        if operation.request_schema_id is None or operation.result_schema_id is None:
            raise ValueError("callable READ operation requires its sealed schema pair")
        return cls(
            policy_hash=operation.policy_hash,
            step_id=operation.step_id,
            operation_key=operation.operation_key,
            attempt_kind=operation.kind,
            selected_instance_id=operation.selected_instance_id,
            configuration_revision=operation.configuration_revision,
            capability_id=operation.capability_id,
            connector_family=operation.connector_family,
            binding_id=operation.binding_id,
            binding_configuration_revision=operation.binding_configuration_revision,
            request_schema_id=operation.request_schema_id,
            result_schema_id=operation.result_schema_id,
            request_redaction_fields=operation.request_redaction_fields,
            result_redaction_fields=operation.result_redaction_fields,
            data_classification=operation.data_classification,
            connector_timeout_seconds=operation.connector_timeout_seconds,
            effective_timeout_seconds=operation.step_timeout_seconds,
            max_input_bytes=operation.max_input_bytes,
            max_input_field_bytes=operation.max_input_field_bytes,
            max_output_bytes=operation.max_output_bytes,
            max_model_output_tokens=operation.max_model_output_tokens,
        )


@dataclass(frozen=True, slots=True)
class ReadAdapterRequest:
    """One durable-attempt-bound request handed to an injected READ adapter."""

    attempt_id: str
    run_id: str
    step_id: str
    operation_key: str
    policy_hash: str
    attempt_number: int
    call_deadline_at: datetime
    correlation_id: str
    requested_timeout_seconds: int
    provenance_ids: tuple[str, ...]
    input_classification: DataClassification
    contract: ReadAdapterContract
    input_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for value, name in (
            (self.attempt_id, "READ adapter attempt ID"),
            (self.run_id, "READ adapter Run ID"),
            (self.step_id, "READ adapter step ID"),
            (self.operation_key, "READ adapter operation key"),
            (self.correlation_id, "READ adapter correlation ID"),
        ):
            require_id(value, name)
        require_digest(self.policy_hash, "READ adapter policy hash")
        if type(self.contract) is not ReadAdapterContract:
            raise ValueError("READ adapter request requires the exact sealed contract")
        if (
            self.step_id != self.contract.step_id
            or self.operation_key != self.contract.operation_key
            or self.policy_hash != self.contract.policy_hash
        ):
            raise ValueError("READ adapter request differs from its sealed operation contract")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("READ adapter attempt number must be positive")
        if (
            type(self.requested_timeout_seconds) is not int
            or not 1 <= self.requested_timeout_seconds <= self.contract.effective_timeout_seconds
        ):
            raise ValueError("READ adapter requested timeout exceeds its sealed contract")
        require_utc(self.call_deadline_at, "READ adapter call deadline")
        _require_provenance_ids(self.provenance_ids, "READ adapter input provenance IDs")
        if type(self.input_classification) is not DataClassification:
            raise ValueError("READ adapter input classification must use the exact enum")
        object.__setattr__(
            self,
            "input_payload",
            frozen_json_mapping(self.input_payload, "READ adapter input payload"),
        )

    @property
    def attempt_kind(self) -> AttemptKind:
        return self.contract.attempt_kind

    @property
    def selected_instance_id(self) -> str:
        return self.contract.selected_instance_id

    @property
    def configuration_revision(self) -> int:
        return self.contract.configuration_revision

    @property
    def capability_id(self) -> str:
        return self.contract.capability_id

    @property
    def connector_family(self) -> str:
        return self.contract.connector_family

    @property
    def binding_id(self) -> str | None:
        return self.contract.binding_id

    @property
    def binding_configuration_revision(self) -> int | None:
        return self.contract.binding_configuration_revision

    @property
    def request_schema_id(self) -> str:
        return self.contract.request_schema_id

    @property
    def result_schema_id(self) -> str:
        return self.contract.result_schema_id

    @property
    def request_redaction_fields(self) -> tuple[str, ...]:
        return self.contract.request_redaction_fields

    @property
    def result_redaction_fields(self) -> tuple[str, ...]:
        return self.contract.result_redaction_fields

    @property
    def data_classification(self) -> DataClassification:
        return self.contract.data_classification

    @property
    def connector_timeout_seconds(self) -> int | None:
        return self.contract.connector_timeout_seconds


@dataclass(frozen=True, slots=True)
class ReadAdapterResult:
    """Untrusted observation bound to one attempt and its declared adapter contract."""

    attempt_id: str
    run_id: str
    step_id: str
    operation_key: str
    policy_hash: str
    attempt_number: int
    contract: ReadAdapterContract
    observation_id: str
    provenance_ids: tuple[str, ...]
    classification: DataClassification
    model_output_tokens: int | None
    output_payload: Mapping[str, Any]
    trust_class: Literal["untrusted_tool_result"] = field(
        default="untrusted_tool_result",
        init=False,
    )

    @classmethod
    def from_request(
        cls,
        request: ReadAdapterRequest,
        *,
        observation_id: str,
        output_payload: Mapping[str, Any],
        provenance_ids: tuple[str, ...] | None = None,
        model_output_tokens: int | None = None,
    ) -> ReadAdapterResult:
        """Build the exact echoed observation shape expected from a safe adapter."""

        if type(request) is not ReadAdapterRequest:
            raise TypeError("READ adapter result requires an exact adapter request")
        return cls(
            attempt_id=request.attempt_id,
            run_id=request.run_id,
            step_id=request.step_id,
            operation_key=request.operation_key,
            policy_hash=request.policy_hash,
            attempt_number=request.attempt_number,
            contract=request.contract,
            observation_id=observation_id,
            provenance_ids=(request.provenance_ids if provenance_ids is None else provenance_ids),
            classification=request.contract.data_classification,
            model_output_tokens=model_output_tokens,
            output_payload=output_payload,
        )

    def __post_init__(self) -> None:
        for value, name in (
            (self.attempt_id, "READ adapter result attempt ID"),
            (self.run_id, "READ adapter result Run ID"),
            (self.step_id, "READ adapter result step ID"),
            (self.operation_key, "READ adapter result operation key"),
            (self.observation_id, "READ adapter observation ID"),
        ):
            require_id(value, name)
        require_digest(self.policy_hash, "READ adapter result policy hash")
        if type(self.contract) is not ReadAdapterContract:
            raise ValueError("READ adapter result requires the exact sealed contract")
        if (
            self.step_id != self.contract.step_id
            or self.operation_key != self.contract.operation_key
            or self.policy_hash != self.contract.policy_hash
        ):
            raise ValueError("READ adapter result differs from its sealed operation contract")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("READ adapter result attempt number must be positive")
        _require_provenance_ids(self.provenance_ids, "READ adapter result provenance IDs")
        if (
            type(self.classification) is not DataClassification
            or self.classification is not self.contract.data_classification
        ):
            raise ValueError("READ adapter result classification differs from its contract")
        if self.contract.attempt_kind is AttemptKind.MODEL:
            if (
                type(self.model_output_tokens) is not int
                or not 0 <= self.model_output_tokens <= 2_147_483_647
            ):
                raise ValueError("model READ results require nonnegative output token usage")
        elif self.model_output_tokens is not None:
            raise ValueError("tool READ results cannot claim model output token usage")
        object.__setattr__(
            self,
            "output_payload",
            frozen_json_mapping(self.output_payload, "READ adapter output payload"),
        )

    @property
    def attempt_kind(self) -> AttemptKind:
        return self.contract.attempt_kind

    @property
    def selected_instance_id(self) -> str:
        return self.contract.selected_instance_id

    @property
    def configuration_revision(self) -> int:
        return self.contract.configuration_revision

    @property
    def capability_id(self) -> str:
        return self.contract.capability_id

    @property
    def connector_family(self) -> str:
        return self.contract.connector_family

    @property
    def binding_id(self) -> str | None:
        return self.contract.binding_id

    @property
    def binding_configuration_revision(self) -> int | None:
        return self.contract.binding_configuration_revision

    @property
    def request_schema_id(self) -> str:
        return self.contract.request_schema_id

    @property
    def result_schema_id(self) -> str:
        return self.contract.result_schema_id

    @property
    def request_redaction_fields(self) -> tuple[str, ...]:
        return self.contract.request_redaction_fields

    @property
    def result_redaction_fields(self) -> tuple[str, ...]:
        return self.contract.result_redaction_fields

    @property
    def connector_timeout_seconds(self) -> int | None:
        return self.contract.connector_timeout_seconds


class ReadAdapterError(RuntimeError):
    """Safe adapter classification with no provider response or secret material."""

    def __init__(self, code: str, message: str) -> None:
        require_id(code, "READ adapter error code")
        super().__init__(message)
        self.code = code


class ReadAdapterTransientError(ReadAdapterError):
    """The adapter explicitly classified a failure as retryable."""


class ReadAdapterPermanentError(ReadAdapterError):
    """The adapter explicitly classified a failure as permanent."""


class ReadAdapterCancelledError(ReadAdapterError):
    """The adapter stopped without returning an effect or observation."""


class ReadAdapter(Protocol):
    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        """Declare exact support before any durable budget or attempt mutation."""
        ...

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        """Perform one exact READ attempt; implementations never receive a UoW."""
        ...


__all__ = [
    "ReadAdapter",
    "ReadAdapterCancelledError",
    "ReadAdapterContract",
    "ReadAdapterError",
    "ReadAdapterPermanentError",
    "ReadAdapterRequest",
    "ReadAdapterResult",
    "ReadAdapterTransientError",
]
