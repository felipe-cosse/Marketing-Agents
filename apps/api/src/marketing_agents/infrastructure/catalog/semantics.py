"""Marketing Agents v1 semantic catalog contracts."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import CatalogIssue
from .models import AgentInstanceRecord, AgentTemplateRecord

COMMUNITY_DEPARTMENT = "dept.community"
COMMUNITY_FUNCTION_TEMPLATE_COUNTS = {
    "func.community.events": 3,
    "func.community.education": 3,
    "func.community.discussion": 1,
}
COMMUNITY_FUNCTION_INSTANCE_COUNTS = {
    "func.community.events": 6,
    "func.community.education": 6,
    "func.community.discussion": 2,
}
INSTANCE_TEMPLATE_OWNED_FIELDS = frozenset(
    {
        "display_name",
        "department_id",
        "function_id",
        "purpose",
        "system_prompt_ref",
        "input_schema_ref",
        "output_schema_ref",
        "allowed_tool_capability_ids",
        "supported_trigger_types",
        "operation_classification",
        "approval_policy_id",
        "retry_policy",
        "timeout_policy",
        "budget_policy",
        "rate_limit_policy",
        "source_confidence",
        "source_references",
        "implementation_notes",
    }
)


def instance_field_ownership_issues(
    records: Sequence[Mapping[str, Any]], source_path: str
) -> tuple[CatalogIssue, ...]:
    """Reject instance documents that copy template-owned role definition fields."""

    issues: list[CatalogIssue] = []
    for index, record in enumerate(records):
        for field in sorted(set(record) & INSTANCE_TEMPLATE_OWNED_FIELDS):
            related = record.get("id")
            issues.append(
                CatalogIssue(
                    source_path,
                    f"/{index}/{field}",
                    "instance-template-field",
                    "deployment instances cannot override template-owned fields",
                    related if isinstance(related, str) else None,
                )
            )
    return tuple(issues)


def marketing_v1_identity_issues(
    templates: Sequence[AgentTemplateRecord],
    instances: Sequence[AgentInstanceRecord],
) -> tuple[CatalogIssue, ...]:
    """Return issues for stable deployment IDs and source-ordinal metadata."""

    issues: list[CatalogIssue] = []
    template_by_id = {template.id: template for template in templates}
    for identifier, count in sorted(Counter(item.id for item in instances).items()):
        if count != 1:
            issues.append(
                CatalogIssue(
                    "instances",
                    "",
                    "duplicate-instance-id",
                    f"instance ID appears {count} times",
                    identifier,
                )
            )
    for instance in instances:
        template = template_by_id.get(instance.template_id)
        if template is None:
            continue
        try:
            ordinal = int(instance.id.rsplit(".", 1)[1])
        except (IndexError, ValueError):
            ordinal = -1
        expected_id = f"inst.{template.id.removeprefix('tpl.')}.{ordinal:02d}"
        if instance.id != expected_id:
            issues.append(
                CatalogIssue(
                    "instances",
                    "",
                    "instance-template-identity",
                    "instance ID namespace must derive from its referenced template",
                    instance.id,
                )
            )
        expected_ordinals = {1, 2} if template.department_id == COMMUNITY_DEPARTMENT else {1}
        if ordinal not in expected_ordinals:
            issues.append(
                CatalogIssue(
                    "instances",
                    "",
                    "instance-source-ordinal",
                    "instance ordinal is not allowed for its department",
                    instance.id,
                )
            )
        if instance.variant is None or instance.variant.source_ordinal != ordinal:
            issues.append(
                CatalogIssue(
                    "instances",
                    "",
                    "instance-variant-ordinal",
                    "variant source ordinal must match the stable instance ID suffix",
                    instance.id,
                )
            )
        if instance.variant is not None and instance.variant.variant_label is not None:
            issues.append(
                CatalogIssue(
                    "instances",
                    "",
                    "invented-variant-label",
                    "variant labels require source evidence and must be null in catalog v1",
                    instance.id,
                )
            )
    return tuple(sorted(issues))


def marketing_v1_multiplicity_issues(
    templates: Sequence[AgentTemplateRecord],
    instances: Sequence[AgentInstanceRecord],
) -> tuple[CatalogIssue, ...]:
    """Return deterministic issues for the authoritative deployment multiplicity contract."""

    issues: list[CatalogIssue] = []
    by_template: dict[str, list[AgentInstanceRecord]] = defaultdict(list)
    for instance in instances:
        by_template[instance.template_id].append(instance)

    community_templates = [
        template for template in templates if template.department_id == COMMUNITY_DEPARTMENT
    ]
    if len(community_templates) != 7:
        issues.append(
            CatalogIssue(
                "instances/community.yaml",
                "",
                "community-template-count",
                f"expected 7 Community templates, found {len(community_templates)}",
            )
        )

    template_function_counts = Counter(template.function_id for template in community_templates)
    if template_function_counts != COMMUNITY_FUNCTION_TEMPLATE_COUNTS:
        issues.append(
            CatalogIssue(
                "templates/community.yaml",
                "",
                "community-function-distribution",
                "Community template function distribution must be "
                "events 3, education 3, discussion 1",
            )
        )

    instance_function_counts: Counter[str] = Counter()
    template_by_id = {template.id: template for template in templates}
    for instance in instances:
        template = template_by_id.get(instance.template_id)
        if template is not None and template.department_id == COMMUNITY_DEPARTMENT:
            instance_function_counts[template.function_id] += 1
    if instance_function_counts != COMMUNITY_FUNCTION_INSTANCE_COUNTS:
        issues.append(
            CatalogIssue(
                "instances/community.yaml",
                "",
                "community-instance-distribution",
                "Community instance distribution must be events 6, education 6, discussion 2",
            )
        )

    for template in templates:
        deployments = sorted(by_template.get(template.id, []), key=lambda item: item.id)
        is_community = template.department_id == COMMUNITY_DEPARTMENT
        expected_ordinals = (1, 2) if is_community else (1,)
        if len(deployments) != len(expected_ordinals):
            issues.append(
                CatalogIssue(
                    "instances",
                    "",
                    "deployment-multiplicity",
                    f"template requires exactly {len(expected_ordinals)} deployment instance(s)",
                    template.id,
                )
            )
            continue
        actual_ids = tuple(item.id for item in deployments)
        expected_ids = tuple(
            f"inst.{template.id.removeprefix('tpl.')}.{ordinal:02d}"
            for ordinal in expected_ordinals
        )
        if actual_ids != expected_ids:
            issues.append(
                CatalogIssue(
                    "instances",
                    "",
                    "deployment-ordinal",
                    "deployment IDs must use the exact source ordinal sequence",
                    template.id,
                )
            )
        if is_community:
            variants = tuple(
                (item.variant.source_ordinal, item.variant.variant_label)
                if item.variant is not None
                else None
                for item in deployments
            )
            if variants != ((1, None), (2, None)):
                issues.append(
                    CatalogIssue(
                        "instances/community.yaml",
                        "",
                        "community-variant",
                        "Community variants must contain source ordinals 1 and 2 with null labels",
                        template.id,
                    )
                )
    return tuple(sorted(issues))
