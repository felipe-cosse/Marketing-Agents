"""Typed, safe public projections for the static marketing-agent catalog."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CatalogApiModel(BaseModel):
    """Catalog DTO base with the camel-case contract documented in Plan 09."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class CatalogManifestView(CatalogApiModel):
    format_version: Literal[1]
    content_version: str = Field(min_length=1, max_length=64)
    json_schema_dialect: Literal["https://json-schema.org/draft/2020-12/schema"]


class CatalogCounts(CatalogApiModel):
    departments: int = Field(ge=0)
    functions: int = Field(ge=0)
    templates: int = Field(ge=0)
    instances: int = Field(ge=0)
    tool_capabilities: int = Field(ge=0)
    approval_policies: int = Field(ge=0)


class HierarchyCounts(CatalogApiModel):
    departments: int = Field(ge=0)
    functions: int = Field(ge=0)
    templates: int = Field(ge=0)
    instances: int = Field(ge=0)


class DepartmentCountView(CatalogApiModel):
    department_id: str
    instance_count: int = Field(ge=0)


class DepartmentView(CatalogApiModel):
    id: str
    display_name: str
    display_order: int = Field(ge=1)
    source_references: tuple[str, ...]


class FunctionView(CatalogApiModel):
    id: str
    department_id: str
    display_name: str
    display_order: int = Field(ge=1)
    source_references: tuple[str, ...]


class ToolCapabilityView(CatalogApiModel):
    id: str
    display_name: str
    description: str
    effect: Literal["read", "write"]
    connector_family: str
    idempotency_support: Literal["not_applicable", "required", "supported", "unavailable"]
    default_timeout_seconds: int = Field(ge=1, le=120)
    data_classification: Literal["public", "internal", "personal", "sensitive"]


class ApprovalPolicyView(CatalogApiModel):
    id: str
    kind: Literal["none", "human_external_write"]
    required_roles: tuple[str, ...]
    expiry_seconds: int = Field(ge=60, le=86_400)
    allow_self_approval: bool


class RetryPolicyView(CatalogApiModel):
    max_attempts: int = Field(ge=1, le=3)
    backoff: Literal["none", "bounded_exponential"]


class TimeoutPolicyView(CatalogApiModel):
    step_seconds: int = Field(ge=1, le=120)
    run_seconds: int = Field(ge=1, le=600)


class BudgetPolicyView(CatalogApiModel):
    max_steps: int = Field(ge=1, le=20)
    max_model_calls: int = Field(ge=0, le=10)
    max_tool_calls: int = Field(ge=0, le=20)
    max_input_bytes: int = Field(ge=1, le=1_048_576)
    max_input_field_bytes: int = Field(ge=1, le=262_144)
    max_output_bytes: int = Field(ge=1, le=4_194_304)
    max_model_output_tokens: int = Field(ge=1, le=32_768)


class RateLimitPolicyView(CatalogApiModel):
    max_calls: int = Field(ge=1, le=100)
    window_seconds: int = Field(ge=1, le=3_600)


class AgentTemplateView(CatalogApiModel):
    id: str
    display_name: str
    department_id: str
    function_id: str
    display_order: int = Field(ge=1)
    purpose: str
    input_schema_id: str
    output_schema_id: str
    allowed_tool_capability_ids: tuple[str, ...]
    supported_trigger_types: tuple[Literal["manual", "webhook", "schedule"], ...]
    operation_classification: Literal["read_only", "mutating"]
    output_handling: Literal["standard", "advisory"]
    approval_policy_id: str
    retry_policy: RetryPolicyView
    timeout_policy: TimeoutPolicyView
    budget_policy: BudgetPolicyView
    rate_limit_policy: RateLimitPolicyView
    source_confidence: Literal["high", "medium", "low"]
    source_references: tuple[str, ...]
    implementation_notes: str


class TriggerBindingView(CatalogApiModel):
    type: Literal["manual", "webhook", "schedule"]
    enabled: bool
    event_source: str | None = None
    cron: str | None = None
    timezone: str | None = None
    misfire_policy: Literal["skip", "run_once"] | None = None
    misfire_grace_seconds: int | None = Field(default=None, ge=0, le=86_400)


class ConnectorBindingView(CatalogApiModel):
    connector_family: str
    binding_id: str
    enabled: bool


class ScheduleBindingView(CatalogApiModel):
    cron: str
    timezone: str
    misfire_policy: Literal["skip", "run_once"]
    misfire_grace_seconds: int = Field(ge=0, le=86_400)


class AgentInstanceView(CatalogApiModel):
    id: str
    template_id: str
    display_order: int = Field(ge=1)
    enabled: bool
    source_ordinal: int = Field(ge=1, le=99)
    variant_label: str | None
    trigger_bindings: tuple[TriggerBindingView, ...]
    connector_bindings: dict[str, ConnectorBindingView]
    schedule: ScheduleBindingView | None
    configuration_revision: int = Field(ge=1)
    configuration_etag: str = Field(pattern=r'^"instance-configuration-v1-[1-9][0-9]*"$')


class CatalogResponse(CatalogApiModel):
    projection_version: Literal["catalog-read-v1"] = "catalog-read-v1"
    manifest: CatalogManifestView
    catalog_version: str
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    counts: CatalogCounts
    department_counts: tuple[DepartmentCountView, ...]
    departments: tuple[DepartmentView, ...]
    functions: tuple[FunctionView, ...]
    templates: tuple[AgentTemplateView, ...]
    instances: tuple[AgentInstanceView, ...]
    tool_capabilities: tuple[ToolCapabilityView, ...]
    approval_policies: tuple[ApprovalPolicyView, ...]


class CapabilitySummaryView(CatalogApiModel):
    id: str
    display_name: str
    connector_family: str
    effect: Literal["read", "write"]


class HierarchyInstanceView(CatalogApiModel):
    id: str
    template_id: str
    display_name: str
    purpose: str
    display_order: int = Field(ge=1)
    enabled: bool
    operation_classification: Literal["read_only", "mutating"]
    trigger_types: tuple[Literal["manual", "webhook", "schedule"], ...]
    capability_summaries: tuple[CapabilitySummaryView, ...]
    source_ordinal: int = Field(ge=1, le=99)


class HierarchyFunctionView(CatalogApiModel):
    id: str
    display_name: str
    display_order: int = Field(ge=1)
    instances: tuple[HierarchyInstanceView, ...]


class HierarchyDepartmentView(CatalogApiModel):
    id: str
    display_name: str
    display_order: int = Field(ge=1)
    functions: tuple[HierarchyFunctionView, ...]


class CatalogHierarchyResponse(CatalogApiModel):
    catalog_version: str
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    counts: HierarchyCounts
    department_counts: tuple[DepartmentCountView, ...]
    departments: tuple[HierarchyDepartmentView, ...]


class AgentTemplateListResponse(CatalogApiModel):
    catalog_version: str
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    count: int = Field(ge=0)
    templates: tuple[AgentTemplateView, ...]


class AgentTemplateDetailResponse(CatalogApiModel):
    catalog_version: str
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    template: AgentTemplateView
    deployment_count: int = Field(ge=0)
    capabilities: tuple[ToolCapabilityView, ...]
    approval_policy: ApprovalPolicyView
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    source_references: tuple[str, ...]
    implementation_notes: str


class ToolCapabilityListResponse(CatalogApiModel):
    catalog_version: str
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    count: int = Field(ge=0)
    tool_capabilities: tuple[ToolCapabilityView, ...]


class ApprovalPolicyListResponse(CatalogApiModel):
    catalog_version: str
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    count: int = Field(ge=0)
    approval_policies: tuple[ApprovalPolicyView, ...]


class AgentInstanceListResponse(CatalogApiModel):
    catalog_version: str
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    count: int = Field(ge=0)
    instances: tuple[AgentInstanceView, ...]


class AgentInstanceDetailResponse(CatalogApiModel):
    catalog_version: str
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    instance: AgentInstanceView
    template: AgentTemplateView
    shared_template_deployment_count: int = Field(ge=1)
    capabilities: tuple[ToolCapabilityView, ...]
    approval_policy: ApprovalPolicyView
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    template_source_references: tuple[str, ...]
    template_implementation_notes: str
    configuration_schema: str


class CatalogProblem(CatalogApiModel):
    code: Literal["catalog_unavailable", "catalog_resource_not_found"]
    message: str
