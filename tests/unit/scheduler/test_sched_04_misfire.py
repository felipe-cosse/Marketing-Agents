"""SCHED-04: explicit, bounded skip and run-once policy planning."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from marketing_agents.application.services import (
    CreateScheduleCommand,
    ScheduleConfigurationService,
    ScheduleMisfireError,
    ScheduleMisfirePlanner,
)
from marketing_agents.domain.entities import Schedule, ScheduleClaim
from marketing_agents.domain.enums import MisfirePolicy
from marketing_agents.domain.schedule_misfire import (
    MAX_COALESCED_MISSED_OCCURRENCES,
    ScheduleDisposition,
    ScheduleOccurrencePlan,
)
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator

DUE = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
RECURRENCE_VERSION = "five-field-cron-adr0008-v1"


def _schedule(
    *,
    policy: MisfirePolicy = MisfirePolicy.RUN_ONCE,
    grace_seconds: int = 0,
    due: datetime = DUE,
    cron: str = "* * * * *",
    timezone_name: str = "UTC",
) -> Schedule:
    return Schedule(
        id="schedule.sched-04.policy",
        trigger_id="trigger.sched-04.policy",
        instance_id="instance.sched-04.target",
        workflow_id="workflow.sched-04.target",
        cron=cron,
        timezone=timezone_name,
        next_run_at_utc=due,
        misfire_policy=policy,
        misfire_grace_seconds=grace_seconds,
        enabled=True,
        recurrence_version=RECURRENCE_VERSION,
        version=2,
    )


def _claim(
    *,
    claimed_at_utc: datetime,
    due: datetime = DUE,
) -> ScheduleClaim:
    latest_utc = datetime.max.replace(tzinfo=UTC)
    lease_duration = min(timedelta(minutes=5), latest_utc - claimed_at_utc)
    return ScheduleClaim(
        schedule_id="schedule.sched-04.policy",
        scheduled_for_utc=due,
        lease_owner="worker.sched-04",
        claimed_at_utc=claimed_at_utc,
        lease_expires_at_utc=claimed_at_utc + lease_duration,
        recurrence_version=RECURRENCE_VERSION,
        version=2,
    )


def _planner(*, limit: int = 10_000) -> ScheduleMisfirePlanner:
    return ScheduleMisfirePlanner(
        CroniterRecurrenceCalculator(),
        max_coalesced_occurrences=limit,
    )


@pytest.mark.parametrize("grace", (-1, 86_401, True, 1.5, "30"))
def test_sched_04_configuration_rejects_invalid_grace_before_persistence(
    grace: object,
) -> None:
    command = CreateScheduleCommand(
        id="schedule.sched-04.invalid-grace",
        trigger_id="trigger.sched-04.invalid-grace",
        instance_id="instance.sched-04.target",
        workflow_id="workflow.sched-04.target",
        cron="0 9 * * *",
        timezone="UTC",
        misfire_policy=MisfirePolicy.SKIP,
        misfire_grace_seconds=grace,  # type: ignore[arg-type]
        enabled=True,
        after_utc=DUE,
    )

    with pytest.raises(ValueError, match="misfire grace"):
        ScheduleConfigurationService(CroniterRecurrenceCalculator()).create(command)


def test_sched_04_exact_grace_boundary_is_on_time_and_processing_delay_is_irrelevant() -> None:
    schedule = _schedule(grace_seconds=300)
    exact = _planner().resolve(
        schedule=schedule,
        claim=_claim(claimed_at_utc=DUE + timedelta(seconds=300)),
    )
    late = _planner().resolve(
        schedule=schedule,
        claim=_claim(claimed_at_utc=DUE + timedelta(seconds=300, microseconds=1)),
    )

    assert exact.disposition is ScheduleDisposition.ON_TIME
    assert exact.next_run_at_utc == DUE + timedelta(minutes=1)
    assert exact.first_missed_at_utc is None
    assert exact.last_missed_at_utc is None
    assert exact.missed_count is None
    assert exact.admits_work is True
    assert late.disposition is ScheduleDisposition.RUN_ONCE
    assert late.last_missed_at_utc == DUE + timedelta(minutes=5)
    assert late.missed_count == 6
    assert late.next_run_at_utc == DUE + timedelta(minutes=6)

    near_max_due = datetime.max.replace(tzinfo=UTC) - timedelta(seconds=2)

    class NearMaximumRecurrence:
        def next_after(
            self,
            *,
            cron: str,
            timezone: str,
            after_utc: datetime,
        ) -> datetime:
            del cron, timezone
            return after_utc + timedelta(seconds=1)

    near_maximum = ScheduleMisfirePlanner(NearMaximumRecurrence()).resolve(
        schedule=_schedule(grace_seconds=10, due=near_max_due),
        claim=_claim(due=near_max_due, claimed_at_utc=near_max_due),
    )
    assert near_maximum.disposition is ScheduleDisposition.ON_TIME
    assert near_maximum.next_run_at_utc == near_max_due + timedelta(seconds=1)


def test_sched_04_skip_coalesces_missed_range_without_work_intent() -> None:
    plan = _planner().resolve(
        schedule=_schedule(policy=MisfirePolicy.SKIP),
        claim=_claim(claimed_at_utc=DUE + timedelta(minutes=3)),
    )

    assert plan == ScheduleOccurrencePlan(
        schedule_id="schedule.sched-04.policy",
        scheduled_for_utc=DUE,
        recurrence_version=RECURRENCE_VERSION,
        disposition=ScheduleDisposition.SKIP,
        next_run_at_utc=DUE + timedelta(minutes=4),
        first_missed_at_utc=DUE,
        last_missed_at_utc=DUE + timedelta(minutes=3),
        missed_count=4,
    )
    assert plan.admits_work is False


def test_sched_04_run_once_coalesces_to_one_catch_up_anchored_to_persisted_due() -> None:
    plan = _planner().resolve(
        schedule=_schedule(policy=MisfirePolicy.RUN_ONCE),
        claim=_claim(claimed_at_utc=DUE + timedelta(minutes=3, seconds=30)),
    )

    assert plan.disposition is ScheduleDisposition.RUN_ONCE
    assert plan.scheduled_for_utc == DUE
    assert plan.first_missed_at_utc == DUE
    assert plan.last_missed_at_utc == DUE + timedelta(minutes=3)
    assert plan.missed_count == 4
    assert plan.next_run_at_utc == DUE + timedelta(minutes=4)
    assert plan.admits_work is True


def test_sched_04_missed_range_limit_and_broken_recurrence_fail_closed() -> None:
    class TrackingMinuteRecurrence:
        def __init__(self) -> None:
            self.calls = 0

        def next_after(
            self,
            *,
            cron: str,
            timezone: str,
            after_utc: datetime,
        ) -> datetime:
            del cron, timezone
            self.calls += 1
            return after_utc + timedelta(minutes=1)

    exact_recurrence = TrackingMinuteRecurrence()
    exact = ScheduleMisfirePlanner(
        exact_recurrence,
        max_coalesced_occurrences=3,
    ).resolve(
        schedule=_schedule(policy=MisfirePolicy.SKIP),
        claim=_claim(claimed_at_utc=DUE + timedelta(minutes=2)),
    )
    assert exact.missed_count == 3
    assert exact.next_run_at_utc == DUE + timedelta(minutes=3)
    assert exact_recurrence.calls == 3

    exhausted_recurrence = TrackingMinuteRecurrence()
    with pytest.raises(ScheduleMisfireError) as exhausted:
        ScheduleMisfirePlanner(
            exhausted_recurrence,
            max_coalesced_occurrences=3,
        ).resolve(
            schedule=_schedule(policy=MisfirePolicy.SKIP),
            claim=_claim(claimed_at_utc=DUE + timedelta(minutes=3)),
        )
    assert exhausted.value.code == "misfire_range_exhausted"
    assert exhausted_recurrence.calls == 3

    class BrokenRecurrence:
        def __init__(self, returned: object) -> None:
            self.returned = returned

        def next_after(
            self,
            *,
            cron: str,
            timezone: str,
            after_utc: datetime,
        ) -> datetime:
            del cron, timezone, after_utc
            return self.returned  # type: ignore[return-value]

    invalid_results = (
        DUE,
        DUE - timedelta(minutes=1),
        DUE.replace(tzinfo=None),
        DUE.astimezone(timezone(timedelta(hours=1))),
        "not-a-time",
    )
    for returned in invalid_results:
        with pytest.raises(ScheduleMisfireError) as broken:
            ScheduleMisfirePlanner(BrokenRecurrence(returned)).resolve(
                schedule=_schedule(),
                claim=_claim(claimed_at_utc=DUE),
            )
        assert broken.value.code == "recurrence_contract_error"

    class RaisingRecurrence:
        def __init__(self, failure: Exception) -> None:
            self.failure = failure

        def next_after(
            self,
            *,
            cron: str,
            timezone: str,
            after_utc: datetime,
        ) -> datetime:
            del cron, timezone, after_utc
            raise self.failure

    for failure in (
        AttributeError("broken"),
        OverflowError("broken"),
        TypeError("broken"),
        ValueError("broken"),
    ):
        with pytest.raises(ScheduleMisfireError) as raised:
            ScheduleMisfirePlanner(RaisingRecurrence(failure)).resolve(
                schedule=_schedule(),
                claim=_claim(claimed_at_utc=DUE),
            )
        assert raised.value.code == "recurrence_contract_error"


def test_sched_04_invalid_grace_clock_and_policy_inputs_fail_closed() -> None:
    schedule = _schedule()
    claim = _claim(claimed_at_utc=DUE)
    mismatches = (
        (replace(schedule, id="schedule.sched-04.other"), claim, "claim_schedule_mismatch"),
        (
            replace(schedule, next_run_at_utc=DUE - timedelta(minutes=1)),
            claim,
            "claim_due_mismatch",
        ),
        (
            replace(schedule, recurrence_version="five-field-cron-next"),
            claim,
            "claim_recurrence_mismatch",
        ),
        (replace(schedule, version=3), claim, "claim_version_mismatch"),
    )
    for changed_schedule, unchanged_claim, code in mismatches:
        with pytest.raises(ScheduleMisfireError) as mismatch:
            _planner().resolve(schedule=changed_schedule, claim=unchanged_claim)
        assert mismatch.value.code == code

    for invalid_limit in (0, 10_001, True, 1.5):
        with pytest.raises(ValueError, match="safe maximum"):
            ScheduleMisfirePlanner(
                CroniterRecurrenceCalculator(),
                max_coalesced_occurrences=invalid_limit,  # type: ignore[arg-type]
            )

    with pytest.raises(ScheduleMisfireError) as invalid_input:
        _planner().resolve(schedule=schedule, claim=object())  # type: ignore[arg-type]
    assert invalid_input.value.code == "misfire_input_invalid"

    invalid_plans = (
        {
            "last_missed_at_utc": DUE,
            "missed_count": MAX_COALESCED_MISSED_OCCURRENCES + 1,
        },
        {"last_missed_at_utc": DUE + timedelta(minutes=1), "missed_count": 1},
        {"last_missed_at_utc": DUE, "missed_count": 2},
    )
    for changed in invalid_plans:
        with pytest.raises(ValueError, match="missed count"):
            ScheduleOccurrencePlan(
                schedule_id=schedule.id,
                scheduled_for_utc=DUE,
                recurrence_version=RECURRENCE_VERSION,
                disposition=ScheduleDisposition.SKIP,
                next_run_at_utc=DUE + timedelta(minutes=2),
                first_missed_at_utc=DUE,
                **changed,  # type: ignore[arg-type]
            )


def test_sched_04_dst_coalescing_preserves_existing_wall_clock_policy() -> None:
    fall_due = datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    fall_plan = _planner().resolve(
        schedule=_schedule(
            policy=MisfirePolicy.SKIP,
            due=fall_due,
            cron="30 1 * * *",
            timezone_name="America/Los_Angeles",
        ),
        claim=_claim(
            due=fall_due,
            claimed_at_utc=datetime(2026, 11, 1, 9, 45, tzinfo=UTC),
        ),
    )
    assert fall_plan.missed_count == 1
    assert fall_plan.last_missed_at_utc == fall_due
    assert fall_plan.next_run_at_utc == datetime(2026, 11, 2, 9, 30, tzinfo=UTC)

    spring_due = datetime(2026, 3, 8, 10, 0, tzinfo=UTC)
    spring_plan = _planner().resolve(
        schedule=_schedule(
            policy=MisfirePolicy.RUN_ONCE,
            due=spring_due,
            cron="30 2 * * *",
            timezone_name="America/Los_Angeles",
        ),
        claim=_claim(
            due=spring_due,
            claimed_at_utc=datetime(2026, 3, 9, 9, 30, tzinfo=UTC),
        ),
    )
    assert spring_plan.missed_count == 2
    assert spring_plan.last_missed_at_utc == datetime(2026, 3, 9, 9, 30, tzinfo=UTC)
    assert spring_plan.next_run_at_utc == datetime(2026, 3, 10, 9, 30, tzinfo=UTC)
