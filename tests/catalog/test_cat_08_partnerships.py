"""CAT-08: preserve Partnerships roles as bounded advisory/read-only work."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"

EXPECTED = (
    (
        "tpl.partnerships.implementation-partners.partner-application-reviewer",
        "Partner Application Reviewer",
        "func.partnerships.implementation-partners",
        "Research applicants and recommend accept or reject.",
    ),
    (
        "tpl.partnerships.implementation-partners.partner-tracker",
        "Partner Tracker",
        "func.partnerships.implementation-partners",
        "Track partner engagement.",
    ),
    (
        "tpl.partnerships.implementation-partners.partner-finder",
        "Partner Finder",
        "func.partnerships.implementation-partners",
        "Recommend partners based on customer requirements.",
    ),
    (
        "tpl.partnerships.implementation-partners.swag-tracker",
        "Swag Tracker",
        "func.partnerships.implementation-partners",
        "Track swag fulfillment.",
    ),
    (
        "tpl.partnerships.implementation-partners.community-challenge-tracker",
        "Community Challenge Tracker",
        "func.partnerships.implementation-partners",
        "Calculate and track points from community challenges.",
    ),
    (
        "tpl.partnerships.integration-partners.integration-partner-tracker",
        "Integration Partner Tracker",
        "func.partnerships.integration-partners",
        "Track partner status across partners' websites and marketplaces.",
    ),
)


def _templates() -> list[dict[str, object]]:
    return yaml.safe_load(
        (CATALOG / "templates" / "partnerships.yaml").read_text(encoding="utf-8")
    )["templates"]


def test_cat_08_exact_partnerships_inventory_and_distribution() -> None:
    templates = _templates()
    assert (
        tuple(
            (item["id"], item["display_name"], item["function_id"], item["purpose"])
            for item in templates
        )
        == EXPECTED
    )
    assert Counter(item["function_id"] for item in templates) == {
        "func.partnerships.implementation-partners": 5,
        "func.partnerships.integration-partners": 1,
    }


def test_cat_08_all_roles_are_advisory_or_read_only() -> None:
    templates = _templates()
    assert all(item["operation_classification"] == "read_only" for item in templates)
    assert all(
        item["approval_policy_id"] == "policy.no-approval.read-only.v1" for item in templates
    )
    reviewer = next(
        item for item in templates if item["display_name"] == "Partner Application Reviewer"
    )
    assert "advisory" in str(reviewer["implementation_notes"])


def test_cat_08_has_no_write_or_scraping_authority() -> None:
    capabilities = {
        capability
        for template in _templates()
        for capability in template["allowed_tool_capability_ids"]
    }
    assert "cap.fulfillment.create" not in capabilities
    assert "cap.spreadsheet.update-rows" not in capabilities
    assert not any("crawl" in item or "http" in item or "browser" in item for item in capabilities)


def test_cat_08_has_one_stable_instance_per_role() -> None:
    instances = yaml.safe_load(
        (CATALOG / "instances" / "partnerships.yaml").read_text(encoding="utf-8")
    )["instances"]
    assert [item["id"] for item in instances] == [
        "inst." + str(template["id"]).removeprefix("tpl.") + ".01" for template in _templates()
    ]
