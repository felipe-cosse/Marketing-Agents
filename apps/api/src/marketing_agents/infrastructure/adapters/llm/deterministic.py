"""Offline deterministic structured-output provider with exact renderer selection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, cast

from pydantic import JsonValue

from marketing_agents.application.policies.runtime_guard import RuntimePolicyGuard
from marketing_agents.application.ports.llm import LLMRequest, LLMResponse, LLMUsage
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.schema_hash import require_schema_hash
from marketing_agents.infrastructure.adapters.llm.validation import (
    LLMRequestPolicyError,
    LLMResponsePolicyError,
    validate_llm_request,
    validate_llm_response,
)

MOCK_PROVIDER_ID = "mock"
MOCK_MODEL_ID = "deterministic"
MOCK_VERSION = "v1"
FIXTURE_KEY_DOMAIN = b"marketing-agents:deterministic-llm:v1\x00"


class DeterministicRendererError(ValueError):
    """Raised when deterministic renderer configuration or output is invalid."""


@dataclass(frozen=True, slots=True, order=True)
class RendererKey:
    """Exact catalog template and output-schema pair; no aliases are resolved."""

    template_id: str
    output_schema_id: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.template_id) <= 200 or self.template_id != self.template_id.strip():
            raise DeterministicRendererError("template ID must be normalized and 1..200 characters")
        if (
            not 1 <= len(self.output_schema_id) <= 240
            or self.output_schema_id != self.output_schema_id.strip()
        ):
            raise DeterministicRendererError(
                "output schema ID must be normalized and 1..240 characters"
            )


@dataclass(frozen=True, slots=True)
class DeterministicRenderContext:
    """Stable material a renderer may use to derive deterministic business fields."""

    fixture_key: str
    provider_version: str
    renderer_version: str


class DeterministicRenderer(Protocol):
    def __call__(
        self,
        request: LLMRequest,
        context: DeterministicRenderContext,
    ) -> dict[str, JsonValue]:
        """Render one JSON object without performing I/O."""
        ...


@dataclass(frozen=True, slots=True)
class RendererRegistration:
    key: RendererKey
    version: str
    output_schema_hash: str
    renderer: DeterministicRenderer

    def __post_init__(self) -> None:
        if not 1 <= len(self.version) <= 100 or self.version != self.version.strip():
            raise DeterministicRendererError(
                "renderer version must be normalized and 1..100 characters"
            )
        try:
            require_schema_hash(self.output_schema_hash, "renderer output schema hash")
        except ValueError as exc:
            raise DeterministicRendererError(
                "renderer output schema hash must be canonical"
            ) from exc


class DeterministicRendererRegistry:
    """Immutable exact-match registry assembled only at the composition root."""

    __slots__ = ("_registrations",)

    def __init__(self, registrations: Iterable[RendererRegistration] = ()) -> None:
        entries: dict[RendererKey, RendererRegistration] = {}
        for registration in registrations:
            if registration.key in entries:
                raise DeterministicRendererError(
                    "duplicate deterministic renderer for "
                    f"{registration.key.template_id!r} and "
                    f"{registration.key.output_schema_id!r}"
                )
            entries[registration.key] = registration
        self._registrations = MappingProxyType(entries)

    @property
    def keys(self) -> tuple[RendererKey, ...]:
        return tuple(sorted(self._registrations))

    def resolve(self, key: RendererKey) -> RendererRegistration:
        try:
            return self._registrations[key]
        except KeyError as exc:
            raise DeterministicRendererError(
                "no deterministic renderer is registered for "
                f"{key.template_id!r} and {key.output_schema_id!r}"
            ) from exc


def _fixture_projection(request: LLMRequest) -> dict[str, JsonValue]:
    """Exclude run metadata so equivalent admitted business input stays stable."""

    return {
        "system_instructions": cast(JsonValue, request.system_instructions.model_dump(mode="json")),
        "retrieved_content": cast(
            JsonValue,
            [item.model_dump(mode="json") for item in request.retrieved_content],
        ),
        "tool_results": cast(
            JsonValue,
            [item.model_dump(mode="json") for item in request.tool_results],
        ),
        "output_schema_id": request.output_schema_id,
        "output_schema_hash": request.output_schema_hash,
        "output_schema": cast(JsonValue, request.output_schema),
    }


def deterministic_fixture_key(request: LLMRequest) -> str:
    """Return a domain-separated key for the normalized admitted business input."""

    return hashlib.sha256(
        FIXTURE_KEY_DOMAIN + canonical_json_bytes(_fixture_projection(request))
    ).hexdigest()


class DeterministicLLMProvider:
    """Credential-free provider that can only execute an explicitly registered renderer."""

    provider_id = MOCK_PROVIDER_ID
    model_id = MOCK_MODEL_ID
    version = MOCK_VERSION

    def __init__(
        self,
        registry: DeterministicRendererRegistry,
        guard: RuntimePolicyGuard,
    ) -> None:
        self._registry = registry
        self._guard = guard

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        preflight = validate_llm_request(self._guard, request)
        canonical_request = preflight.request
        registration = self._registry.resolve(
            RendererKey(
                template_id=canonical_request.system_instructions.template_id,
                output_schema_id=canonical_request.output_schema_id,
            )
        )
        if registration.output_schema_hash != preflight.output_schema_hash:
            raise LLMRequestPolicyError(
                "renderer_schema_hash_mismatch",
                "LLM output schema does not match the registered renderer contract",
            )
        fixture_projection = _fixture_projection(canonical_request)
        fixture_bytes = canonical_json_bytes(fixture_projection)
        context = DeterministicRenderContext(
            fixture_key=hashlib.sha256(FIXTURE_KEY_DOMAIN + fixture_bytes).hexdigest(),
            provider_version=self.version,
            renderer_version=registration.version,
        )
        payload: dict[str, JsonValue] | None = None
        payload_bytes = b""
        renderer_output_invalid = False
        try:
            rendered = registration.renderer(canonical_request, context)
            if type(rendered) is not dict:
                raise TypeError("deterministic renderer returned a non-exact object")
            payload_bytes = canonical_json_bytes(rendered)
            plain_payload = json.loads(payload_bytes)
            if type(plain_payload) is not dict:
                raise TypeError("deterministic renderer returned a non-object")
            payload = cast(dict[str, JsonValue], plain_payload)
        except Exception:
            renderer_output_invalid = True
        if renderer_output_invalid or payload is None:
            raise LLMResponsePolicyError(
                "provider_response_invalid",
                "deterministic renderer returned an invalid structured response",
            )

        output_tokens = max(1, (len(payload_bytes) + 3) // 4)
        if output_tokens > canonical_request.context.max_output_tokens:
            raise LLMResponsePolicyError(
                "output_token_limit",
                "deterministic output exceeds the request token budget",
            )
        response: LLMResponse | None = None
        response_invalid = False
        try:
            response = LLMResponse(
                structured_payload=payload,
                provider=self.provider_id,
                model=self.model_id,
                version=self.version,
                finish_reason="complete",
                usage=LLMUsage(
                    input_tokens=max(1, (len(fixture_bytes) + 3) // 4),
                    output_tokens=output_tokens,
                ),
            )
        except Exception:
            response_invalid = True
        if response_invalid or response is None:
            raise LLMResponsePolicyError(
                "provider_response_invalid",
                "deterministic renderer returned an invalid structured response",
            )
        return validate_llm_response(
            self._guard,
            preflight,
            response,
            expected_provider=self.provider_id,
        )
