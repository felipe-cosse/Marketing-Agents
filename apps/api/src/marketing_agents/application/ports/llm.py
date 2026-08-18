"""Structured LLM port with explicit, non-interchangeable trust classes."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator

from marketing_agents.security.content_trust import UntrustedContentPart


class TrustedSystemInstructions(BaseModel):
    """Catalog-controlled instructions, never constructed from work content."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trust_class: Literal["trusted_system"] = "trusted_system"
    template_id: str = Field(min_length=1, max_length=200)
    catalog_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(min_length=1, max_length=32_768)


class UntrustedToolResult(BaseModel):
    """A read observation that remains untrusted even after a connector returns it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trust_class: Literal["untrusted_tool_result"] = "untrusted_tool_result"
    capability_id: str = Field(min_length=1, max_length=200)
    observation_id: str = Field(min_length=1, max_length=200)
    payload: JsonValue
    provenance_ids: tuple[str, ...] = Field(min_length=1, max_length=64)


class LLMInvocationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(min_length=1, max_length=200)
    deadline: AwareDatetime
    max_output_tokens: int = Field(ge=1, le=32_768)

    @field_validator("deadline")
    @classmethod
    def require_utc_deadline(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("LLM deadline must be UTC")
        return value


class LLMRequest(BaseModel):
    """No messages/tools field exists: trust classes cannot be flattened by callers."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    system_instructions: TrustedSystemInstructions
    retrieved_content: tuple[UntrustedContentPart, ...] = ()
    tool_results: tuple[UntrustedToolResult, ...] = ()
    output_schema_id: str = Field(min_length=1, max_length=240)
    output_schema: dict[str, JsonValue]
    context: LLMInvocationContext


class LLMUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_tokens: int = Field(ge=0, le=1_000_000)
    output_tokens: int = Field(ge=0, le=32_768)


class LLMResponse(BaseModel):
    """Structured data and bounded metadata; never executable tool calls."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    structured_payload: dict[str, JsonValue]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    finish_reason: Literal["complete", "length", "filtered"]
    usage: LLMUsage


class LLMProvider(Protocol):
    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        """Generate one schema-bound response without exposing model tool calling."""
        ...
