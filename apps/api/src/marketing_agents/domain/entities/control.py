"""Schedule and occurrence entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from marketing_agents.domain.enums import (
    MisfirePolicy,
    OccurrenceState,
)

from ._validation import require_iana_timezone, require_id, require_text, require_utc


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
        if len(self.cron.split()) != 5:
            raise ValueError("schedule cron must contain exactly five fields")
        require_iana_timezone(self.timezone, "schedule timezone")
        require_utc(self.next_run_at_utc, "next scheduled UTC time")
        if type(self.misfire_policy) is not MisfirePolicy:
            raise ValueError("schedule misfire policy must be supported")
        if type(self.enabled) is not bool:
            raise ValueError("schedule enabled flag must be a boolean")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("schedule version must be positive")


@dataclass(frozen=True, slots=True)
class ScheduleClaim:
    """One fenced lease over a schedule's exact persisted due instant."""

    schedule_id: str
    scheduled_for_utc: datetime
    lease_owner: str
    claimed_at_utc: datetime
    lease_expires_at_utc: datetime
    version: int

    def __post_init__(self) -> None:
        require_id(self.schedule_id, "claim schedule ID")
        require_utc(self.scheduled_for_utc, "claim scheduled time")
        require_id(self.lease_owner, "claim lease owner")
        require_utc(self.claimed_at_utc, "claim time")
        require_utc(self.lease_expires_at_utc, "claim lease expiry")
        if self.scheduled_for_utc > self.claimed_at_utc:
            raise ValueError("claim scheduled time must be due at claim time")
        if self.lease_expires_at_utc <= self.claimed_at_utc:
            raise ValueError("claim lease expiry must follow claim time")
        if type(self.version) is not int or self.version < 2:
            raise ValueError("claim version must reflect one successful claim")


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
