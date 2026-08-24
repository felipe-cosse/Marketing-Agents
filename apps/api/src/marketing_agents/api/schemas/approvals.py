"""Narrow approval decision DTOs; create, inspect, and full projections remain API-06."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from marketing_agents.application.services.approval_decisions import MAX_APPROVAL_REASON_LENGTH
from marketing_agents.domain.validation import require_text


class ApprovalDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    expected_generation: int = Field(ge=1)
    expected_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: SecretStr | None = Field(default=None, repr=False)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            require_text(
                value.get_secret_value(),
                "approval decision reason",
                maximum=MAX_APPROVAL_REASON_LENGTH,
            )
        return value

    @model_validator(mode="after")
    def retain_exact_types(self) -> Self:
        if type(self.expected_generation) is not int or type(self.expected_payload_hash) is not str:
            raise ValueError("approval decision preconditions must use exact scalar types")
        return self

    def reason_text(self) -> str | None:
        return None if self.reason is None else self.reason.get_secret_value()


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    decision_id: str
    action_id: str
    run_id: str
    status: Literal["approved", "rejected"]
