"""Private, typed schedule control projections; saved business input is never returned."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from marketing_agents.api.schemas.instance_configuration import (
    InstanceConfigurationApiModel,
    InstanceConfigurationInputModel,
    ScheduleBindingInput,
    ScheduledInputInput,
)


class ScheduleCreateInput(InstanceConfigurationInputModel):
    instance_id: str = Field(pattern=r"^inst\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+\.[0-9]{2}$")
    schedule: ScheduleBindingInput
    scheduled_input: ScheduledInputInput


class SchedulePatchInput(InstanceConfigurationInputModel):
    schedule: ScheduleBindingInput | None = None
    scheduled_input: ScheduledInputInput | None = None

    @model_validator(mode="after")
    def explicit_nonnull_change(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("supply a non-null schedule or scheduledInput")
        return self


class ScheduleToggleInput(InstanceConfigurationInputModel):
    pass


class ScheduleResponse(InstanceConfigurationApiModel):
    id: str = Field(min_length=1, max_length=240)
    instance_id: str = Field(min_length=1, max_length=240)
    trigger_id: str = Field(min_length=1, max_length=240)
    workflow_id: str = Field(min_length=1, max_length=240)
    cron: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=100)
    misfire_policy: Literal["skip", "run_once"]
    misfire_grace_seconds: StrictInt = Field(ge=0, le=86400)
    enabled: StrictBool
    configuration_revision: StrictInt = Field(ge=1)
    version: StrictInt = Field(ge=1)
    recurrence_version: str = Field(min_length=1, max_length=64)
    next_run_at_utc: datetime
    last_scheduled_at_utc: datetime | None
    preview_utc: tuple[datetime, ...] = Field(min_length=3, max_length=3)


class ScheduleListResponse(InstanceConfigurationApiModel):
    items: tuple[ScheduleResponse, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=2048)


class ScheduleOccurrenceResponse(InstanceConfigurationApiModel):
    id: str = Field(min_length=1, max_length=240)
    schedule_id: str = Field(min_length=1, max_length=240)
    scheduled_for_utc: datetime
    scheduled_local: str = Field(min_length=1, max_length=32)
    timezone: str = Field(min_length=1, max_length=100)
    timezone_fold: StrictInt = Field(ge=0, le=1)
    recurrence_version: str = Field(min_length=1, max_length=64)
    state: Literal["due", "claimed", "enqueued", "skipped", "completed"]
    work_item_id: str | None = Field(default=None, min_length=1, max_length=240)
    run_id: str | None = Field(default=None, min_length=1, max_length=240)
    misfire_policy_applied: Literal["skip", "run_once"] | None = None
    first_missed_at_utc: datetime | None = None
    last_missed_at_utc: datetime | None = None
    missed_count: StrictInt | None = Field(default=None, ge=1, le=10000)


class ScheduleOccurrenceListResponse(InstanceConfigurationApiModel):
    items: tuple[ScheduleOccurrenceResponse, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=2048)
