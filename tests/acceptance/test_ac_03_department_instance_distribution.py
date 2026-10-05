"""AC-03 acceptance for the exact department instance distribution."""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.api.catalog_queries import LocalCatalogQueryService
from marketing_agents.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = ROOT / "catalog" / "v1"
EXPECTED_GLOBAL_COUNTS = {
    "departments": 5,
    "functions": 12,
    "templates": 36,
    "instances": 43,
}
EXPECTED_DEPARTMENT_COUNTS = {
    "dept.social-media": 12,
    "dept.blog-seo": 6,
    "dept.email": 5,
    "dept.community": 14,
    "dept.partnerships": 6,
}
RESERVED_UI_NODE_IDS = {
    "root",
    "control-plane.marketing-orchestrator",
}


def _copy_catalog(tmp_path: Path) -> Path:
    copied_catalog = tmp_path / "catalog"
    shutil.copytree(ROOT / "catalog", copied_catalog)
    return copied_catalog / "v1"


async def _read_real_default_api(catalog_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    app = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            catalog_root=catalog_root,
        )
    )
    assert type(app.state.catalog_query_service) is LocalCatalogQueryService

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        catalog_response = await client.get("/api/v1/catalog")
        hierarchy_response = await client.get("/api/v1/catalog/hierarchy")

    assert catalog_response.status_code == 200
    assert hierarchy_response.status_code == 200
    return catalog_response.json(), hierarchy_response.json()


def _advertised_counts(document: dict[str, Any]) -> dict[str, int]:
    summaries = document["departmentCounts"]
    department_ids = [item["departmentId"] for item in summaries]
    assert len(summaries) == 5
    assert len(department_ids) == len(set(department_ids)) == 5
    return {item["departmentId"]: item["instanceCount"] for item in summaries}


def _flat_join_counts(catalog: dict[str, Any]) -> dict[str, int]:
    template_by_id = {item["id"]: item for item in catalog["templates"]}
    return dict(
        Counter(
            template_by_id[instance["templateId"]]["departmentId"]
            for instance in catalog["instances"]
        )
    )


def _flatten_hierarchy(hierarchy: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        instance
        for department in hierarchy["departments"]
        for function in department["functions"]
        for instance in function["instances"]
    ]


def _nested_counts(hierarchy: dict[str, Any]) -> dict[str, int]:
    return {
        department["id"]: sum(len(function["instances"]) for function in department["functions"])
        for department in hierarchy["departments"]
    }


def _nested_department_by_instance(hierarchy: dict[str, Any]) -> dict[str, str]:
    return {
        instance["id"]: department["id"]
        for department in hierarchy["departments"]
        for function in department["functions"]
        for instance in function["instances"]
    }


def _assert_exact_structural_distribution(
    catalog: dict[str, Any],
    hierarchy: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    catalog_counts = {key: catalog["counts"][key] for key in EXPECTED_GLOBAL_COUNTS}
    assert catalog_counts == EXPECTED_GLOBAL_COUNTS
    assert hierarchy["counts"] == EXPECTED_GLOBAL_COUNTS

    for document in (catalog, hierarchy):
        department_ids = [item["id"] for item in document["departments"]]
        assert len(department_ids) == len(set(department_ids)) == 5
        assert set(department_ids) == set(EXPECTED_DEPARTMENT_COUNTS)
    template_ids = [item["id"] for item in catalog["templates"]]
    assert len(template_ids) == len(set(template_ids)) == 36

    flat_instances = catalog["instances"]
    nested_instances = _flatten_hierarchy(hierarchy)
    flat_ids = [item["id"] for item in flat_instances]
    nested_ids = [item["id"] for item in nested_instances]

    assert len(flat_ids) == len(set(flat_ids)) == 43
    assert len(nested_ids) == len(set(nested_ids)) == 43
    assert set(nested_ids) == set(flat_ids)
    assert RESERVED_UI_NODE_IDS.isdisjoint(flat_ids)
    assert RESERVED_UI_NODE_IDS.isdisjoint(nested_ids)
    assert "Marketing Orchestrator" not in {item["displayName"] for item in nested_instances}

    template_by_id = {item["id"]: item for item in catalog["templates"]}
    flat_department_by_instance = {
        instance["id"]: template_by_id[instance["templateId"]]["departmentId"]
        for instance in flat_instances
    }
    assert _nested_department_by_instance(hierarchy) == flat_department_by_instance

    # These are independent derivations: flat instance-to-template joins, nested
    # hierarchy membership, and each endpoint's advertised aggregate.
    assert _flat_join_counts(catalog) == EXPECTED_DEPARTMENT_COUNTS
    assert _nested_counts(hierarchy) == EXPECTED_DEPARTMENT_COUNTS
    assert _advertised_counts(catalog) == EXPECTED_DEPARTMENT_COUNTS
    assert _advertised_counts(hierarchy) == EXPECTED_DEPARTMENT_COUNTS
    return flat_instances, nested_instances


@pytest.mark.asyncio
async def test_ac_03_real_default_catalog_api_reports_exact_department_distribution() -> None:
    catalog, hierarchy = await _read_real_default_api(CATALOG_ROOT)

    _assert_exact_structural_distribution(catalog, hierarchy)


@pytest.mark.asyncio
async def test_ac_03_disabled_source_instances_remain_in_structural_counts(
    tmp_path: Path,
) -> None:
    copied_root = _copy_catalog(tmp_path)
    expected_disabled_ids: set[str] = set()

    # This mutates copied source defaults only. It intentionally makes no claim
    # about effective live database configuration.
    for instance_path in sorted((copied_root / "instances").glob("*.yaml")):
        payload = yaml.safe_load(instance_path.read_text(encoding="utf-8"))
        instance = payload["instances"][0]
        assert instance["enabled"] is True
        instance["enabled"] = False
        expected_disabled_ids.add(instance["id"])
        instance_path.write_text(
            yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )

    assert len(expected_disabled_ids) == len(EXPECTED_DEPARTMENT_COUNTS)
    catalog, hierarchy = await _read_real_default_api(copied_root)
    flat_instances, nested_instances = _assert_exact_structural_distribution(
        catalog,
        hierarchy,
    )

    flat_by_id = {item["id"]: item for item in flat_instances}
    nested_by_id = {item["id"]: item for item in nested_instances}
    template_by_id = {item["id"]: item for item in catalog["templates"]}
    disabled_departments = Counter(
        template_by_id[flat_by_id[instance_id]["templateId"]]["departmentId"]
        for instance_id in expected_disabled_ids
    )

    assert disabled_departments == Counter(
        {department_id: 1 for department_id in EXPECTED_DEPARTMENT_COUNTS}
    )
    assert {
        item["id"] for item in flat_instances if item["enabled"] is False
    } == expected_disabled_ids
    assert {
        item["id"] for item in nested_instances if item["enabled"] is False
    } == expected_disabled_ids
    assert all(flat_by_id[instance_id]["enabled"] is False for instance_id in expected_disabled_ids)
    assert all(
        nested_by_id[instance_id]["enabled"] is False for instance_id in expected_disabled_ids
    )
