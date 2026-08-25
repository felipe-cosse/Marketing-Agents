"""Schedule and occurrence entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from marketing_agents.domain.enums import (
    MisfirePolicy,
    OccurrenceState,
)
from marketing_agents.domain.schedule_misfire import (
    MAX_COALESCED_MISSED_OCCURRENCES,
    MAX_MISFIRE_GRACE_SECONDS,
)
from marketing_agents.domain.schedule_occurrence_identity import (
    schedule_local_snapshot,
    schedule_occurrence_id,
)

from ._validation import require_iana_timezone, require_id, require_text, require_utc


@dataclass(frozen=True, slots=True)
class Schedule:
    id: str
    trigger_id: str
    instance_id: str
    workflow_id: str
    cron: str
    timezone: str
    next_run_at_utc: datetime
    misfire_policy: MisfirePolicy
    misfire_grace_seconds: int
    enabled: bool
    recurrence_version: str
    version: int = 1
    last_scheduled_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "trigger_id",
            "instance_id",
            "workflow_id",
            "recurrence_version",
        ):
            require_id(getattr(self, field_name), field_name)
        require_text(self.recurrence_version, "schedule recurrence version", maximum=64)
        require_text(self.cron, "schedule cron", maximum=100)
        if len(self.cron.split()) != 5:
            raise ValueError("schedule cron must contain exactly five fields")
        require_iana_timezone(self.timezone, "schedule timezone")
        require_utc(self.next_run_at_utc, "next scheduled UTC time")
        if type(self.misfire_policy) is not MisfirePolicy:
            raise ValueError("schedule misfire policy must be supported")
        if (
            type(self.misfire_grace_seconds) is not int
            or not 0 <= self.misfire_grace_seconds <= MAX_MISFIRE_GRACE_SECONDS
        ):
            raise ValueError("schedule misfire grace must be an integer from zero through one day")
        if type(self.enabled) is not bool:
            raise ValueError("schedule enabled flag must be a boolean")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("schedule version must be positive")
        if self.last_scheduled_at_utc is not None:
            require_utc(self.last_scheduled_at_utc, "last scheduled UTC time")
            if self.last_scheduled_at_utc >= self.next_run_at_utc:
                raise ValueError("last scheduled UTC time must precede the next run")


@dataclass(frozen=True, slots=True)
class ScheduleClaim:
    """One fenced lease over a schedule's exact persisted due instant."""

    schedule_id: str
    scheduled_for_utc: datetime
    lease_owner: str
    claimed_at_utc: datetime
    lease_expires_at_utc: datetime
    recurrence_version: str
    version: int

    def __post_init__(self) -> None:
        require_id(self.schedule_id, "claim schedule ID")
        require_utc(self.scheduled_for_utc, "claim scheduled time")
        require_id(self.lease_owner, "claim lease owner")
        require_utc(self.claimed_at_utc, "claim time")
        require_utc(self.lease_expires_at_utc, "claim lease expiry")
        require_id(self.recurrence_version, "claim recurrence version")
        require_text(self.recurrence_version, "claim recurrence version", maximum=64)
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
    scheduled_for_utc: datetime
    scheduled_local: str
    timezone: str
    timezone_fold: int
    recurrence_version: str
    state: OccurrenceState
    work_item_id: str | None = None
    run_id: str | None = None
    misfire_policy_applied: MisfirePolicy | None = None
    misfire_grace_seconds: int | None = None
    misfire_evaluated_at_utc: datetime | None = None
    first_missed_at_utc: datetime | None = None
    last_missed_at_utc: datetime | None = None
    missed_count: int | None = None

    def __post_init__(self) -> None:
        require_id(self.id, "occurrence ID")
        require_id(self.schedule_id, "schedule ID")
        require_utc(self.scheduled_for_utc, "scheduled occurrence time")
        require_text(self.scheduled_local, "scheduled local representation", maximum=32)
        try:
            local = datetime.fromisoformat(self.scheduled_local)
        except ValueError as exc:
            raise ValueError("scheduled local representation must be ISO-8601") from exc
        if (
            local.tzinfo is not None
            or local.isoformat(timespec="microseconds") != self.scheduled_local
        ):
            raise ValueError("scheduled local representation must be canonical and timezone-free")
        require_iana_timezone(self.timezone, "occurrence timezone")
        if type(self.timezone_fold) is not int or self.timezone_fold not in (0, 1):
            raise ValueError("occurrence timezone fold must be zero or one")
        expected_local, expected_fold = schedule_local_snapshot(
            self.scheduled_for_utc,
            self.timezone,
        )
        if (self.scheduled_local, self.timezone_fold) != (
            expected_local,
            expected_fold,
        ):
            raise ValueError("occurrence local representation and fold must match its UTC instant")
        require_id(self.recurrence_version, "occurrence recurrence version")
        require_text(self.recurrence_version, "occurrence recurrence version", maximum=64)
        expected_id = schedule_occurrence_id(
            self.schedule_id,
            self.scheduled_for_utc,
            recurrence_version=self.recurrence_version,
        )
        if self.id != expected_id:
            raise ValueError("occurrence ID does not match its immutable identity facts")
        if type(self.state) is not OccurrenceState:
            raise ValueError("occurrence state must be supported")
        if (self.work_item_id is None) != (self.run_id is None):
            raise ValueError("occurrence WorkItem and Run links must be set together")
        if self.work_item_id is not None:
            require_id(self.work_item_id, "occurrence work item ID")
            require_id(self.run_id, "occurrence run ID")  # type: ignore[arg-type]
        linked_states = (OccurrenceState.ENQUEUED, OccurrenceState.COMPLETED)
        if (self.state in linked_states) != (self.work_item_id is not None):
            raise ValueError("occurrence state and WorkItem/Run links disagree")

        misfire_facts = (
            self.misfire_policy_applied,
            self.misfire_grace_seconds,
            self.misfire_evaluated_at_utc,
            self.first_missed_at_utc,
            self.last_missed_at_utc,
            self.missed_count,
        )
        if misfire_facts == (None, None, None, None, None, None):
            if self.state is OccurrenceState.SKIPPED:
                raise ValueError("skipped occurrences require complete misfire facts")
            return
        if any(value is None for value in misfire_facts):
            raise ValueError("occurrence misfire facts must be all present or all absent")
        if type(self.misfire_policy_applied) is not MisfirePolicy:
            raise ValueError("occurrence misfire policy must be supported")
        if (
            type(self.misfire_grace_seconds) is not int
            or not 0 <= self.misfire_grace_seconds <= MAX_MISFIRE_GRACE_SECONDS
        ):
            raise ValueError("occurrence misfire grace must be a safely bounded integer")

        evaluated_at_utc = self.misfire_evaluated_at_utc
        first_missed_at_utc = self.first_missed_at_utc
        last_missed_at_utc = self.last_missed_at_utc
        assert evaluated_at_utc is not None
        assert first_missed_at_utc is not None
        assert last_missed_at_utc is not None
        assert self.misfire_grace_seconds is not None
        require_utc(evaluated_at_utc, "occurrence misfire evaluation time")
        require_utc(first_missed_at_utc, "occurrence first missed time")
        require_utc(last_missed_at_utc, "occurrence last missed time")
        if first_missed_at_utc != self.scheduled_for_utc:
            raise ValueError("occurrence missed range must begin at its persisted due instant")
        if not first_missed_at_utc <= last_missed_at_utc <= evaluated_at_utc:
            raise ValueError("occurrence missed range must be ordered through evaluation time")
        if evaluated_at_utc - self.scheduled_for_utc <= timedelta(
            seconds=self.misfire_grace_seconds
        ):
            raise ValueError("occurrence misfire facts must exceed the configured grace")
        if (
            type(self.missed_count) is not int
            or not 1 <= self.missed_count <= MAX_COALESCED_MISSED_OCCURRENCES
        ):
            raise ValueError("occurrence missed count must be positive and safely bounded")
        if (self.missed_count == 1) != (last_missed_at_utc == first_missed_at_utc):
            raise ValueError("occurrence missed count must agree with its range endpoints")
        if (self.misfire_policy_applied is MisfirePolicy.SKIP) != (
            self.state is OccurrenceState.SKIPPED
        ):
            raise ValueError("occurrence skip policy and state must agree")
        if self.state is OccurrenceState.DUE:
            raise ValueError("evaluated misfire occurrences cannot remain due")
