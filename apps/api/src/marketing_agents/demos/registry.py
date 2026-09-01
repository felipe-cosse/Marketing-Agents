"""Immutable registry and scenario-specific input resolution."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from marketing_agents.application.policies.json_schema import (
    JsonSchemaPolicyError,
    compile_json_schema,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.validation import frozen_json_mapping
from marketing_agents.security.url_policy import UrlPolicyError, validate_reference_url

from .blog_content_review import (
    BLOG_CONTENT_REVIEW_SCENARIO,
    BLOG_CONTENT_REVIEW_SCENARIO_ID,
)
from .contracts import DemoScenarioDefinition, DemoScenarioInputError, DemoScenarioRegistryError
from .email_signup_onboarding import (
    EMAIL_SIGNUP_ONBOARDING_SCENARIO,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
)
from .social_content_draft import SOCIAL_CONTENT_DRAFT_SCENARIO, SOCIAL_CONTENT_DRAFT_SCENARIO_ID

_RFC3339_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


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
            validated_fixture = _validate_scenario_input(scenario, scenario.fixture)
            if canonical_json_bytes(validated_fixture) != canonical_json_bytes(scenario.fixture):
                raise ValueError("demo fixture must already use canonical input values")
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
    return DemoScenarioRegistry(
        (
            BLOG_CONTENT_REVIEW_SCENARIO,
            EMAIL_SIGNUP_ONBOARDING_SCENARIO,
            SOCIAL_CONTENT_DRAFT_SCENARIO,
        )
    )


def resolve_demo_input(
    scenario_id: str,
    overrides: Mapping[str, Any] | None = None,
    *,
    registry: DemoScenarioRegistry | None = None,
) -> Mapping[str, Any]:
    """Merge bounded overrides onto the trusted preset and validate the exact schema."""

    if registry is None:
        registry = DEMO_SCENARIOS
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
        merged = json.loads(canonical_json_bytes(scenario.fixture))
        merged.update(json.loads(canonical_json_bytes(overrides)))
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
        normalized_payload = json.loads(canonical_json_bytes(payload))
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
    elif scenario.id == BLOG_CONTENT_REVIEW_SCENARIO_ID:
        _normalize_blog_input(normalized_payload)
    elif scenario.id == EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID:
        _normalize_email_signup_input(normalized_payload)
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


def _normalize_blog_input(payload: dict[str, Any]) -> None:
    canonical_url = payload.get("canonical_url")
    if type(canonical_url) is str:
        try:
            payload["canonical_url"] = validate_reference_url(canonical_url).value
        except UrlPolicyError as exc:
            raise DemoScenarioInputError(
                "demo_scenario_invalid",
                str(exc),
                pointer="/canonical_url",
            ) from None

    for field_name in ("last_updated_at", "assessment_at"):
        value = payload.get(field_name)
        if type(value) is str:
            payload[field_name] = _canonical_utc_timestamp(
                value,
                pointer=f"/{field_name}",
            )

    keywords = payload.get("target_keywords")
    if type(keywords) is list:
        seen: set[str] = set()
        for index, keyword in enumerate(keywords):
            if type(keyword) is not str:
                continue
            normalized = " ".join(keyword.split()).casefold()
            if normalized in seen:
                raise DemoScenarioInputError(
                    "demo_scenario_invalid",
                    "target keywords must be unique after case folding",
                    pointer=f"/target_keywords/{index}",
                )
            seen.add(normalized)

    last_updated_at = payload.get("last_updated_at")
    assessment_at = payload.get("assessment_at")
    if type(last_updated_at) is str and type(assessment_at) is str:
        last_updated = _parse_canonical_utc(last_updated_at, pointer="/last_updated_at")
        assessment = _parse_canonical_utc(assessment_at, pointer="/assessment_at")
        if last_updated > assessment:
            raise DemoScenarioInputError(
                "demo_scenario_invalid",
                "last-updated timestamp cannot be after the assessment timestamp",
                pointer="/last_updated_at",
            )


def _normalize_email_signup_input(payload: dict[str, Any]) -> None:
    email = payload.get("email")
    if type(email) is str and "@" in email:
        local_part, domain = email.rsplit("@", 1)
        normalized_domain = domain.casefold()
        payload["email"] = f"{local_part}@{normalized_domain}"
        if not normalized_domain.endswith(".test"):
            raise DemoScenarioInputError(
                "demo_scenario_invalid",
                "Email demo addresses must use the reserved .test domain",
                pointer="/email",
            )

    consent = payload.get("consent")
    captured_at: str | None = None
    if type(consent) is dict:
        raw_captured_at = consent.get("captured_at")
        if type(raw_captured_at) is str:
            captured_at = _canonical_utc_timestamp(
                raw_captured_at,
                pointer="/consent/captured_at",
            )
            consent["captured_at"] = captured_at

    signup_at = payload.get("signup_at")
    if type(signup_at) is str:
        signup_at = _canonical_utc_timestamp(signup_at, pointer="/signup_at")
        payload["signup_at"] = signup_at

    if captured_at is not None and type(signup_at) is str:
        captured = _parse_canonical_utc(captured_at, pointer="/consent/captured_at")
        signup = _parse_canonical_utc(signup_at, pointer="/signup_at")
        if captured > signup:
            raise DemoScenarioInputError(
                "demo_scenario_invalid",
                "Email consent cannot be captured after signup",
                pointer="/consent/captured_at",
            )


def _canonical_utc_timestamp(value: str, *, pointer: str) -> str:
    if _RFC3339_TIMESTAMP.fullmatch(value) is None:
        raise DemoScenarioInputError(
            "demo_scenario_invalid",
            "demo timestamp must be an RFC3339 date-time",
            pointer=pointer,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DemoScenarioInputError(
            "demo_scenario_invalid",
            "demo timestamp must be an RFC3339 date-time",
            pointer=pointer,
        ) from None
    offset = parsed.utcoffset()
    if offset is None:
        raise DemoScenarioInputError(
            "demo_scenario_invalid",
            "demo timestamp must include a UTC offset",
            pointer=pointer,
        )
    try:
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        raise DemoScenarioInputError(
            "demo_scenario_invalid",
            "demo timestamp cannot be represented in UTC",
            pointer=pointer,
        ) from None


def _parse_canonical_utc(value: str, *, pointer: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise DemoScenarioInputError(
            "demo_scenario_invalid",
            "demo timestamp must be an RFC3339 date-time",
            pointer=pointer,
        ) from None
    return parsed


DEMO_SCENARIOS = build_demo_scenario_registry()


__all__ = [
    "DEMO_SCENARIOS",
    "DemoScenarioRegistry",
    "build_demo_scenario_registry",
    "resolve_demo_input",
]
