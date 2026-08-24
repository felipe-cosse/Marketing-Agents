"""DOM-03: every role template has a stable typed input/output schema pair."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.semantics import template_io_schema_issues

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"


def test_dom_03_all_72_schemas_compile_and_are_recursively_bounded() -> None:
    compiled = compile_catalog(CATALOG)
    assert len(compiled.input_schema_by_template) == 36
    assert len(compiled.output_schema_by_template) == 36
    for template in compiled.templates:
        assert template.input_schema_id == compiled.input_schema_by_template[template.id]["$id"]
        assert template.output_schema_id == compiled.output_schema_by_template[template.id]["$id"]
    assert (
        template_io_schema_issues(
            compiled.templates,
            compiled.input_schema_by_template,
            compiled.output_schema_by_template,
        )
        == ()
    )


def test_dom_03_positive_and_negative_payloads_obey_every_schema_pair() -> None:
    compiled = compile_catalog(CATALOG)
    for template in compiled.templates:
        input_validator = Draft202012Validator(compiled.input_schema_by_template[template.id])
        output_validator = Draft202012Validator(compiled.output_schema_by_template[template.id])
        valid_input = {"request_id": "request_1", "source_content": "bounded example"}
        valid_output = {
            "artifact_id": "artifact_1",
            "summary": "summary",
            "artifact": "artifact",
            "proposed_actions": [],
            "provenance": {"template_id": template.id, "source_request_id": "request_1"},
        }
        if template.output_handling == "advisory":
            valid_output["advisory"] = {
                "status": "advisory_only",
                "automated_decision": False,
                "external_action": "none",
            }
        assert input_validator.is_valid(valid_input)
        assert input_validator.is_valid({**valid_input, "request_id": "r" * 80})
        assert not input_validator.is_valid({**valid_input, "request_id": "r" * 81})
        assert output_validator.is_valid(valid_output)
        assert not input_validator.is_valid({**valid_input, "unexpected": True})
        assert not output_validator.is_valid({"artifact_id": "artifact_1"})


def test_dom_03_schema_identity_shape_and_bounds_drift_is_rejected() -> None:
    compiled = compile_catalog(CATALOG)
    template = compiled.templates[0]
    inputs = {
        key: deepcopy(dict(value)) for key, value in compiled.input_schema_by_template.items()
    }
    outputs = {
        key: deepcopy(dict(value)) for key, value in compiled.output_schema_by_template.items()
    }
    inputs[template.id]["$id"] = "urn:wrong"
    inputs[template.id]["additionalProperties"] = True
    outputs[template.id]["properties"]["summary"].pop("maxLength")
    codes = {issue.code for issue in template_io_schema_issues(compiled.templates, inputs, outputs)}
    assert codes >= {
        "template-schema-identity",
        "schema-unbounded-object",
        "schema-unbounded-string",
    }
