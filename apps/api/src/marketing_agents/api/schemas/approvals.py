"""Typed approval request, inspection, and decision transport contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    field_validator,
    model_validator,
)

from marketing_agents.application.services.approval_decisions import (
    MAX_APPROVAL_REASON_LENGTH,
    validate_approval_reason,
)


class ApprovalDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    expected_generation: int = Field(ge=1)
    expected_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: SecretStr | None = Field(default=None, repr=False)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            validate_approval_reason(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def retain_exact_types(self) -> Self:
        if type(self.expected_generation) is not int or type(self.expected_payload_hash) is not str:
            raise ValueError("approval decision preconditions must use exact scalar types")
        return self

    def reason_text(self) -> str | None:
        return None if self.reason is None else self.reason.get_secret_value()


class ApprovalRequestInput(BaseModel):
    """Only optimistic preconditions; action and requester authority are server-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    expected_generation: StrictInt = Field(ge=0)
    expected_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovalResourceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    status: Literal[
        "pending",
        "approved",
        "rejected",
        "expired",
        "consumed",
        "superseded",
    ]
    resource_version: int = Field(ge=1)
    generation: int = Field(ge=1)
    one_time_use_state: Literal["unused", "consumed"]
    action_id: str
    action_type: str
    capability_id: str
    connector_family: str
    binding_id: str
    destination_summary: str
    redacted_payload: dict[str, Any]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str
    step_id: str
    template_id: str
    instance_id: str
    policy_id: str
    required_roles: tuple[str, ...]
    required_scopes: tuple[str, ...]
    allow_self_approval: bool
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    updated_at: datetime
    is_expired: bool
    is_actionable: bool
    decision_id: str | None
    decision_kind: Literal["approve", "reject"] | None
    decision_actor_id: str | None
    decision_reason_code: Literal["approval_granted", "approval_rejected"] | None
    decision_reason: str | None = Field(max_length=MAX_APPROVAL_REASON_LENGTH)
    decided_at: datetime | None
    expired_at: datetime | None
    replacement_approval_id: str | None
    renewed_at: datetime | None
    superseded_at: datetime | None
    superseded_reason_code: str | None
    consumed_at: datetime | None
    approval_url: str
    action_url: str
    run_url: str
    step_url: str
    template_url: str
    instance_url: str


class ApprovalSummaryView(BaseModel):
    """Bounded list projection that omits redacted payload and access-controlled hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    status: Literal[
        "pending",
        "approved",
        "rejected",
        "expired",
        "consumed",
        "superseded",
    ]
    resource_version: int = Field(ge=1)
    generation: int = Field(ge=1)
    action_id: str
    action_type: str
    destination_summary: str
    run_id: str
    template_id: str
    instance_id: str
    requested_at: datetime
    expires_at: datetime
    is_expired: bool
    is_actionable: bool
    approval_url: str
    action_url: str
    run_url: str


class ApprovalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ApprovalSummaryView, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=1_024)


class ApprovalRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: Literal["existing", "renewed"]
    approval: ApprovalResourceView


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    decision_id: str
    action_id: str
    run_id: str
    status: Literal["approved", "rejected"]


class ApprovalDecisionResourceResponse(ApprovalDecisionResponse):
    approval: ApprovalResourceView


class ApprovalProblem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    current_status: str | None = None
    current_resource_version: int | None = Field(default=None, ge=1)


class ApprovalFieldError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pointer: str
    code: str
    message: str


class ApprovalValidationProblem(ApprovalProblem):
    field_errors: tuple[ApprovalFieldError, ...] = Field(max_length=32)


class ApprovalHttpError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: ApprovalProblem


class ApprovalPlainHttpError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: str


class ApprovalRequestValidationError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: ApprovalValidationProblem


__all__ = [
    "ApprovalDecisionInput",
    "ApprovalDecisionResourceResponse",
    "ApprovalDecisionResponse",
    "ApprovalFieldError",
    "ApprovalHttpError",
    "ApprovalListResponse",
    "ApprovalPlainHttpError",
    "ApprovalProblem",
    "ApprovalRequestInput",
    "ApprovalRequestResponse",
    "ApprovalRequestValidationError",
    "ApprovalResourceView",
    "ApprovalSummaryView",
    "ApprovalValidationProblem",
]
