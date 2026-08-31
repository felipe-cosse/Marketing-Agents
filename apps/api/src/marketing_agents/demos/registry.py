"""Immutable registry and scenario-specific input resolution."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from marketing_agents.application.policies.json_schema import (
    JsonSchemaPolicyError,
    compile_json_schema,
)
from marketing_agents.domain.validation import frozen_json_mapping
from marketing_agents.security.url_policy import UrlPolicyError, validate_reference_url

from .contracts import DemoScenarioDefinition, DemoScenarioInputError, DemoScenarioRegistryError
from .social_content_draft import SOCIAL_CONTENT_DRAFT_SCENARIO, SOCIAL_CONTENT_DRAFT_SCENARIO_ID


class DemoScenarioRegistry:
    """Exact-match immutable registry; unknown aliases fail closed."""

    __slots__ = ("_scenarios",)

    def __init__(self, scenarios: Iterable[DemoScenarioDefinition]) -> None:
        indexed: dict[str, DemoScenarioDefinition] = {}
        for scenario in scenarios:
            if type(scenario) is not DemoScenarioDefinition:
                raise ValueError("demo registry requires exact scenario definitions")
            if scenario.id in indexed:
                raise ValueError("demo scenario IDs must be unique")
            input_contract = compile_json_schema(
                scenario.input_schema,
                expected_schema_id=scenario.input_schema_id,
            )
            input_contract.validate(scenario.fixture, pointer_root="/fixture", max_depth=16)
            compile_json_schema(
                scenario.output_schema,
                expected_schema_id=scenario.output_schema_id,
            )
            indexed[scenario.id] = scenario
        if not indexed:
            raise ValueError("demo registry cannot be empty")
        self._scenarios = MappingProxyType(indexed)

    def list(self) -> tuple[DemoScenarioDefinition, ...]:
        return tuple(self._scenarios[key] for key in sorted(self._scenarios))

    def get(self, scenario_id: str) -> DemoScenarioDefinition:
        if type(scenario_id) is not str:
            raise DemoScenarioRegistryError(
                "demo_scenario_unknown", "demo scenario is not registered"
            )
        try:
            return self._scenarios[scenario_id]
        except KeyError:
            raise DemoScenarioRegistryError(
                "demo_scenario_unknown", "demo scenario is not registered"
            ) from None

    def resolve_input(
        self,
        scenario_id: str,
        overrides: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Resolve against this exact injected registry, never a module global."""

        return resolve_demo_input(scenario_id, overrides, registry=self)

    def validate_input(
        self,
        scenario_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Validate one complete payload without applying preset defaults."""

        return _validate_scenario_input(self.get(scenario_id), payload)


def build_demo_scenario_registry() -> DemoScenarioRegistry:
    return DemoScenarioRegistry((SOCIAL_CONTENT_DRAFT_SCENARIO,))


DEMO_SCENARIOS = build_demo_scenario_registry()


def resolve_demo_input(
    scenario_id: str,
    overrides: Mapping[str, Any] | None = None,
    *,
    registry: DemoScenarioRegistry = DEMO_SCENARIOS,
) -> Mapping[str, Any]:
    """Merge bounded overrides onto the trusted preset and validate the exact schema."""

    scenario = registry.get(scenario_id)
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, Mapping):
        raise DemoScenarioInputError(
            "demo_scenario_invalid", "demo input overrides must be an object", pointer="/"
        )
    properties = scenario.input_schema.get("properties")
    allowed_keys = set(properties) if isinstance(properties, Mapping) else set()
    unknown = sorted(set(overrides) - allowed_keys)
    if unknown:
        raise DemoScenarioInputError(
            "demo_scenario_invalid",
            "demo input contains an unknown field",
            pointer=f"/{unknown[0]}",
        )
    try:
        merged = json.loads(json.dumps(dict(scenario.fixture)))
        merged.update(json.loads(json.dumps(dict(overrides))))
    except (TypeError, ValueError):
        raise DemoScenarioInputError(
            "demo_scenario_invalid", "demo input must be canonical JSON", pointer="/"
        ) from None
    return _validate_scenario_input(scenario, merged)


def _validate_scenario_input(
    scenario: DemoScenarioDefinition,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        normalized_payload = json.loads(json.dumps(dict(payload)))
    except (TypeError, ValueError):
        raise DemoScenarioInputError(
            "demo_scenario_invalid", "demo input must be canonical JSON", pointer="/"
        ) from None

    if scenario.id == SOCIAL_CONTENT_DRAFT_SCENARIO_ID:
        urls = normalized_payload.get("source_urls")
        if type(urls) is list:
            normalized: list[str] = []
            for index, value in enumerate(urls):
                if type(value) is not str:
                    continue
                try:
                    normalized.append(validate_reference_url(value).value)
                except UrlPolicyError as exc:
                    raise DemoScenarioInputError(
                        "demo_scenario_invalid",
                        str(exc),
                        pointer=f"/source_urls/{index}",
                    ) from None
            if len(normalized) == len(urls):
                normalized_payload["source_urls"] = normalized
    try:
        compiled = compile_json_schema(
            scenario.input_schema,
            expected_schema_id=scenario.input_schema_id,
        )
        compiled.validate(normalized_payload, pointer_root="/input", max_depth=16)
    except JsonSchemaPolicyError as exc:
        pointer = getattr(exc, "pointer", None)
        if pointer == "/input":
            pointer = "/"
        elif isinstance(pointer, str) and pointer.startswith("/input/"):
            pointer = pointer.removeprefix("/input")
        raise DemoScenarioInputError(
            "demo_scenario_invalid",
            "demo input does not match the scenario schema",
            pointer=pointer or "/",
        ) from None
    return frozen_json_mapping(normalized_payload, "validated demo input")


__all__ = [
    "DEMO_SCENARIOS",
    "DemoScenarioRegistry",
    "build_demo_scenario_registry",
    "resolve_demo_input",
]
