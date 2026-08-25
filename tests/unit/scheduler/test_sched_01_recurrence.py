"""SCHED-01: preserve IANA wall-clock intent while sealing the next UTC run."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest
from marketing_agents.application.ports.recurrence import RecurrenceCalculationError
from marketing_agents.application.services.schedule_configuration import (
    CreateScheduleCommand,
    ScheduleConfigurationError,
    ScheduleConfigurationService,
)
from marketing_agents.domain.enums import MisfirePolicy
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator


def _command(**updates: object) -> CreateScheduleCommand:
    values: dict[str, object] = {
        "id": "schedule.community.reminder",
        "trigger_id": "trigger.community.schedule",
        "instance_id": "inst.community.events.live-session-reminder.01",
        "workflow_id": "workflow.community.reminder",
        "cron": "0 9 * * *",
        "timezone": "US/Pacific",
        "misfire_policy": MisfirePolicy.RUN_ONCE,
        "misfire_grace_seconds": 300,
        "enabled": True,
        "after_utc": datetime(2026, 1, 15, 16, 0, tzinfo=UTC),
    }
    values.update(updates)
    return CreateScheduleCommand(**values)  # type: ignore[arg-type]


def test_sched_01_service_preserves_original_iana_key_and_seals_next_utc_run() -> None:
    schedule = ScheduleConfigurationService(CroniterRecurrenceCalculator()).create(_command())

    assert schedule.cron == "0 9 * * *"
    assert schedule.timezone == "US/Pacific"
    assert schedule.next_run_at_utc == datetime(2026, 1, 15, 17, 0, tzinfo=UTC)
    assert schedule.next_run_at_utc.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        schedule.timezone = "UTC"  # type: ignore[misc]


def test_sched_01_spring_gap_advances_to_next_valid_local_instant() -> None:
    calculator = CroniterRecurrenceCalculator()

    assert calculator.next_after(
        cron="30 2 * * *",
        timezone="America/Los_Angeles",
        after_utc=datetime(2026, 3, 8, 9, 59, tzinfo=UTC),
    ) == datetime(2026, 3, 8, 10, 0, tzinfo=UTC)


def test_sched_01_fall_fold_chooses_earlier_utc_instant_once() -> None:
    calculator = CroniterRecurrenceCalculator()

    assert calculator.next_after(
        cron="30 1 * * *",
        timezone="America/Los_Angeles",
        after_utc=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
    ) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    assert calculator.next_after(
        cron="30 1 * * *",
        timezone="America/Los_Angeles",
        after_utc=datetime(2026, 11, 1, 8, 45, tzinfo=UTC),
    ) == datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("cron", "timezone_name", "after_utc", "code"),
    [
        ("0 9 * *", "UTC", datetime(2026, 1, 1, tzinfo=UTC), "invalid_cron"),
        ("@daily", "UTC", datetime(2026, 1, 1, tzinfo=UTC), "invalid_cron"),
        ("0 0 L * *", "UTC", datetime(2026, 1, 1, tzinfo=UTC), "invalid_cron"),
        ("0 0 * * ?", "UTC", datetime(2026, 1, 1, tzinfo=UTC), "invalid_cron"),
        ("60 * * * *", "UTC", datetime(2026, 1, 1, tzinfo=UTC), "invalid_cron"),
        ("0 0 31 2 *", "UTC", datetime(2026, 1, 1, tzinfo=UTC), "invalid_cron"),
        ("0 9 * * *", "Not/A_Zone", datetime(2026, 1, 1, tzinfo=UTC), "invalid_timezone"),
        ("0 9 * * *", "UTC", datetime(2026, 1, 1), "invalid_boundary"),
    ],
)
def test_sched_01_invalid_cron_timezone_and_boundary_fail_closed(
    cron: str,
    timezone_name: str,
    after_utc: datetime,
    code: str,
) -> None:
    with pytest.raises(RecurrenceCalculationError) as captured:
        CroniterRecurrenceCalculator().next_after(
            cron=cron,
            timezone=timezone_name,
            after_utc=after_utc,
        )
    assert captured.value.code == code


@pytest.mark.parametrize(
    "returned",
    [
        datetime(2026, 1, 15, 16, 0, tzinfo=UTC),
        datetime(2026, 1, 15, 17, 0),
        datetime(2026, 1, 15, 17, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_sched_01_service_rejects_nonfuture_or_nonutc_adapter_results(
    returned: datetime,
) -> None:
    class BrokenCalculator:
        def next_after(
            self,
            *,
            cron: str,
            timezone: str,
            after_utc: datetime,
        ) -> datetime:
            del cron, timezone, after_utc
            return returned

    service = ScheduleConfigurationService(BrokenCalculator())
    with pytest.raises(ScheduleConfigurationError):
        service.create(_command())
