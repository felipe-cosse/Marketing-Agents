"""Shared postconditions for every structured LLM implementation."""

from __future__ import annotations

from marketing_agents.application.policies.runtime_guard import RuntimePolicyGuard
from marketing_agents.application.ports.llm import LLMRequest, LLMResponse


class LLMResponsePolicyError(ValueError):
    """Raised when a provider violates request-scoped response bounds."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_llm_request(guard: RuntimePolicyGuard, request: LLMRequest) -> None:
    """Apply the central content policy before any provider is invoked."""

    guard.validate_content(request.retrieved_content)


def validate_llm_response(
    guard: RuntimePolicyGuard,
    request: LLMRequest,
    response: LLMResponse,
    *,
    expected_provider: str,
) -> LLMResponse:
    """Independently validate schema, global bounds, token budget, and identity."""

    guard.validate_output(response.structured_payload, request.output_schema)
    if response.usage.output_tokens > request.context.max_output_tokens:
        raise LLMResponsePolicyError(
            "output_token_limit",
            "provider output exceeds the request token budget",
        )
    if response.provider != expected_provider:
        raise LLMResponsePolicyError(
            "provider_identity_mismatch",
            "provider response identity does not match the selected adapter",
        )
    return response
