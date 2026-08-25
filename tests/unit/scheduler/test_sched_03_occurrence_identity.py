"""SCHED-03: occurrence identity depends only on immutable recurrence facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from marketing_agents.domain.entities import ScheduleClaim, ScheduleOccurrence
from marketing_agents.domain.enums import OccurrenceState
from marketing_agents.domain.schedule_occurrence_identity import (
    SCHEDULE_RECURRENCE_VERSION,
    schedule_local_snapshot,
    schedule_occurrence_id,
)

SCHEDULE_ID = "schedule.community.reminder"
DUE_AT = datetime(2026, 11, 1, 8, 30, tzinfo=UTC)


def _claim_occurrence_id(claim: ScheduleClaim) -> str:
    return schedule_occurrence_id(
        claim.schedule_id,
        claim.scheduled_for_utc,
        recurrence_version=claim.recurrence_version,
    )


def test_sched_03_occurrence_id_has_a_fixed_known_vector() -> None:
    assert schedule_occurrence_id(
        SCHEDULE_ID,
        DUE_AT,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
    ) == (
        "schedule-occurrence-sha256-v1:"
        "e6d3c46f469f01bc4f54e3aa91e37d37c3e27d7ee0f1681ca46083b9b1dd0fd2"
    )


def test_sched_03_occurrence_id_ignores_claim_worker_and_restart_facts() -> None:
    first_claim = ScheduleClaim(
        schedule_id=SCHEDULE_ID,
        scheduled_for_utc=DUE_AT,
        lease_owner="worker.scheduler.first",
        claimed_at_utc=DUE_AT + timedelta(minutes=1),
        lease_expires_at_utc=DUE_AT + timedelta(minutes=3),
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
        version=2,
    )
    recovered_claim = ScheduleClaim(
        schedule_id=SCHEDULE_ID,
        scheduled_for_utc=datetime.fromisoformat(DUE_AT.isoformat()),
        lease_owner="worker.scheduler.after-restart",
        claimed_at_utc=DUE_AT + timedelta(minutes=4),
        lease_expires_at_utc=DUE_AT + timedelta(minutes=6),
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
        version=3,
    )

    assert first_claim.lease_owner != recovered_claim.lease_owner
    assert first_claim.version != recovered_claim.version
    assert _claim_occurrence_id(first_claim) == _claim_occurrence_id(recovered_claim)


@pytest.mark.parametrize(
    ("schedule_id", "scheduled_for_utc", "recurrence_version"),
    [
        ("schedule.community.digest", DUE_AT, SCHEDULE_RECURRENCE_VERSION),
        (SCHEDULE_ID, DUE_AT + timedelta(microseconds=1), SCHEDULE_RECURRENCE_VERSION),
        (SCHEDULE_ID, DUE_AT, "five-field-cron-adr0008-v2"),
    ],
)
def test_sched_03_occurrence_id_changes_with_each_immutable_identity_fact(
    schedule_id: str,
    scheduled_for_utc: datetime,
    recurrence_version: str,
) -> None:
    baseline = schedule_occurrence_id(
        SCHEDULE_ID,
        DUE_AT,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
    )

    assert (
        schedule_occurrence_id(
            schedule_id,
            scheduled_for_utc,
            recurrence_version=recurrence_version,
        )
        != baseline
    )


def test_sched_03_local_snapshot_records_which_fall_back_instant_was_chosen() -> None:
    first_instant = datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    second_instant = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)

    assert schedule_local_snapshot(first_instant, "America/Los_Angeles") == (
        "2026-11-01T01:30:00.000000",
        0,
    )
    assert schedule_local_snapshot(second_instant, "America/Los_Angeles") == (
        "2026-11-01T01:30:00.000000",
        1,
    )
    assert schedule_occurrence_id(
        SCHEDULE_ID,
        first_instant,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
    ) != schedule_occurrence_id(
        SCHEDULE_ID,
        second_instant,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
    )


@pytest.mark.parametrize(
    ("scheduled_local", "timezone_fold"),
    [
        ("2026-11-01T02:30:00.000000", 0),
        ("2026-11-01T01:30:00.000000", 1),
    ],
)
def test_sched_03_occurrence_rejects_a_false_local_or_fold_snapshot(
    scheduled_local: str,
    timezone_fold: int,
) -> None:
    occurrence_id = schedule_occurrence_id(
        SCHEDULE_ID,
        DUE_AT,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
    )
    with pytest.raises(ValueError, match="local representation and fold"):
        ScheduleOccurrence(
            id=occurrence_id,
            schedule_id=SCHEDULE_ID,
            scheduled_for_utc=DUE_AT,
            scheduled_local=scheduled_local,
            timezone="America/Los_Angeles",
            timezone_fold=timezone_fold,
            recurrence_version=SCHEDULE_RECURRENCE_VERSION,
            state=OccurrenceState.CLAIMED,
        )


@pytest.mark.parametrize(
    ("schedule_id", "scheduled_for_utc", "recurrence_version", "message"),
    [
        ("", DUE_AT, SCHEDULE_RECURRENCE_VERSION, "occurrence schedule ID"),
        ("schedule with spaces", DUE_AT, SCHEDULE_RECURRENCE_VERSION, "occurrence schedule ID"),
        (
            SCHEDULE_ID,
            datetime(2026, 11, 1, 8, 30),
            SCHEDULE_RECURRENCE_VERSION,
            "scheduled occurrence time",
        ),
        (
            SCHEDULE_ID,
            datetime(2026, 11, 1, 9, 30, tzinfo=timezone(timedelta(hours=1))),
            SCHEDULE_RECURRENCE_VERSION,
            "scheduled occurrence time",
        ),
        (SCHEDULE_ID, DUE_AT, "", "schedule recurrence version"),
        (SCHEDULE_ID, DUE_AT, "version with spaces", "schedule recurrence version"),
        (SCHEDULE_ID, DUE_AT, "v" * 65, "schedule recurrence version"),
    ],
)
def test_sched_03_occurrence_id_rejects_invalid_identity_inputs(
    schedule_id: str,
    scheduled_for_utc: datetime,
    recurrence_version: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        schedule_occurrence_id(
            schedule_id,
            scheduled_for_utc,
            recurrence_version=recurrence_version,
        )
