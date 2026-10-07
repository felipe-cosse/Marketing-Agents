"""AC-12: retain selected nonexistent local times without changing recurrence."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.application.services.schedule_misfire import (
    ScheduleMisfireError,
    ScheduleMisfirePlanner,
)
from marketing_agents.domain.entities import ScheduleOccurrence
from marketing_agents.domain.enums import MisfirePolicy, OccurrenceState
from marketing_agents.domain.recurrence_resolution import (
    MAX_RECORDED_RECURRENCE_RESOLUTIONS,
    RecurrenceResolution,
    RecurrenceResult,
    recurrence_resolution_from_dict,
    recurrence_resolution_to_dict,
    recurrence_resolutions_from_list,
    recurrence_resolutions_to_list,
    recurrence_result_from_dict,
    recurrence_result_to_dict,
)
from marketing_agents.domain.schedule_occurrence_identity import (
    schedule_local_snapshot,
    schedule_occurrence_id,
)
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator

from tests.unit.scheduler.test_sched_04_misfire import _claim, _schedule

ZONE = "America/Los_Angeles"
GAP = datetime(2026, 3, 8, 10, tzinfo=UTC)


def gap_selection() -> RecurrenceResult:
    return CroniterRecurrenceCalculator().next_occurrence_after(
        cron="30 2 * * *",
        timezone=ZONE,
        after_utc=datetime(2026, 3, 8, 9, 59, tzinfo=UTC),
    )


def occurrence_for(plan, *, pending_only=False):
    local, fold = schedule_local_snapshot(plan.scheduled_for_utc, ZONE)
    return ScheduleOccurrence(
        id=schedule_occurrence_id(
            plan.schedule_id,
            plan.scheduled_for_utc,
            recurrence_version=plan.recurrence_version,
        ),
        schedule_id=plan.schedule_id,
        scheduled_for_utc=plan.scheduled_for_utc,
        scheduled_local=local,
        timezone=ZONE,
        timezone_fold=fold,
        recurrence_version=plan.recurrence_version,
        state=OccurrenceState.CLAIMED,
        scheduled_recurrence=plan.scheduled_recurrence,
        next_recurrence=None if pending_only else plan.next_recurrence,
        recurrence_resolutions=None if pending_only else plan.recurrence_resolutions,
    )


def test_ac_12_rich_gap_selection_records_nominal_time_and_keeps_datetime_api() -> None:
    result = gap_selection()
    assert result.scheduled_for_utc == GAP
    assert result.resolution == RecurrenceResolution(
        reason="nonexistent_local_time",
        nominal_local="2026-03-08T02:30:00.000000",
        timezone=ZONE,
        resolved_at_utc=GAP,
    )
    assert recurrence_result_to_dict(result) == {
        "schema_version": 1,
        "scheduled_for_utc": "2026-03-08T10:00:00.000000+00:00",
        "resolution": {
            "schema_version": 1,
            "reason": "nonexistent_local_time",
            "nominal_local": "2026-03-08T02:30:00.000000",
            "timezone": ZONE,
            "resolved_at_utc": "2026-03-08T10:00:00.000000+00:00",
        },
    }
    assert recurrence_result_from_dict(recurrence_result_to_dict(result)) == result
    assert recurrence_resolution_from_dict(recurrence_resolution_to_dict(result.resolution)) == (
        result.resolution
    )
    assert recurrence_resolutions_from_list(
        recurrence_resolutions_to_list((result.resolution,))
    ) == (result.resolution,)
    assert (
        CroniterRecurrenceCalculator().next_after(
            cron="30 2 * * *", timezone=ZONE, after_utc=datetime(2026, 3, 8, 9, 59, tzinfo=UTC)
        )
        == result.scheduled_for_utc
    )


def test_ac_12_ordinary_and_first_fold_selections_have_no_nonexistent_reason() -> None:
    calculator = CroniterRecurrenceCalculator()
    first = calculator.next_occurrence_after(
        cron="30 1 * * *", timezone=ZONE, after_utc=datetime(2026, 11, 1, 7, tzinfo=UTC)
    )
    next_day = calculator.next_occurrence_after(
        cron="30 1 * * *", timezone=ZONE, after_utc=first.scheduled_for_utc
    )
    assert first == RecurrenceResult(scheduled_for_utc=datetime(2026, 11, 1, 8, 30, tzinfo=UTC))
    assert next_day == RecurrenceResult(scheduled_for_utc=datetime(2026, 11, 2, 9, 30, tzinfo=UTC))
    assert recurrence_result_from_dict(recurrence_result_to_dict(first)) == first


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("schema_version", 2),
        ("reason", "ordinary"),
        ("nominal_local", "2026-03-08T02:30:00"),
        ("nominal_local", "2026-03-08T02:30:01.000000"),
        ("nominal_local", "2026-03-08T02:30:00.000000+00:00"),
        ("timezone", "Unknown/Timezone"),
        ("resolved_at_utc", "2026-03-08T10:00:00.000000Z"),
        ("resolved_at_utc", "2026-03-08T03:00:00.000000-07:00"),
        ("resolved_at_utc", "2026-03-08T10:00:00.000000"),
    ),
)
def test_ac_12_resolution_json_rejects_noncanonical_or_unsupported_facts(field, value) -> None:
    raw = recurrence_resolution_to_dict(gap_selection().resolution)
    raw[field] = value
    with pytest.raises(ValueError):
        recurrence_resolution_from_dict(raw)


def test_ac_12_snapshot_json_requires_exact_shapes_and_selected_utc_binding() -> None:
    result = gap_selection()
    raw = recurrence_result_to_dict(result)
    for invalid in (
        {**raw, "extra": None},
        {key: value for key, value in raw.items() if key != "resolution"},
        {**raw, "schema_version": True},
        {**raw, "scheduled_for_utc": "2026-03-09T10:00:00.000000+00:00"},
        {**raw, "resolution": {**raw["resolution"], "extra": None}},
    ):
        with pytest.raises(ValueError):
            recurrence_result_from_dict(invalid)
    with pytest.raises(ValueError):
        recurrence_resolutions_from_list((raw["resolution"],))
    with pytest.raises(ValueError):
        recurrence_resolutions_from_list([raw["resolution"]] * 2)
    with pytest.raises(ValueError):
        recurrence_resolutions_from_list(
            [raw["resolution"]] * (MAX_RECORDED_RECURRENCE_RESOLUTIONS + 1)
        )


def test_ac_12_pending_gap_becomes_exact_due_evidence_and_future_gap_stays_pending() -> None:
    calculator = CroniterRecurrenceCalculator()
    selection = gap_selection()
    schedule = replace(
        _schedule(due=GAP, cron="30 2 * * *", timezone_name=ZONE), next_recurrence=selection
    )
    plan = ScheduleMisfirePlanner(calculator).resolve(
        schedule=schedule, claim=_claim(due=GAP, claimed_at_utc=GAP)
    )
    assert plan.scheduled_recurrence is selection
    assert plan.recurrence_resolutions == (selection.resolution,)
    assert plan.next_recurrence == RecurrenceResult(
        scheduled_for_utc=datetime(2026, 3, 9, 9, 30, tzinfo=UTC)
    )
    occurrence = occurrence_for(plan)
    assert occurrence.scheduled_local == "2026-03-08T03:00:00.000000"
    assert occurrence.recurrence_resolutions[0].nominal_local == "2026-03-08T02:30:00.000000"
    pending = occurrence_for(plan, pending_only=True)
    assert pending.scheduled_recurrence == selection
    assert pending.next_recurrence is pending.recurrence_resolutions is None
    for changed in (
        {"recurrence_resolutions": ()},
        {"scheduled_recurrence": None},
        {"next_recurrence": selection},
    ):
        with pytest.raises(ValueError):
            replace(occurrence, **changed)
    with pytest.raises(ValueError):
        replace(schedule, next_run_at_utc=GAP + timedelta(days=1))
    with pytest.raises(ValueError):
        replace(schedule, timezone="UTC")

    previous_due = datetime(2026, 3, 7, 10, 30, tzinfo=UTC)
    previous = _schedule(due=previous_due, cron="30 2 * * *", timezone_name=ZONE)
    before_gap = ScheduleMisfirePlanner(calculator).resolve(
        schedule=previous, claim=_claim(due=previous_due, claimed_at_utc=previous_due)
    )
    assert before_gap.scheduled_recurrence is None  # Legacy due has no invented evidence.
    assert before_gap.recurrence_resolutions == ()
    assert before_gap.next_recurrence == selection


@pytest.mark.parametrize("policy", (MisfirePolicy.SKIP, MisfirePolicy.RUN_ONCE))
def test_ac_12_coalesced_range_retains_interior_gap_and_separate_future_selection(policy) -> None:
    calculator = CroniterRecurrenceCalculator()
    initial = calculator.next_occurrence_after(
        cron="30 2 * * *", timezone=ZONE, after_utc=datetime(2026, 3, 7, 9, tzinfo=UTC)
    )
    schedule = replace(
        _schedule(
            policy=policy, due=initial.scheduled_for_utc, cron="30 2 * * *", timezone_name=ZONE
        ),
        next_recurrence=initial,
    )
    plan = ScheduleMisfirePlanner(calculator).resolve(
        schedule=schedule,
        claim=_claim(
            due=initial.scheduled_for_utc, claimed_at_utc=datetime(2026, 3, 9, 10, tzinfo=UTC)
        ),
    )
    assert plan.scheduled_recurrence == initial and initial.resolution is None
    assert plan.missed_count == 3
    assert plan.first_missed_at_utc == datetime(2026, 3, 7, 10, 30, tzinfo=UTC)
    assert plan.last_missed_at_utc == datetime(2026, 3, 9, 9, 30, tzinfo=UTC)
    assert plan.recurrence_resolutions == (gap_selection().resolution,)
    assert plan.next_recurrence == RecurrenceResult(
        scheduled_for_utc=datetime(2026, 3, 10, 9, 30, tzinfo=UTC)
    )
    assert plan.next_run_at_utc == plan.next_recurrence.scheduled_for_utc


def test_ac_12_rich_planning_is_mandatory_except_explicit_legacy_replay() -> None:
    class DatetimeOnly:
        def next_after(self, *, cron, timezone, after_utc):
            return after_utc + timedelta(minutes=1)

    schedule = _schedule()
    claim = _claim(claimed_at_utc=schedule.next_run_at_utc)
    planner = ScheduleMisfirePlanner(DatetimeOnly())
    with pytest.raises(ScheduleMisfireError) as failure:
        planner.resolve(schedule=schedule, claim=claim)
    assert failure.value.code == "recurrence_contract_error"
    legacy = planner.resolve(schedule=schedule, claim=claim, record_resolution=False)
    assert (
        legacy.scheduled_recurrence
        is legacy.next_recurrence
        is legacy.recurrence_resolutions
        is None
    )
    with pytest.raises(ValueError, match="boolean"):
        planner.resolve(schedule=schedule, claim=claim, record_resolution=1)
