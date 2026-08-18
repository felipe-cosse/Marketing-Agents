"""Marketing Agents v1 semantic catalog contracts."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import CatalogIssue
from .models import (
    AgentInstanceRecord,
    AgentTemplateRecord,
    ApprovalPolicyRecord,
    ToolCapabilityRecord,
)

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


def template_core_issues(
    templates: Sequence[AgentTemplateRecord], prompts: Mapping[str, str]
) -> tuple[CatalogIssue, ...]:
    """Validate stable hierarchy identity and the local instructions for every role template."""

    issues: list[CatalogIssue] = []
    sibling_orders: Counter[tuple[str, int]] = Counter(
        (template.function_id, template.display_order) for template in templates
    )
    for (function_id, display_order), count in sorted(sibling_orders.items()):
        if count > 1:
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-display-order-collision",
                    f"display order {display_order} appears {count} times in one function",
                    function_id,
                )
            )
    for template in templates:
        department_slug = template.department_id.removeprefix("dept.")
        function_prefix = f"func.{department_slug}."
        function_slug = template.function_id.removeprefix(function_prefix)
        expected_prefix = f"tpl.{department_slug}.{function_slug}."
        if not template.function_id.startswith(function_prefix) or not template.id.startswith(
            expected_prefix
        ):
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-hierarchy-identity",
                    "template ID must derive from its department and function namespaces",
                    template.id,
                )
            )
        if (
            not template.display_name.strip()
            or template.display_name != template.display_name.strip()
        ):
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-display-name",
                    "template display name must be nonempty and trimmed",
                    template.id,
                )
            )
        if not template.purpose.strip() or template.purpose != template.purpose.strip():
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-purpose",
                    "template purpose must be nonempty and trimmed",
                    template.id,
                )
            )
        prompt = prompts.get(template.id, "")
        if not prompt.startswith(f"# {template.display_name}\n") or (
            f"Purpose: {template.purpose}" not in prompt
        ):
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-instructions-identity",
                    "local instructions must identify the exact template name and purpose",
                    template.id,
                )
            )
    return tuple(sorted(issues))


def _bounded_schema_issues(
    schema: Mapping[str, Any],
    *,
    pointer: str,
    related_id: str,
    source_path: str,
) -> list[CatalogIssue]:
    issues: list[CatalogIssue] = []
    schema_type = schema.get("type")
    if schema_type == "object" and schema.get("additionalProperties") is not False:
        issues.append(
            CatalogIssue(
                source_path,
                pointer,
                "schema-unbounded-object",
                "object schemas must set additionalProperties to false",
                related_id,
            )
        )
    if schema_type == "array" and not isinstance(schema.get("maxItems"), int):
        issues.append(
            CatalogIssue(
                source_path,
                pointer,
                "schema-unbounded-array",
                "array schemas must declare a finite maxItems",
                related_id,
            )
        )
    if (
        schema_type == "string"
        and "const" not in schema
        and "enum" not in schema
        and not isinstance(schema.get("maxLength"), int)
    ):
        issues.append(
            CatalogIssue(
                source_path,
                pointer,
                "schema-unbounded-string",
                "string schemas must declare maxLength or a finite constant/enum",
                related_id,
            )
        )
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            if isinstance(child, Mapping):
                issues.extend(
                    _bounded_schema_issues(
                        child,
                        pointer=f"{pointer}/properties/{name}",
                        related_id=related_id,
                        source_path=source_path,
                    )
                )
    items = schema.get("items")
    if isinstance(items, Mapping):
        issues.extend(
            _bounded_schema_issues(
                items,
                pointer=f"{pointer}/items",
                related_id=related_id,
                source_path=source_path,
            )
        )
    return issues


def template_io_schema_issues(
    templates: Sequence[AgentTemplateRecord],
    input_schemas: Mapping[str, Mapping[str, Any]],
    output_schemas: Mapping[str, Mapping[str, Any]],
) -> tuple[CatalogIssue, ...]:
    """Require one stable, object-shaped, recursively bounded schema pair per template."""

    issues: list[CatalogIssue] = []
    dialect = "https://json-schema.org/draft/2020-12/schema"
    for template in templates:
        for direction, schemas in (("input", input_schemas), ("output", output_schemas)):
            schema = schemas.get(template.id)
            source_path = getattr(template, f"{direction}_schema_ref")
            if schema is None:
                issues.append(
                    CatalogIssue(
                        source_path,
                        "",
                        "template-schema-missing",
                        f"template is missing its {direction} schema",
                        template.id,
                    )
                )
                continue
            expected_id = f"urn:marketing-agents:catalog:v1:{template.id}:{direction}"
            if schema.get("$schema") != dialect or schema.get("$id") != expected_id:
                issues.append(
                    CatalogIssue(
                        source_path,
                        "",
                        "template-schema-identity",
                        "schema dialect and stable identity must match the template and direction",
                        template.id,
                    )
                )
            if schema.get("type") != "object" or not schema.get("required"):
                issues.append(
                    CatalogIssue(
                        source_path,
                        "",
                        "template-schema-shape",
                        "template schemas must be object-shaped with explicit required fields",
                        template.id,
                    )
                )
            issues.extend(
                _bounded_schema_issues(
                    schema,
                    pointer="",
                    related_id=template.id,
                    source_path=source_path,
                )
            )
    return tuple(sorted(issues))


def template_runtime_policy_issues(
    templates: Sequence[AgentTemplateRecord],
    capabilities: Sequence[ToolCapabilityRecord],
    policies: Sequence[ApprovalPolicyRecord],
) -> tuple[CatalogIssue, ...]:
    """Validate each role's maximum authority and finite runtime policy as one contract."""

    issues: list[CatalogIssue] = []
    capability_by_id = {item.id: item for item in capabilities}
    policy_by_id = {item.id: item for item in policies}
    forbidden_families = {"browser", "generic-http", "shell", "sql"}
    for template in templates:
        selected = [
            capability_by_id[item]
            for item in template.allowed_tool_capability_ids
            if item in capability_by_id
        ]
        policy = policy_by_id.get(template.approval_policy_id)
        if not template.allowed_tool_capability_ids:
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-capabilities-empty",
                    "every template must declare its bounded maximum capability set",
                    template.id,
                )
            )
        if any(item.connector_family in forbidden_families for item in selected):
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-generic-authority",
                    "generic browser, HTTP, shell, and SQL authority is forbidden",
                    template.id,
                )
            )
        writes = [item for item in selected if item.effect == "write"]
        if writes and (
            template.operation_classification != "mutating"
            or policy is None
            or policy.kind != "human_external_write"
        ):
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-write-approval",
                    "write capabilities require mutating classification and human approval",
                    template.id,
                )
            )
        if template.operation_classification == "mutating" and not writes:
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-mutating-without-write",
                    "mutating classification requires at least one explicit write capability",
                    template.id,
                )
            )
        if writes and (
            template.retry_policy.max_attempts != 1 or template.retry_policy.backoff != "none"
        ):
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-write-retry",
                    "catalog write actions cannot enable blind automatic retries",
                    template.id,
                )
            )
        if any(item.idempotency_support != "required" for item in writes):
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-write-idempotency",
                    "assigned write capabilities must require connector idempotency",
                    template.id,
                )
            )
        if template.timeout_policy.step_seconds > template.timeout_policy.run_seconds:
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-timeout-order",
                    "step timeout cannot exceed the whole run timeout",
                    template.id,
                )
            )
        if "cap.model.generate-structured" in template.allowed_tool_capability_ids and (
            template.budget_policy.max_model_calls < 1
        ):
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-model-budget",
                    "templates with model authority require a finite positive model-call budget",
                    template.id,
                )
            )
        if not template.source_references or not template.implementation_notes.strip():
            issues.append(
                CatalogIssue(
                    "templates",
                    "",
                    "template-source-notes",
                    "source references and separate implementation notes are required",
                    template.id,
                )
            )
    return tuple(sorted(issues))


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
