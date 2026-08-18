"""CAT-03: enforce stable unique instance identity and deployment-only ownership."""

from __future__ import annotations

from pathlib import Path

import yaml
from marketing_agents.infrastructure.catalog.models import (
    AgentInstanceRecord,
    AgentTemplateRecord,
    InstanceVariant,
)
from marketing_agents.infrastructure.catalog.semantics import (
    instance_field_ownership_issues,
    marketing_v1_identity_issues,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"


def _records() -> tuple[list[AgentTemplateRecord], list[AgentInstanceRecord]]:
    templates: list[AgentTemplateRecord] = []
    instances: list[AgentInstanceRecord] = []
    for path in sorted((CATALOG / "templates").glob("*.yaml")):
        templates.extend(
            AgentTemplateRecord.model_validate(item)
            for item in yaml.safe_load(path.read_text(encoding="utf-8"))["templates"]
        )
    for path in sorted((CATALOG / "instances").glob("*.yaml")):
        instances.extend(
            AgentInstanceRecord.model_validate(item)
            for item in yaml.safe_load(path.read_text(encoding="utf-8"))["instances"]
        )
    return templates, instances


def test_cat_03_all_43_instance_ids_are_unique_derived_and_source_ordinal_only() -> None:
    templates, instances = _records()
    assert len(instances) == 43
    assert len({item.id for item in instances}) == 43
    assert marketing_v1_identity_issues(templates, instances) == ()
    assert all(
        item.variant is not None and item.variant.variant_label is None for item in instances
    )


def test_cat_03_duplicate_namespace_and_ordinal_drift_are_rejected() -> None:
    templates, instances = _records()
    original = instances[0]
    duplicate = original.model_copy()
    assert "duplicate-instance-id" in {
        issue.code for issue in marketing_v1_identity_issues(templates, [*instances, duplicate])
    }

    wrong_namespace = original.model_copy(update={"id": "inst.email.newsletter.fake-role.01"})
    assert "instance-template-identity" in {
        issue.code
        for issue in marketing_v1_identity_issues(
            templates, [wrong_namespace if item is original else item for item in instances]
        )
    }

    wrong_variant = original.model_copy(
        update={"variant": InstanceVariant(source_ordinal=2, variant_label=None)}
    )
    assert "instance-variant-ordinal" in {
        issue.code
        for issue in marketing_v1_identity_issues(
            templates, [wrong_variant if item is original else item for item in instances]
        )
    }


def test_cat_03_invented_business_variant_label_is_rejected() -> None:
    templates, instances = _records()
    community = next(item for item in instances if item.id.startswith("inst.community."))
    labeled = community.model_copy(
        update={
            "variant": InstanceVariant(
                source_ordinal=community.variant.source_ordinal if community.variant else 1,
                variant_label="enterprise-specialty",
            )
        }
    )
    assert "invented-variant-label" in {
        issue.code
        for issue in marketing_v1_identity_issues(
            templates, [labeled if item is community else item for item in instances]
        )
    }


def test_cat_03_template_owned_fields_are_rejected_from_instances() -> None:
    copied = {
        "id": "inst.community.events.attendee-scheduler.01",
        "template_id": "tpl.community.events.attendee-scheduler",
        "display_order": 10,
        "enabled": True,
        "variant": {"source_ordinal": 1, "variant_label": None},
        "trigger_bindings": [],
        "connector_bindings": {},
        "schedule": None,
        "configuration_revision": 1,
        "purpose": "Invented duplicate purpose",
        "system_prompt_ref": "copied.md",
        "approval_policy_id": "policy.no-approval.read-only.v1",
    }
    issues = instance_field_ownership_issues([copied], "instances/community.yaml")
    assert {issue.json_pointer for issue in issues} == {
        "/0/purpose",
        "/0/system_prompt_ref",
        "/0/approval_policy_id",
    }
