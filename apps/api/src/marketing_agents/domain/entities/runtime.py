"""Run, step, artifact, and external-action entities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import Effect, ExternalActionState, RunState, StepState

from ._validation import (
    frozen_mapping,
    require_digest,
    require_id,
    require_unique,
    require_utc,
)


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

    def __post_init__(self) -> None:
        for field_name in ("id", "run_id", "key", "selected_instance_id", "capability_id"):
            require_id(getattr(self, field_name), field_name)
        require_id(self.kind, "step kind")
        require_unique(self.dependency_keys, "step dependencies")
        if self.key in self.dependency_keys:
            raise ValueError("step cannot depend on itself")


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


@dataclass(frozen=True, slots=True)
class ExternalAction:
    id: str
    run_id: str
    step_id: str
    action_hash: str
    idempotency_key: str
    connector_binding_id: str
    state: ExternalActionState
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "run_id", "step_id", "idempotency_key", "connector_binding_id"):
            require_id(getattr(self, field_name), field_name)
        require_digest(self.action_hash, "external action hash")
        require_utc(self.created_at, "external action creation time")
