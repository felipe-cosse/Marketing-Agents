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
from marketing_agents.infrastructure.adapters.llm.read_adapter import (
    LLMReadBinding,
    StructuredLLMReadAdapter,
)

__all__ = [
    "DeterministicLLMProvider",
    "DeterministicRenderContext",
    "DeterministicRenderer",
    "DeterministicRendererRegistry",
    "LLMProviderConfigurationError",
    "LLMProviderSettings",
    "LLMReadBinding",
    "RealLLMProviderFactory",
    "RealLLMProviderRegistry",
    "RealProviderRegistration",
    "RendererKey",
    "RendererRegistration",
    "StructuredLLMReadAdapter",
    "ValidatingRealLLMProvider",
    "build_llm_provider",
]
