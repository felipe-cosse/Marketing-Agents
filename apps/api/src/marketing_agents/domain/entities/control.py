"""Schedule and occurrence entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from marketing_agents.domain.enums import (
    MisfirePolicy,
    OccurrenceState,
)

from ._validation import require_id, require_text, require_utc


@dataclass(frozen=True, slots=True)
class Schedule:
    id: str
    trigger_id: str
    instance_id: str
    cron: str
    timezone: str
    next_run_at_utc: datetime
    misfire_policy: MisfirePolicy
    enabled: bool
    version: int = 1

    def __post_init__(self) -> None:
        for field_name in ("id", "trigger_id", "instance_id"):
            require_id(getattr(self, field_name), field_name)
        require_text(self.cron, "schedule cron", maximum=100)
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("schedule timezone must be a valid IANA zone") from exc
        require_utc(self.next_run_at_utc, "next scheduled UTC time")
        if self.version < 1:
            raise ValueError("schedule version must be positive")


@dataclass(frozen=True, slots=True)
class ScheduleOccurrence:
    id: str
    schedule_id: str
    scheduled_at_utc: datetime
    state: OccurrenceState
    work_item_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        require_id(self.id, "occurrence ID")
        require_id(self.schedule_id, "schedule ID")
        require_utc(self.scheduled_at_utc, "scheduled occurrence time")
        if self.work_item_id is not None:
            require_id(self.work_item_id, "occurrence work item ID")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease owner and expiry must be set together")
        if self.lease_owner is not None:
            require_id(self.lease_owner, "lease owner")
            require_utc(self.lease_expires_at, "lease expiry")  # type: ignore[arg-type]
