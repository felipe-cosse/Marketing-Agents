"""DOM-04: every template has a complete bounded runtime authority policy."""

from __future__ import annotations

from pathlib import Path

from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import (
    AgentTemplateRecord,
    RetryPolicy,
    TimeoutPolicy,
)
from marketing_agents.infrastructure.catalog.semantics import template_runtime_policy_issues

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"


def _codes(templates: list[AgentTemplateRecord]) -> set[str]:
    compiled = compile_catalog(CATALOG)
    return {
        issue.code
        for issue in template_runtime_policy_issues(
            templates, compiled.tool_capabilities, compiled.approval_policies
        )
    }


def test_dom_04_all_36_templates_have_complete_runtime_policy() -> None:
    compiled = compile_catalog(CATALOG)
    assert (
        template_runtime_policy_issues(
            compiled.templates, compiled.tool_capabilities, compiled.approval_policies
        )
        == ()
    )
    for template in compiled.templates:
        assert template.allowed_tool_capability_ids
        assert template.supported_trigger_types
        assert template.source_references
        assert template.implementation_notes


def test_dom_04_write_without_approval_or_idempotency_is_rejected() -> None:
    compiled = compile_catalog(CATALOG)
    original = compiled.templates[0]
    unsafe = original.model_copy(
        update={"allowed_tool_capability_ids": ("cap.newsletter.subscribe",)}
    )
    assert "template-write-approval" in _codes([unsafe, *compiled.templates[1:]])


def test_dom_04_mutating_without_write_and_blind_write_retry_are_rejected() -> None:
    compiled = compile_catalog(CATALOG)
    mutating = next(
        item for item in compiled.templates if item.operation_classification == "mutating"
    )
    no_write = mutating.model_copy(
        update={"allowed_tool_capability_ids": ("cap.model.generate-structured",)}
    )
    assert "template-mutating-without-write" in _codes(
        [no_write if item is mutating else item for item in compiled.templates]
    )

    retried = mutating.model_copy(
        update={"retry_policy": RetryPolicy(max_attempts=2, backoff="bounded_exponential")}
    )
    assert "template-write-retry" in _codes(
        [retried if item is mutating else item for item in compiled.templates]
    )


def test_dom_04_empty_authority_bad_timeout_and_missing_notes_are_rejected() -> None:
    compiled = compile_catalog(CATALOG)
    original = compiled.templates[0]
    malformed = original.model_copy(
        update={
            "allowed_tool_capability_ids": (),
            "timeout_policy": TimeoutPolicy(step_seconds=120, run_seconds=10),
            "implementation_notes": "   ",
        }
    )
    assert _codes([malformed, *compiled.templates[1:]]) >= {
        "template-capabilities-empty",
        "template-timeout-order",
        "template-source-notes",
    }
