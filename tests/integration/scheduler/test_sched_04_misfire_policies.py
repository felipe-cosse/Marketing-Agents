"""SCHED-04: durable explicit schedule policy and grace configuration."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
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
from sqlalchemy import update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

DUE = datetime(2026, 8, 26, 16, 30, tzinfo=UTC)


def _schedule(
    *,
    schedule_id: str,
    policy: MisfirePolicy,
    grace_seconds: int,
) -> Schedule:
    return Schedule(
        id=schedule_id,
        trigger_id=f"trigger.{schedule_id}",
        instance_id="instance.sched-04.target",
        workflow_id="workflow.sched-04.target",
        cron="30 9 * * *",
        timezone="America/Argentina/Buenos_Aires",
        next_run_at_utc=DUE,
        misfire_policy=policy,
        misfire_grace_seconds=grace_seconds,
        enabled=True,
        recurrence_version="five-field-cron-adr0008-v1",
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


def _factory(runtime: DatabaseRuntime) -> SQLAlchemyUnitOfWorkFactory:
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
) -> None:
    async with factory() as unit_of_work:
        await unit_of_work.schedules.add_or_get(schedule)
        await unit_of_work.commit()


@pytest.mark.asyncio
async def test_sched_04_explicit_policy_and_grace_round_trip_across_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "misfire-policy-restart.db"
    schedules = (
        _schedule(
            schedule_id="schedule.sched-04.skip",
            policy=MisfirePolicy.SKIP,
            grace_seconds=0,
        ),
        _schedule(
            schedule_id="schedule.sched-04.run-once",
            policy=MisfirePolicy.RUN_ONCE,
            grace_seconds=86_400,
        ),
    )
    runtime = await _runtime(database_path)
    try:
        for schedule in schedules:
            await _persist(_factory(runtime), schedule)
    finally:
        await runtime.dispose()

    restarted = await _runtime(database_path)
    try:
        factory = _factory(restarted)
        async with factory() as unit_of_work:
            restored = tuple(
                [await unit_of_work.schedules.get(schedule.id) for schedule in schedules]
            )
        assert restored == schedules

        with pytest.raises(SchedulePersistenceConflict) as changed:
            await _persist(factory, replace(schedules[0], misfire_grace_seconds=1))
        assert changed.value.code == "schedule_id_conflict"
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_04_database_rejects_invalid_grace_and_detects_unsealed_drift(
    tmp_path: Path,
) -> None:
    sqlite_ddl = " ".join(
        str(CreateTable(ScheduleRecord.__table__).compile(dialect=sqlite.dialect())).lower().split()
    )
    postgres_ddl = " ".join(
        str(CreateTable(ScheduleRecord.__table__).compile(dialect=postgresql.dialect()))
        .lower()
        .split()
    )
    for ddl in (sqlite_ddl, postgres_ddl):
        assert "misfire_grace_seconds integer not null" in ddl
        assert "ck_schedules_misfire_grace_bounded" in ddl

    runtime = await _runtime(tmp_path / "misfire-policy-integrity.db")
    schedule = _schedule(
        schedule_id="schedule.sched-04.integrity",
        policy=MisfirePolicy.SKIP,
        grace_seconds=60,
    )
    try:
        factory = _factory(runtime)
        await _persist(factory, schedule)
        for invalid in (-1, 60.5, 86_401):
            with pytest.raises(IntegrityError):
                async with runtime.engine.begin() as connection:
                    await connection.execute(
                        update(ScheduleRecord)
                        .where(ScheduleRecord.id == schedule.id)
                        .values(misfire_grace_seconds=invalid)
                    )

        async with runtime.engine.begin() as connection:
            await connection.execute(
                update(ScheduleRecord)
                .where(ScheduleRecord.id == schedule.id)
                .values(misfire_grace_seconds=61)
            )
        async with factory() as unit_of_work:
            with pytest.raises(SchedulePersistenceConflict) as tampered:
                await unit_of_work.schedules.get(schedule.id)
        assert tampered.value.code == "schedule_tampered"
    finally:
        await runtime.dispose()
