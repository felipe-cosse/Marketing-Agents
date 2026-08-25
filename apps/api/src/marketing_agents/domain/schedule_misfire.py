"""Immutable scheduler policy decisions and their bounded configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from marketing_agents.domain.validation import require_id, require_text, require_utc

MAX_MISFIRE_GRACE_SECONDS = 86_400
MAX_COALESCED_MISSED_OCCURRENCES = 10_000


class ScheduleDisposition(StrEnum):
    """One deterministic action for an already claimed scheduled occurrence."""

    ON_TIME = "on_time"
    SKIP = "skip"
    RUN_ONCE = "run_once"


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleOccurrencePlan:
    """Pure policy intent consumed by the later atomic occurrence transaction."""

    schedule_id: str
    scheduled_for_utc: datetime
    recurrence_version: str
    disposition: ScheduleDisposition
    next_run_at_utc: datetime
    first_missed_at_utc: datetime | None = None
    last_missed_at_utc: datetime | None = None
    missed_count: int | None = None

    def __post_init__(self) -> None:
        require_id(self.schedule_id, "misfire plan schedule ID")
        require_utc(self.scheduled_for_utc, "misfire plan scheduled time")
        require_id(self.recurrence_version, "misfire plan recurrence version")
        require_text(self.recurrence_version, "misfire plan recurrence version", maximum=64)
        if type(self.disposition) is not ScheduleDisposition:
            raise ValueError("misfire plan disposition must be supported")
        require_utc(self.next_run_at_utc, "misfire plan next scheduled time")
        if self.next_run_at_utc <= self.scheduled_for_utc:
            raise ValueError("misfire plan next scheduled time must follow the original due time")

        missed_values = (
            self.first_missed_at_utc,
            self.last_missed_at_utc,
            self.missed_count,
        )
        if self.disposition is ScheduleDisposition.ON_TIME:
            if missed_values != (None, None, None):
                raise ValueError("on-time schedule plans must not contain missed-range facts")
            return
        if any(value is None for value in missed_values):
            raise ValueError("misfire schedule plans require a complete missed range")

        first_missed_at_utc = self.first_missed_at_utc
        last_missed_at_utc = self.last_missed_at_utc
        assert first_missed_at_utc is not None
        assert last_missed_at_utc is not None
        require_utc(first_missed_at_utc, "first missed scheduled time")
        require_utc(last_missed_at_utc, "last missed scheduled time")
        if first_missed_at_utc != self.scheduled_for_utc:
            raise ValueError("misfire range must begin at the original persisted due time")
        if last_missed_at_utc < first_missed_at_utc:
            raise ValueError("misfire range must not be reversed")
        if last_missed_at_utc >= self.next_run_at_utc:
            raise ValueError("misfire plan next scheduled time must follow the missed range")
        if (
            type(self.missed_count) is not int
            or not 1 <= self.missed_count <= MAX_COALESCED_MISSED_OCCURRENCES
        ):
            raise ValueError("misfire missed count must be positive and safely bounded")
        if (self.missed_count == 1) != (last_missed_at_utc == first_missed_at_utc):
            raise ValueError("misfire missed count must agree with its range endpoints")

    @property
    def admits_work(self) -> bool:
        """Whether the later transaction should admit one WorkItem/Run receipt."""

        return self.disposition in (
            ScheduleDisposition.ON_TIME,
            ScheduleDisposition.RUN_ONCE,
        )
