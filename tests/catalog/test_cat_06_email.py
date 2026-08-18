"""CAT-06: preserve Email roles and their explicit write/read boundaries."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"

EXPECTED = (
    (
        "tpl.email.newsletter.newsletter-subscriber",
        "Newsletter Subscriber",
        "func.email.newsletter",
        "Add new website signups to the configured newsletter system; "
        "the source chart names Loops.",
    ),
    (
        "tpl.email.newsletter.unsubscribe-assistant",
        "Unsubscribe Assistant",
        "func.email.newsletter",
        "Handle unsubscribe requests safely.",
    ),
    (
        "tpl.email.lifecycle-marketing.customer-onboarder",
        "Customer Onboarder",
        "func.email.lifecycle-marketing",
        "Add new users to the configured CRM and prepare a welcome message.",
    ),
    (
        "tpl.email.lifecycle-marketing.new-customer-tracker",
        "New Customer Tracker",
        "func.email.lifecycle-marketing",
        "Track new customers and highlight interesting cases.",
    ),
    (
        "tpl.email.lifecycle-marketing.churned-user-monitor",
        "Churned User Monitor",
        "func.email.lifecycle-marketing",
        "Identify churned users and prepare a check-in draft.",
    ),
)


def _templates() -> list[dict[str, object]]:
    return yaml.safe_load((CATALOG / "templates" / "email.yaml").read_text(encoding="utf-8"))[
        "templates"
    ]


def test_cat_06_exact_email_inventory_and_distribution() -> None:
    templates = _templates()
    assert (
        tuple(
            (item["id"], item["display_name"], item["function_id"], item["purpose"])
            for item in templates
        )
        == EXPECTED
    )
    assert Counter(item["function_id"] for item in templates) == {
        "func.email.newsletter": 2,
        "func.email.lifecycle-marketing": 3,
    }


def test_cat_06_write_roles_require_human_approval() -> None:
    templates = _templates()
    by_id = {str(item["id"]): item for item in templates}
    mutating = {
        "tpl.email.newsletter.newsletter-subscriber",
        "tpl.email.newsletter.unsubscribe-assistant",
        "tpl.email.lifecycle-marketing.customer-onboarder",
    }
    assert {
        identifier
        for identifier, item in by_id.items()
        if item["operation_classification"] == "mutating"
    } == mutating
    for identifier in mutating:
        assert by_id[identifier]["approval_policy_id"] == "policy.human-approval.external-write.v1"
        assert by_id[identifier]["retry_policy"] == {"max_attempts": 1, "backoff": "none"}


def test_cat_06_drafts_are_advisory_and_no_email_send_is_assigned() -> None:
    templates = _templates()
    capabilities = {
        capability
        for template in templates
        for capability in template["allowed_tool_capability_ids"]
    }
    assert "cap.email.send-message" not in capabilities
    assert "cap.newsletter.subscribe" in capabilities
    assert "cap.newsletter.unsubscribe" in capabilities
    assert "cap.crm.upsert-contact" in capabilities
    churn = next(item for item in templates if item["display_name"] == "Churned User Monitor")
    assert churn["operation_classification"] == "read_only"
    assert "advisory" in str(churn["implementation_notes"])


def test_cat_06_has_one_stable_instance_per_role() -> None:
    instances = yaml.safe_load((CATALOG / "instances" / "email.yaml").read_text(encoding="utf-8"))[
        "instances"
    ]
    assert [item["id"] for item in instances] == [
        "inst." + str(template["id"]).removeprefix("tpl.") + ".01" for template in _templates()
    ]
