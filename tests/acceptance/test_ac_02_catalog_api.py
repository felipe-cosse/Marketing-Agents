"""AC-02: the default Catalog API exposes the exact authoritative inventory."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import create_app
from marketing_agents.api.catalog_queries import LocalCatalogQueryService
from marketing_agents.config import Settings

from tests.support.api import assert_problem

ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = ROOT / "catalog" / "v1"
EXPECTED_COUNTS = {
    "departments": 5,
    "functions": 12,
    "templates": 36,
    "instances": 43,
}
EXPECTED_VERSION = "1.0.0"
EXPECTED_HASH = "catalog-sha256-v1:3970f3f23341d3e43a83ff73985e0485addd6c0df7519595f535420c09a9ced1"
SUCCESS_PATHS = (
    "/api/v1/catalog",
    "/api/v1/catalog/hierarchy",
    "/api/v1/agent-templates",
    "/api/v1/agent-instances",
)


async def _get_all(app: FastAPI, paths: tuple[str, ...]) -> dict[str, Response]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return {path: await client.get(path) for path in paths}


def _assert_default_catalog_composition(app: FastAPI) -> None:
    # AC-02 must cross the production composition seam. Supplying a projected
    # CatalogDocuments fixture or StaticCatalogQuery would not prove that the
    # default app can compile and expose the tracked catalog.
    assert type(app.state.catalog_query_service) is LocalCatalogQueryService


@pytest.mark.asyncio
async def test_ac_02_default_composition_exposes_exact_5_12_36_43_catalog() -> None:
    app = create_app(Settings(_env_file=None, catalog_root=CATALOG_ROOT))
    _assert_default_catalog_composition(app)

    responses = await _get_all(app, SUCCESS_PATHS)
    assert {path: response.status_code for path, response in responses.items()} == {
        path: 200 for path in SUCCESS_PATHS
    }

    catalog = responses["/api/v1/catalog"].json()
    hierarchy = responses["/api/v1/catalog/hierarchy"].json()
    templates = responses["/api/v1/agent-templates"].json()
    instances = responses["/api/v1/agent-instances"].json()

    assert {name: catalog["counts"][name] for name in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert hierarchy["counts"] == EXPECTED_COUNTS
    assert templates["count"] == EXPECTED_COUNTS["templates"]
    assert instances["count"] == EXPECTED_COUNTS["instances"]
    assert len(catalog["departments"]) == EXPECTED_COUNTS["departments"]
    assert len(catalog["functions"]) == EXPECTED_COUNTS["functions"]
    assert len(catalog["templates"]) == len(templates["templates"]) == 36
    assert len(catalog["instances"]) == len(instances["instances"]) == 43


@pytest.mark.asyncio
async def test_ac_02_default_projections_agree_on_stable_ids_version_and_hash() -> None:
    app = create_app(Settings(_env_file=None, catalog_root=CATALOG_ROOT))
    _assert_default_catalog_composition(app)

    responses = await _get_all(app, SUCCESS_PATHS)
    assert all(response.status_code == 200 for response in responses.values())
    catalog = responses["/api/v1/catalog"].json()
    hierarchy = responses["/api/v1/catalog/hierarchy"].json()
    templates = responses["/api/v1/agent-templates"].json()
    instances = responses["/api/v1/agent-instances"].json()

    hierarchy_functions = [
        function for department in hierarchy["departments"] for function in department["functions"]
    ]
    hierarchy_instances = [
        instance for function in hierarchy_functions for instance in function["instances"]
    ]
    catalog_department_ids = tuple(item["id"] for item in catalog["departments"])
    catalog_function_ids = tuple(item["id"] for item in catalog["functions"])
    catalog_template_ids = tuple(item["id"] for item in catalog["templates"])
    catalog_instance_ids = tuple(item["id"] for item in catalog["instances"])

    assert catalog_department_ids == tuple(item["id"] for item in hierarchy["departments"])
    assert catalog_function_ids == tuple(item["id"] for item in hierarchy_functions)
    assert catalog_template_ids == tuple(item["id"] for item in templates["templates"])
    assert catalog_instance_ids == tuple(item["id"] for item in instances["instances"])
    assert catalog_instance_ids == tuple(item["id"] for item in hierarchy_instances)
    assert len(catalog_department_ids) == len(set(catalog_department_ids)) == 5
    assert len(catalog_function_ids) == len(set(catalog_function_ids)) == 12
    assert len(catalog_template_ids) == len(set(catalog_template_ids)) == 36
    assert len(catalog_instance_ids) == len(set(catalog_instance_ids)) == 43
    assert tuple((item["id"], item["templateId"]) for item in catalog["instances"]) == tuple(
        (item["id"], item["templateId"]) for item in hierarchy_instances
    )
    assert tuple((item["id"], item["templateId"]) for item in catalog["instances"]) == tuple(
        (item["id"], item["templateId"]) for item in instances["instances"]
    )
    assert {item["templateId"] for item in catalog["instances"]} == set(catalog_template_ids)

    assert catalog["projectionVersion"] == "catalog-read-v1"
    assert catalog["manifest"]["contentVersion"] == EXPECTED_VERSION
    assert {
        catalog["catalogVersion"],
        hierarchy["catalogVersion"],
        templates["catalogVersion"],
        instances["catalogVersion"],
    } == {EXPECTED_VERSION}
    assert {
        catalog["catalogHash"],
        hierarchy["catalogHash"],
        templates["catalogHash"],
        instances["catalogHash"],
    } == {EXPECTED_HASH}
    assert all(
        re.fullmatch(r'"[a-f0-9]{64}"', response.headers["etag"]) for response in responses.values()
    )


def _catalog_with_one_instance_removed(tmp_path: Path) -> tuple[Path, str]:
    copied_catalog = tmp_path / "catalog"
    shutil.copytree(ROOT / "catalog", copied_catalog)
    instances_path = copied_catalog / "v1" / "instances" / "community.yaml"
    document = yaml.safe_load(instances_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    records = document.get("instances")
    assert isinstance(records, list) and len(records) == 14
    removed = records.pop()
    assert isinstance(removed, dict) and isinstance(removed.get("id"), str)
    instances_path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )
    return copied_catalog / "v1", removed["id"]


@pytest.mark.parametrize(
    "path",
    SUCCESS_PATHS,
)
@pytest.mark.asyncio
async def test_ac_02_default_composition_fails_closed_on_catalog_count_drift(
    tmp_path: Path,
    path: str,
) -> None:
    drifted_root, removed_instance_id = _catalog_with_one_instance_removed(tmp_path)
    app = create_app(Settings(_env_file=None, catalog_root=drifted_root))
    _assert_default_catalog_composition(app)

    response = (await _get_all(app, (path,)))[path]

    assert_problem(response, status_code=503, code="catalog_unavailable")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert removed_instance_id not in response.text
    assert "contract-count" not in response.text
    assert "community.yaml" not in response.text
