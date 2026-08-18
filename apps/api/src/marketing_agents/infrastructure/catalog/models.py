"""Frozen boundary models and immutable compiler result."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import CatalogIssue


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogFiles(FrozenModel):
    departments: str
    functions: str
    tool_capabilities: str
    approval_policies: str
    templates: tuple[str, ...]
    instances: tuple[str, ...]


class CatalogManifest(FrozenModel):
    format_version: Literal[1]
    content_version: str
    json_schema_dialect: Literal["https://json-schema.org/draft/2020-12/schema"]
    files: CatalogFiles
    source_evidence: tuple[str, ...] = ()


class DepartmentRecord(FrozenModel):
    id: str
    display_name: str
    display_order: int = Field(ge=1, le=10000)
    source_references: tuple[str, ...]


class FunctionRecord(FrozenModel):
    id: str
    department_id: str
    display_name: str
    display_order: int = Field(ge=1, le=10000)
    source_references: tuple[str, ...]


class ToolCapabilityRecord(FrozenModel):
    id: str
    description: str
    effect: Literal["read", "write"]
    connector_family: str
    idempotency_support: Literal["not_applicable", "required", "supported", "unavailable"]
    default_timeout_seconds: int = Field(ge=1, le=120)
    data_classification: Literal["public", "internal", "personal", "sensitive"] = "internal"


class ApprovalPolicyRecord(FrozenModel):
    id: str
    kind: Literal["none", "human_external_write"]
    required_roles: tuple[str, ...]
    expiry_seconds: int = Field(ge=60, le=86400)
    allow_self_approval: bool


class RetryPolicy(FrozenModel):
    max_attempts: int = Field(ge=1, le=3)
    backoff: Literal["none", "bounded_exponential"]


class TimeoutPolicy(FrozenModel):
    step_seconds: int = Field(ge=1, le=120)
    run_seconds: int = Field(ge=1, le=600)


class BudgetPolicy(FrozenModel):
    max_steps: int = Field(ge=1, le=20)
    max_model_calls: int = Field(ge=0, le=10)
    max_tool_calls: int = Field(ge=0, le=20)


class RateLimitPolicy(FrozenModel):
    max_calls: int = Field(ge=1, le=100)
    window_seconds: int = Field(ge=1, le=3600)


class AgentTemplateRecord(FrozenModel):
    id: str
    display_name: str
    department_id: str
    function_id: str
    display_order: int = Field(ge=1, le=10000)
    purpose: str
    system_prompt_ref: str
    input_schema_ref: str
    output_schema_ref: str
    allowed_tool_capability_ids: tuple[str, ...]
    supported_trigger_types: tuple[Literal["manual", "webhook", "schedule"], ...]
    operation_classification: Literal["read_only", "mutating"]
    output_handling: Literal["standard", "advisory"] = "standard"
    approval_policy_id: str
    retry_policy: RetryPolicy
    timeout_policy: TimeoutPolicy
    budget_policy: BudgetPolicy
    rate_limit_policy: RateLimitPolicy
    source_confidence: Literal["high", "medium", "low"]
    source_references: tuple[str, ...]
    implementation_notes: str


class InstanceVariant(FrozenModel):
    source_ordinal: int = Field(ge=1, le=99)
    variant_label: str | None = Field(default=None, max_length=100)


class TriggerBinding(FrozenModel):
    type: Literal["manual", "webhook", "schedule"]
    enabled: bool = True
    event_source: str | None = Field(default=None, max_length=100)
    cron: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=100)
    misfire_policy: Literal["skip", "run_once"] | None = None


class ConnectorBinding(FrozenModel):
    connector_family: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    binding_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    enabled: bool = True


class ScheduleBinding(FrozenModel):
    cron: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=100)
    misfire_policy: Literal["skip", "run_once"]


class AgentInstanceRecord(FrozenModel):
    id: str
    template_id: str
    display_order: int = Field(ge=1, le=10000)
    enabled: bool
    variant: InstanceVariant | None
    trigger_bindings: tuple[TriggerBinding, ...]
    connector_bindings: dict[str, ConnectorBinding]
    schedule: ScheduleBinding | None
    configuration_revision: int = Field(ge=1)


@dataclass(frozen=True)
class CatalogContract:
    departments: int | None = None
    functions: int | None = None
    templates: int | None = None
    instances: int | None = None
    department_instance_counts: Mapping[str, int] | None = None


MARKETING_AGENTS_V1_CONTRACT = CatalogContract(
    departments=5,
    functions=12,
    templates=36,
    instances=43,
    department_instance_counts=MappingProxyType(
        {
            "dept.social-media": 12,
            "dept.blog-seo": 6,
            "dept.email": 5,
            "dept.community": 14,
            "dept.partnerships": 6,
        }
    ),
)


@dataclass(frozen=True)
class CompiledCatalog:
    manifest: CatalogManifest
    departments: tuple[DepartmentRecord, ...]
    functions: tuple[FunctionRecord, ...]
    tool_capabilities: tuple[ToolCapabilityRecord, ...]
    approval_policies: tuple[ApprovalPolicyRecord, ...]
    templates: tuple[AgentTemplateRecord, ...]
    instances: tuple[AgentInstanceRecord, ...]
    prompt_text_by_template: Mapping[str, str]
    input_schema_by_template: Mapping[str, Mapping[str, Any]]
    output_schema_by_template: Mapping[str, Mapping[str, Any]]
    department_instance_counts: Mapping[str, int]
    content_hash: str


@dataclass(frozen=True)
class CatalogValidationReport:
    valid: bool
    issues: tuple[CatalogIssue, ...]
    content_hash: str | None = None
