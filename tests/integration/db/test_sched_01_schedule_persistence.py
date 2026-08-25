"""SCHED-01: persistent original timezone and next UTC run."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from marketing_agents.application.services.schedule_configuration import (
    CreateScheduleCommand,
    ScheduleConfigurationService,
)
from marketing_agents.domain.entities import Schedule
from marketing_agents.domain.enums import MisfirePolicy
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    SchedulePersistenceConflict,
    ScheduleRecord,
    SQLAlchemyAuditRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyScheduleRepository,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from sqlalchemy import update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

NEXT_RUN = datetime(2026, 8, 26, 16, 30, tzinfo=UTC)


def _schedule(
    *,
    schedule_id: str = "schedule.sched-01.daily",
    timezone_name: str = "America/Argentina/Buenos_Aires",
    next_run_at_utc: datetime = NEXT_RUN,
) -> Schedule:
    return Schedule(
        id=schedule_id,
        trigger_id="trigger.sched-01.daily",
        instance_id="instance.sched-01.target",
        workflow_id="workflow.sched-01.target",
        cron="30 9 * * *",
        timezone=timezone_name,
        next_run_at_utc=next_run_at_utc,
        misfire_policy=MisfirePolicy.RUN_ONCE,
        misfire_grace_seconds=300,
        enabled=True,
        recurrence_version="five-field-cron-adr0008-v1",
        version=1,
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


def _uow_factory(runtime: DatabaseRuntime) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            schedules=SQLAlchemyScheduleRepository,
        ),
    )


async def _persist(
    factory: SQLAlchemyUnitOfWorkFactory,
    schedule: Schedule,
):
    async with factory() as unit_of_work:
        result = await unit_of_work.schedules.add_or_get(schedule)
        await unit_of_work.commit()
    return result


def test_sched_01_schedule_schema_is_portable_and_indexed_by_next_utc_run() -> None:
    sqlite_ddl = " ".join(
        str(CreateTable(ScheduleRecord.__table__).compile(dialect=sqlite.dialect())).lower().split()
    )
    postgres_ddl = " ".join(
        str(CreateTable(ScheduleRecord.__table__).compile(dialect=postgresql.dialect()))
        .lower()
        .split()
    )
    for ddl in (sqlite_ddl, postgres_ddl):
        assert "timezone_name" in ddl
        assert "next_run_at_utc" in ddl
        assert "ck_schedules_cron_bounded" in ddl
        assert "ck_schedules_timezone_bounded" in ddl
        assert "ck_schedules_misfire_policy_supported" in ddl
        assert "ck_schedules_misfire_grace_bounded" in ddl
        assert "ck_schedules_version_positive" in ddl
        assert "ck_schedules_integrity_digest_length" in ddl
    assert "bool_schedules_enabled" in sqlite_ddl
    assert "enabled boolean not null" in postgres_ddl
    assert "next_run_at_utc timestamp with time zone not null" in postgres_ddl

    indexes = {
        index.name: " ".join(
            str(CreateIndex(index).compile(dialect=postgresql.dialect())).lower().split()
        )
        for index in ScheduleRecord.__table__.indexes
    }
    assert indexes == {
        "ix_schedules_enabled_next_run_id": (
            "create index ix_schedules_enabled_next_run_id on schedules "
            "(enabled, next_run_at_utc, id)"
        )
    }


@pytest.mark.asyncio
async def test_sched_01_round_trip_preserves_original_zone_and_next_utc_across_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "schedule-restart.db"
    schedule = _schedule()
    runtime = await _runtime(database_path)
    try:
        inserted = await _persist(_uow_factory(runtime), schedule)
        assert inserted.inserted is True
        assert inserted.schedule is schedule
    finally:
        await runtime.dispose()

    restarted = await _runtime(database_path)
    try:
        factory = _uow_factory(restarted)
        async with factory() as unit_of_work:
            restored = await unit_of_work.schedules.get(schedule.id)
        assert restored == schedule
        assert restored is not None
        assert restored.timezone == "America/Argentina/Buenos_Aires"
        assert restored.next_run_at_utc == NEXT_RUN
        assert restored.next_run_at_utc.tzinfo is UTC

        replay = await _persist(factory, schedule)
        assert replay.inserted is False
        assert replay.schedule == schedule
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_01_calculated_next_utc_is_the_exact_restart_persisted_value(
    tmp_path: Path,
) -> None:
    schedule = ScheduleConfigurationService(CroniterRecurrenceCalculator()).create(
        CreateScheduleCommand(
            id="schedule.sched-01.calculated",
            trigger_id="trigger.sched-01.calculated",
            instance_id="instance.sched-01.calculated",
            workflow_id="workflow.sched-01.calculated",
            cron="0 9 * * *",
            timezone="US/Pacific",
            misfire_policy=MisfirePolicy.SKIP,
            misfire_grace_seconds=300,
            enabled=True,
            after_utc=datetime(2026, 1, 15, 16, 0, tzinfo=UTC),
        )
    )
    assert schedule.next_run_at_utc == datetime(2026, 1, 15, 17, 0, tzinfo=UTC)

    database_path = tmp_path / "calculated-restart.db"
    runtime = await _runtime(database_path)
    try:
        await _persist(_uow_factory(runtime), schedule)
    finally:
        await runtime.dispose()

    restarted = await _runtime(database_path)
    try:
        async with _uow_factory(restarted)() as unit_of_work:
            restored = await unit_of_work.schedules.get(schedule.id)
        assert restored == schedule
        assert restored is not None
        assert restored.timezone == "US/Pacific"
        assert restored.next_run_at_utc == datetime(2026, 1, 15, 17, 0, tzinfo=UTC)
        assert restored.next_run_at_utc.tzinfo is UTC
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    (
        replace(_schedule(), timezone="Asia/Tokyo"),
        replace(_schedule(), next_run_at_utc=NEXT_RUN + timedelta(hours=1)),
        replace(_schedule(), cron="0 10 * * *"),
    ),
    ids=("timezone", "next-utc-run", "cron"),
)
async def test_sched_01_same_id_with_changed_create_facts_is_rejected(
    tmp_path: Path,
    changed: Schedule,
) -> None:
    runtime = await _runtime(tmp_path / "conflict.db")
    try:
        factory = _uow_factory(runtime)
        await _persist(factory, _schedule())
        with pytest.raises(SchedulePersistenceConflict) as caught:
            await _persist(factory, changed)
        assert caught.value.code == "schedule_id_conflict"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_01_uncommitted_insert_rolls_back(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "rollback.db")
    try:
        factory = _uow_factory(runtime)
        schedule = _schedule()
        async with factory() as unit_of_work:
            result = await unit_of_work.schedules.add_or_get(schedule)
            assert result.inserted is True

        async with factory() as unit_of_work:
            assert await unit_of_work.schedules.get(schedule.id) is None
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_01_constraints_reject_invalid_persisted_configuration(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "constraints.db")
    try:
        await _persist(_uow_factory(runtime), _schedule())
        invalid_updates = (
            {"version": 0},
            {"misfire_policy": "burst"},
            {"misfire_grace_seconds": -1},
            {"misfire_grace_seconds": 86_401},
            {"timezone_name": " "},
            {"cron_expression": " "},
            {"integrity_digest": "too-short"},
        )
        for values in invalid_updates:
            with pytest.raises(IntegrityError):
                async with runtime.engine.begin() as connection:
                    await connection.execute(
                        update(ScheduleRecord)
                        .where(ScheduleRecord.id == _schedule().id)
                        .values(**values)
                    )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampered_values",
    (
        {"timezone_name": "Europe/London"},
        {"next_run_at_utc": NEXT_RUN + timedelta(days=1)},
        {"cron_expression": "0 8 * * *"},
    ),
    ids=("timezone", "next-utc-run", "cron"),
)
async def test_sched_01_hydration_fails_closed_on_valid_looking_tampering(
    tmp_path: Path,
    tampered_values: dict[str, object],
) -> None:
    runtime = await _runtime(tmp_path / f"tamper-{next(iter(tampered_values))}.db")
    try:
        schedule = _schedule()
        factory = _uow_factory(runtime)
        await _persist(factory, schedule)
        async with runtime.engine.begin() as connection:
            await connection.execute(
                update(ScheduleRecord)
                .where(ScheduleRecord.id == schedule.id)
                .values(**tampered_values)
            )

        async with factory() as unit_of_work:
            with pytest.raises(SchedulePersistenceConflict) as caught:
                await unit_of_work.schedules.get(schedule.id)
        assert caught.value.code == "schedule_tampered"
    finally:
        await runtime.dispose()
