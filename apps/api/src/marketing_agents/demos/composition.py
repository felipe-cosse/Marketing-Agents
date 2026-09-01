"""Trusted composition for the sealed deterministic read-only demo adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

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
from marketing_agents.infrastructure.catalog.models import AgentTemplateRecord, CompiledCatalog

from .blog_content_review import (
    BLOG_CONTENT_REVIEW_INPUT_SCHEMA,
    BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA,
    BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA_ID,
    BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA,
    BLOG_CONTENT_REVIEW_RENDERER,
    BLOG_CONTENT_REVIEW_SCENARIO,
    finalize_blog_content_review,
)
from .community_reminder_draft import (
    COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA,
    COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA,
    COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
    COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA,
    COMMUNITY_REMINDER_DRAFT_RENDERER,
    COMMUNITY_REMINDER_DRAFT_SCENARIO,
    finalize_community_reminder_draft,
)
from .contracts import DemoScenarioDefinition
from .email_signup_onboarding import (
    EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
    EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
    EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA_ID,
    EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_RENDERER,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO,
    finalize_email_signup_onboarding,
)
from .social_content_draft import (
    SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA,
    SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA,
    SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
    SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA,
    SOCIAL_CONTENT_DRAFT_RENDERER,
    SOCIAL_CONTENT_DRAFT_SCENARIO,
    finalize_social_content_draft,
)

_SUPPORTED_MODEL_BINDINGS = (
    (SOCIAL_CONTENT_DRAFT_SCENARIO, None),
    (BLOG_CONTENT_REVIEW_SCENARIO, None),
    (COMMUNITY_REMINDER_DRAFT_SCENARIO, None),
    (
        EMAIL_SIGNUP_ONBOARDING_SCENARIO,
        EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
    ),
)


class DeterministicDemoReadAdapter:
    """Exact demo adapter sealed to the credential-free deterministic provider."""

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
            raise ValueError("deterministic mock provider identity is required for demos")
        if provider is not None and type(provider) is not DeterministicLLMProvider:
            raise ValueError("demos require the credential-free deterministic provider")
        selected_provider = (
            build_demo_deterministic_provider(catalog) if provider is None else provider
        )
        self._provider = selected_provider
        self._require_deterministic_mock()
        self._delegate = _build_demo_structured_adapter(catalog, selected_provider)

    def _require_deterministic_mock(self) -> None:
        if (
            type(self._provider) is not DeterministicLLMProvider
            or self._provider.provider_id != "mock"
            or self._provider.model_id != "deterministic"
            or self._provider.version != "v1"
        ):
            raise ValueError("deterministic demo mock provider identity is unavailable")

    def require_deterministic_mock(self) -> None:
        """Fail closed if the sealed provider identity has drifted."""

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


# Backward-compatible exact runtime aliases retained for DEMO-01 callers.
SocialContentDraftReadAdapter = DeterministicDemoReadAdapter
BlogContentReviewReadAdapter = DeterministicDemoReadAdapter
CommunityReminderDraftReadAdapter = DeterministicDemoReadAdapter
EmailSignupOnboardingReadAdapter = DeterministicDemoReadAdapter


def build_demo_read_adapter(
    catalog: CompiledCatalog,
    provider: LLMProvider | None = None,
    *,
    provider_mode: Literal["mock", "real", "local"] = "mock",
    provider_name: str = "mock",
    provider_version: str = "v1",
) -> DeterministicDemoReadAdapter:
    """Build the only schema-bound adapter supported by the deterministic demo drain."""

    return DeterministicDemoReadAdapter(
        catalog,
        provider,
        provider_mode=provider_mode,
        provider_name=provider_name,
        provider_version=provider_version,
    )


def build_social_content_draft_read_adapter(
    catalog: CompiledCatalog,
    provider: LLMProvider | None = None,
    *,
    provider_mode: Literal["mock", "real", "local"] = "mock",
    provider_name: str = "mock",
    provider_version: str = "v1",
) -> SocialContentDraftReadAdapter:
    """Preserve the DEMO-01 builder while returning the sealed shared adapter."""

    return build_demo_read_adapter(
        catalog,
        provider,
        provider_mode=provider_mode,
        provider_name=provider_name,
        provider_version=provider_version,
    )


def build_blog_content_review_read_adapter(
    catalog: CompiledCatalog,
    provider: LLMProvider | None = None,
    *,
    provider_mode: Literal["mock", "real", "local"] = "mock",
    provider_name: str = "mock",
    provider_version: str = "v1",
) -> BlogContentReviewReadAdapter:
    """Build the Blog compatibility entry point over the exact shared adapter."""

    return build_demo_read_adapter(
        catalog,
        provider,
        provider_mode=provider_mode,
        provider_name=provider_name,
        provider_version=provider_version,
    )


def build_email_signup_onboarding_read_adapter(
    catalog: CompiledCatalog,
    provider: LLMProvider | None = None,
    *,
    provider_mode: Literal["mock", "real", "local"] = "mock",
    provider_name: str = "mock",
    provider_version: str = "v1",
) -> EmailSignupOnboardingReadAdapter:
    """Build the Email compatibility entry point over the exact shared adapter."""

    return build_demo_read_adapter(
        catalog,
        provider,
        provider_mode=provider_mode,
        provider_name=provider_name,
        provider_version=provider_version,
    )


def build_community_reminder_draft_read_adapter(
    catalog: CompiledCatalog,
    provider: LLMProvider | None = None,
    *,
    provider_mode: Literal["mock", "real", "local"] = "mock",
    provider_name: str = "mock",
    provider_version: str = "v1",
) -> CommunityReminderDraftReadAdapter:
    """Build the Community compatibility entry point over the exact shared adapter."""

    return build_demo_read_adapter(
        catalog,
        provider,
        provider_mode=provider_mode,
        provider_name=provider_name,
        provider_version=provider_version,
    )


def _catalog_binding(
    catalog: CompiledCatalog,
    scenario: DemoScenarioDefinition,
    *,
    model_instance_id: str | None = None,
) -> tuple[AgentTemplateRecord, str, str]:
    templates = {item.id: item for item in catalog.templates}
    instances = {item.id: item for item in catalog.instances}
    capabilities = {item.id: item for item in catalog.tool_capabilities}
    selected_instance_id = model_instance_id or scenario.instance_id
    selected_agent = next(
        (agent for agent in scenario.selected_agents if agent.instance_id == selected_instance_id),
        None,
    )
    template_id = None if selected_agent is None else selected_agent.template_id
    template = None if template_id is None else templates.get(template_id)
    instance = instances.get(selected_instance_id)
    capability = capabilities.get("cap.model.generate-structured")
    system_prompt = (
        None if template_id is None else catalog.prompt_text_by_template.get(template_id)
    )
    if (
        type(template) is not AgentTemplateRecord
        or instance is None
        or instance.template_id != template_id
        or capability is None
        or capability.id not in template.allowed_tool_capability_ids
        or capability.effect != "read"
        or capability.connector_family != "model"
        or type(system_prompt) is not str
        or not system_prompt.strip()
    ):
        raise ValueError("deterministic demo catalog binding is unavailable")
    return template, selected_instance_id, system_prompt


def _binding(
    *,
    catalog: CompiledCatalog,
    scenario: DemoScenarioDefinition,
    model_instance_id: str | None,
    input_schema_id: str,
    input_schema: Mapping[str, Any],
    model_output_schema_id: str,
    model_output_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    output_transform: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> LLMReadBinding:
    template, selected_instance_id, system_prompt = _catalog_binding(
        catalog,
        scenario,
        model_instance_id=model_instance_id,
    )
    return LLMReadBinding(
        scenario_id=scenario.id,
        template_id=template.id,
        instance_id=selected_instance_id,
        capability_id="cap.model.generate-structured",
        input_schema_id=input_schema_id,
        input_schema=input_schema,
        model_output_schema_id=model_output_schema_id,
        model_output_schema=model_output_schema,
        output_schema_id=scenario.output_schema_id,
        output_schema=output_schema,
        catalog_content_hash=catalog.content_hash.removeprefix("catalog-sha256-v1:"),
        system_prompt=system_prompt,
        provider_mode="mock",
        provider_name="mock",
        provider_version="v1",
        output_transform=output_transform,
    )


def _build_demo_structured_adapter(
    catalog: CompiledCatalog,
    provider: DeterministicLLMProvider,
) -> StructuredLLMReadAdapter:
    if type(catalog) is not CompiledCatalog:
        raise ValueError("deterministic demo adapter requires one compiled catalog")
    return StructuredLLMReadAdapter(
        provider,
        (
            _binding(
                catalog=catalog,
                scenario=SOCIAL_CONTENT_DRAFT_SCENARIO,
                model_instance_id=None,
                input_schema_id=SOCIAL_CONTENT_DRAFT_SCENARIO.input_schema_id,
                input_schema=SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA,
                model_output_schema_id=SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
                model_output_schema=SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA,
                output_schema=SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA,
                output_transform=finalize_social_content_draft,
            ),
            _binding(
                catalog=catalog,
                scenario=BLOG_CONTENT_REVIEW_SCENARIO,
                model_instance_id=None,
                input_schema_id=BLOG_CONTENT_REVIEW_SCENARIO.input_schema_id,
                input_schema=BLOG_CONTENT_REVIEW_INPUT_SCHEMA,
                model_output_schema_id=BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA_ID,
                model_output_schema=BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA,
                output_schema=BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA,
                output_transform=finalize_blog_content_review,
            ),
            _binding(
                catalog=catalog,
                scenario=COMMUNITY_REMINDER_DRAFT_SCENARIO,
                model_instance_id=None,
                input_schema_id=COMMUNITY_REMINDER_DRAFT_SCENARIO.input_schema_id,
                input_schema=COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA,
                model_output_schema_id=COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
                model_output_schema=COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA,
                output_schema=COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA,
                output_transform=finalize_community_reminder_draft,
            ),
            _binding(
                catalog=catalog,
                scenario=EMAIL_SIGNUP_ONBOARDING_SCENARIO,
                model_instance_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
                input_schema_id=EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
                input_schema=EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA,
                model_output_schema_id=EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA_ID,
                model_output_schema=EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA,
                output_schema=EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA,
                output_transform=finalize_email_signup_onboarding,
            ),
        ),
    )


def build_demo_deterministic_provider(catalog: CompiledCatalog) -> DeterministicLLMProvider:
    """Build the offline provider registered for exactly the trusted demo renderers."""

    if type(catalog) is not CompiledCatalog:
        raise ValueError("deterministic demo provider requires one compiled catalog")
    templates = tuple(
        _catalog_binding(catalog, scenario, model_instance_id=model_instance_id)[0]
        for scenario, model_instance_id in _SUPPORTED_MODEL_BINDINGS
    )
    capability = next(
        (item for item in catalog.tool_capabilities if item.id == "cap.model.generate-structured"),
        None,
    )
    if capability is None:
        raise ValueError("deterministic demo provider binding is unavailable")
    policy_shapes = {
        (
            template.budget_policy.max_input_bytes,
            template.budget_policy.max_input_field_bytes,
            template.budget_policy.max_output_bytes,
            template.budget_policy.max_model_output_tokens,
            template.rate_limit_policy.max_calls,
            template.rate_limit_policy.window_seconds,
            template.timeout_policy.step_seconds,
            template.timeout_policy.run_seconds,
        )
        for template in templates
    }
    if len(policy_shapes) != 1:
        raise ValueError("deterministic demo provider policies must remain compatible")
    template = templates[0]
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
        DeterministicRendererRegistry(
            (
                SOCIAL_CONTENT_DRAFT_RENDERER,
                BLOG_CONTENT_REVIEW_RENDERER,
                COMMUNITY_REMINDER_DRAFT_RENDERER,
                EMAIL_SIGNUP_ONBOARDING_RENDERER,
            )
        ),
        guard,
    )


def build_social_content_draft_deterministic_provider(
    catalog: CompiledCatalog,
) -> DeterministicLLMProvider:
    """Preserve the DEMO-01 provider builder over the exact shared registry."""

    return build_demo_deterministic_provider(catalog)


def build_blog_content_review_deterministic_provider(
    catalog: CompiledCatalog,
) -> DeterministicLLMProvider:
    """Build the Blog compatibility entry point over the exact shared registry."""

    return build_demo_deterministic_provider(catalog)


def build_email_signup_onboarding_deterministic_provider(
    catalog: CompiledCatalog,
) -> DeterministicLLMProvider:
    """Build the Email compatibility entry point over the exact shared registry."""

    return build_demo_deterministic_provider(catalog)


def build_community_reminder_draft_deterministic_provider(
    catalog: CompiledCatalog,
) -> DeterministicLLMProvider:
    """Build the Community compatibility entry point over the exact shared registry."""

    return build_demo_deterministic_provider(catalog)


__all__ = [
    "BlogContentReviewReadAdapter",
    "CommunityReminderDraftReadAdapter",
    "DeterministicDemoReadAdapter",
    "EmailSignupOnboardingReadAdapter",
    "SocialContentDraftReadAdapter",
    "build_blog_content_review_deterministic_provider",
    "build_blog_content_review_read_adapter",
    "build_community_reminder_draft_deterministic_provider",
    "build_community_reminder_draft_read_adapter",
    "build_demo_deterministic_provider",
    "build_demo_read_adapter",
    "build_email_signup_onboarding_deterministic_provider",
    "build_email_signup_onboarding_read_adapter",
    "build_social_content_draft_deterministic_provider",
    "build_social_content_draft_read_adapter",
]
