"""API-02: complete safe catalog reads and the exact ordered UI hierarchy."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import threading
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import catalog_queries, create_app
from marketing_agents.api.catalog_queries import (
    CatalogDocuments,
    CatalogQueryExecutor,
    CatalogRepresentation,
    LocalCatalogQueryService,
    project_catalog,
)
from marketing_agents.api.routes import catalog as catalog_routes
from marketing_agents.application.policies.catalog_authorization import (
    CatalogAuthorizationError,
)
from marketing_agents.config import Settings
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import CompiledCatalog

from tests.support.api import assert_problem
from tests.support.identity import (
    StaticIdentityProvider,
    human_principal,
    service_principal,
)

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
EXPECTED_HASH = "catalog-sha256-v1:3970f3f23341d3e43a83ff73985e0485addd6c0df7519595f535420c09a9ced1"
AGGREGATE_PATHS = ("/api/v1/catalog", "/api/v1/catalog/hierarchy")
_UNCHANGED = object()


class StaticCatalogQuery:
    def __init__(self, documents: CatalogDocuments) -> None:
        self.documents = documents
        self.principals: list[AuthenticatedPrincipal] = []

    async def read(self, principal: AuthenticatedPrincipal) -> CatalogDocuments:
        self.principals.append(principal)
        return self.documents


class ThrowingCatalogQuery:
    def __init__(self, message: str) -> None:
        self.message = message

    async def read(self, _principal: AuthenticatedPrincipal) -> CatalogDocuments:
        raise RuntimeError(self.message)


class MalformedCatalogQuery:
    async def read(self, _principal: AuthenticatedPrincipal) -> CatalogDocuments:
        return cast(CatalogDocuments, object())


class SlowCatalogQuery:
    async def read(self, _principal: AuthenticatedPrincipal) -> CatalogDocuments:
        await asyncio.sleep(1)
        raise AssertionError("the bounded catalog read should have timed out")


class BlockingSyncCatalogQuery:
    def __init__(self, documents: CatalogDocuments) -> None:
        self.documents = documents
        self.called = False

    def read(self, _principal: AuthenticatedPrincipal) -> CatalogDocuments:
        self.called = True
        time.sleep(0.05)
        return self.documents


@pytest.fixture(scope="module")
def compiled() -> CompiledCatalog:
    return compile_catalog(CATALOG_ROOT)


@pytest.fixture(scope="module")
def documents(compiled: CompiledCatalog) -> CatalogDocuments:
    return project_catalog(compiled)


def _settings() -> Settings:
    return Settings(_env_file=None, catalog_root=CATALOG_ROOT)


def _app(
    query: CatalogQueryExecutor,
    *,
    principal: AuthenticatedPrincipal | None = None,
) -> object:
    provider = None if principal is None else StaticIdentityProvider(principal)
    return create_app(
        _settings(),
        identity_provider=provider,
        catalog_query_service=query,
    )


async def _get(
    app: object,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as client:
        return await client.get(path, headers=headers)


def _file_fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _minimal_catalog(compiled: CompiledCatalog) -> CompiledCatalog:
    template = compiled.templates[0]
    instance = next(item for item in compiled.instances if item.template_id == template.id)
    function = next(item for item in compiled.functions if item.id == template.function_id)
    department = next(item for item in compiled.departments if item.id == template.department_id)
    capabilities = tuple(
        item
        for identifier in template.allowed_tool_capability_ids
        for item in compiled.tool_capabilities
        if item.id == identifier
    )
    policy = next(
        item for item in compiled.approval_policies if item.id == template.approval_policy_id
    )
    return replace(
        compiled,
        departments=(department,),
        functions=(function,),
        tool_capabilities=capabilities,
        approval_policies=(policy,),
        templates=(template,),
        instances=(instance,),
        prompt_text_by_template=MappingProxyType(
            {template.id: compiled.prompt_text_by_template[template.id]}
        ),
        input_schema_by_template=MappingProxyType(
            {template.id: compiled.input_schema_by_template[template.id]}
        ),
        output_schema_by_template=MappingProxyType(
            {template.id: compiled.output_schema_by_template[template.id]}
        ),
        department_instance_counts=MappingProxyType({department.id: 1}),
        content_hash="catalog-sha256-v1:" + "a" * 64,
    )


def _corrupt_representation(
    source: CatalogRepresentation,
    *,
    content: bytes | None = None,
    etag: object = _UNCHANGED,
) -> CatalogRepresentation:
    corrupted = object.__new__(CatalogRepresentation)
    object.__setattr__(corrupted, "label", source.label)
    object.__setattr__(corrupted, "model_type", source.model_type)
    object.__setattr__(corrupted, "content", source.content if content is None else content)
    object.__setattr__(corrupted, "etag", source.etag if etag is _UNCHANGED else etag)
    return corrupted


def _expected_capability_label(identifier: str) -> str:
    initialisms = {"api": "API", "cms": "CMS", "crm": "CRM", "llm": "LLM", "seo": "SEO"}
    _prefix, raw_family, raw_operation = identifier.split(".")
    family = " ".join(initialisms.get(word, word.title()) for word in raw_family.split("-"))
    raw_words = raw_operation.split("-")
    words = [initialisms.get(word, word) for word in raw_words]
    words[0] = initialisms.get(raw_words[0], raw_words[0].title())
    return f"{family}: {' '.join(words)}"


@pytest.mark.asyncio
async def test_api_02_catalog_returns_complete_resolved_inventory(
    compiled: CompiledCatalog,
    documents: CatalogDocuments,
) -> None:
    query = StaticCatalogQuery(documents)
    response = await _get(_app(query), "/api/v1/catalog")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "projectionVersion",
        "manifest",
        "catalogVersion",
        "catalogHash",
        "counts",
        "departmentCounts",
        "departments",
        "functions",
        "templates",
        "instances",
        "toolCapabilities",
        "approvalPolicies",
    }
    assert body["projectionVersion"] == "catalog-read-v1"
    assert body["manifest"] == {
        "formatVersion": 1,
        "contentVersion": "1.0.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
    }
    assert body["catalogHash"] == EXPECTED_HASH == compiled.content_hash
    assert body["counts"] == {
        "departments": 5,
        "functions": 12,
        "templates": 36,
        "instances": 43,
        "toolCapabilities": 22,
        "approvalPolicies": 2,
    }
    assert [item["id"] for item in body["departments"]] == [
        item.id for item in compiled.departments
    ]
    assert [item["id"] for item in body["functions"]] == [item.id for item in compiled.functions]
    assert [item["id"] for item in body["templates"]] == [item.id for item in compiled.templates]
    assert [item["id"] for item in body["instances"]] == [item.id for item in compiled.instances]
    assert [item["id"] for item in body["toolCapabilities"]] == [
        item.id for item in compiled.tool_capabilities
    ]
    assert [item["id"] for item in body["approvalPolicies"]] == [
        item.id for item in compiled.approval_policies
    ]

    for department_source, projected in zip(
        compiled.departments,
        body["departments"],
        strict=True,
    ):
        assert projected == {
            "id": department_source.id,
            "displayName": department_source.display_name,
            "displayOrder": department_source.display_order,
            "sourceReferences": list(department_source.source_references),
        }
    for function_source, projected in zip(compiled.functions, body["functions"], strict=True):
        assert projected == {
            "id": function_source.id,
            "departmentId": function_source.department_id,
            "displayName": function_source.display_name,
            "displayOrder": function_source.display_order,
            "sourceReferences": list(function_source.source_references),
        }
    for capability_source, projected in zip(
        compiled.tool_capabilities,
        body["toolCapabilities"],
        strict=True,
    ):
        assert projected == {
            "id": capability_source.id,
            "displayName": _expected_capability_label(capability_source.id),
            "description": capability_source.description,
            "effect": capability_source.effect,
            "connectorFamily": capability_source.connector_family,
            "idempotencySupport": capability_source.idempotency_support,
            "defaultTimeoutSeconds": capability_source.default_timeout_seconds,
            "dataClassification": capability_source.data_classification,
        }
    for policy_source, projected in zip(
        compiled.approval_policies,
        body["approvalPolicies"],
        strict=True,
    ):
        assert projected == {
            "id": policy_source.id,
            "kind": policy_source.kind,
            "requiredRoles": list(policy_source.required_roles),
            "expirySeconds": policy_source.expiry_seconds,
            "allowSelfApproval": policy_source.allow_self_approval,
        }
    for template_source, projected in zip(compiled.templates, body["templates"], strict=True):
        assert projected == {
            "id": template_source.id,
            "displayName": template_source.display_name,
            "departmentId": template_source.department_id,
            "functionId": template_source.function_id,
            "displayOrder": template_source.display_order,
            "purpose": template_source.purpose,
            "inputSchemaId": template_source.input_schema_id,
            "outputSchemaId": template_source.output_schema_id,
            "allowedToolCapabilityIds": list(template_source.allowed_tool_capability_ids),
            "supportedTriggerTypes": list(template_source.supported_trigger_types),
            "operationClassification": template_source.operation_classification,
            "outputHandling": template_source.output_handling,
            "approvalPolicyId": template_source.approval_policy_id,
            "retryPolicy": {
                "maxAttempts": template_source.retry_policy.max_attempts,
                "backoff": template_source.retry_policy.backoff,
            },
            "timeoutPolicy": {
                "stepSeconds": template_source.timeout_policy.step_seconds,
                "runSeconds": template_source.timeout_policy.run_seconds,
            },
            "budgetPolicy": {
                "maxSteps": template_source.budget_policy.max_steps,
                "maxModelCalls": template_source.budget_policy.max_model_calls,
                "maxToolCalls": template_source.budget_policy.max_tool_calls,
                "maxInputBytes": template_source.budget_policy.max_input_bytes,
                "maxInputFieldBytes": template_source.budget_policy.max_input_field_bytes,
                "maxOutputBytes": template_source.budget_policy.max_output_bytes,
                "maxModelOutputTokens": (template_source.budget_policy.max_model_output_tokens),
            },
            "rateLimitPolicy": {
                "maxCalls": template_source.rate_limit_policy.max_calls,
                "windowSeconds": template_source.rate_limit_policy.window_seconds,
            },
            "sourceConfidence": template_source.source_confidence,
            "sourceReferences": list(template_source.source_references),
            "implementationNotes": template_source.implementation_notes,
        }
    for instance_source, projected in zip(compiled.instances, body["instances"], strict=True):
        assert instance_source.variant is not None
        expected_schedule = (
            None
            if instance_source.schedule is None
            else {
                "cron": instance_source.schedule.cron,
                "timezone": instance_source.schedule.timezone,
                "misfirePolicy": instance_source.schedule.misfire_policy,
                "misfireGraceSeconds": instance_source.schedule.misfire_grace_seconds,
            }
        )
        assert projected == {
            "id": instance_source.id,
            "templateId": instance_source.template_id,
            "displayOrder": instance_source.display_order,
            "enabled": instance_source.enabled,
            "sourceOrdinal": instance_source.variant.source_ordinal,
            "variantLabel": instance_source.variant.variant_label,
            "triggerBindings": [
                {
                    "type": binding.type,
                    "enabled": binding.enabled,
                    "eventSource": binding.event_source,
                    "cron": binding.cron,
                    "timezone": binding.timezone,
                    "misfirePolicy": binding.misfire_policy,
                    "misfireGraceSeconds": binding.misfire_grace_seconds,
                }
                for binding in instance_source.trigger_bindings
            ],
            "connectorBindings": {
                key: {
                    "connectorFamily": binding.connector_family,
                    "bindingId": binding.binding_id,
                    "enabled": binding.enabled,
                }
                for key, binding in sorted(instance_source.connector_bindings.items())
            },
            "schedule": expected_schedule,
            "configurationRevision": instance_source.configuration_revision,
            "configurationEtag": (
                f'"instance-configuration-v1-{instance_source.configuration_revision}"'
            ),
        }

    department_ids = {item["id"] for item in body["departments"]}
    function_ids = {item["id"] for item in body["functions"]}
    template_ids = {item["id"] for item in body["templates"]}
    capability_ids = {item["id"] for item in body["toolCapabilities"]}
    policy_ids = {item["id"] for item in body["approvalPolicies"]}
    assert all(item["departmentId"] in department_ids for item in body["functions"])
    assert all(
        item["departmentId"] in department_ids
        and item["functionId"] in function_ids
        and item["approvalPolicyId"] in policy_ids
        and set(item["allowedToolCapabilityIds"]) <= capability_ids
        for item in body["templates"]
    )
    assert all(item["templateId"] in template_ids for item in body["instances"])
    assert len({item["id"] for item in body["instances"]}) == 43
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Authorization"
    assert len(query.principals) == 1


@pytest.mark.asyncio
async def test_api_02_hierarchy_returns_exact_ordered_ui_projection(
    compiled: CompiledCatalog,
    documents: CatalogDocuments,
) -> None:
    response = await _get(_app(StaticCatalogQuery(documents)), "/api/v1/catalog/hierarchy")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "catalogVersion",
        "catalogHash",
        "counts",
        "departmentCounts",
        "departments",
    }
    assert body["catalogVersion"] == "1.0.0"
    assert body["catalogHash"] == EXPECTED_HASH
    assert body["counts"] == {
        "departments": 5,
        "functions": 12,
        "templates": 36,
        "instances": 43,
    }
    assert all(set(item) == {"departmentId", "instanceCount"} for item in body["departmentCounts"])
    assert [item["id"] for item in body["departments"]] == [
        item.id for item in compiled.departments
    ]

    template_by_id = {item.id: item for item in compiled.templates}
    capability_by_id = {item.id: item for item in compiled.tool_capabilities}
    instance_by_id = {item.id: item for item in compiled.instances}
    flattened: list[dict[str, object]] = []
    for department in body["departments"]:
        assert set(department) == {"id", "displayName", "displayOrder", "functions"}
        expected_functions = [
            item.id for item in compiled.functions if item.department_id == department["id"]
        ]
        assert [item["id"] for item in department["functions"]] == expected_functions
        for function in department["functions"]:
            assert set(function) == {"id", "displayName", "displayOrder", "instances"}
            for instance in function["instances"]:
                assert set(instance) == {
                    "id",
                    "templateId",
                    "displayName",
                    "purpose",
                    "displayOrder",
                    "enabled",
                    "operationClassification",
                    "triggerTypes",
                    "capabilitySummaries",
                    "sourceOrdinal",
                }
                template = template_by_id[cast(str, instance["templateId"])]
                source_instance = instance_by_id[cast(str, instance["id"])]
                assert source_instance.variant is not None
                assert template.department_id == department["id"]
                assert template.function_id == function["id"]
                assert instance["displayName"] == template.display_name
                assert instance["purpose"] == template.purpose
                assert instance["displayOrder"] == source_instance.display_order
                assert instance["enabled"] is source_instance.enabled
                assert instance["operationClassification"] == template.operation_classification
                assert instance["triggerTypes"] == list(template.supported_trigger_types)
                assert instance["sourceOrdinal"] == source_instance.variant.source_ordinal
                assert [item["id"] for item in instance["capabilitySummaries"]] == list(
                    template.allowed_tool_capability_ids
                )
                for summary in instance["capabilitySummaries"]:
                    assert set(summary) == {
                        "id",
                        "displayName",
                        "connectorFamily",
                        "effect",
                    }
                    capability = capability_by_id[summary["id"]]
                    assert summary["connectorFamily"] == capability.connector_family
                    assert summary["effect"] == capability.effect
                flattened.append(instance)
    assert [item["id"] for item in flattened] == [item.id for item in compiled.instances]


@pytest.mark.asyncio
async def test_api_02_hierarchy_preserves_department_and_community_multiplicity(
    documents: CatalogDocuments,
) -> None:
    response = await _get(_app(StaticCatalogQuery(documents)), "/api/v1/catalog/hierarchy")
    body = response.json()
    assert [item["instanceCount"] for item in body["departmentCounts"]] == [12, 6, 5, 14, 6]
    assert [
        [len(function["instances"]) for function in department["functions"]]
        for department in body["departments"]
    ] == [[6, 2, 4], [3, 3], [2, 3], [6, 6, 2], [5, 1]]

    instances = [
        instance
        for department in body["departments"]
        for function in department["functions"]
        for instance in function["instances"]
    ]
    community = [item for item in instances if item["id"].startswith("inst.community.")]
    assert len(community) == 14
    by_template: dict[str, list[dict[str, object]]] = {}
    for item in community:
        by_template.setdefault(cast(str, item["templateId"]), []).append(item)
    assert len(by_template) == 7
    assert all(
        [item["sourceOrdinal"] for item in items] == [1, 2]
        and [cast(str, item["id"]).rsplit(".", 1)[-1] for item in items] == ["01", "02"]
        for items in by_template.values()
    )
    non_community_counts = Counter(
        item["templateId"] for item in instances if not item["id"].startswith("inst.community.")
    )
    assert set(non_community_counts.values()) == {1}
    assert "Marketing Orchestrator" not in {item["displayName"] for item in instances}


@pytest.mark.asyncio
async def test_api_02_counts_are_derived_from_resolved_records(
    compiled: CompiledCatalog,
) -> None:
    minimal_source = _minimal_catalog(compiled)
    minimal = project_catalog(minimal_source)
    app = _app(StaticCatalogQuery(minimal))
    catalog = await _get(app, "/api/v1/catalog")
    hierarchy = await _get(app, "/api/v1/catalog/hierarchy")

    assert catalog.json()["counts"] == {
        "departments": 1,
        "functions": 1,
        "templates": 1,
        "instances": 1,
        "toolCapabilities": len(minimal_source.tool_capabilities),
        "approvalPolicies": 1,
    }
    assert hierarchy.json()["counts"] == {
        "departments": 1,
        "functions": 1,
        "templates": 1,
        "instances": 1,
    }
    assert hierarchy.json()["departmentCounts"] == [
        {
            "departmentId": hierarchy.json()["departments"][0]["id"],
            "instanceCount": 1,
        }
    ]


@pytest.mark.asyncio
async def test_api_02_etag_is_strong_stable_conditional_and_projection_specific(
    compiled: CompiledCatalog,
    documents: CatalogDocuments,
) -> None:
    app = _app(StaticCatalogQuery(documents))
    first = await _get(app, "/api/v1/catalog/hierarchy")
    second = await _get(app, "/api/v1/catalog/hierarchy")
    catalog = await _get(app, "/api/v1/catalog")
    etag = first.headers["etag"]

    assert first.content == second.content
    assert etag == second.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"') and not etag.startswith("W/")
    assert catalog.headers["etag"] != etag

    not_modified = await _get(
        app,
        "/api/v1/catalog/hierarchy",
        headers={"If-None-Match": etag},
    )
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == etag
    assert not_modified.headers["cache-control"] == "private, no-cache"

    weak = await _get(
        app,
        "/api/v1/catalog/hierarchy",
        headers={"If-None-Match": f"W/{etag}"},
    )
    listed = await _get(
        app,
        "/api/v1/catalog/hierarchy",
        headers={"If-None-Match": f'"another-tag", {etag}'},
    )
    mismatched = await _get(
        app,
        "/api/v1/catalog/hierarchy",
        headers={"If-None-Match": '"not-the-current-representation"'},
    )
    assert weak.status_code == listed.status_code == 304
    assert mismatched.status_code == 200

    changed_hash = project_catalog(replace(compiled, content_hash="catalog-sha256-v1:" + "b" * 64))
    changed_instance = compiled.instances[0].model_copy(
        update={"enabled": not compiled.instances[0].enabled}
    )
    changed_configuration = project_catalog(
        replace(compiled, instances=(changed_instance, *compiled.instances[1:]))
    )
    assert changed_hash.hierarchy.etag != documents.hierarchy.etag
    assert changed_configuration.hierarchy.etag != documents.hierarchy.etag


@pytest.mark.asyncio
async def test_api_02_aggregate_routes_do_not_leak_prompt_or_internal_fields(
    compiled: CompiledCatalog,
    documents: CatalogDocuments,
) -> None:
    app = _app(StaticCatalogQuery(documents))
    for path in AGGREGATE_PATHS:
        response = await _get(app, path)
        rendered = response.text
        assert response.status_code == 200
        assert next(iter(compiled.prompt_text_by_template.values())) not in rendered
        for forbidden in (
            "promptTextByTemplate",
            "systemPromptRef",
            "inputSchemaRef",
            "outputSchemaRef",
            "prompt_text_by_template",
            "system_prompt_ref",
            "input_schema_ref",
            "output_schema_ref",
            "recentRun",
            "latestRun",
        ):
            assert forbidden not in rendered
    hierarchy = (await _get(app, "/api/v1/catalog/hierarchy")).text
    for forbidden in (
        "sourceReferences",
        "implementationNotes",
        "connectorBindings",
        "configurationRevision",
    ):
        assert forbidden not in hierarchy


@pytest.mark.asyncio
async def test_api_02_representation_bytes_and_etag_cannot_drift_after_projection(
    compiled: CompiledCatalog,
) -> None:
    template = compiled.templates[0]
    mutable_schemas = {
        identifier: copy.deepcopy(dict(schema))
        for identifier, schema in compiled.input_schema_by_template.items()
    }
    mutable_source = replace(
        compiled,
        input_schema_by_template=MappingProxyType(mutable_schemas),
    )
    immutable_documents = project_catalog(mutable_source)
    app = _app(StaticCatalogQuery(immutable_documents))
    path = f"/api/v1/agent-templates/{template.id}"
    first = await _get(app, path)
    original_etag = first.headers["etag"]
    canary = "private-user:secret@internal/catalog/mutated-prompt.md"

    mutable_schemas[template.id]["x-security-canary"] = canary
    repeated = await _get(app, path)
    conditional = await _get(app, path, headers={"If-None-Match": original_etag})

    assert repeated.content == first.content
    assert repeated.headers["etag"] == original_etag
    assert canary not in repeated.text
    assert conditional.status_code == 304
    assert conditional.content == b""


@pytest.mark.asyncio
async def test_api_02_static_list_and_detail_routes_are_typed_and_prompt_free(
    compiled: CompiledCatalog,
    documents: CatalogDocuments,
) -> None:
    app = _app(StaticCatalogQuery(documents))
    list_expectations = {
        "/api/v1/agent-templates": (36, "templates"),
        "/api/v1/tool-capabilities": (22, "toolCapabilities"),
        "/api/v1/approval-policies": (2, "approvalPolicies"),
        "/api/v1/agent-instances": (43, "instances"),
    }
    for path, (count, field) in list_expectations.items():
        response = await _get(app, path)
        assert response.status_code == 200
        assert response.json()["count"] == count
        assert len(response.json()[field]) == count

    template = compiled.templates[0]
    instance = next(item for item in compiled.instances if item.template_id == template.id)
    template_detail = await _get(app, f"/api/v1/agent-templates/{template.id}")
    instance_detail = await _get(app, f"/api/v1/agent-instances/{instance.id}")
    assert template_detail.status_code == instance_detail.status_code == 200
    assert template_detail.json()["template"]["id"] == template.id
    assert template_detail.json()["inputSchema"]["$id"] == template.input_schema_id
    assert template_detail.json()["outputSchema"]["$id"] == template.output_schema_id
    assert instance_detail.json()["instance"]["id"] == instance.id
    assert instance_detail.json()["template"]["id"] == template.id
    assert instance_detail.json()["sharedTemplateDeploymentCount"] >= 1
    assert instance_detail.json()["configurationSchema"] == (
        f"/api/v1/agent-instances/{instance.id}/configuration-schema"
    )
    prompt = compiled.prompt_text_by_template[template.id]
    assert prompt not in template_detail.text
    assert prompt not in instance_detail.text
    assert "systemPromptRef" not in template_detail.text
    assert "systemPromptRef" not in instance_detail.text

    missing_template = await _get(
        app,
        "/api/v1/agent-templates/tpl.unknown.group.missing",
    )
    missing_instance = await _get(
        app,
        "/api/v1/agent-instances/inst.unknown.group.missing.01",
    )
    for response in (missing_template, missing_instance):
        assert_problem(
            response,
            status_code=404,
            code="catalog_resource_not_found",
        )

    first_id, second_id = (item.id for item in compiled.templates[:2])
    swapped = dict(documents.template_details)
    swapped[first_id] = documents.template_details[second_id]
    malformed_documents = replace(
        documents,
        template_details=MappingProxyType(swapped),
    )
    mislabeled = await _get(
        _app(StaticCatalogQuery(malformed_documents)),
        f"/api/v1/agent-templates/{first_id}",
    )
    assert mislabeled.status_code == 503
    assert mislabeled.json()["code"] == "catalog_unavailable"


@pytest.mark.asyncio
async def test_api_02_reads_are_offline_side_effect_free_and_compilation_is_offloaded(
    compiled: CompiledCatalog,
) -> None:
    before = _file_fingerprint(CATALOG_ROOT)
    caller_thread = threading.get_ident()
    compiler_threads: list[int] = []

    def recording_compiler(root: Path) -> CompiledCatalog:
        assert root == CATALOG_ROOT
        compiler_threads.append(threading.get_ident())
        return compiled

    service = LocalCatalogQueryService(CATALOG_ROOT, compiler=recording_compiler)
    app = _app(service)
    catalog = await _get(app, "/api/v1/catalog")
    hierarchy = await _get(app, "/api/v1/catalog/hierarchy")

    assert catalog.status_code == hierarchy.status_code == 200
    assert len(compiler_threads) == 1
    assert all(item != caller_thread for item in compiler_threads)
    assert _file_fingerprint(CATALOG_ROOT) == before


@pytest.mark.parametrize("path", AGGREGATE_PATHS)
@pytest.mark.asyncio
async def test_api_02_catalog_source_failures_are_sanitized(
    path: str,
    compiled: CompiledCatalog,
    documents: CatalogDocuments,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "private-user:secret@internal/catalog/private-prompt.md"
    bad_function = compiled.functions[0].model_copy(update={"department_id": "dept.missing"})
    broken = replace(compiled, functions=(bad_function, *compiled.functions[1:]))
    queries: tuple[CatalogQueryExecutor, ...] = (
        ThrowingCatalogQuery(canary),
        MalformedCatalogQuery(),
        StaticCatalogQuery(replace(documents, template_details={})),
        StaticCatalogQuery(
            replace(
                documents,
                catalog=_corrupt_representation(documents.catalog, etag=None),
                hierarchy=_corrupt_representation(documents.hierarchy, etag=None),
            )
        ),
        StaticCatalogQuery(
            replace(
                documents,
                catalog=_corrupt_representation(
                    documents.catalog,
                    content=b'{"promptText":"private-user:secret@internal/catalog/prompt.md"}',
                ),
                hierarchy=_corrupt_representation(
                    documents.hierarchy,
                    content=b'{"promptText":"private-user:secret@internal/catalog/prompt.md"}',
                ),
            )
        ),
        LocalCatalogQueryService(
            CATALOG_ROOT,
            compiler=lambda _root: broken,
        ),
    )
    for query in queries:
        response = await _get(_app(query), path)
        assert_problem(response, status_code=503, code="catalog_unavailable")
        assert response.headers["cache-control"] == "no-store"
        assert canary not in response.text
        assert "private-user" not in response.text
        assert "secret" not in response.text

    monkeypatch.setattr(catalog_routes, "CATALOG_QUERY_TIMEOUT_SECONDS", 0.001)
    timed_out = await _get(_app(SlowCatalogQuery()), path)
    assert timed_out.status_code == 503
    assert timed_out.json()["code"] == "catalog_unavailable"


@pytest.mark.asyncio
async def test_api_02_blocking_projection_and_sync_query_cannot_escape_timeout(
    compiled: CompiledCatalog,
    documents: CatalogDocuments,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_projector = catalog_queries.project_catalog
    compiler_calls = 0

    def slow_projector(source: CompiledCatalog) -> CatalogDocuments:
        time.sleep(0.05)
        return original_projector(source)

    def recording_compiler(_root: Path) -> CompiledCatalog:
        nonlocal compiler_calls
        compiler_calls += 1
        return compiled

    monkeypatch.setattr(catalog_queries, "project_catalog", slow_projector)
    monkeypatch.setattr(catalog_routes, "CATALOG_QUERY_TIMEOUT_SECONDS", 0.001)
    service = LocalCatalogQueryService(CATALOG_ROOT, compiler=recording_compiler)
    app = _app(service)
    timed_out = await asyncio.gather(*(_get(app, "/api/v1/catalog") for _index in range(5)))
    assert {response.status_code for response in timed_out} == {503}
    for response in timed_out:
        assert_problem(response, status_code=503, code="catalog_unavailable")
    assert compiler_calls == 1

    sync_query = BlockingSyncCatalogQuery(documents)
    rejected = await _get(
        _app(cast(CatalogQueryExecutor, sync_query)),
        "/api/v1/catalog",
    )
    assert rejected.status_code == 503
    assert sync_query.called is False


@pytest.mark.asyncio
async def test_api_02_catalog_routes_require_human_control_plane_reader_before_loading(
    compiled: CompiledCatalog,
    documents: CatalogDocuments,
) -> None:
    viewer_query = StaticCatalogQuery(documents)
    viewer = human_principal(roles=frozenset({"viewer"}), scopes=frozenset())
    assert (await _get(_app(viewer_query, principal=viewer), "/api/v1/catalog")).status_code == 200
    assert len(viewer_query.principals) == 1

    for role in ("viewer", "operator", "approver", "local_admin"):
        query = StaticCatalogQuery(documents)
        allowed = human_principal(roles=frozenset({role}), scopes=frozenset())
        response = await _get(_app(query, principal=allowed), "/api/v1/catalog")
        assert response.status_code == 200
        assert len(query.principals) == 1

    template_id = compiled.templates[0].id
    instance_id = compiled.instances[0].id
    read_paths = (
        "/api/v1/catalog",
        "/api/v1/catalog/hierarchy",
        "/api/v1/agent-templates",
        f"/api/v1/agent-templates/{template_id}",
        "/api/v1/tool-capabilities",
        "/api/v1/approval-policies",
        "/api/v1/agent-instances",
        f"/api/v1/agent-instances/{instance_id}",
    )
    auditor = human_principal(roles=frozenset({"auditor"}), scopes=frozenset())
    for denied in (
        auditor,
        service_principal(roles=frozenset({"viewer"}), scopes=frozenset()),
    ):
        query = StaticCatalogQuery(documents)
        for path in read_paths:
            response = await _get(_app(query, principal=denied), path)
            assert_problem(response, status_code=403, code="request_forbidden")
        assert query.principals == []

    compiler_called = False

    def forbidden_compiler(_root: Path) -> CompiledCatalog:
        nonlocal compiler_called
        compiler_called = True
        return compiled

    service = LocalCatalogQueryService(CATALOG_ROOT, compiler=forbidden_compiler)
    with pytest.raises(CatalogAuthorizationError):
        await service.read(auditor)
    assert compiler_called is False

    missing_identity = create_app(
        _settings(),
        catalog_query_service=StaticCatalogQuery(documents),
    )
    del missing_identity.state.identity_provider
    response = await _get(missing_identity, "/api/v1/catalog")
    assert_problem(response, status_code=401, code="authentication_required")

    spoof_query = StaticCatalogQuery(documents)
    spoofed = await _get(
        _app(spoof_query),
        "/api/v1/catalog",
        headers={"X-Actor": "forged-viewer"},
    )
    assert spoofed.status_code == 400
    assert spoof_query.principals == []


def test_api_02_openapi_declares_all_static_typed_read_contracts(
    documents: CatalogDocuments,
) -> None:
    application = _app(StaticCatalogQuery(documents))
    openapi = application.openapi()  # type: ignore[attr-defined]
    expected = {
        "/api/v1/catalog": ("getCatalog", "CatalogResponse"),
        "/api/v1/catalog/hierarchy": ("getCatalogHierarchy", "CatalogHierarchyResponse"),
        "/api/v1/agent-templates": ("listAgentTemplates", "AgentTemplateListResponse"),
        "/api/v1/agent-templates/{template_id}": (
            "getAgentTemplate",
            "AgentTemplateDetailResponse",
        ),
        "/api/v1/tool-capabilities": (
            "listToolCapabilities",
            "ToolCapabilityListResponse",
        ),
        "/api/v1/approval-policies": (
            "listApprovalPolicies",
            "ApprovalPolicyListResponse",
        ),
        "/api/v1/agent-instances": ("listAgentInstances", "AgentInstanceListResponse"),
        "/api/v1/agent-instances/{instance_id}": (
            "getAgentInstance",
            "AgentInstanceDetailResponse",
        ),
    }
    for path, (operation_id, schema_name) in expected.items():
        operation = openapi["paths"][path]["get"]
        assert operation["operationId"] == operation_id
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        if path == "/api/v1/agent-instances/{instance_id}":
            assert response_schema["anyOf"] == [
                {"$ref": "#/components/schemas/AgentInstanceRuntimeDetailResponse"},
                {"$ref": "#/components/schemas/AgentInstanceDetailResponse"},
            ]
        else:
            assert response_schema == {"$ref": f"#/components/schemas/{schema_name}"}
        assert "304" in operation["responses"]
        assert operation["responses"]["503"]["content"] == {
            "application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemDetails"}}
        }
        for response_status in ("200", "304"):
            headers = operation["responses"][response_status]["headers"]
            assert headers["ETag"]["schema"] == {
                "type": "string",
                "pattern": '^"[a-f0-9]{64}"$',
            }
            assert headers["Cache-Control"]["schema"] == {
                "type": "string",
                "const": "private, no-cache",
            }
            assert headers["Vary"]["schema"] == {
                "type": "string",
                "const": "Authorization",
            }
        assert operation["responses"]["503"]["headers"]["Cache-Control"]["schema"] == {
            "type": "string",
            "const": "no-store",
        }
        assert any(
            parameter["name"] == "If-None-Match" and parameter["in"] == "header"
            for parameter in operation["parameters"]
        )
        assert "requestBody" not in operation
    hierarchy_schema = openapi["components"]["schemas"]["CatalogHierarchyResponse"]
    assert hierarchy_schema["additionalProperties"] is False
    assert set(hierarchy_schema["required"]) == {
        "catalogVersion",
        "catalogHash",
        "counts",
        "departmentCounts",
        "departments",
    }
