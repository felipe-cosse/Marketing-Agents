"""Strict empty mutation input and honest cancellation-time snapshot."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ResourceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$")]


class RunCancellationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunCancellationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: ResourceId
    state: Literal["cancelled"]
    version: int = Field(ge=1)
    cancelled_at: datetime
    cancelled_step_ids: tuple[ResourceId, ...]
    preserved_step_ids: tuple[ResourceId, ...] = Field(
        description="Members not cancelled by this command, including in-flight or terminal work."
    )
    cancelled_action_ids: tuple[ResourceId, ...]
    preserved_action_ids: tuple[ResourceId, ...] = Field(
        description="Members not cancelled by this command; no rollback or final delivery claim."
    )
    succeeded_effect_count_at_cancellation: int = Field(ge=0)
    outcome_unknown_effect_count_at_cancellation: int = Field(ge=0)
    effects_reversed: Literal[False] = False
    run_url: str = Field(pattern=r"^/api/v1/runs/[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$")
    timeline_url: str = Field(pattern=r"^/api/v1/runs/[A-Za-z0-9][A-Za-z0-9:._-]{0,239}/timeline$")
