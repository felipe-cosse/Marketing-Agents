"""SAFE-06: schemas, allowlists, URLs, content, rates, deadlines, and sizes are bounded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.application.policies.runtime_guard import (
    AttemptContext,
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
    RuntimePolicyViolation,
    RuntimeUsage,
)
from marketing_agents.security.content_trust import ExternalContentKind, UntrustedContentPart
from marketing_agents.security.url_policy import UrlPolicyError, validate_reference_url


def _policy(**updates: object) -> RuntimePolicySnapshot:
    values: dict[str, object] = {
        "allowed_capabilities": (
            CapabilityPolicy(
                capability_id="social.read_posts", effect="read", connector_family="social"
            ),
        ),
        "input_max_bytes": 32,
        "output_max_bytes": 32,
        "max_json_depth": 3,
        "max_content_parts": 2,
        "max_content_characters": 10,
        "max_model_calls": 2,
        "max_tool_calls": 2,
        "rate_window_max_calls": 2,
        "rate_window_seconds": 60,
        "step_timeout_seconds": 10,
        "run_timeout_seconds": 60,
    }
    values.update(updates)
    return RuntimePolicySnapshot.model_validate(values)


def _part(content: str, number: int = 1) -> UntrustedContentPart:
    return UntrustedContentPart(
        kind=ExternalContentKind.USER_INPUT,
        source_id=f"input:{number}",
        content=content,
        provenance_ids=(f"input:{number}",),
    )


def test_safe_06_exact_schema_size_depth_and_content_limits_pass() -> None:
    guard = RuntimePolicyGuard(_policy())
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    guard.validate_input({"a": "1234567890"}, schema)
    guard.validate_output({"a": "1234567890"}, schema)
    guard.validate_content((_part("12345", 1), _part("67890", 2)))


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        (lambda guard: guard.validate_input({"a": "x" * 40}, {}), "input_byte_limit"),
        (lambda guard: guard.validate_output({"a": "x" * 40}, {}), "output_byte_limit"),
        (lambda guard: guard.validate_input({"a": {"b": {"c": 1}}}, {}), "json_depth_limit"),
        (lambda guard: guard.validate_input({"a": float("nan")}, {}), "invalid_json"),
        (lambda guard: guard.validate_input({"a": 1}, {"type": "array"}), "input_schema_invalid"),
        (
            lambda guard: guard.validate_content((_part("a", 1), _part("b", 2), _part("c", 3))),
            "content_part_limit",
        ),
        (lambda guard: guard.validate_content((_part("12345678901"),)), "content_character_limit"),
    ],
)
def test_safe_06_limit_plus_one_and_invalid_schema_payloads_fail(
    operation: object, code: str
) -> None:
    with pytest.raises(RuntimePolicyViolation) as captured:
        operation(RuntimePolicyGuard(_policy()))  # type: ignore[operator]
    assert captured.value.code == code


def test_safe_06_attempts_require_allowlist_effect_budget_rate_and_deadline() -> None:
    guard = RuntimePolicyGuard(_policy())
    now = datetime.now(UTC)
    context = AttemptContext(
        now=now,
        deadline=now + timedelta(seconds=10),
        requested_timeout_seconds=10,
    )
    usage = RuntimeUsage(model_calls=0, tool_calls=0, rate_window_calls=0)
    allowed = guard.authorize_attempt("social.read_posts", "read", "tool", usage, context)
    assert allowed.connector_family == "social"

    cases = [
        ("missing", "read", "tool", usage, context, "capability_not_allowed"),
        (
            "social.read_posts",
            "write",
            "tool",
            usage,
            context,
            "capability_effect_mismatch",
        ),
        (
            "social.read_posts",
            "read",
            "model",
            RuntimeUsage(model_calls=2, tool_calls=0, rate_window_calls=0),
            context,
            "model_budget_exhausted",
        ),
        (
            "social.read_posts",
            "read",
            "tool",
            RuntimeUsage(model_calls=0, tool_calls=2, rate_window_calls=0),
            context,
            "tool_budget_exhausted",
        ),
        (
            "social.read_posts",
            "read",
            "tool",
            RuntimeUsage(model_calls=0, tool_calls=0, rate_window_calls=2),
            context,
            "rate_limit_exhausted",
        ),
        (
            "social.read_posts",
            "read",
            "tool",
            usage,
            AttemptContext(now=now, deadline=now, requested_timeout_seconds=1),
            "deadline_exceeded",
        ),
        (
            "social.read_posts",
            "read",
            "tool",
            usage,
            AttemptContext(
                now=now, deadline=now + timedelta(seconds=5), requested_timeout_seconds=6
            ),
            "timeout_out_of_bounds",
        ),
    ]
    for capability_id, effect, attempt_kind, case_usage, case_context, code in cases:
        with pytest.raises(RuntimePolicyViolation) as captured:
            guard.authorize_attempt(  # type: ignore[arg-type]
                capability_id, effect, attempt_kind, case_usage, case_context
            )
        assert captured.value.code == code


def test_safe_06_reference_urls_are_https_allowlisted_and_provenance_only() -> None:
    reference = validate_reference_url(
        "https://example.com/source?id=1", allowed_hosts=frozenset({"example.com"})
    )
    assert reference.value == "https://example.com/source?id=1"
    assert reference.provenance_only
    assert not hasattr(reference, "fetch")

    unsafe = [
        "http://example.com/source",
        "file:///etc/passwd",
        "https://user:pass@example.com/source",
        "https://example.com/source#fragment",
        "https://127.0.0.1/source",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/source",
        "https://example.com:8443/source",
        "https://not-allowed.example/source",
    ]
    for value in unsafe:
        with pytest.raises(UrlPolicyError):
            validate_reference_url(value, allowed_hosts=frozenset({"example.com"}))
