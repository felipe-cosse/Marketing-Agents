"""CAT-07: preserve seven Community templates as fourteen deployments."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"

EXPECTED = (
    (
        "tpl.community.events.attendee-scheduler",
        "Attendee Scheduler",
        "func.community.events",
        "Add new signups to live-event sessions.",
    ),
    (
        "tpl.community.events.live-session-reminder",
        "Live Session Reminder",
        "func.community.events",
        "Communicate with attendees about live events.",
    ),
    (
        "tpl.community.events.event-stats-tracker",
        "Event Stats Tracker",
        "func.community.events",
        "Report event signups and attendance to the team.",
    ),
    (
        "tpl.community.education.course-cohort-onboarder",
        "Course Cohort Onboarder",
        "func.community.education",
        "Add and welcome course participants.",
    ),
    (
        "tpl.community.education.material-builder",
        "Material Builder",
        "func.community.education",
        "Create, personalize, and share course materials.",
    ),
    (
        "tpl.community.education.course-progress-reminders",
        "Course Progress Reminders",
        "func.community.education",
        "Check in with participants about progress.",
    ),
    (
        "tpl.community.discussion.new-member-onboarder",
        "New Member Onboarder",
        "func.community.discussion",
        "Welcome new members to the Slack community.",
    ),
)


def _document(name: str) -> list[dict[str, object]]:
    key = "templates" if name == "templates" else "instances"
    return yaml.safe_load((CATALOG / name / "community.yaml").read_text(encoding="utf-8"))[key]


def test_cat_07_exact_community_role_inventory() -> None:
    templates = _document("templates")
    assert (
        tuple(
            (item["id"], item["display_name"], item["function_id"], item["purpose"])
            for item in templates
        )
        == EXPECTED
    )
    assert Counter(item["function_id"] for item in templates) == {
        "func.community.events": 3,
        "func.community.education": 3,
        "func.community.discussion": 1,
    }


def test_cat_07_every_template_has_two_ordinal_instances_without_invented_variant() -> None:
    templates = _document("templates")
    instances = _document("instances")
    by_template: dict[str, list[dict[str, object]]] = defaultdict(list)
    for instance in instances:
        by_template[str(instance["template_id"])].append(instance)
    assert len(templates) == 7
    assert len(instances) == 14
    for template in templates:
        template_id = str(template["id"])
        deployments = by_template[template_id]
        assert [item["id"] for item in deployments] == [
            "inst." + template_id.removeprefix("tpl.") + ".01",
            "inst." + template_id.removeprefix("tpl.") + ".02",
        ]
        assert [item["variant"] for item in deployments] == [
            {"source_ordinal": 1, "variant_label": None},
            {"source_ordinal": 2, "variant_label": None},
        ]


def test_cat_07_instances_only_contain_deployment_fields() -> None:
    allowed = {
        "id",
        "template_id",
        "display_order",
        "enabled",
        "variant",
        "trigger_bindings",
        "connector_bindings",
        "schedule",
        "configuration_revision",
    }
    assert all(set(instance) == allowed for instance in _document("instances"))


def test_cat_07_external_writes_are_approval_bound() -> None:
    templates = _document("templates")
    for template in templates:
        if template["operation_classification"] == "mutating":
            assert template["approval_policy_id"] == "policy.human-approval.external-write.v1"
            assert template["retry_policy"] == {"max_attempts": 1, "backoff": "none"}
    reminder = next(item for item in templates if item["display_name"] == "Live Session Reminder")
    assert reminder["operation_classification"] == "read_only"
    assert "cap.messaging.send-message" not in reminder["allowed_tool_capability_ids"]
