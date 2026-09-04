"""Fail-closed adapter registry bootstrap for the local safe profile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AdapterModeSettings(Protocol):
    """Narrow settings view used at the adapter composition boundary."""

    llm_provider: str
    connector_mode: str
    allow_external_network: bool


class AdapterRegistryError(ValueError):
    """Raised when configured adapters are not explicitly registered."""


@dataclass(frozen=True)
class RuntimeSafetyStatus:
    """Secret-free status safe to expose through readiness and the web shell."""

    execution_mode: str
    llm_provider: str
    connector_mode: str
    external_network: bool
    external_side_effects: bool
    banner: str

    @property
    def safe_default(self) -> bool:
        return (
            self.execution_mode == "dry_run"
            and self.llm_provider == "mock"
            and self.connector_mode == "mock"
            and not self.external_network
            and not self.external_side_effects
        )


@dataclass(frozen=True)
class AdapterRegistry:
    """Names the deliberately installed provider and connector implementations."""

    llm_provider_id: str
    connector_bundle_id: str
    status: RuntimeSafetyStatus


def build_local_adapter_registry(settings: AdapterModeSettings) -> AdapterRegistry:
    """Build the credential-free registry; unknown or real modes never fall back."""

    if settings.llm_provider != "mock":
        raise AdapterRegistryError(
            f"LLM provider {settings.llm_provider!r} is not registered in the local profile"
        )
    if settings.connector_mode != "mock":
        raise AdapterRegistryError(
            f"connector mode {settings.connector_mode!r} is not registered in the local profile"
        )
    if settings.allow_external_network:
        raise AdapterRegistryError(
            "the local mock registry requires external network to be disabled"
        )

    status = RuntimeSafetyStatus(
        execution_mode="dry_run",
        llm_provider="mock",
        connector_mode="mock",
        external_network=False,
        external_side_effects=False,
        banner="Dry run · deterministic mocks · no external calls",
    )
    return AdapterRegistry(
        llm_provider_id="mock.deterministic.v1",
        connector_bundle_id="mock.connectors.v1",
        status=status,
    )
