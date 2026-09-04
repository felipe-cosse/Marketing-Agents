"""SAFE-01: safe startup defaults to dry-run mock adapters."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from marketing_agents.config import Settings
from marketing_agents.infrastructure.adapters.safe_profile import (
    AdapterRegistryError,
    build_local_adapter_registry,
)


def test_safe_01_default_session_is_explicitly_dry_run_and_mock_only() -> None:
    registry = build_local_adapter_registry(Settings(_env_file=None))

    assert registry.llm_provider_id == "mock.deterministic.v1"
    assert registry.connector_bundle_id == "mock.connectors.v1"
    assert registry.status.safe_default
    assert registry.status.banner == "Dry run · deterministic mocks · no external calls"
    assert not registry.status.external_network
    assert not registry.status.external_side_effects
    with pytest.raises(FrozenInstanceError):
        registry.status.external_network = True  # type: ignore[misc]


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            Settings(
                _env_file=None,
                llm_provider="real",
                allow_external_network=True,
                real_llm_opt_in=True,
                real_llm_api_key="test-only-value",
            ),
            "LLM provider",
        ),
        (
            Settings(
                _env_file=None,
                connector_mode="real",
                allow_external_network=True,
                real_connector_opt_in=True,
            ),
            "connector mode",
        ),
    ],
)
def test_safe_01_unregistered_real_modes_fail_without_mock_fallback(
    settings: Settings, message: str
) -> None:
    with pytest.raises(AdapterRegistryError, match=message):
        build_local_adapter_registry(settings)
