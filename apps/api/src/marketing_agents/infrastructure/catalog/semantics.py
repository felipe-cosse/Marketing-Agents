"""Marketing Agents v1 semantic catalog contracts."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence

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
