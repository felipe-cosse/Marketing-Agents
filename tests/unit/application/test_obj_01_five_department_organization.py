"""OBJ-01: represent the exact five-department marketing-agent organization."""

from __future__ import annotations

from pathlib import Path

import pytest
from marketing_agents.application.organization import build_organization_projection
from marketing_agents.infrastructure.catalog import compile_catalog

ROOT = Path(__file__).resolve().parents[3]


def test_obj_01_projection_preserves_exact_five_department_organization() -> None:
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    projection = build_organization_projection(
        catalog.departments, catalog.functions, catalog.templates, catalog.instances
    )
    assert [item.display_name for item in projection.departments] == [
        "Social media",
        "Blog & SEO",
        "Email",
        "Community",
        "Partnerships",
    ]
    assert projection.function_count == 12
    assert projection.template_count == 36
    assert projection.instance_count == 43
    assert [
        sum(
            len(template.instances)
            for function in department.functions
            for template in function.templates
        )
        for department in projection.departments
    ] == [12, 6, 5, 14, 6]


def test_obj_01_projection_is_immutable_and_resolves_instance_names_from_templates() -> None:
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    projection = build_organization_projection(
        catalog.departments, catalog.functions, catalog.templates, catalog.instances
    )
    community = projection.departments[3]
    first_template = community.functions[0].templates[0]
    assert len(first_template.instances) == 2
    assert {item.display_name for item in first_template.instances} == {first_template.display_name}
    with pytest.raises(AttributeError):
        projection.departments = ()  # type: ignore[misc]


def test_obj_01_unknown_hierarchy_reference_fails_closed() -> None:
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    first = catalog.templates[0]
    broken = first.model_copy(update={"function_id": "func.unknown.missing"})
    with pytest.raises(ValueError, match="known function"):
        build_organization_projection(
            catalog.departments,
            catalog.functions,
            [broken, *catalog.templates[1:]],
            catalog.instances,
        )
