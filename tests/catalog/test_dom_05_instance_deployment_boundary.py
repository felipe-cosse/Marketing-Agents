"""DOM-05: instances reference one template and contain deployment configuration only."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import (
    AgentInstanceRecord,
    ConnectorBinding,
    ScheduleBinding,
    TriggerBinding,
)
from marketing_agents.infrastructure.catalog.semantics import deployment_configuration_issues

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"
DEPLOYMENT_FIELDS = {
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


def _codes(instances: list[AgentInstanceRecord]) -> set[str]:
    compiled = compile_catalog(CATALOG)
    return {
        issue.code
        for issue in deployment_configuration_issues(
            compiled.templates, instances, compiled.tool_capabilities
        )
    }


def test_dom_05_all_43_instances_are_deployment_only_and_reference_one_template() -> None:
    compiled = compile_catalog(CATALOG)
    template_ids = {item.id for item in compiled.templates}
    assert len(compiled.instances) == 43
    assert (
        deployment_configuration_issues(
            compiled.templates, compiled.instances, compiled.tool_capabilities
        )
        == ()
    )
    for instance in compiled.instances:
        assert instance.template_id in template_ids
        assert set(instance.model_dump()) == DEPLOYMENT_FIELDS


def test_dom_05_structural_schema_rejects_template_fields_and_untyped_bindings() -> None:
    schema = json.loads(
        (ROOT / "catalog" / "schema" / "instance.schema.json").read_text(encoding="utf-8")
    )
    raw = yaml.safe_load((CATALOG / "instances" / "social-media.yaml").read_text(encoding="utf-8"))[
        "instances"
    ][0]
    validator = Draft202012Validator(schema)
    assert validator.is_valid(raw)
    assert not validator.is_valid({**raw, "purpose": "copied role purpose"})
    assert not validator.is_valid({**raw, "connector_bindings": {"primary": {"url": "x"}}})


def test_dom_05_unsupported_trigger_or_connector_binding_is_rejected() -> None:
    compiled = compile_catalog(CATALOG)
    original = compiled.instances[0]
    triggered = original.model_copy(
        update={"trigger_bindings": (TriggerBinding(type="schedule", enabled=True),)}
    )
    assert "instance-trigger-unsupported" in _codes(
        [triggered if item is original else item for item in compiled.instances]
    )

    connected = original.model_copy(
        update={
            "connector_bindings": {
                "primary": ConnectorBinding(
                    connector_family="crm", binding_id="local.crm", enabled=True
                )
            }
        }
    )
    assert "instance-connector-unsupported" in _codes(
        [connected if item is original else item for item in compiled.instances]
    )


def test_dom_05_schedule_requires_one_enabled_supported_trigger() -> None:
    compiled = compile_catalog(CATALOG)
    original = compiled.instances[0]
    scheduled = original.model_copy(
        update={
            "schedule": ScheduleBinding(
                cron="0 9 * * 1", timezone="America/Los_Angeles", misfire_policy="run_once"
            )
        }
    )
    assert "instance-schedule-binding" in _codes(
        [scheduled if item is original else item for item in compiled.instances]
    )
