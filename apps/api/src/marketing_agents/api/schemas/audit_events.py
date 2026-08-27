"""Strict public transport projections for the bounded global audit feed."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class AuditApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AuditEventView(AuditApiModel):
    id: str = Field(min_length=1, max_length=240)
    schema_version: int = Field(ge=1)
    sequence: int = Field(ge=1)
    run_sequence: int | None = Field(default=None, ge=1)
    run_id: str | None = Field(default=None, max_length=240)
    schedule_id: str | None = Field(default=None, max_length=240)
    occurrence_id: str | None = Field(default=None, max_length=240)
    event_type: str = Field(min_length=1, max_length=120)
    aggregate_type: str = Field(min_length=1, max_length=40)
    aggregate_id: str = Field(min_length=1, max_length=240)
    outcome: str = Field(min_length=1, max_length=40)
    actor_id: str = Field(min_length=1, max_length=240)
    actor_source: str = Field(min_length=1, max_length=40)
    auth_method: str = Field(min_length=1, max_length=80)
    correlation_id: str = Field(min_length=1, max_length=240)
    occurred_at: datetime
    step_id: str | None = Field(default=None, max_length=240)
    action_id: str | None = Field(default=None, max_length=240)
    action_attempt_number: int | None = Field(default=None, ge=1)
    receipt_id: str | None = Field(default=None, max_length=240)
    approval_request_id: str | None = Field(default=None, max_length=240)
    approval_decision_id: str | None = Field(default=None, max_length=240)
    artifact_id: str | None = Field(default=None, max_length=240)
    attempt_id: str | None = Field(default=None, max_length=240)
    attempted_command: str | None = Field(default=None, max_length=120)
    expected_version: int | None = Field(default=None, ge=0)
    observed_version: int | None = Field(default=None, ge=1)
    observed_state: str | None = Field(default=None, max_length=120)
    requested_state: str | None = Field(default=None, max_length=120)
    mutation_version: int | None = Field(default=None, ge=1)
    transition_sequence: int | None = Field(default=None, ge=1)
    previous_state: str | None = Field(default=None, max_length=120)
    new_state: str | None = Field(default=None, max_length=120)
    reason_code: str | None = Field(default=None, max_length=120)
    metadata: dict[str, JsonValue]
    metadata_classification: str = Field(min_length=1, max_length=40)
    metadata_expires_at: datetime
    metadata_expired: bool
    run_url: str | None = None
    step_url: str | None = None
    action_url: str | None = None
    approval_url: str | None = None
    artifact_url: str | None = None


class AuditEventListResponse(AuditApiModel):
    endpoint_version: str = Field(min_length=1, max_length=80)
    high_watermark: int = Field(ge=0)
    items: tuple[AuditEventView, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=1_024)


class AuditProblemDetail(AuditApiModel):
    code: str
    message: str


class AuditHttpError(AuditApiModel):
    detail: AuditProblemDetail


class AuditPlainHttpError(AuditApiModel):
    detail: str


__all__ = [
    "AuditEventListResponse",
    "AuditEventView",
    "AuditHttpError",
    "AuditPlainHttpError",
    "AuditProblemDetail",
]
