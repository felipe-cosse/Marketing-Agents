"""Structured LLM adapters and their explicit composition registry."""

from marketing_agents.infrastructure.adapters.llm.deterministic import (
    DeterministicLLMProvider,
    DeterministicRenderContext,
    DeterministicRenderer,
    DeterministicRendererRegistry,
    RendererKey,
    RendererRegistration,
)
from marketing_agents.infrastructure.adapters.llm.factory import (
    LLMProviderConfigurationError,
    LLMProviderSettings,
    RealLLMProviderFactory,
    RealLLMProviderRegistry,
    RealProviderRegistration,
    ValidatingRealLLMProvider,
    build_llm_provider,
)

__all__ = [
    "DeterministicLLMProvider",
    "DeterministicRenderContext",
    "DeterministicRenderer",
    "DeterministicRendererRegistry",
    "LLMProviderConfigurationError",
    "LLMProviderSettings",
    "RealLLMProviderFactory",
    "RealLLMProviderRegistry",
    "RealProviderRegistration",
    "RendererKey",
    "RendererRegistration",
    "ValidatingRealLLMProvider",
    "build_llm_provider",
]
