"""SAFE-09: partner-review and churn outcomes remain explicit inert advice."""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.semantics import advisory_output_issues

ROOT = Path(__file__).resolve().parents[2]
ADVISORY_IDS = {
    "tpl.email.lifecycle-marketing.churned-user-monitor",
    "tpl.partnerships.implementation-partners.partner-application-reviewer",
}


def _valid_payload(template_id: str) -> dict[str, object]:
    return {
        "artifact_id": "artifact_advisory_1",
        "summary": "Review based on supplied evidence.",
        "artifact": "Human review is required before any consequential decision.",
        "proposed_actions": [],
        "provenance": {"template_id": template_id, "source_request_id": "request-1"},
        "advisory": {
            "status": "advisory_only",
            "automated_decision": False,
            "external_action": "none",
        },
    }


def test_safe_09_required_partner_and_churn_templates_are_advisory_only() -> None:
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    templates = {item.id: item for item in catalog.templates}
    capabilities = {item.id: item for item in catalog.tool_capabilities}
    policies = {item.id: item for item in catalog.approval_policies}

    actual_advisory = {item.id for item in catalog.templates if item.output_handling == "advisory"}
    assert actual_advisory == ADVISORY_IDS
    for template_id in ADVISORY_IDS:
        template = templates[template_id]
        assert template.operation_classification == "read_only"
        assert policies[template.approval_policy_id].kind == "none"
        assert all(
            capabilities[capability_id].effect == "read"
            for capability_id in template.allowed_tool_capability_ids
        )
        Draft202012Validator(catalog.output_schema_by_template[template_id]).validate(
            _valid_payload(template_id)
        )
        assert "This outcome is advisory only" in catalog.prompt_text_by_template[template_id]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.pop("advisory"),
        lambda payload: payload["advisory"].update({"automated_decision": True}),
        lambda payload: payload["advisory"].update({"external_action": "send"}),
        lambda payload: payload.update(
            {
                "proposed_actions": [
                    {
                        "action_type": "email.send",
                        "destination": "contact",
                        "payload_preview": "message",
                    }
                ]
            }
        ),
    ],
)
def test_safe_09_unlabeled_automated_or_actionable_outputs_fail_schema(mutator: object) -> None:
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    template_id = sorted(ADVISORY_IDS)[0]
    payload = _valid_payload(template_id)
    mutator(payload)  # type: ignore[operator]
    assert not Draft202012Validator(catalog.output_schema_by_template[template_id]).is_valid(
        payload
    )


def test_safe_09_advisory_label_and_read_only_authority_cannot_be_downgraded() -> None:
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    templates = list(catalog.templates)
    index = next(index for index, item in enumerate(templates) if item.id in ADVISORY_IDS)
    original = templates[index]

    templates[index] = original.model_copy(update={"output_handling": "standard"})
    issues = advisory_output_issues(
        templates,
        catalog.tool_capabilities,
        catalog.approval_policies,
        catalog.output_schema_by_template,
    )
    assert any(issue.code == "advisory-required" for issue in issues)

    templates[index] = original.model_copy(
        update={
            "operation_classification": "mutating",
            "allowed_tool_capability_ids": ("cap.newsletter.subscribe",),
        }
    )
    issues = advisory_output_issues(
        templates,
        catalog.tool_capabilities,
        catalog.approval_policies,
        catalog.output_schema_by_template,
    )
    assert any(issue.code == "advisory-authority" for issue in issues)
