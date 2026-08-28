"""Strict transport projections for Runs and runtime child resources."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from marketing_agents.api.schemas.artifacts import ArtifactSummaryView

RunStateValue = Literal[
    "received",
    "validated",
    "planned",
    "awaiting_approval",
    "executing",
    "completed",
    "failed",
    "rejected",
    "cancelled",
]
StepStateValue = Literal[
    "pending",
    "ready",
    "awaiting_approval",
    "executing",
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "skipped",
]
ActionStateValue = Literal[
    "proposed",
    "awaiting_approval",
    "approved",
    "dispatch_reserved",
    "dispatching",
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "superseded",
    "outcome_unknown",
]
Classification = Literal["public", "internal", "personal", "sensitive", "secret"]


class RunApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunRuntimePolicyView(RunApiModel):
    max_steps: int = Field(ge=1, le=20)
    max_model_calls: int = Field(ge=0, le=10)
    max_tool_calls: int = Field(ge=0, le=20)
    run_timeout_seconds: int = Field(ge=1, le=600)


class StepRuntimePolicyView(RunApiModel):
    operation_key: str
    attempt_kind: Literal["model", "tool", "no_call"]
    max_attempts: int = Field(ge=1, le=3)
    backoff: Literal["none", "bounded_exponential"]
    step_timeout_seconds: int = Field(ge=1, le=120)
    template_run_timeout_seconds: int = Field(ge=1, le=600)
    max_steps: int = Field(ge=1, le=20)
    max_model_calls: int = Field(ge=0, le=10)
    max_tool_calls: int = Field(ge=0, le=20)
    max_input_bytes: int = Field(ge=1, le=1_048_576)
    max_input_field_bytes: int = Field(ge=1, le=262_144)
    max_output_bytes: int = Field(ge=1, le=4_194_304)
    max_model_output_tokens: int = Field(ge=1, le=32_768)
    rate_limit_scope: str
    rate_limit_key: str
    rate_limit_max_calls: int = Field(ge=1, le=100)
    rate_limit_window_seconds: int = Field(ge=1, le=3_600)


class RunTransitionView(RunApiModel):
    sequence: int = Field(ge=1)
    command: str
    previous_state: RunStateValue | None
    new_state: RunStateValue
    reason_code: str
    occurred_at: datetime
    expected_version: int = Field(ge=0)
    resulting_version: int = Field(ge=1)
    completed_effect_count: int = Field(ge=0)
    outcome_unknown_effect_count: int = Field(ge=0)


class RunSelectedInstanceView(RunApiModel):
    instance_id: str
    template_id: str
    configuration_revision: int = Field(ge=1)
    display_order: int = Field(ge=1)
    source_ordinal: int | None = Field(default=None, ge=1)
    selection_order: int = Field(ge=1)
    target: bool
    instance_url: str
    template_url: str


class RunRoutingAssignmentView(RunApiModel):
    slot_key: str
    instance_id: str
    template_id: str
    required_capability_ids: tuple[str, ...]
    assignment_order: int = Field(ge=1)
    instance_url: str
    template_url: str


class RunStepTransitionView(RunApiModel):
    sequence: int = Field(ge=1)
    command: str
    previous_state: StepStateValue | None
    new_state: StepStateValue
    reason_code: str
    occurred_at: datetime
    expected_version: int = Field(ge=0)
    resulting_version: int = Field(ge=1)


class RunStepView(RunApiModel):
    id: str
    run_id: str
    key: str
    kind: str
    selected_instance_id: str
    template_id: str
    dependency_keys: tuple[str, ...]
    capability_id: str
    effect: Literal["read", "write"]
    state: StepStateValue
    ordinal: int = Field(ge=1)
    source_order: int = Field(ge=1)
    configuration_revision: int = Field(ge=1)
    connector_family: str
    routing_slot_key: str | None
    binding_id: str | None
    binding_configuration_revision: int | None = Field(default=None, ge=1)
    request_schema_id: str | None
    result_schema_id: str | None
    result_schema_hash: str | None
    data_classification: Classification
    idempotency_support: Literal["not_applicable", "required", "supported", "unavailable"]
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    runtime_policy: StepRuntimePolicyView
    approval_policy_id: str
    approval_required_roles: tuple[str, ...]
    approval_required_scopes: tuple[str, ...]
    approval_expires_after_seconds: int | None = Field(default=None, ge=1)
    approval_allow_self_approval: bool | None
    terminal_result: bool
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)
    terminal_reason_code: str | None
    transitions: tuple[RunStepTransitionView, ...] = Field(max_length=64)
    step_url: str
    run_url: str
    instance_url: str
    template_url: str


class RunPlanView(RunApiModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_id: str
    workflow_version: int = Field(ge=1)
    workflow_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_content_hash: str = Field(pattern=r"^catalog-sha256-v1:[0-9a-f]{64}$")
    graph_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    routing_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_required: bool
    step_count: int = Field(ge=1, le=20)
    runtime_policy: RunRuntimePolicyView
    created_at: datetime
    selected_instances: tuple[RunSelectedInstanceView, ...] = Field(min_length=1, max_length=100)
    routing_assignments: tuple[RunRoutingAssignmentView, ...] = Field(max_length=100)
    steps: tuple[RunStepView, ...] = Field(min_length=1, max_length=20)


class ExternalActionView(RunApiModel):
    id: str
    run_id: str
    step_id: str
    step_key: str
    template_id: str
    instance_id: str
    proposal_revision: int = Field(ge=1)
    action_type: str
    capability_id: str
    connector_family: str
    binding_id: str
    destination_summary: str
    redacted_payload: dict[str, JsonValue]
    payload_schema_id: str
    state: ActionStateValue
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)
    delivery_attempt_count: int = Field(ge=0, le=10)
    delivery_attempt_limit: int = Field(ge=1, le=10)
    approval_policy_id: str
    approval_required_roles: tuple[str, ...]
    approval_required_scopes: tuple[str, ...]
    approval_expires_after_seconds: int = Field(ge=1)
    approval_allow_self_approval: bool
    terminal_reason_code: str | None
    superseded_by_action_id: str | None
    superseded_at: datetime | None
    receipt_id: str | None
    result_status: str | None
    result_safe_metadata: None
    completed_at: datetime | None
    action_url: str
    run_url: str
    step_url: str
    instance_url: str
    template_url: str


class RunExecutionControlView(RunApiModel):
    run_timeout_seconds: int = Field(ge=1, le=3_600)
    max_model_calls: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    remaining_model_calls: int = Field(ge=0)
    remaining_tool_calls: int = Field(ge=0)
    started_at: datetime | None
    deadline_at: datetime | None
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)


class PendingApprovalSummaryView(RunApiModel):
    id: str
    action_id: str
    step_id: str
    status: Literal["pending"]
    destination_summary: str
    requested_at: datetime
    expires_at: datetime
    is_expired: bool
    approval_url: str
    action_url: str
    step_url: str


class RunSummaryView(RunApiModel):
    id: str
    work_item_id: str
    instance_id: str
    workflow_id: str
    trigger_id: str
    source: str
    mode: Literal["dry_run", "mock_execution"]
    state: RunStateValue
    catalog_hash: str = Field(pattern=r"^(?:catalog-sha256-v1:)?[0-9a-f]{64}$")
    configuration_revision: int = Field(ge=1)
    approval_required: bool | None
    terminal_reason_code: str | None
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)
    run_url: str
    timeline_url: str
    artifacts_url: str
    instance_url: str


class RunResourceView(RunSummaryView):
    transitions: tuple[RunTransitionView, ...] = Field(min_length=1, max_length=64)
    plan: RunPlanView | None
    execution_control: RunExecutionControlView | None
    pending_approvals: tuple[PendingApprovalSummaryView, ...] = Field(max_length=100)
    artifact_summaries: tuple[ArtifactSummaryView, ...] = Field(max_length=10)
    artifacts_truncated: bool
    external_actions: tuple[ExternalActionView, ...] = Field(max_length=100)


class RunListResponse(RunApiModel):
    items: tuple[RunSummaryView, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=1_024)


class RunTimelineEventView(RunApiModel):
    id: str
    sequence: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    event_type: str
    aggregate_type: str
    aggregate_id: str
    outcome: str
    actor_id: str
    actor_source: str
    auth_method: str
    correlation_id: str
    occurred_at: datetime
    step_id: str | None
    action_id: str | None
    approval_request_id: str | None
    artifact_id: str | None
    attempted_command: str | None
    previous_state: str | None
    new_state: str | None
    reason_code: str | None
    metadata: dict[str, JsonValue]
    metadata_classification: Classification
    metadata_expires_at: datetime
    metadata_expired: bool
    run_url: str
    step_url: str | None
    action_url: str | None
    approval_url: str | None
    artifact_url: str | None


class RunTimelineResponse(RunApiModel):
    run_id: str
    items: tuple[RunTimelineEventView, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=1_024)


class InstanceRuntimeStatusView(RunApiModel):
    instance_id: str
    status: Literal["never_run"] | RunStateValue
    latest_run_id: str | None
    latest_run_state: RunStateValue | None
    latest_run_created_at: datetime | None
    latest_run_updated_at: datetime | None
    instance_url: str
    latest_run_url: str | None


class InstanceStatusSummaryResponse(RunApiModel):
    scope: Literal["single-local-installation"]
    runtime_watermark: str = Field(pattern=r"^instance-status-sha256-v1:[0-9a-f]{64}$")
    items: tuple[InstanceRuntimeStatusView, ...] = Field(max_length=100)


class RunProblemDetail(RunApiModel):
    code: str
    message: str


class RunHttpError(RunApiModel):
    detail: RunProblemDetail


class RunPlainHttpError(RunApiModel):
    detail: str


__all__ = [
    "ExternalActionView",
    "InstanceRuntimeStatusView",
    "InstanceStatusSummaryResponse",
    "PendingApprovalSummaryView",
    "RunExecutionControlView",
    "RunHttpError",
    "RunListResponse",
    "RunPlainHttpError",
    "RunResourceView",
    "RunStepView",
    "RunSummaryView",
    "RunTimelineResponse",
]
