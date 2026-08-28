"""Typed connector contracts; mutations accept only sealed exact authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from marketing_agents.application.policies.write_authorization import AuthorizedExternalWrite
from marketing_agents.application.ports.llm import UntrustedToolResult
from marketing_agents.domain.data_classification import DataClassification


class ConnectorPortError(ValueError):
    """Safe, classified connector failure with no raw provider payload."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConnectorCallContext(BaseModel):
    """Bounded execution context shared by every connector read."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    binding_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(min_length=1, max_length=200)
    deadline: AwareDatetime
    provenance_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    requested_timeout_seconds: int = Field(ge=1, le=120)

    @field_validator("deadline")
    @classmethod
    def require_utc_deadline(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("connector deadline must be UTC")
        return value

    @model_validator(mode="after")
    def require_unique_provenance(self) -> ConnectorCallContext:
        if len(self.provenance_ids) != len(set(self.provenance_ids)):
            raise ValueError("connector provenance IDs must be unique")
        return self


class ConnectorReadRequest[ParametersT: BaseModel](BaseModel):
    """A capability-discriminated typed read request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capability_id: str = Field(min_length=1, max_length=200)
    context: ConnectorCallContext
    parameters: ParametersT


class ConnectorObservation[PayloadT: BaseModel](BaseModel):
    """Typed connector output that remains untrusted at every later boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trust_class: Literal["untrusted_tool_result"] = "untrusted_tool_result"
    capability_id: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    observation_id: str = Field(min_length=1, max_length=200)
    payload: PayloadT
    provenance_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    classification: DataClassification

    def as_untrusted_tool_result(self) -> UntrustedToolResult:
        """Project the typed result into the LLM port without changing its trust class."""

        return UntrustedToolResult(
            capability_id=self.capability_id,
            observation_id=self.observation_id,
            payload=self.payload.model_dump(mode="json"),
            provenance_ids=self.provenance_ids,
        )


@dataclass(frozen=True, slots=True)
class AuthorizedConnectorCommand[CommandT: BaseModel]:
    """Typed command paired with the dispatcher-issued sealed proof."""

    authorization: AuthorizedExternalWrite
    command: CommandT


class ConnectorWriteResult(BaseModel):
    """Strict connector response DTO reconstructed after every WRITE call."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    receipt_id: str = Field(min_length=1, max_length=240)
    status: str = Field(min_length=1, max_length=100)
    safe_metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=128)


class MutatingConnector(Protocol):
    async def execute(self, write: AuthorizedExternalWrite) -> ConnectorWriteResult:
        """Execute one exact reserved write using its stable idempotency key."""
        ...
