"""Explicit LLM composition: deterministic by default, exact real factories on opt-in."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from marketing_agents.application.policies.runtime_guard import RuntimePolicyGuard
from marketing_agents.application.ports.llm import LLMProvider, LLMRequest, LLMResponse
from marketing_agents.infrastructure.adapters.llm.deterministic import (
    DeterministicLLMProvider,
    DeterministicRendererRegistry,
)
from marketing_agents.infrastructure.adapters.llm.validation import (
    validate_llm_request,
    validate_llm_response,
)


class LLMProviderConfigurationError(ValueError):
    """Raised when selected provider configuration cannot be honored exactly."""


class LLMProviderSettings(Protocol):
    """Read-only settings projection needed by the LLM composition boundary."""

    @property
    def llm_provider(self) -> str: ...

    @property
    def allow_external_network(self) -> bool: ...

    @property
    def real_llm_opt_in(self) -> bool: ...

    @property
    def real_llm_api_key(self) -> object | None: ...


class RealLLMProviderFactory(Protocol):
    def __call__(self, settings: LLMProviderSettings) -> LLMProvider:
        """Build the exact selected provider; exceptions propagate without fallback."""
        ...


@dataclass(frozen=True, slots=True)
class RealProviderRegistration:
    provider_id: str
    factory: RealLLMProviderFactory

    def __post_init__(self) -> None:
        if (
            self.provider_id == "mock"
            or not 1 <= len(self.provider_id) <= 100
            or self.provider_id != self.provider_id.strip()
        ):
            raise LLMProviderConfigurationError(
                "real provider ID must be normalized, non-mock, and 1..100 characters"
            )


class RealLLMProviderRegistry:
    """Immutable case-sensitive provider-to-factory mapping."""

    __slots__ = ("_factories",)

    def __init__(self, registrations: Iterable[RealProviderRegistration] = ()) -> None:
        factories: dict[str, RealLLMProviderFactory] = {}
        for registration in registrations:
            if registration.provider_id in factories:
                raise LLMProviderConfigurationError(
                    f"duplicate real LLM provider {registration.provider_id!r}"
                )
            factories[registration.provider_id] = registration.factory
        self._factories = MappingProxyType(factories)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def build(self, provider_id: str, settings: LLMProviderSettings) -> LLMProvider:
        try:
            factory = self._factories[provider_id]
        except KeyError as exc:
            raise LLMProviderConfigurationError(
                f"real LLM provider {provider_id!r} is not explicitly registered"
            ) from exc
        provider = factory(settings)
        if not callable(getattr(provider, "generate_structured", None)):
            raise LLMProviderConfigurationError(
                f"factory for {provider_id!r} did not return an LLM provider"
            )
        return provider


class ValidatingRealLLMProvider:
    """Independent schema/bounds enforcement around an explicitly selected adapter."""

    def __init__(
        self,
        delegate: LLMProvider,
        guard: RuntimePolicyGuard,
        *,
        expected_provider: str,
    ) -> None:
        self._delegate = delegate
        self._guard = guard
        self._expected_provider = expected_provider

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        preflight = validate_llm_request(self._guard, request)
        response = await self._delegate.generate_structured(preflight.request)
        return validate_llm_response(
            self._guard,
            preflight,
            response,
            expected_provider=self._expected_provider,
        )


def _has_credential(value: object | None) -> bool:
    if value is None:
        return False
    reveal = getattr(value, "get_secret_value", None)
    secret = reveal() if callable(reveal) else value
    return isinstance(secret, str) and bool(secret.strip())


def build_llm_provider(
    settings: LLMProviderSettings,
    *,
    renderer_registry: DeterministicRendererRegistry,
    guard: RuntimePolicyGuard,
    real_registry: RealLLMProviderRegistry | None = None,
) -> LLMProvider:
    """Build only the configured provider; no error path crosses into another mode."""

    if settings.llm_provider == "mock":
        if settings.allow_external_network or settings.real_llm_opt_in:
            raise LLMProviderConfigurationError(
                "mock provider cannot be composed with real-provider network opt-ins"
            )
        return DeterministicLLMProvider(renderer_registry, guard)

    if not settings.allow_external_network:
        raise LLMProviderConfigurationError(
            "real LLM provider requires the external-network opt-in"
        )
    if not settings.real_llm_opt_in:
        raise LLMProviderConfigurationError("real LLM provider requires its independent opt-in")
    if not _has_credential(settings.real_llm_api_key):
        raise LLMProviderConfigurationError("real LLM provider requires a non-empty credential")
    if real_registry is None:
        raise LLMProviderConfigurationError(
            f"real LLM provider {settings.llm_provider!r} is not explicitly registered"
        )

    delegate = real_registry.build(settings.llm_provider, settings)
    return ValidatingRealLLMProvider(
        delegate,
        guard,
        expected_provider=settings.llm_provider,
    )
