"""Run, step, artifact, and external-action entities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import Effect, RunState, StepState
from marketing_agents.domain.runtime_policy import (
    RunRuntimePolicy,
    StepRuntimePolicy,
    attempt_kind_for_connector,
    runtime_rate_limit_key,
)

from ._validation import (
    frozen_mapping,
    require_digest,
    require_id,
    require_json_pointers,
    require_unique,
    require_utc,
)


@dataclass(frozen=True, slots=True)
class RunPlanSnapshot:
    run_id: str
    plan_hash: str
    workflow_id: str
    workflow_version: int
    workflow_definition_hash: str
    catalog_content_hash: str
    graph_hash: str
    routing_hash: str
    approval_required: bool
    step_count: int
    runtime_policy: RunRuntimePolicy
    created_at: datetime

    def __post_init__(self) -> None:
        require_id(self.run_id, "plan run ID")
        require_id(self.workflow_id, "plan workflow ID")
        for value, name in (
            (self.plan_hash, "plan hash"),
            (self.workflow_definition_hash, "workflow definition hash"),
            (self.graph_hash, "plan graph hash"),
            (self.routing_hash, "plan routing hash"),
        ):
            require_digest(value, name)
        if not self.catalog_content_hash.startswith("catalog-sha256-v1:"):
            raise ValueError("plan catalog hash version is invalid")
        require_digest(
            self.catalog_content_hash.removeprefix("catalog-sha256-v1:"),
            "plan catalog hash",
        )
        for numeric_value, name in (
            (self.workflow_version, "plan workflow version"),
            (self.step_count, "plan step count"),
        ):
            if (
                not isinstance(numeric_value, int)
                or isinstance(numeric_value, bool)
                or numeric_value < 1
            ):
                raise ValueError(f"{name} must be positive")
        if not isinstance(self.approval_required, bool):
            raise ValueError("plan approval disposition must be boolean")
        if type(self.runtime_policy) is not RunRuntimePolicy:
            raise ValueError("plan runtime policy must use the exact immutable snapshot")
        if self.step_count > self.runtime_policy.max_steps:
            raise ValueError("plan step count exceeds its immutable runtime policy")
        require_utc(self.created_at, "plan creation time")


@dataclass(frozen=True, slots=True)
class RunPlanSelectedInstance:
    run_id: str
    plan_hash: str
    instance_id: str
    template_id: str
    configuration_revision: int
    display_order: int
    source_ordinal: int | None
    selection_order: int
    target: bool

    def __post_init__(self) -> None:
        for value, name in (
            (self.run_id, "selected instance run ID"),
            (self.instance_id, "selected instance ID"),
            (self.template_id, "selected template ID"),
        ):
            require_id(value, name)
        require_digest(self.plan_hash, "selected instance plan hash")
        for numeric_value, name in (
            (self.configuration_revision, "selected instance configuration revision"),
            (self.display_order, "selected instance display order"),
            (self.selection_order, "selected instance selection order"),
        ):
            if (
                not isinstance(numeric_value, int)
                or isinstance(numeric_value, bool)
                or numeric_value < 1
            ):
                raise ValueError(f"{name} must be positive")
        if self.source_ordinal is not None and (
            not isinstance(self.source_ordinal, int)
            or isinstance(self.source_ordinal, bool)
            or self.source_ordinal < 1
        ):
            raise ValueError("selected instance source ordinal must be positive")
        if not isinstance(self.target, bool):
            raise ValueError("selected instance target flag must be boolean")


@dataclass(frozen=True, slots=True)
class RunPlanRoutingAssignment:
    run_id: str
    plan_hash: str
    slot_key: str
    instance_id: str
    template_id: str
    required_capability_ids: tuple[str, ...]
    assignment_order: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.run_id, "routing assignment run ID"),
            (self.slot_key, "routing assignment slot key"),
            (self.instance_id, "routing assignment instance ID"),
            (self.template_id, "routing assignment template ID"),
        ):
            require_id(value, name)
        require_digest(self.plan_hash, "routing assignment plan hash")
        if type(self.required_capability_ids) is not tuple:
            raise ValueError("routing capabilities must be an immutable tuple")
        require_unique(self.required_capability_ids, "routing required capabilities")
        if (
            not isinstance(self.assignment_order, int)
            or isinstance(self.assignment_order, bool)
            or self.assignment_order < 1
        ):
            raise ValueError("routing assignment order must be positive")


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    work_item_id: str
    state: RunState
    catalog_hash: str
    configuration_revision: int
    created_at: datetime
    version: int = 1
    updated_at: datetime = field(kw_only=True)
    approval_required: bool | None = field(default=None, kw_only=True)
    terminal_reason_code: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        require_id(self.id, "run ID")
        require_id(self.work_item_id, "work item ID")
        if self.catalog_hash.startswith("catalog-sha256-v1:"):
            require_digest(self.catalog_hash.removeprefix("catalog-sha256-v1:"), "catalog hash")
        else:
            require_digest(self.catalog_hash, "catalog hash")
        require_utc(self.created_at, "run creation time")
        require_utc(self.updated_at, "run update time")
        if self.updated_at < self.created_at:
            raise ValueError("run update time cannot precede creation")
        if self.configuration_revision < 1 or self.version < 1:
            raise ValueError("run revisions must be positive")
        if (
            self.state in {RunState.RECEIVED, RunState.VALIDATED}
            and self.approval_required is not None
        ):
            raise ValueError("pre-plan run cannot have an approval disposition")
        if (
            self.state
            in {
                RunState.PLANNED,
                RunState.AWAITING_APPROVAL,
                RunState.EXECUTING,
                RunState.COMPLETED,
                RunState.REJECTED,
            }
            and self.approval_required is None
        ):
            raise ValueError("planned run must retain its approval disposition")
        if (
            self.state in {RunState.AWAITING_APPROVAL, RunState.REJECTED}
            and not self.approval_required
        ):
            raise ValueError("approval states require a write-bearing plan")
        terminal = self.state in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.REJECTED,
            RunState.CANCELLED,
        }
        if terminal != (self.terminal_reason_code is not None):
            raise ValueError("only terminal runs require a terminal reason code")
        if self.terminal_reason_code is not None:
            require_id(self.terminal_reason_code, "terminal reason code")


@dataclass(frozen=True, slots=True)
class RunStep:
    id: str
    run_id: str
    key: str
    kind: str
    selected_instance_id: str
    dependency_keys: tuple[str, ...]
    capability_id: str
    effect: Effect
    state: StepState
    plan_hash: str = field(kw_only=True)
    graph_hash: str = field(kw_only=True)
    ordinal: int = field(kw_only=True)
    source_order: int = field(kw_only=True)
    template_id: str = field(kw_only=True)
    configuration_revision: int = field(kw_only=True)
    connector_family: str = field(kw_only=True)
    routing_slot_key: str | None = field(kw_only=True)
    binding_id: str | None = field(kw_only=True)
    binding_configuration_revision: int | None = field(kw_only=True)
    request_schema_id: str | None = field(kw_only=True)
    result_schema_id: str | None = field(kw_only=True)
    request_redaction_fields: tuple[str, ...] = field(kw_only=True)
    result_redaction_fields: tuple[str, ...] = field(kw_only=True)
    data_classification: DataClassification = field(kw_only=True)
    idempotency_support: str = field(kw_only=True)
    timeout_seconds: int | None = field(kw_only=True)
    runtime_policy: StepRuntimePolicy = field(kw_only=True)
    approval_policy_id: str = field(kw_only=True)
    approval_required_roles: tuple[str, ...] = field(kw_only=True)
    approval_required_scopes: tuple[str, ...] = field(kw_only=True)
    approval_expires_after_seconds: int | None = field(kw_only=True)
    approval_allow_self_approval: bool | None = field(kw_only=True)
    terminal_result: bool = field(kw_only=True)
    created_at: datetime = field(kw_only=True)
    updated_at: datetime = field(kw_only=True)
    version: int = field(default=1, kw_only=True)
    terminal_reason_code: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        for field_name in ("id", "run_id", "key", "selected_instance_id", "capability_id"):
            require_id(getattr(self, field_name), field_name)
        if type(self.dependency_keys) is not tuple:
            raise ValueError("step dependencies must be an immutable tuple")
        if type(self.effect) is not Effect:
            raise ValueError("step effect must use the exact Effect enum")
        if type(self.state) is not StepState:
            raise ValueError("step state must use the exact StepState enum")
        require_id(self.kind, "step kind")
        if len(self.kind) > 120 or len(self.connector_family) > 120:
            raise ValueError("step kind and connector family must not exceed 120 characters")
        require_unique(self.dependency_keys, "step dependencies")
        if self.key in self.dependency_keys:
            raise ValueError("step cannot depend on itself")
        require_digest(self.plan_hash, "step plan hash")
        require_digest(self.graph_hash, "step graph hash")
        require_id(self.template_id, "step template ID")
        require_id(self.connector_family, "step connector family")
        if self.routing_slot_key is not None:
            require_id(self.routing_slot_key, "step routing slot key")
        if self.binding_id is not None:
            require_id(self.binding_id, "step binding ID")
        if self.request_schema_id is not None:
            require_id(self.request_schema_id, "step request schema ID")
        if self.result_schema_id is not None:
            require_id(self.result_schema_id, "step result schema ID")
        if type(self.data_classification) is not DataClassification:
            raise ValueError("step data classification must use the exact enum")
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 1:
            raise ValueError("step ordinal must be positive")
        if (
            not isinstance(self.source_order, int)
            or isinstance(self.source_order, bool)
            or self.source_order < 1
        ):
            raise ValueError("step source order must be a positive integer")
        if (
            not isinstance(self.configuration_revision, int)
            or isinstance(self.configuration_revision, bool)
            or self.configuration_revision < 1
        ):
            raise ValueError("step configuration revision must be positive")
        if self.binding_configuration_revision is not None and (
            not isinstance(self.binding_configuration_revision, int)
            or isinstance(self.binding_configuration_revision, bool)
            or self.binding_configuration_revision < 1
        ):
            raise ValueError("step binding configuration revision must be positive")
        require_json_pointers(
            self.request_redaction_fields,
            "step request redaction fields",
        )
        require_json_pointers(
            self.result_redaction_fields,
            "step result redaction fields",
        )
        for values, name in (
            (self.approval_required_roles, "step approval roles"),
            (self.approval_required_scopes, "step approval scopes"),
        ):
            if type(values) is not tuple:
                raise ValueError(f"{name} must be an immutable tuple")
            require_unique(values, name)
        if self.idempotency_support not in {
            "not_applicable",
            "required",
            "supported",
            "unavailable",
        }:
            raise ValueError("step idempotency support is invalid")
        if type(self.runtime_policy) is not StepRuntimePolicy:
            raise ValueError("step runtime policy must use the exact immutable snapshot")
        if self.runtime_policy.attempt_kind is not attempt_kind_for_connector(
            self.connector_family
        ):
            raise ValueError("step runtime attempt kind differs from its connector family")
        if self.runtime_policy.rate_limit.key != runtime_rate_limit_key(
            template_id=self.template_id,
            max_calls=self.runtime_policy.rate_limit.max_calls,
            window_seconds=self.runtime_policy.rate_limit.window_seconds,
        ):
            raise ValueError("step runtime rate limit must bind its selected template")
        require_id(self.approval_policy_id, "step approval policy ID")
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 120
        ):
            raise ValueError("step timeout must be from 1 through 120 seconds")
        if self.connector_family in {"model", "artifact"}:
            if (
                self.binding_id is not None
                or self.binding_configuration_revision is not None
                or self.timeout_seconds is not None
                or self.request_redaction_fields
                or self.result_redaction_fields
                or self.data_classification is not DataClassification.INTERNAL
            ):
                raise ValueError("non-connector steps cannot retain connector contract metadata")
            if self.connector_family == "model" and (
                self.request_schema_id is None or self.result_schema_id is None
            ):
                raise ValueError("model steps require their selected template schema pair")
            if self.connector_family == "artifact" and (
                self.request_schema_id is not None or self.result_schema_id is not None
            ):
                raise ValueError("artifact no-call steps cannot retain call schema IDs")
        elif (
            self.binding_id is None
            or self.binding_configuration_revision is None
            or self.binding_configuration_revision != self.configuration_revision
            or self.timeout_seconds is None
            or self.request_schema_id is None
            or self.result_schema_id is None
        ):
            raise ValueError("external connector steps require a complete contract snapshot")
        if self.approval_expires_after_seconds is not None and (
            not isinstance(self.approval_expires_after_seconds, int)
            or isinstance(self.approval_expires_after_seconds, bool)
            or self.approval_expires_after_seconds < 1
        ):
            raise ValueError("step approval expiry must be positive")
        if self.approval_allow_self_approval is not None and not isinstance(
            self.approval_allow_self_approval, bool
        ):
            raise ValueError("step self-approval flag must be boolean")
        if self.effect is Effect.READ:
            if (
                self.idempotency_support != "not_applicable"
                or self.state in {StepState.AWAITING_APPROVAL, StepState.REJECTED}
                or self.approval_required_roles
                or self.approval_required_scopes
                or self.approval_expires_after_seconds is not None
                or self.approval_allow_self_approval is not None
            ):
                raise ValueError("read step cannot retain write approval authority")
        elif (
            self.connector_family in {"model", "artifact"}
            or self.idempotency_support != "required"
            or not self.approval_required_roles
            or not self.approval_required_scopes
            or self.approval_expires_after_seconds is None
            or self.approval_allow_self_approval is None
            or self.request_schema_id is None
            or self.result_schema_id is None
        ):
            raise ValueError("write step requires a complete external approval snapshot")
        if not isinstance(self.terminal_result, bool):
            raise ValueError("step terminal-result flag must be boolean")
        require_utc(self.created_at, "step creation time")
        require_utc(self.updated_at, "step update time")
        if self.updated_at < self.created_at:
            raise ValueError("step update time cannot precede creation")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("step version must be positive")
        terminal = self.state in {
            StepState.SUCCEEDED,
            StepState.FAILED,
            StepState.REJECTED,
            StepState.CANCELLED,
            StepState.SKIPPED,
        }
        if terminal != (self.terminal_reason_code is not None):
            raise ValueError("only terminal steps require a terminal reason code")
        if self.terminal_reason_code is not None:
            require_id(self.terminal_reason_code, "step terminal reason code")


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    run_id: str
    step_id: str
    schema_id: str
    payload: Mapping[str, Any]
    payload_hash: str
    parent_artifact_ids: tuple[str, ...]
    classification: DataClassification
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "run_id", "step_id", "schema_id"):
            require_id(getattr(self, field_name), field_name)
        require_digest(self.payload_hash, "artifact payload hash")
        require_unique(self.parent_artifact_ids, "parent artifact IDs")
        if self.id in self.parent_artifact_ids:
            raise ValueError("artifact cannot be its own parent")
        require_utc(self.created_at, "artifact creation time")
        object.__setattr__(self, "payload", frozen_mapping(self.payload))
