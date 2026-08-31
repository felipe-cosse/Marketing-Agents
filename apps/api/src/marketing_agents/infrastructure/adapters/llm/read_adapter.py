"""Production controlled-READ adapter for schema-bound LLM generation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from marketing_agents.application.ports.llm import (
    LLMInvocationContext,
    LLMProvider,
    LLMRequest,
    TrustedSystemInstructions,
)
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterContract,
    ReadAdapterPermanentError,
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.runtime_inputs import RuntimeInputContract
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.domain.validation import frozen_json_mapping, require_digest, require_id
from marketing_agents.security.content_trust import ExternalContentKind, UntrustedContentPart

StructuredOutputTransform = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class LLMReadBinding:
    """Trusted schemas, prompt, and deterministic post-model transform for one operation."""

    scenario_id: str
    template_id: str
    instance_id: str
    capability_id: str
    input_schema_id: str
    input_schema: Mapping[str, Any] = field(repr=False)
    model_output_schema_id: str
    model_output_schema: Mapping[str, Any] = field(repr=False)
    output_schema_id: str
    output_schema: Mapping[str, Any] = field(repr=False)
    catalog_content_hash: str
    system_prompt: str = field(repr=False)
    provider_mode: Literal["mock", "real", "local"]
    provider_name: str
    provider_version: str
    output_transform: StructuredOutputTransform = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        for value, name in (
            (self.scenario_id, "LLM binding scenario ID"),
            (self.template_id, "LLM binding template ID"),
            (self.instance_id, "LLM binding instance ID"),
            (self.capability_id, "LLM binding capability ID"),
            (self.input_schema_id, "LLM binding input schema ID"),
            (self.model_output_schema_id, "LLM binding model output schema ID"),
            (self.output_schema_id, "LLM binding output schema ID"),
            (self.provider_name, "LLM binding provider name"),
            (self.provider_version, "LLM binding provider version"),
        ):
            require_id(value, name)
        require_digest(self.catalog_content_hash, "LLM binding catalog content hash")
        if (
            type(self.system_prompt) is not str
            or self.system_prompt != self.system_prompt.strip()
            or not self.system_prompt
            or len(self.system_prompt) > 32_768
        ):
            raise ValueError("LLM binding system prompt must be trusted, trimmed, and bounded")
        if self.provider_mode not in {"mock", "real", "local"}:
            raise ValueError("LLM binding provider mode is unsupported")
        if not callable(self.output_transform):
            raise ValueError("LLM binding output transform must be callable")
        input_schema = frozen_json_mapping(self.input_schema, "LLM binding input schema")
        model_schema = frozen_json_mapping(
            self.model_output_schema, "LLM binding model output schema"
        )
        output_schema = frozen_json_mapping(self.output_schema, "LLM binding output schema")
        if (
            input_schema.get("$id") != self.input_schema_id
            or model_schema.get("$id") != self.model_output_schema_id
            or output_schema.get("$id") != self.output_schema_id
        ):
            raise ValueError("LLM binding schema identity differs from its definition")
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "model_output_schema", model_schema)
        object.__setattr__(self, "output_schema", output_schema)


class StructuredLLMReadAdapter:
    """Execute exactly one structured model call and return a transformed inert observation."""

    def __init__(
        self,
        provider: LLMProvider,
        bindings: tuple[LLMReadBinding, ...],
    ) -> None:
        if not callable(getattr(provider, "generate_structured", None)):
            raise ValueError("LLM READ adapter requires a structured-output provider")
        indexed: dict[tuple[str, str, str, str], LLMReadBinding] = {}
        for binding in bindings:
            if type(binding) is not LLMReadBinding:
                raise ValueError("LLM READ adapter requires exact bindings")
            key = (
                binding.instance_id,
                binding.capability_id,
                binding.input_schema_id,
                binding.output_schema_id,
            )
            if key in indexed:
                raise ValueError("LLM READ adapter bindings must be unique")
            indexed[key] = binding
        if not indexed:
            raise ValueError("LLM READ adapter requires at least one binding")
        self._provider = provider
        self._bindings = MappingProxyType(indexed)

    def _binding_for(self, operation: OperationExecutionPolicy) -> LLMReadBinding:
        if type(operation) is not OperationExecutionPolicy:
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid", "LLM READ requires an exact operation policy"
            )
        if (
            operation.kind is not AttemptKind.MODEL
            or operation.connector_family != "model"
            or operation.request_schema_id is None
            or operation.result_schema_id is None
        ):
            raise ReadAdapterPermanentError(
                "adapter_contract_invalid", "LLM READ accepts only sealed model operations"
            )
        key = (
            operation.selected_instance_id,
            operation.capability_id,
            operation.request_schema_id,
            operation.result_schema_id,
        )
        try:
            binding = self._bindings[key]
        except KeyError:
            raise ReadAdapterPermanentError(
                "adapter_contract_unavailable", "LLM READ binding is unavailable"
            ) from None
        if canonical_schema_hash(binding.output_schema) != operation.result_schema_hash:
            raise ReadAdapterPermanentError(
                "adapter_contract_drift", "LLM READ output schema differs from sealed policy"
            )
        return binding

    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        self._binding_for(operation)
        return ReadAdapterContract.from_operation(operation)

    def input_contract_for(
        self,
        operation: OperationExecutionPolicy,
    ) -> RuntimeInputContract:
        binding = self._binding_for(operation)
        return RuntimeInputContract(
            schema_id=binding.input_schema_id,
            schema_version="v1",
            schema=binding.input_schema,
            classification=DataClassification.INTERNAL,
        )

    def output_contract_for(
        self,
        operation: OperationExecutionPolicy,
    ) -> RuntimeOutputContract:
        binding = self._binding_for(operation)
        return RuntimeOutputContract(
            schema_id=binding.output_schema_id,
            schema_version="v1",
            schema=binding.output_schema,
            classification=DataClassification.INTERNAL,
            provider_kind="llm",
            provider_mode=binding.provider_mode,
            provider_name=binding.provider_name,
            provider_version=binding.provider_version,
        )

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        if type(request) is not ReadAdapterRequest:
            raise ReadAdapterPermanentError(
                "invalid_request", "LLM READ requires an exact adapter request"
            )
        try:
            binding = self._binding_for_request(request)
            llm_request = LLMRequest(
                system_instructions=TrustedSystemInstructions(
                    template_id=binding.template_id,
                    catalog_content_hash=binding.catalog_content_hash,
                    content=binding.system_prompt,
                ),
                retrieved_content=(
                    UntrustedContentPart(
                        kind=ExternalContentKind.USER_INPUT,
                        source_id=f"input:{binding.scenario_id}",
                        content=canonical_json_bytes(request.input_payload).decode("utf-8"),
                        provenance_ids=request.provenance_ids,
                    ),
                ),
                output_schema_id=binding.model_output_schema_id,
                output_schema_hash=canonical_schema_hash(binding.model_output_schema),
                output_schema=json.loads(canonical_json_bytes(binding.model_output_schema)),
                context=LLMInvocationContext(
                    run_id=request.run_id,
                    step_id=request.step_id,
                    correlation_id=request.correlation_id,
                    deadline=request.call_deadline_at,
                    max_output_tokens=request.contract.max_model_output_tokens,
                ),
            )
            response = await self._provider.generate_structured(llm_request)
            if (
                response.provider != binding.provider_name
                or response.version != binding.provider_version
            ):
                raise ValueError("LLM provider identity differs from its binding")
            transformed = binding.output_transform(response.structured_payload)
            if type(transformed) is not dict:
                raise TypeError("LLM output transform returned a non-object")
            return ReadAdapterResult.from_request(
                request,
                observation_id=f"observation:{request.attempt_id}",
                output_payload=transformed,
                model_output_tokens=response.usage.output_tokens,
            )
        except asyncio.CancelledError:
            raise
        except ReadAdapterPermanentError:
            raise
        except Exception:
            raise ReadAdapterPermanentError(
                "unclassified_failure", "structured model generation failed"
            ) from None

    def _binding_for_request(self, request: ReadAdapterRequest) -> LLMReadBinding:
        contract = request.contract
        key = (
            contract.selected_instance_id,
            contract.capability_id,
            contract.request_schema_id,
            contract.result_schema_id,
        )
        try:
            binding = self._bindings[key]
        except KeyError:
            raise ReadAdapterPermanentError(
                "adapter_contract_unavailable", "LLM READ binding is unavailable"
            ) from None
        if (
            contract.attempt_kind is not AttemptKind.MODEL
            or contract.connector_family != "model"
            or contract.data_classification is not DataClassification.INTERNAL
            or canonical_schema_hash(binding.output_schema) != contract.result_schema_hash
        ):
            raise ReadAdapterPermanentError(
                "adapter_contract_drift", "LLM READ request differs from its sealed binding"
            )
        return binding


__all__ = ["LLMReadBinding", "StructuredLLMReadAdapter", "StructuredOutputTransform"]
