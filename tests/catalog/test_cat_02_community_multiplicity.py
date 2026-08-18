"""CAT-02: enforce seven shared Community roles as fourteen deployments."""

from __future__ import annotations

from pathlib import Path

import yaml
from marketing_agents.infrastructure.catalog.models import (
    AgentInstanceRecord,
    AgentTemplateRecord,
    InstanceVariant,
)
from marketing_agents.infrastructure.catalog.semantics import marketing_v1_multiplicity_issues

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"


def _records() -> tuple[list[AgentTemplateRecord], list[AgentInstanceRecord]]:
    templates: list[AgentTemplateRecord] = []
    instances: list[AgentInstanceRecord] = []
    for path in sorted((CATALOG / "templates").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        templates.extend(AgentTemplateRecord.model_validate(item) for item in document["templates"])
    for path in sorted((CATALOG / "instances").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        instances.extend(AgentInstanceRecord.model_validate(item) for item in document["instances"])
    return templates, instances


def test_cat_02_authoritative_community_multiplicity_is_valid() -> None:
    templates, instances = _records()
    assert marketing_v1_multiplicity_issues(templates, instances) == ()
    community = [item for item in templates if item.department_id == "dept.community"]
    deployments = [item for item in instances if item.template_id.startswith("tpl.community.")]
    assert len(community) == 7
    assert len(deployments) == 14


def test_cat_02_missing_or_extra_community_ordinal_is_rejected() -> None:
    templates, instances = _records()
    without_second = [item for item in instances if not item.id.endswith("attendee-scheduler.02")]
    assert {
        issue.code for issue in marketing_v1_multiplicity_issues(templates, without_second)
    } >= {
        "deployment-multiplicity",
        "community-instance-distribution",
    }

    attendee_second = next(item for item in instances if item.id.endswith("attendee-scheduler.02"))
    extra = attendee_second.model_copy(
        update={
            "id": "inst.community.events.attendee-scheduler.03",
            "variant": InstanceVariant(source_ordinal=3, variant_label=None),
        }
    )
    assert "deployment-multiplicity" in {
        issue.code for issue in marketing_v1_multiplicity_issues(templates, [*instances, extra])
    }


def test_cat_02_duplicate_reference_and_cross_function_drift_are_rejected() -> None:
    templates, instances = _records()
    first = next(item for item in instances if item.id.endswith("attendee-scheduler.01"))
    reminder_second = next(
        item for item in instances if item.id.endswith("live-session-reminder.02")
    )
    reassigned = reminder_second.model_copy(update={"template_id": first.template_id})
    mutated_instances = [reassigned if item is reminder_second else item for item in instances]
    assert "deployment-multiplicity" in {
        issue.code for issue in marketing_v1_multiplicity_issues(templates, mutated_instances)
    }

    discussion = next(item for item in templates if item.function_id == "func.community.discussion")
    moved = discussion.model_copy(update={"function_id": "func.community.events"})
    mutated_templates = [moved if item is discussion else item for item in templates]
    assert "community-function-distribution" in {
        issue.code for issue in marketing_v1_multiplicity_issues(mutated_templates, instances)
    }


def test_cat_02_non_community_templates_cannot_gain_a_duplicate_deployment() -> None:
    templates, instances = _records()
    social = next(item for item in instances if item.id.endswith("linkedin-post-drafter.01"))
    duplicate = social.model_copy(
        update={
            "id": "inst.social-media.new-content.linkedin-post-drafter.02",
            "variant": InstanceVariant(source_ordinal=2, variant_label=None),
        }
    )
    assert "deployment-multiplicity" in {
        issue.code for issue in marketing_v1_multiplicity_issues(templates, [*instances, duplicate])
    }
