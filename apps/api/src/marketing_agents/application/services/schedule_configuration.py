"""Create initial schedules only from a sealed recurrence calculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from marketing_agents.application.ports.recurrence import RecurrenceCalculator
from marketing_agents.domain.entities import Schedule
from marketing_agents.domain.enums import MisfirePolicy
from marketing_agents.domain.schedule_occurrence_identity import (
    SCHEDULE_RECURRENCE_VERSION,
)
from marketing_agents.domain.validation import require_utc


class ScheduleConfigurationError(RuntimeError):
    """Raised when a recurrence adapter violates the application contract."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateScheduleCommand:
    id: str
    trigger_id: str
    instance_id: str
    workflow_id: str
    cron: str
    timezone: str
    misfire_policy: MisfirePolicy
    enabled: bool
    after_utc: datetime


class ScheduleConfigurationService:
    def __init__(self, recurrence: RecurrenceCalculator) -> None:
        self._recurrence = recurrence

    def create(self, command: CreateScheduleCommand) -> Schedule:
        require_utc(command.after_utc, "schedule calculation boundary")
        next_run_at_utc = self._recurrence.next_after(
            cron=command.cron,
            timezone=command.timezone,
            after_utc=command.after_utc,
        )
        try:
            require_utc(next_run_at_utc, "calculated next scheduled time")
        except (AttributeError, ValueError) as exc:
            raise ScheduleConfigurationError(
                "recurrence calculator returned a non-UTC scheduled time"
            ) from exc
        if next_run_at_utc <= command.after_utc:
            raise ScheduleConfigurationError(
                "recurrence calculator must return a time strictly after the boundary"
            )
        return Schedule(
            id=command.id,
            trigger_id=command.trigger_id,
            instance_id=command.instance_id,
            workflow_id=command.workflow_id,
            cron=command.cron,
            timezone=command.timezone,
            next_run_at_utc=next_run_at_utc,
            misfire_policy=command.misfire_policy,
            enabled=command.enabled,
            recurrence_version=SCHEDULE_RECURRENCE_VERSION,
        )
