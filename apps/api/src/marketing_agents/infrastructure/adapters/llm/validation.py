"""Fail-closed request and response validation for every structured LLM adapter."""

from __future__ import annotations

from dataclasses import dataclass, field

from marketing_agents.application.policies.json_schema import (
    CompiledJsonSchema,
    compile_json_schema,
)
from marketing_agents.application.policies.runtime_guard import RuntimePolicyGuard
from marketing_agents.application.ports.llm import LLMRequest, LLMResponse
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.schema_hash import canonical_schema_hash


class LLMRequestPolicyError(ValueError):
    """Raised before an untrusted renderer or provider can observe an invalid request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LLMResponsePolicyError(ValueError):
    """Raised when a provider violates request-scoped response bounds."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LLMRequestPreflight:
    """Canonical request plus an independent trusted output-schema snapshot."""

    _request_bytes: bytes = field(repr=False)
    compiled_schema: CompiledJsonSchema
    output_schema_hash: str

    @property
    def request(self) -> LLMRequest:
        """Return a fresh canonical copy so a delegate cannot mutate the trusted snapshot."""

        return LLMRequest.model_validate_json(self._request_bytes, strict=True)


def _canonical_request(request: LLMRequest) -> tuple[LLMRequest, bytes]:
    if type(request) is not LLMRequest:
        raise LLMRequestPolicyError(
            "request_invalid",
            "LLM request must be an exact validated LLMRequest",
        )
    try:
        encoded = canonical_json_bytes(request.model_dump(mode="json", warnings="error"))
        return LLMRequest.model_validate_json(encoded, strict=True), encoded
    except Exception:
        pass
    raise LLMRequestPolicyError(
        "request_invalid",
        "LLM request is not canonical validated data",
    )


def validate_llm_request(
    guard: RuntimePolicyGuard,
    request: LLMRequest,
) -> LLMRequestPreflight:
    """Canonicalize and bind the exact request schema before any provider call."""

    canonical_request, request_bytes = _canonical_request(request)
    if canonical_request.output_schema_id != canonical_request.output_schema_id.strip():
        raise LLMRequestPolicyError(
            "request_invalid",
            "LLM output schema identity must be normalized",
        )

    compiled_schema = compile_json_schema(
        canonical_request.output_schema,
        expected_schema_id=canonical_request.output_schema_id,
    )
    actual_hash = canonical_schema_hash(compiled_schema.schema)
    if actual_hash != canonical_request.output_schema_hash:
        raise LLMRequestPolicyError(
            "schema_hash_mismatch",
            "LLM output schema does not match its declared hash",
        )

    guard.validate_content(canonical_request.retrieved_content)
    return LLMRequestPreflight(
        _request_bytes=request_bytes,
        compiled_schema=compiled_schema,
        output_schema_hash=actual_hash,
    )


def _canonical_response(response: LLMResponse) -> LLMResponse:
    if type(response) is not LLMResponse:
        raise LLMResponsePolicyError(
            "provider_response_invalid",
            "provider returned an invalid structured response",
        )
    try:
        encoded = canonical_json_bytes(response.model_dump(mode="json", warnings="error"))
        return LLMResponse.model_validate_json(encoded, strict=True)
    except Exception:
        pass
    raise LLMResponsePolicyError(
        "provider_response_invalid",
        "provider returned an invalid structured response",
    )


def validate_llm_response(
    guard: RuntimePolicyGuard,
    preflight: LLMRequestPreflight,
    response: LLMResponse,
    *,
    expected_provider: str,
) -> LLMResponse:
    """Reconstruct, then independently validate completion, schema, bounds, and identity."""

    canonical_response = _canonical_response(response)
    if canonical_response.finish_reason != "complete":
        raise LLMResponsePolicyError(
            "provider_response_incomplete",
            "provider did not return a complete structured response",
        )

    guard.validate_output(
        canonical_response.structured_payload,
        preflight.compiled_schema.schema,
    )
    if canonical_response.usage.output_tokens > preflight.request.context.max_output_tokens:
        raise LLMResponsePolicyError(
            "output_token_limit",
            "provider output exceeds the request token budget",
        )
    if canonical_response.provider != expected_provider:
        raise LLMResponsePolicyError(
            "provider_identity_mismatch",
            "provider response identity does not match the selected adapter",
        )
    return canonical_response
