"""Framework-independent immutable organization projection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

EXPECTED_DEPARTMENT_IDS = (
    "dept.social-media",
    "dept.blog-seo",
    "dept.email",
    "dept.community",
    "dept.partnerships",
)


class DepartmentSource(Protocol):
    id: str
    display_name: str
    display_order: int


class FunctionSource(Protocol):
    id: str
    department_id: str
    display_name: str
    display_order: int


class TemplateSource(Protocol):
    id: str
    function_id: str
    display_name: str
    display_order: int


class InstanceSource(Protocol):
    id: str
    template_id: str
    display_order: int
    enabled: bool


@dataclass(frozen=True)
class AgentInstanceView:
    id: str
    template_id: str
    display_name: str
    display_order: int
    enabled: bool


@dataclass(frozen=True)
class RoleTemplateView:
    id: str
    display_name: str
    display_order: int
    instances: tuple[AgentInstanceView, ...]


@dataclass(frozen=True)
class FunctionView:
    id: str
    display_name: str
    display_order: int
    templates: tuple[RoleTemplateView, ...]


@dataclass(frozen=True)
class DepartmentView:
    id: str
    display_name: str
    display_order: int
    functions: tuple[FunctionView, ...]


@dataclass(frozen=True)
class OrganizationProjection:
    departments: tuple[DepartmentView, ...]

    @property
    def function_count(self) -> int:
        return sum(len(department.functions) for department in self.departments)

    @property
    def template_count(self) -> int:
        return sum(
            len(function.templates)
            for department in self.departments
            for function in department.functions
        )

    @property
    def instance_count(self) -> int:
        return sum(
            len(template.instances)
            for department in self.departments
            for function in department.functions
            for template in function.templates
        )


def build_organization_projection(
    departments: Sequence[DepartmentSource],
    functions: Sequence[FunctionSource],
    templates: Sequence[TemplateSource],
    instances: Sequence[InstanceSource],
) -> OrganizationProjection:
    """Resolve and validate the exact source-modeled organization hierarchy."""

    ordered_departments = sorted(departments, key=lambda item: (item.display_order, item.id))
    if tuple(item.id for item in ordered_departments) != EXPECTED_DEPARTMENT_IDS:
        raise ValueError("organization must contain the exact five source departments in order")
    department_ids = {item.id for item in departments}
    if any(item.department_id not in department_ids for item in functions):
        raise ValueError("function references an unknown department")
    function_by_id = {item.id: item for item in functions}
    if len(function_by_id) != len(functions) or any(
        item.function_id not in function_by_id for item in templates
    ):
        raise ValueError("templates must reference one unique known function")
    template_by_id = {item.id: item for item in templates}
    if len(template_by_id) != len(templates) or any(
        item.template_id not in template_by_id for item in instances
    ):
        raise ValueError("instances must reference one unique known template")

    department_views: list[DepartmentView] = []
    for department in ordered_departments:
        function_views: list[FunctionView] = []
        selected_functions = sorted(
            (item for item in functions if item.department_id == department.id),
            key=lambda item: (item.display_order, item.id),
        )
        for function in selected_functions:
            template_views: list[RoleTemplateView] = []
            selected_templates = sorted(
                (item for item in templates if item.function_id == function.id),
                key=lambda item: (item.display_order, item.id),
            )
            for template in selected_templates:
                selected_instances = sorted(
                    (item for item in instances if item.template_id == template.id),
                    key=lambda item: (item.display_order, item.id),
                )
                template_views.append(
                    RoleTemplateView(
                        id=template.id,
                        display_name=template.display_name,
                        display_order=template.display_order,
                        instances=tuple(
                            AgentInstanceView(
                                id=instance.id,
                                template_id=template.id,
                                display_name=template.display_name,
                                display_order=instance.display_order,
                                enabled=instance.enabled,
                            )
                            for instance in selected_instances
                        ),
                    )
                )
            function_views.append(
                FunctionView(
                    id=function.id,
                    display_name=function.display_name,
                    display_order=function.display_order,
                    templates=tuple(template_views),
                )
            )
        department_views.append(
            DepartmentView(
                id=department.id,
                display_name=department.display_name,
                display_order=department.display_order,
                functions=tuple(function_views),
            )
        )
    projection = OrganizationProjection(tuple(department_views))
    if (projection.function_count, projection.template_count, projection.instance_count) != (
        12,
        36,
        43,
    ):
        raise ValueError("organization hierarchy must resolve exactly 12/36/43 children")
    return projection
