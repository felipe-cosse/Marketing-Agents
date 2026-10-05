"""AC-06: all public template contracts resolve valid schemas and safe authority."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from marketing_agents.api import create_app
from marketing_agents.api.catalog_queries import LocalCatalogQueryService
from marketing_agents.config import Settings
from marketing_agents.infrastructure.catalog import CatalogCompilationError, compile_catalog

from tests.support.api import assert_problem

ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = ROOT / "catalog" / "v1"
TARGET_TEMPLATE_ID = "tpl.email.newsletter.newsletter-subscriber"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _default_app(catalog_root: Path) -> FastAPI:
    app = create_app(Settings(_env_file=None, app_env="test", catalog_root=catalog_root))
    assert type(app.state.catalog_query_service) is LocalCatalogQueryService
    return app


async def _read_document(client: AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    document = response.json()
    assert isinstance(document, dict)
    return document


@pytest.mark.asyncio
async def test_ac_06_all_36_default_api_template_details_expose_complete_contracts() -> None:
    # The compiled source is an independent read-side oracle, not injected into
    # the app. Every HTTP response still crosses the default compiler/query seam.
    compiled = compile_catalog(CATALOG_ROOT)
    expected_templates = {template.id: template for template in compiled.templates}
    source_capabilities = {item.id: item for item in compiled.tool_capabilities}
    source_policies = {item.id: item for item in compiled.approval_policies}
    assert len(compiled.templates) == len(expected_templates) == 36
    app = _default_app(CATALOG_ROOT)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        templates = await _read_document(client, "/api/v1/agent-templates")
        capabilities = await _read_document(client, "/api/v1/tool-capabilities")
        policies = await _read_document(client, "/api/v1/approval-policies")
        listed_ids = [template["id"] for template in templates["templates"]]
        assert templates["count"] == len(listed_ids) == len(set(listed_ids)) == 36
        assert listed_ids == [template.id for template in compiled.templates]

        capability_views = {item["id"]: item for item in capabilities["toolCapabilities"]}
        policy_views = {item["id"]: item for item in policies["approvalPolicies"]}
        assert capabilities["count"] == len(capabilities["toolCapabilities"])
        assert len(capability_views) == len(capabilities["toolCapabilities"])
        assert set(capability_views) == set(source_capabilities)
        assert policies["count"] == len(policies["approvalPolicies"])
        assert len(policy_views) == len(policies["approvalPolicies"])
        assert set(policy_views) == set(source_policies)
        for document in (templates, capabilities, policies):
            assert document["catalogVersion"] == compiled.manifest.content_version
            assert document["catalogHash"] == compiled.content_hash

        schema_ids: list[str] = []
        visited_ids: list[str] = []
        write_template_ids: set[str] = set()
        for listed in templates["templates"]:
            template_id = listed["id"]
            source = expected_templates[template_id]
            detail = await _read_document(client, f"/api/v1/agent-templates/{template_id}")
            visited_ids.append(template_id)
            assert detail["catalogVersion"] == compiled.manifest.content_version
            assert detail["catalogHash"] == compiled.content_hash
            assert detail["template"] == listed

            for direction, schema_map in (
                ("input", compiled.input_schema_by_template),
                ("output", compiled.output_schema_by_template),
            ):
                schema = detail[f"{direction}Schema"]
                expected_schema_id = f"urn:marketing-agents:catalog:v1:{template_id}:{direction}"
                assert schema == dict(schema_map[template_id])
                assert schema["$schema"] == SCHEMA_DIALECT
                assert schema["$id"] == listed[f"{direction}SchemaId"] == expected_schema_id
                assert schema["type"] == "object"
                assert schema["additionalProperties"] is False
                assert schema["required"]
                assert schema["title"] and schema["description"]
                Draft202012Validator.check_schema(schema)
                schema_ids.append(schema["$id"])

            input_validator = Draft202012Validator(detail["inputSchema"])
            output_validator = Draft202012Validator(detail["outputSchema"])
            valid_input = {"request_id": "ac06.request-1", "source_content": "bounded example"}
            valid_output: dict[str, Any] = {
                "artifact_id": "artifact_ac06",
                "summary": "summary",
                "artifact": "artifact",
                "proposed_actions": [],
                "provenance": {
                    "template_id": template_id,
                    "source_request_id": "ac06.request-1",
                },
            }
            if source.output_handling == "advisory":
                valid_output["advisory"] = {
                    "status": "advisory_only",
                    "automated_decision": False,
                    "external_action": "none",
                }
            input_validator.validate(valid_input)
            output_validator.validate(valid_output)
            assert not input_validator.is_valid({**valid_input, "unlisted_field": True})
            assert not output_validator.is_valid({"artifact_id": "artifact_ac06"})

            allowlist = listed["allowedToolCapabilityIds"]
            assert allowlist == list(source.allowed_tool_capability_ids)
            assert allowlist and len(allowlist) == len(set(allowlist))
            assert [item["id"] for item in detail["capabilities"]] == allowlist
            assert detail["capabilities"] == [capability_views[item] for item in allowlist]
            for capability in detail["capabilities"]:
                expected = source_capabilities[capability["id"]]
                assert capability["effect"] == expected.effect
                assert capability["connectorFamily"] == expected.connector_family
                assert capability["idempotencySupport"] == expected.idempotency_support

            policy = detail["approvalPolicy"]
            assert policy["id"] == listed["approvalPolicyId"] == source.approval_policy_id
            assert policy == policy_views[source.approval_policy_id]
            expected_policy = source_policies[source.approval_policy_id]
            assert policy == {
                "id": expected_policy.id,
                "kind": expected_policy.kind,
                "requiredRoles": list(expected_policy.required_roles),
                "expirySeconds": expected_policy.expiry_seconds,
                "allowSelfApproval": expected_policy.allow_self_approval,
            }
            if any(item["effect"] == "write" for item in detail["capabilities"]):
                write_template_ids.add(template_id)
                assert listed["operationClassification"] == "mutating"
                assert policy["id"] == "policy.human-approval.external-write.v1"
                assert policy["kind"] == "human_external_write"
                assert "approver" in policy["requiredRoles"]
            else:
                assert listed["operationClassification"] == "read_only"
                assert policy["kind"] == "none"

        assert visited_ids == listed_ids
        assert len(schema_ids) == len(set(schema_ids)) == 72
        assert write_template_ids == {
            template.id
            for template in compiled.templates
            if template.operation_classification == "mutating"
        }
        assert write_template_ids and len(write_template_ids) < 36


def _invalid_catalog_copy(tmp_path: Path, defect: str) -> Path:
    copied_catalog = tmp_path / "catalog"
    shutil.copytree(ROOT / "catalog", copied_catalog)
    copied_root = copied_catalog / "v1"
    # Establish that the copy was valid before introducing exactly this defect.
    assert len(compile_catalog(copied_root).templates) == 36
    if defect in {"input-schema-identity", "output-schema-unbounded"}:
        direction = "input" if defect == "input-schema-identity" else "output"
        path = copied_root / "schemas" / TARGET_TEMPLATE_ID / f"{direction}.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        if defect == "input-schema-identity":
            schema["$id"] = "urn:marketing-agents:ac06:wrong-schema"
        else:
            schema["properties"]["summary"].pop("maxLength")
        path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    else:
        path = copied_root / "templates" / "email.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        template = next(item for item in document["templates"] if item["id"] == TARGET_TEMPLATE_ID)
        if defect == "missing-capability":
            template["allowed_tool_capability_ids"] = ["cap.newsletter.ac-06-missing"]
        elif defect == "empty-capability-set":
            template["allowed_tool_capability_ids"] = []
        elif defect == "missing-approval-policy":
            template["approval_policy_id"] = "policy.ac-06-missing"
        else:
            assert defect == "write-without-approval"
            template["approval_policy_id"] = "policy.no-approval.read-only.v1"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return copied_root


@pytest.mark.parametrize(
    ("defect", "required_issue_codes"),
    (
        ("input-schema-identity", {"template-schema-identity"}),
        ("output-schema-unbounded", {"schema-unbounded-string"}),
        ("missing-capability", {"broken-reference"}),
        ("empty-capability-set", {"template-capabilities-empty"}),
        ("missing-approval-policy", {"broken-reference"}),
        ("write-without-approval", {"unsafe-write-policy", "template-write-approval"}),
    ),
)
@pytest.mark.asyncio
async def test_ac_06_invalid_template_contracts_fail_closed_before_public_projection(
    tmp_path: Path,
    defect: str,
    required_issue_codes: set[str],
) -> None:
    copied_root = _invalid_catalog_copy(tmp_path, defect)
    with pytest.raises(CatalogCompilationError) as captured:
        compile_catalog(copied_root)
    assert {issue.code for issue in captured.value.issues} >= required_issue_codes

    app = _default_app(copied_root)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        for path in (
            "/api/v1/agent-templates",
            f"/api/v1/agent-templates/{TARGET_TEMPLATE_ID}",
        ):
            response = await client.get(path)
            assert_problem(response, status_code=503, code="catalog_unavailable")
            assert response.headers["vary"] == "Authorization"
            assert "etag" not in response.headers
            for source_detail in (
                str(copied_root),
                "email.yaml",
                "input.schema.json",
                "output.schema.json",
                "urn:marketing-agents:ac06:wrong-schema",
                "cap.newsletter.ac-06-missing",
                "policy.ac-06-missing",
                *required_issue_codes,
            ):
                assert source_detail not in response.text
