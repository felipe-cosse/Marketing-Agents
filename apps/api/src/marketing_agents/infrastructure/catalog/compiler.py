"""Deterministic offline compiler for the version-controlled agent catalog."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import BaseModel, ValidationError

from .errors import CatalogCompilationError, CatalogIssue
from .loader import load_json, load_prompt, load_yaml, record_list
from .models import (
    MARKETING_AGENTS_V1_CONTRACT,
    AgentInstanceRecord,
    AgentTemplateRecord,
    ApprovalPolicyRecord,
    CatalogContract,
    CatalogManifest,
    CatalogValidationReport,
    CompiledCatalog,
    DepartmentRecord,
    FunctionRecord,
    ToolCapabilityRecord,
)
from .references import reject_remote_or_escaped_refs
from .semantics import (
    instance_field_ownership_issues,
    marketing_v1_identity_issues,
    marketing_v1_multiplicity_issues,
    template_core_issues,
)

ID_PATTERNS = {
    "department": re.compile(r"^dept\.[a-z0-9]+(?:-[a-z0-9]+)*$"),
    "function": re.compile(r"^func\.[a-z0-9-]+\.[a-z0-9]+(?:-[a-z0-9]+)*$"),
    "template": re.compile(r"^tpl\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9]+(?:-[a-z0-9]+)*$"),
    "instance": re.compile(r"^inst\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9]+(?:-[a-z0-9]+)*\.[0-9]{2}$"),
    "capability": re.compile(r"^cap\.[a-z0-9-]+\.[a-z0-9]+(?:-[a-z0-9]+)*$"),
    "policy": re.compile(r"^policy\.[a-z0-9]+(?:[.-][a-z0-9]+)*$"),
}


class IdentifiedRecord(Protocol):
    @property
    def id(self) -> str: ...


def _schema_root(catalog_root: Path) -> Path:
    sibling = catalog_root.resolve().parent / "schema"
    if sibling.is_dir():
        return sibling
    project = Path.cwd() / "catalog" / "schema"
    if project.is_dir():
        return project.resolve()
    raise ValueError("catalog structural schema directory is missing")


def _pointer(parts: Any) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(encoded) if encoded else ""


def _issue(
    issues: list[CatalogIssue],
    *,
    code: str,
    message: str,
    source_path: str,
    pointer: str = "",
    related_id: str | None = None,
) -> None:
    issues.append(CatalogIssue(source_path, pointer, code, message, related_id))


def _structural_validate(
    schema_root: Path,
    schema_name: str,
    value: dict[str, Any],
    source_path: str,
    issues: list[CatalogIssue],
) -> None:
    schema = json.loads((schema_root / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path)):
        _issue(
            issues,
            code="structural-schema",
            message=error.message,
            source_path=source_path,
            pointer=_pointer(error.absolute_path),
        )


def _parse_model[ModelT: BaseModel](
    model: type[ModelT],
    value: dict[str, Any],
    source_path: str,
    index: int,
    issues: list[CatalogIssue],
) -> ModelT | None:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        for error in exc.errors(include_url=False, include_input=False):
            _issue(
                issues,
                code="boundary-model",
                message=str(error["msg"]),
                source_path=source_path,
                pointer=_pointer((index, *error["loc"])),
                related_id=value.get("id") if isinstance(value.get("id"), str) else None,
            )
        return None


def _load_record_group[ModelT: BaseModel](
    catalog_root: Path,
    relative_paths: tuple[str, ...],
    wrapper_key: str,
    schema_name: str,
    model: type[ModelT],
    schema_root: Path,
    issues: list[CatalogIssue],
) -> list[ModelT]:
    parsed: list[ModelT] = []
    for relative in relative_paths:
        try:
            document = load_yaml(catalog_root, relative)
            records = record_list(document, wrapper_key)
        except (OSError, UnicodeError, ValueError) as exc:
            _issue(
                issues,
                code="file-load",
                message=str(exc),
                source_path=relative,
            )
            continue
        if wrapper_key == "instances":
            issues.extend(instance_field_ownership_issues(records, relative))
        for index, record in enumerate(records):
            before = len(issues)
            _structural_validate(schema_root, schema_name, record, relative, issues)
            if len(issues) != before:
                continue
            item = _parse_model(model, record, relative, index, issues)
            if item is not None:
                parsed.append(item)
    return parsed


def _check_unique_ids(
    records: Sequence[IdentifiedRecord],
    kind: str,
    source_path: str,
    issues: list[CatalogIssue],
) -> None:
    identifiers = [str(record.id) for record in records]
    for identifier, count in sorted(Counter(identifiers).items()):
        if count > 1:
            _issue(
                issues,
                code="duplicate-id",
                message=f"{kind} ID appears {count} times",
                source_path=source_path,
                related_id=identifier,
            )
        if not ID_PATTERNS[kind].fullmatch(identifier):
            _issue(
                issues,
                code="invalid-id",
                message=f"{kind} ID does not follow the stable ID grammar",
                source_path=source_path,
                related_id=identifier,
            )


def _check_contract(
    contract: CatalogContract,
    departments: list[DepartmentRecord],
    functions: list[FunctionRecord],
    templates: list[AgentTemplateRecord],
    instances: list[AgentInstanceRecord],
    department_counts: dict[str, int],
    issues: list[CatalogIssue],
) -> None:
    actual = {
        "departments": len(departments),
        "functions": len(functions),
        "templates": len(templates),
        "instances": len(instances),
    }
    for field, count in actual.items():
        expected = getattr(contract, field)
        if expected is not None and expected != count:
            _issue(
                issues,
                code="contract-count",
                message=f"expected {expected} {field}, found {count}",
                source_path="manifest.yaml",
            )
    if (
        contract.department_instance_counts is not None
        and dict(contract.department_instance_counts) != department_counts
    ):
        _issue(
            issues,
            code="contract-distribution",
            message="department instance distribution does not match the v1 contract",
            source_path="manifest.yaml",
        )


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "catalog-sha256-v1:" + hashlib.sha256(encoded).hexdigest()


def compile_catalog(
    catalog_root: Path,
    *,
    contract: CatalogContract = MARKETING_AGENTS_V1_CONTRACT,
) -> CompiledCatalog:
    catalog_root = catalog_root.resolve()
    schema_root = _schema_root(catalog_root)
    issues: list[CatalogIssue] = []
    try:
        manifest_data = load_yaml(catalog_root, "manifest.yaml")
        _structural_validate(
            schema_root, "manifest.schema.json", manifest_data, "manifest.yaml", issues
        )
        manifest = CatalogManifest.model_validate(manifest_data)
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        _issue(
            issues,
            code="manifest",
            message=str(exc),
            source_path="manifest.yaml",
        )
        raise CatalogCompilationError(tuple(issues)) from exc
    if issues:
        raise CatalogCompilationError(tuple(issues))

    departments = _load_record_group(
        catalog_root,
        (manifest.files.departments,),
        "departments",
        "department.schema.json",
        DepartmentRecord,
        schema_root,
        issues,
    )
    functions = _load_record_group(
        catalog_root,
        (manifest.files.functions,),
        "functions",
        "function.schema.json",
        FunctionRecord,
        schema_root,
        issues,
    )
    capabilities = _load_record_group(
        catalog_root,
        (manifest.files.tool_capabilities,),
        "tool_capabilities",
        "tool-capability.schema.json",
        ToolCapabilityRecord,
        schema_root,
        issues,
    )
    policies = _load_record_group(
        catalog_root,
        (manifest.files.approval_policies,),
        "approval_policies",
        "approval-policy.schema.json",
        ApprovalPolicyRecord,
        schema_root,
        issues,
    )
    templates = _load_record_group(
        catalog_root,
        manifest.files.templates,
        "templates",
        "template.schema.json",
        AgentTemplateRecord,
        schema_root,
        issues,
    )
    instances = _load_record_group(
        catalog_root,
        manifest.files.instances,
        "instances",
        "instance.schema.json",
        AgentInstanceRecord,
        schema_root,
        issues,
    )

    _check_unique_ids(departments, "department", manifest.files.departments, issues)
    _check_unique_ids(functions, "function", manifest.files.functions, issues)
    _check_unique_ids(capabilities, "capability", manifest.files.tool_capabilities, issues)
    _check_unique_ids(policies, "policy", manifest.files.approval_policies, issues)
    _check_unique_ids(templates, "template", "templates", issues)
    _check_unique_ids(instances, "instance", "instances", issues)

    department_by_id = {item.id: item for item in departments}
    function_by_id = {item.id: item for item in functions}
    capability_by_id = {item.id: item for item in capabilities}
    policy_by_id = {item.id: item for item in policies}
    template_by_id = {item.id: item for item in templates}

    for function_record in functions:
        if function_record.department_id not in department_by_id:
            _issue(
                issues,
                code="broken-reference",
                message="function references an unknown department",
                source_path=manifest.files.functions,
                related_id=function_record.id,
            )

    prompts: dict[str, str] = {}
    input_schemas: dict[str, dict[str, Any]] = {}
    output_schemas: dict[str, dict[str, Any]] = {}
    for template in templates:
        selected_function = function_by_id.get(template.function_id)
        if template.department_id not in department_by_id or selected_function is None:
            _issue(
                issues,
                code="broken-reference",
                message="template references an unknown department or function",
                source_path="templates",
                related_id=template.id,
            )
        elif selected_function.department_id != template.department_id:
            _issue(
                issues,
                code="hierarchy-mismatch",
                message="template department does not match its function department",
                source_path="templates",
                related_id=template.id,
            )
        policy = policy_by_id.get(template.approval_policy_id)
        if policy is None:
            _issue(
                issues,
                code="broken-reference",
                message="template references an unknown approval policy",
                source_path="templates",
                related_id=template.id,
            )
        tools = [
            capability_by_id.get(identifier) for identifier in template.allowed_tool_capability_ids
        ]
        if any(tool is None for tool in tools):
            _issue(
                issues,
                code="broken-reference",
                message="template references an unknown tool capability",
                source_path="templates",
                related_id=template.id,
            )
        has_write = any(tool is not None and tool.effect == "write" for tool in tools)
        if has_write and (
            template.operation_classification != "mutating"
            or policy is None
            or policy.kind != "human_external_write"
        ):
            _issue(
                issues,
                code="unsafe-write-policy",
                message="write capabilities require mutating classification and human approval",
                source_path="templates",
                related_id=template.id,
            )
        if template.operation_classification == "read_only" and has_write:
            _issue(
                issues,
                code="read-only-write-capability",
                message="read-only template cannot contain a write capability",
                source_path="templates",
                related_id=template.id,
            )
        try:
            prompts[template.id] = load_prompt(catalog_root, template.system_prompt_ref)
            input_schema = load_json(catalog_root, template.input_schema_ref)
            output_schema = load_json(catalog_root, template.output_schema_ref)
            for schema in (input_schema, output_schema):
                reject_remote_or_escaped_refs(schema)
                Draft202012Validator.check_schema(schema)
            input_schemas[template.id] = input_schema
            output_schemas[template.id] = output_schema
        except (OSError, UnicodeError, ValueError) as exc:
            _issue(
                issues,
                code="template-resource",
                message=str(exc),
                source_path="templates",
                related_id=template.id,
            )

    department_counts = {identifier: 0 for identifier in department_by_id}
    for instance in instances:
        selected_template = template_by_id.get(instance.template_id)
        if selected_template is None:
            _issue(
                issues,
                code="broken-reference",
                message="instance references an unknown template",
                source_path="instances",
                related_id=instance.id,
            )
            continue
        expected_prefix = "inst." + instance.template_id.removeprefix("tpl.") + "."
        if not instance.id.startswith(expected_prefix):
            _issue(
                issues,
                code="instance-id-template-mismatch",
                message="instance ID must derive from its template ID",
                source_path="instances",
                related_id=instance.id,
            )
        if selected_template.department_id in department_counts:
            department_counts[selected_template.department_id] += 1

    _check_contract(
        contract,
        departments,
        functions,
        templates,
        instances,
        department_counts,
        issues,
    )
    if contract is MARKETING_AGENTS_V1_CONTRACT:
        issues.extend(template_core_issues(templates, prompts))
        issues.extend(marketing_v1_multiplicity_issues(templates, instances))
        issues.extend(marketing_v1_identity_issues(templates, instances))
    if issues:
        raise CatalogCompilationError(tuple(issues))

    departments.sort(key=lambda item: item.display_order)
    functions.sort(
        key=lambda item: (department_by_id[item.department_id].display_order, item.display_order)
    )
    templates.sort(
        key=lambda item: (
            department_by_id[item.department_id].display_order,
            function_by_id[item.function_id].display_order,
            item.display_order,
        )
    )
    template_order = {item.id: index for index, item in enumerate(templates)}
    instances.sort(key=lambda item: (template_order[item.template_id], item.display_order, item.id))
    capabilities.sort(key=lambda item: item.id)
    policies.sort(key=lambda item: item.id)

    semantic_payload = {
        "manifest": manifest.model_dump(mode="json"),
        "departments": [item.model_dump(mode="json") for item in departments],
        "functions": [item.model_dump(mode="json") for item in functions],
        "tool_capabilities": [item.model_dump(mode="json") for item in capabilities],
        "approval_policies": [item.model_dump(mode="json") for item in policies],
        "templates": [item.model_dump(mode="json") for item in templates],
        "instances": [item.model_dump(mode="json") for item in instances],
        "prompts": prompts,
        "input_schemas": input_schemas,
        "output_schemas": output_schemas,
    }
    return CompiledCatalog(
        manifest=manifest,
        departments=tuple(departments),
        functions=tuple(functions),
        tool_capabilities=tuple(capabilities),
        approval_policies=tuple(policies),
        templates=tuple(templates),
        instances=tuple(instances),
        prompt_text_by_template=MappingProxyType(dict(prompts)),
        input_schema_by_template=MappingProxyType(
            {key: MappingProxyType(value) for key, value in input_schemas.items()}
        ),
        output_schema_by_template=MappingProxyType(
            {key: MappingProxyType(value) for key, value in output_schemas.items()}
        ),
        department_instance_counts=MappingProxyType(dict(department_counts)),
        content_hash=_canonical_hash(semantic_payload),
    )


def validate_catalog(
    catalog_root: Path,
    *,
    contract: CatalogContract = MARKETING_AGENTS_V1_CONTRACT,
) -> CatalogValidationReport:
    try:
        compiled = compile_catalog(catalog_root, contract=contract)
    except CatalogCompilationError as exc:
        return CatalogValidationReport(valid=False, issues=exc.issues)
    return CatalogValidationReport(valid=True, issues=(), content_hash=compiled.content_hash)
