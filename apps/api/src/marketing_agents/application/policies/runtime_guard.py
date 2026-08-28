"""Central schema, allowlist, budget, deadline, and content-size guard."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from marketing_agents.application.policies.json_schema import (
    JsonSchemaPolicyError,
    compile_json_schema,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.runtime_policy import payload_fields_within_byte_limit
from marketing_agents.security.content_trust import UntrustedContentPart


class RuntimePolicyViolation(ValueError):
    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.pointer = pointer


class CapabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capability_id: str = Field(min_length=1, max_length=200)
    effect: Literal["read", "write"]
    connector_family: str = Field(min_length=1, max_length=100)


class RuntimePolicySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    allowed_capabilities: tuple[CapabilityPolicy, ...] = Field(min_length=1, max_length=100)
    input_max_bytes: int = Field(ge=1, le=1_048_576)
    max_input_field_bytes: int = Field(default=262_144, ge=1, le=262_144)
    output_max_bytes: int = Field(ge=1, le=1_048_576)
    max_json_depth: int = Field(ge=1, le=64)
    max_content_parts: int = Field(ge=1, le=256)
    max_content_characters: int = Field(ge=1, le=1_000_000)
    max_model_calls: int = Field(ge=0, le=100)
    max_tool_calls: int = Field(ge=0, le=1_000)
    rate_window_max_calls: int = Field(ge=1, le=10_000)
    rate_window_seconds: int = Field(ge=1, le=86_400)
    step_timeout_seconds: int = Field(ge=1, le=600)
    run_timeout_seconds: int = Field(ge=1, le=3_600)

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        ids = [item.capability_id for item in self.allowed_capabilities]
        if len(ids) != len(set(ids)):
            raise ValueError("allowed capability IDs must be unique")
        if self.step_timeout_seconds > self.run_timeout_seconds:
            raise ValueError("step timeout cannot exceed run timeout")
        return self


class RuntimeUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    rate_window_calls: int = Field(ge=0)


class AttemptContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    now: AwareDatetime
    deadline: AwareDatetime
    requested_timeout_seconds: int = Field(ge=1, le=3_600)

    @model_validator(mode="after")
    def validate_utc(self) -> Self:
        for field_name, value in (("now", self.now), ("deadline", self.deadline)):
            offset = value.utcoffset()
            if offset is None or offset.total_seconds() != 0:
                raise ValueError(f"{field_name} must be UTC")
        return self


class RuntimePolicyGuard:
    def __init__(self, policy: RuntimePolicySnapshot) -> None:
        self._policy = policy
        self._capabilities = {item.capability_id: item for item in policy.allowed_capabilities}

    def validate_input(self, payload: Any, schema: Mapping[str, Any]) -> None:
        self._validate_json(payload, schema, self._policy.input_max_bytes, "input")

    def validate_output(self, payload: Any, schema: Mapping[str, Any]) -> None:
        self._validate_json(payload, schema, self._policy.output_max_bytes, "output")

    def validate_content(self, parts: Sequence[UntrustedContentPart]) -> None:
        if len(parts) > self._policy.max_content_parts:
            raise RuntimePolicyViolation("content_part_limit", "too many untrusted content parts")
        total_characters = sum(len(part.content) for part in parts)
        if total_characters > self._policy.max_content_characters:
            raise RuntimePolicyViolation(
                "content_character_limit", "untrusted content character limit exceeded"
            )

    def authorize_attempt(
        self,
        capability_id: str,
        expected_effect: Literal["read", "write"],
        attempt_kind: Literal["model", "tool"],
        usage: RuntimeUsage,
        context: AttemptContext,
    ) -> CapabilityPolicy:
        capability = self._capabilities.get(capability_id)
        if capability is None:
            raise RuntimePolicyViolation("capability_not_allowed", "capability is not allowlisted")
        if capability.effect != expected_effect:
            raise RuntimePolicyViolation("capability_effect_mismatch", "capability effect changed")
        if attempt_kind == "model" and usage.model_calls >= self._policy.max_model_calls:
            raise RuntimePolicyViolation("model_budget_exhausted", "model call budget exhausted")
        if attempt_kind == "tool" and usage.tool_calls >= self._policy.max_tool_calls:
            raise RuntimePolicyViolation("tool_budget_exhausted", "tool call budget exhausted")
        if usage.rate_window_calls >= self._policy.rate_window_max_calls:
            raise RuntimePolicyViolation("rate_limit_exhausted", "rate window exhausted")
        remaining = (context.deadline - context.now).total_seconds()
        if remaining <= 0:
            raise RuntimePolicyViolation("deadline_exceeded", "run deadline has expired")
        allowed_timeout = min(float(self._policy.step_timeout_seconds), remaining)
        if context.requested_timeout_seconds > allowed_timeout:
            raise RuntimePolicyViolation(
                "timeout_out_of_bounds", "attempt timeout exceeds step or run deadline"
            )
        return capability

    def _validate_json(
        self,
        payload: Any,
        schema: Mapping[str, Any],
        max_bytes: int,
        direction: str,
    ) -> None:
        encoded = b""
        plain_payload: Any = None
        invalid_json = False
        try:
            encoded = canonical_json_bytes(payload)
            plain_payload = json.loads(encoded)
        except Exception:
            invalid_json = True
        if invalid_json:
            raise RuntimePolicyViolation("invalid_json", "payload is not strict JSON")
        if len(encoded) > max_bytes:
            raise RuntimePolicyViolation(
                f"{direction}_byte_limit",
                "payload byte limit exceeded",
                pointer=f"/{direction}",
            )
        if direction == "input" and not payload_fields_within_byte_limit(
            plain_payload,
            self._policy.max_input_field_bytes,
        ):
            raise RuntimePolicyViolation(
                "input_field_too_large",
                "input payload field exceeds its byte limit",
                pointer="/input",
            )
        pointer_root = f"/{direction}"
        policy_failure: tuple[str, str, str] | None = None
        try:
            compiled_schema = compile_json_schema(schema)
            compiled_schema.validate(
                plain_payload,
                pointer_root=pointer_root,
                max_depth=self._policy.max_json_depth,
            )
        except JsonSchemaPolicyError as exc:
            if exc.code == "json_depth_limit":
                policy_failure = (
                    "json_depth_limit",
                    "payload nesting limit exceeded",
                    exc.pointer or pointer_root,
                )
            else:
                message = (
                    "payload does not conform to its schema"
                    if exc.code == "instance_invalid"
                    else "payload schema is invalid"
                )
                policy_failure = (
                    f"{direction}_schema_invalid",
                    message,
                    exc.pointer or pointer_root,
                )
        if policy_failure is not None:
            raise RuntimePolicyViolation(
                policy_failure[0],
                policy_failure[1],
                pointer=policy_failure[2],
            )
