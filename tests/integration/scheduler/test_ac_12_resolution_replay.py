"""AC-12: committed replay preserves sealed historical gap evidence after restart."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from marketing_agents.application.services.schedule_claiming import ScheduleClaimService
from marketing_agents.application.services.schedule_processing import (
    ScheduleClaimProcessingDisposition,
    ScheduleClaimProcessingService,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.recurrence_resolution import RecurrenceResult
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.models import (
    AuditEventRecord,
    RunRecord,
    RunStateTransitionRecord,
    ScheduleOccurrenceRecord,
    ScheduleRecord,
    WorkItemRecord,
)
from marketing_agents.infrastructure.db.repositories.audit import AuditPersistenceInvariantError
from marketing_agents.infrastructure.db.repositories.schedule import SchedulePersistenceConflict
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from sqlalchemy import select, update

from tests.integration.scheduler.test_sched_06_restart_recovery import (
    CATALOG_HASH,
    SCHEDULE_ID,
    CommitForbiddenUnitOfWork,
    CommitThenLoseResponseUnitOfWork,
    ForbiddenClock,
    ForbiddenIds,
    MutableClock,
    ResponseLostAfterCommit,
    _command,
    _counts,
    _custom_uow_factory,
    _dependencies,
    _key,
    _runtime,
    _schedule,
    _uow_factory,
    _validator,
)


class ChangedResolutionCalculator:
    """A changed evidence provider whose legacy UTC selection remains identical."""

    def __init__(self):
        self.rich_calls = 0
        self.datetime_calls = 0

    def next_occurrence_after(self, *, cron, timezone, after_utc):
        self.rich_calls += 1
        selected = CroniterRecurrenceCalculator().next_occurrence_after(
            cron=cron, timezone=timezone, after_utc=after_utc
        )
        return RecurrenceResult(scheduled_for_utc=selected.scheduled_for_utc)

    def next_after(self, *, cron, timezone, after_utc):
        self.datetime_calls += 1
        return CroniterRecurrenceCalculator().next_after(
            cron=cron, timezone=timezone, after_utc=after_utc
        )


def processor(dependencies, calculator):
    return ScheduleClaimProcessingService(
        dependencies,
        _key(),
        _validator(),
        calculator,
        current_catalog_hash=CATALOG_HASH,
    )


async def durable_facts(runtime):
    result = {}
    async with runtime.session_factory() as session:
        for model in (
            ScheduleRecord,
            ScheduleOccurrenceRecord,
            WorkItemRecord,
            RunRecord,
            RunStateTransitionRecord,
            AuditEventRecord,
        ):
            table = model.__table__
            rows = (
                (await session.execute(select(table).order_by(*table.primary_key.columns)))
                .mappings()
                .all()
            )
            result[model.__tablename__] = tuple(deepcopy(dict(row)) for row in rows)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", (None, "occurrence", "audit"))
async def test_ac_12_response_loss_restart_retains_historical_interior_gap_or_fails_closed(
    tmp_path: Path, corruption: str | None
) -> None:
    path = tmp_path / "recorded-resolution.db"
    runtime = await _runtime(path)
    calculator = CroniterRecurrenceCalculator()
    initial = calculator.next_occurrence_after(
        cron="30 2 * * *",
        timezone="America/Los_Angeles",
        after_utc=datetime(2026, 3, 7, 9, tzinfo=UTC),
    )
    clock = MutableClock(datetime(2026, 3, 9, 10, tzinfo=UTC))
    factory = _uow_factory(runtime)
    try:
        async with factory() as uow:
            inserted = await uow.schedules.add_or_get(
                replace(
                    _schedule(),
                    cron="30 2 * * *",
                    timezone="America/Los_Angeles",
                    next_run_at_utc=initial.scheduled_for_utc,
                    next_recurrence=initial,
                )
            )
            assert inserted.inserted
            await uow.commit()
        claim = await ScheduleClaimService(_dependencies(factory, clock)).claim_due_once(
            lease_owner="worker.ac12.response-loss"
        )
        assert claim is not None
        losing_factory = _custom_uow_factory(
            runtime, factory.repository_factories, CommitThenLoseResponseUnitOfWork
        )
        with pytest.raises(ResponseLostAfterCommit):
            await processor(_dependencies(losing_factory, clock), calculator).process_claimed_once(
                _command(claim)
            )
        assert await _counts(runtime) == (1, 1, 1, 1, 3)
        async with factory() as uow:
            occurrence = await uow.schedules.get_occurrence_by_schedule_due(
                SCHEDULE_ID, initial.scheduled_for_utc
            )
        assert occurrence is not None
        assert occurrence.scheduled_recurrence == initial
        assert occurrence.missed_count == 3
        assert len(occurrence.recurrence_resolutions) == 1
        gap = occurrence.recurrence_resolutions[0]
        assert gap.reason == "nonexistent_local_time"
        assert gap.nominal_local == "2026-03-08T02:30:00.000000"
        assert gap.resolved_at_utc == datetime(2026, 3, 8, 10, tzinfo=UTC)
    finally:
        await runtime.dispose()

    # Reopen the committed database without create_all, migrations or reseeding.
    restarted = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    factory = _uow_factory(restarted)
    readonly_factory = _custom_uow_factory(
        restarted, factory.repository_factories, CommitForbiddenUnitOfWork
    )
    changed = ChangedResolutionCalculator()
    probe = changed.next_occurrence_after(
        cron="30 2 * * *", timezone="America/Los_Angeles", after_utc=initial.scheduled_for_utc
    )
    assert probe.scheduled_for_utc == gap.resolved_at_utc and probe.resolution is None
    assert changed.rich_calls == 1
    try:
        if corruption == "occurrence":
            async with restarted.engine.begin() as connection:
                await connection.execute(
                    update(ScheduleOccurrenceRecord)
                    .where(ScheduleOccurrenceRecord.id == occurrence.id)
                    .values(recurrence_resolutions_json=canonical_json_bytes([]).decode())
                )
        elif corruption == "audit":
            async with restarted.session_factory() as session:
                record = (
                    await session.scalars(
                        select(AuditEventRecord).where(
                            AuditEventRecord.event_type == "schedule.misfire_run_once"
                        )
                    )
                ).one()
                metadata = deepcopy(record.safe_metadata)
                assert "recurrence_resolution" in metadata
                metadata["recurrence_resolution"] = {}
                record.safe_metadata = metadata
                await session.commit()
        before = await durable_facts(restarted)
        service = processor(
            _dependencies(readonly_factory, ForbiddenClock(), ids=ForbiddenIds()), changed
        )
        if corruption is not None:
            expected_error = (
                SchedulePersistenceConflict
                if corruption == "occurrence"
                else AuditPersistenceInvariantError
            )
            with pytest.raises(expected_error):
                await service.process_claimed_once(_command(claim))
        else:
            for _ in range(2):
                replayed = await service.process_claimed_once(_command(claim))
                assert (
                    replayed.disposition is ScheduleClaimProcessingDisposition.DUPLICATE_SUPPRESSED
                )
                assert replayed.occurrence == occurrence
                assert replayed.plan.scheduled_recurrence == occurrence.scheduled_recurrence
                assert replayed.plan.next_recurrence == occurrence.next_recurrence
                assert replayed.plan.recurrence_resolutions == (gap,)
                assert replayed.work_item.id == occurrence.work_item_id
                assert replayed.run.id == occurrence.run_id
            assert changed.datetime_calls == 6
        assert changed.rich_calls == 1  # Only the explicit probe, never historical replay.
        assert await durable_facts(restarted) == before
        assert await _counts(restarted) == (1, 1, 1, 1, 3)
    finally:
        await restarted.dispose()
