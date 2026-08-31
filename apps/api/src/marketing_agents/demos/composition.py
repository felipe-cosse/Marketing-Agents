"""Trusted production composition for the deterministic social draft model adapter."""

from __future__ import annotations

from typing import Literal

from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.ports.llm import LLMProvider
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterContract,
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.runtime_inputs import RuntimeInputContract
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.infrastructure.adapters.llm import (
    DeterministicLLMProvider,
    DeterministicRendererRegistry,
    LLMReadBinding,
    StructuredLLMReadAdapter,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog

from .social_content_draft import (
    SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA,
    SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA,
    SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
    SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA,
    SOCIAL_CONTENT_DRAFT_RENDERER,
    SOCIAL_CONTENT_DRAFT_SCENARIO,
    finalize_social_content_draft,
)


class SocialContentDraftReadAdapter:
    """Social demo adapter sealed to the credential-free deterministic provider."""

    __slots__ = ("_delegate", "_provider")

    def __init__(
        self,
        catalog: CompiledCatalog,
        provider: LLMProvider | None = None,
        *,
        provider_mode: Literal["mock", "real", "local"] = "mock",
        provider_name: str = "mock",
        provider_version: str = "v1",
    ) -> None:
        if (provider_mode, provider_name, provider_version) != ("mock", "mock", "v1"):
            raise ValueError("social demo requires the deterministic mock provider identity")
        if provider is not None and type(provider) is not DeterministicLLMProvider:
            raise ValueError("social demo requires the credential-free deterministic provider")
        selected_provider = (
            build_social_content_draft_deterministic_provider(catalog)
            if provider is None
            else provider
        )
        self._provider = selected_provider
        self._require_deterministic_mock()
        self._delegate = _build_social_content_draft_structured_adapter(
            catalog,
            selected_provider,
        )

    def _require_deterministic_mock(self) -> None:
        if (
            type(self._provider) is not DeterministicLLMProvider
            or self._provider.provider_id != "mock"
            or self._provider.model_id != "deterministic"
            or self._provider.version != "v1"
        ):
            raise ValueError("social demo deterministic mock provider identity is unavailable")

    def require_deterministic_mock(self) -> None:
        """Fail closed if the sealed Social demo provider identity has drifted."""

        self._require_deterministic_mock()

    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        self._require_deterministic_mock()
        return self._delegate.contract_for(operation)

    def input_contract_for(self, operation: OperationExecutionPolicy) -> RuntimeInputContract:
        self._require_deterministic_mock()
        return self._delegate.input_contract_for(operation)

    def output_contract_for(self, operation: OperationExecutionPolicy) -> RuntimeOutputContract:
        self._require_deterministic_mock()
        return self._delegate.output_contract_for(operation)

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self._require_deterministic_mock()
        return await self._delegate.execute(request)


def build_social_content_draft_read_adapter(
    catalog: CompiledCatalog,
    provider: LLMProvider | None = None,
    *,
    provider_mode: Literal["mock", "real", "local"] = "mock",
    provider_name: str = "mock",
    provider_version: str = "v1",
) -> SocialContentDraftReadAdapter:
    """Build the exact schema-bound adapter; default provider is credential-free and offline."""

    return SocialContentDraftReadAdapter(
        catalog,
        provider,
        provider_mode=provider_mode,
        provider_name=provider_name,
        provider_version=provider_version,
    )


def _build_social_content_draft_structured_adapter(
    catalog: CompiledCatalog,
    provider: DeterministicLLMProvider,
) -> StructuredLLMReadAdapter:
    if type(catalog) is not CompiledCatalog:
        raise ValueError("social demo adapter requires one compiled catalog")
    scenario = SOCIAL_CONTENT_DRAFT_SCENARIO
    templates = {item.id: item for item in catalog.templates}
    instances = {item.id: item for item in catalog.instances}
    capabilities = {item.id: item for item in catalog.tool_capabilities}
    template = templates.get(scenario.template_id)
    instance = instances.get(scenario.instance_id)
    capability = capabilities.get("cap.model.generate-structured")
    system_prompt = catalog.prompt_text_by_template.get(scenario.template_id)
    if (
        template is None
        or instance is None
        or instance.template_id != scenario.template_id
        or capability is None
        or capability.id not in template.allowed_tool_capability_ids
        or capability.effect != "read"
        or capability.connector_family != "model"
        or type(system_prompt) is not str
        or not system_prompt.strip()
    ):
        raise ValueError("social demo catalog binding is unavailable")
    return StructuredLLMReadAdapter(
        provider,
        (
            LLMReadBinding(
                scenario_id=scenario.id,
                template_id=scenario.template_id,
                instance_id=scenario.instance_id,
                capability_id=capability.id,
                input_schema_id=scenario.input_schema_id,
                input_schema=SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA,
                model_output_schema_id=SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
                model_output_schema=SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA,
                output_schema_id=scenario.output_schema_id,
                output_schema=SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA,
                catalog_content_hash=catalog.content_hash.removeprefix("catalog-sha256-v1:"),
                system_prompt=system_prompt,
                provider_mode="mock",
                provider_name="mock",
                provider_version="v1",
                output_transform=finalize_social_content_draft,
            ),
        ),
    )


def build_social_content_draft_deterministic_provider(
    catalog: CompiledCatalog,
) -> DeterministicLLMProvider:
    """Build the credential-free provider used by local and acceptance demo execution."""

    if type(catalog) is not CompiledCatalog:
        raise ValueError("social demo provider requires one compiled catalog")
    scenario = SOCIAL_CONTENT_DRAFT_SCENARIO
    template = next(
        (item for item in catalog.templates if item.id == scenario.template_id),
        None,
    )
    capability = next(
        (item for item in catalog.tool_capabilities if item.id == "cap.model.generate-structured"),
        None,
    )
    if (
        template is None
        or capability is None
        or capability.id not in template.allowed_tool_capability_ids
        or capability.effect != "read"
        or capability.connector_family != "model"
    ):
        raise ValueError("social demo deterministic provider binding is unavailable")
    guard = RuntimePolicyGuard(
        RuntimePolicySnapshot(
            allowed_capabilities=(
                CapabilityPolicy(
                    capability_id=capability.id,
                    effect=capability.effect,
                    connector_family=capability.connector_family,
                ),
            ),
            input_max_bytes=template.budget_policy.max_input_bytes,
            max_input_field_bytes=template.budget_policy.max_input_field_bytes,
            output_max_bytes=template.budget_policy.max_output_bytes,
            max_json_depth=16,
            max_content_parts=1,
            max_content_characters=template.budget_policy.max_input_bytes,
            max_model_calls=1,
            max_tool_calls=0,
            rate_window_max_calls=template.rate_limit_policy.max_calls,
            rate_window_seconds=template.rate_limit_policy.window_seconds,
            step_timeout_seconds=template.timeout_policy.step_seconds,
            run_timeout_seconds=template.timeout_policy.run_seconds,
        )
    )
    return DeterministicLLMProvider(
        DeterministicRendererRegistry((SOCIAL_CONTENT_DRAFT_RENDERER,)),
        guard,
    )


__all__ = [
    "SocialContentDraftReadAdapter",
    "build_social_content_draft_deterministic_provider",
    "build_social_content_draft_read_adapter",
]
