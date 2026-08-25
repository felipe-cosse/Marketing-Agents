"""SCHED-02: one durable lease winner for each persisted due instant."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.repositories import ScheduleRepository
from marketing_agents.application.services import ScheduleClaimService
from marketing_agents.domain.entities import Schedule, ScheduleClaim
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
from marketing_agents.infrastructure.db.models import RunRecord, WorkItemRecord
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
LEASE_DURATION = timedelta(minutes=2)


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class UnusedIds:
    def new(self, namespace: str) -> str:
        raise AssertionError(f"SCHED-02 must not allocate {namespace} IDs")


class AsyncBarrier:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrivals = 0
        self._released = asyncio.Event()

    async def wait(self) -> None:
        self._arrivals += 1
        if self._arrivals == self._parties:
            self._released.set()
        await self._released.wait()


class BarrierScheduleRepository:
    """Make both workers retain the same due/version snapshot before CAS."""

    def __init__(self, delegate: ScheduleRepository, barrier: AsyncBarrier) -> None:
        self._delegate = delegate
        self._barrier = barrier

    async def get(self, schedule_id: str) -> Schedule | None:
        return await self._delegate.get(schedule_id)

    async def get_claim(self, schedule_id: str) -> ScheduleClaim | None:
        return await self._delegate.get_claim(schedule_id)

    async def add_or_get(self, schedule: Schedule):  # type: ignore[no-untyped-def]
        return await self._delegate.add_or_get(schedule)

    async def list_claimable_due(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Schedule, ...]:
        candidates = await self._delegate.list_claimable_due(now=now, limit=limit)
        await self._barrier.wait()
        return candidates

    async def try_claim(
        self,
        *,
        schedule_id: str,
        expected_version: int,
        expected_due_at_utc: datetime,
        lease_owner: str,
        claimed_at_utc: datetime,
        lease_expires_at_utc: datetime,
    ) -> ScheduleClaim | None:
        return await self._delegate.try_claim(
            schedule_id=schedule_id,
            expected_version=expected_version,
            expected_due_at_utc=expected_due_at_utc,
            lease_owner=lease_owner,
            claimed_at_utc=claimed_at_utc,
            lease_expires_at_utc=lease_expires_at_utc,
        )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


def _uow_factory(
    runtime: DatabaseRuntime,
    *,
    schedule_factory: Any = SQLAlchemyScheduleRepository,
) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            schedules=schedule_factory,
        ),
    )


def _schedule(
    *,
    schedule_id: str = "schedule.sched-02.due",
    next_run_at_utc: datetime = NOW,
    enabled: bool = True,
) -> Schedule:
    return Schedule(
        id=schedule_id,
        trigger_id=f"trigger.{schedule_id}",
        instance_id="instance.sched-02.target",
        cron="0 12 * * *",
        timezone="UTC",
        next_run_at_utc=next_run_at_utc,
        misfire_policy=MisfirePolicy.RUN_ONCE,
        enabled=enabled,
    )


async def _persist(
    factory: SQLAlchemyUnitOfWorkFactory,
    *schedules: Schedule,
) -> None:
    async with factory() as unit_of_work:
        for schedule in schedules:
            await unit_of_work.schedules.add_or_get(schedule)
        await unit_of_work.commit()


def _service(
    factory: SQLAlchemyUnitOfWorkFactory,
    clock: MutableClock,
) -> ScheduleClaimService:
    return ScheduleClaimService(
        OrchestrationDependencies(clock, UnusedIds(), factory),
        lease_duration=LEASE_DURATION,
    )


@pytest.mark.asyncio
async def test_sched_02_two_independent_workers_race_to_one_claim(tmp_path: Path) -> None:
    database_path = tmp_path / "claim-race.db"
    runtime = await _runtime(database_path)
    plain_factory = _uow_factory(runtime)
    await _persist(plain_factory, _schedule())
    barrier = AsyncBarrier(2)

    def barrier_factory(session: AsyncSession) -> ScheduleRepository:
        return BarrierScheduleRepository(SQLAlchemyScheduleRepository(session), barrier)

    race_factory = _uow_factory(runtime, schedule_factory=barrier_factory)
    first = _service(race_factory, MutableClock())
    second = _service(race_factory, MutableClock())
    try:
        outcomes = await asyncio.gather(
            first.claim_due_once(lease_owner="worker.sched-02.first"),
            second.claim_due_once(lease_owner="worker.sched-02.second"),
        )
        claims = [item for item in outcomes if item is not None]
        assert len(claims) == 1
        assert sum(item is None for item in outcomes) == 1
        winner = claims[0]
        assert winner.schedule_id == _schedule().id
        assert winner.scheduled_for_utc == NOW
        assert winner.claimed_at_utc == NOW
        assert winner.lease_expires_at_utc == NOW + LEASE_DURATION
        assert winner.version == 2

        async with plain_factory() as unit_of_work:
            persisted = await unit_of_work.schedules.get_claim(_schedule().id)
            schedule = await unit_of_work.schedules.get(_schedule().id)
            work_count = await cast(AsyncSession, unit_of_work._session).scalar(
                select(func.count()).select_from(WorkItemRecord)
            )
            run_count = await cast(AsyncSession, unit_of_work._session).scalar(
                select(func.count()).select_from(RunRecord)
            )
        assert persisted == winner
        assert schedule is not None and schedule.version == 2
        assert schedule.next_run_at_utc == NOW
        assert work_count == 0 and run_count == 0

        async with plain_factory() as unit_of_work:
            replay = await unit_of_work.schedules.add_or_get(_schedule())
            await unit_of_work.commit()
        assert replay.inserted is False
        assert replay.schedule.version == 2
        assert replay.schedule.next_run_at_utc == NOW

        async with plain_factory() as unit_of_work:
            assert await unit_of_work.schedules.get_claim(_schedule().id) == winner
    finally:
        await runtime.dispose()

    restarted = await _runtime(database_path)
    try:
        restarted_factory = _uow_factory(restarted)
        async with restarted_factory() as unit_of_work:
            assert await unit_of_work.schedules.get_claim(_schedule().id) == winner
        assert (
            await _service(restarted_factory, MutableClock()).claim_due_once(
                lease_owner="worker.sched-02.restart"
            )
            is None
        )
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_02_strict_expiry_reclaims_with_a_new_fencing_version(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "strict-expiry.db")
    factory = _uow_factory(runtime)
    clock = MutableClock()
    await _persist(factory, _schedule())
    try:
        first = await _service(factory, clock).claim_due_once(
            lease_owner="worker.sched-02.original"
        )
        assert first is not None and first.version == 2
        assert (
            await _service(factory, clock).claim_due_once(lease_owner="worker.sched-02.original")
            is None
        )

        clock.current = first.lease_expires_at_utc
        assert (
            await _service(factory, clock).claim_due_once(lease_owner="worker.sched-02.at-expiry")
            is None
        )

        clock.current += timedelta(microseconds=1)
        replacement = await _service(factory, clock).claim_due_once(
            lease_owner="worker.sched-02.replacement"
        )
        assert replacement is not None
        assert replacement.version == 3
        assert replacement.scheduled_for_utc == first.scheduled_for_utc
        assert replacement.lease_owner == "worker.sched-02.replacement"

        async with factory() as unit_of_work:
            stale = await unit_of_work.schedules.try_claim(
                schedule_id=first.schedule_id,
                expected_version=first.version,
                expected_due_at_utc=first.scheduled_for_utc,
                lease_owner=first.lease_owner,
                claimed_at_utc=clock.current,
                lease_expires_at_utc=clock.current + LEASE_DURATION,
            )
        assert stale is None
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_02_due_scan_is_bounded_ordered_and_filters_future_or_disabled(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "due-scan.db")
    factory = _uow_factory(runtime)
    await _persist(
        factory,
        _schedule(schedule_id="schedule.sched-02.b", next_run_at_utc=NOW - timedelta(minutes=1)),
        _schedule(schedule_id="schedule.sched-02.a", next_run_at_utc=NOW - timedelta(minutes=1)),
        _schedule(schedule_id="schedule.sched-02.exact", next_run_at_utc=NOW),
        _schedule(
            schedule_id="schedule.sched-02.disabled",
            next_run_at_utc=NOW - timedelta(hours=1),
            enabled=False,
        ),
        _schedule(
            schedule_id="schedule.sched-02.future",
            next_run_at_utc=NOW + timedelta(seconds=1),
        ),
    )
    try:
        async with factory() as unit_of_work:
            all_due = await unit_of_work.schedules.list_claimable_due(now=NOW, limit=10)
            due = await unit_of_work.schedules.list_claimable_due(now=NOW, limit=2)
        assert tuple(item.id for item in all_due) == (
            "schedule.sched-02.a",
            "schedule.sched-02.b",
            "schedule.sched-02.exact",
        )
        assert tuple(item.id for item in due) == (
            "schedule.sched-02.a",
            "schedule.sched-02.b",
        )

        claim = await ScheduleClaimService(
            OrchestrationDependencies(MutableClock(), UnusedIds(), factory),
            lease_duration=LEASE_DURATION,
            batch_size=1,
        ).claim_due_once(lease_owner="worker.sched-02.ordered")
        assert claim is not None and claim.schedule_id == "schedule.sched-02.a"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_02_stale_due_and_uncommitted_claims_fail_closed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "rollback.db")
    factory = _uow_factory(runtime)
    schedule = _schedule()
    await _persist(factory, schedule)
    try:
        async with factory() as unit_of_work:
            assert (
                await unit_of_work.schedules.try_claim(
                    schedule_id=schedule.id,
                    expected_version=2,
                    expected_due_at_utc=schedule.next_run_at_utc,
                    lease_owner="worker.sched-02.stale",
                    claimed_at_utc=NOW,
                    lease_expires_at_utc=NOW + LEASE_DURATION,
                )
                is None
            )
            assert (
                await unit_of_work.schedules.try_claim(
                    schedule_id=schedule.id,
                    expected_version=1,
                    expected_due_at_utc=schedule.next_run_at_utc - timedelta(seconds=1),
                    lease_owner="worker.sched-02.wrong-due",
                    claimed_at_utc=NOW,
                    lease_expires_at_utc=NOW + LEASE_DURATION,
                )
                is None
            )
            rolled_back = await unit_of_work.schedules.try_claim(
                schedule_id=schedule.id,
                expected_version=1,
                expected_due_at_utc=schedule.next_run_at_utc,
                lease_owner="worker.sched-02.rollback",
                claimed_at_utc=NOW,
                lease_expires_at_utc=NOW + LEASE_DURATION,
            )
            assert rolled_back is not None

        async with factory() as unit_of_work:
            assert await unit_of_work.schedules.get_claim(schedule.id) is None
            restored = await unit_of_work.schedules.get(schedule.id)
        assert restored == schedule
    finally:
        await runtime.dispose()


def test_sched_02_lease_schema_is_portable_and_constrained() -> None:
    schedule_table = cast(Table, ScheduleRecord.__table__)
    postgres_dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    sqlite_ddl = " ".join(
        str(CreateTable(schedule_table).compile(dialect=sqlite.dialect())).lower().split()
    )
    postgres_ddl = " ".join(
        str(CreateTable(schedule_table).compile(dialect=postgres_dialect)).lower().split()
    )
    for ddl in (sqlite_ddl, postgres_ddl):
        assert "lease_owner" in ddl
        assert "lease_claimed_at_utc" in ddl
        assert "lease_expires_at_utc" in ddl
        assert "ck_schedules_lease_owner_bounded" in ddl
        assert "ck_schedules_lease_complete" in ddl
        assert "ck_schedules_lease_expiry_after_claim" in ddl
        assert "ck_schedules_lease_due_at_claim" in ddl
        assert "ck_schedules_lease_version_advanced" in ddl
    assert "lease_claimed_at_utc timestamp with time zone" in postgres_ddl
    assert "lease_expires_at_utc timestamp with time zone" in postgres_ddl


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_lease",
    (
        {"lease_owner": "worker.sched-02.incomplete"},
        {
            "lease_owner": "worker.sched-02.reversed",
            "lease_claimed_at_utc": NOW,
            "lease_expires_at_utc": NOW,
            "version": 2,
        },
        {
            "lease_owner": "worker.sched-02.before-due",
            "lease_claimed_at_utc": NOW - timedelta(minutes=1),
            "lease_expires_at_utc": NOW + timedelta(minutes=1),
            "version": 2,
        },
        {
            "lease_owner": "worker.sched-02.version-one",
            "lease_claimed_at_utc": NOW,
            "lease_expires_at_utc": NOW + timedelta(minutes=1),
        },
    ),
    ids=("incomplete", "nonfuture-expiry", "before-due", "version-not-advanced"),
)
async def test_sched_02_database_rejects_invalid_lease_tuples(
    tmp_path: Path,
    invalid_lease: dict[str, object],
) -> None:
    runtime = await _runtime(tmp_path / f"constraint-{next(iter(invalid_lease))}.db")
    factory = _uow_factory(runtime)
    schedule = _schedule()
    await _persist(factory, schedule)
    try:
        with pytest.raises(IntegrityError):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    update(ScheduleRecord)
                    .where(ScheduleRecord.id == schedule.id)
                    .values(**invalid_lease)
                )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_02_lease_drift_without_digest_recomputation_fails_closed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "lease-tamper.db")
    factory = _uow_factory(runtime)
    schedule = _schedule()
    await _persist(factory, schedule)
    try:
        claim = await _service(factory, MutableClock()).claim_due_once(
            lease_owner="worker.sched-02.integrity"
        )
        assert claim is not None
        async with runtime.engine.begin() as connection:
            await connection.execute(
                update(ScheduleRecord)
                .where(ScheduleRecord.id == schedule.id)
                .values(lease_owner="worker.sched-02.tampered")
            )

        async with factory() as unit_of_work:
            with pytest.raises(SchedulePersistenceConflict) as captured:
                await unit_of_work.schedules.get_claim(schedule.id)
        assert captured.value.code == "schedule_tampered"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_02_sqlite_busy_is_a_lost_claim_not_an_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(tmp_path / "busy.db")
    factory = _uow_factory(runtime)
    schedule = _schedule()
    await _persist(factory, schedule)
    busy_error = sqlite3.OperationalError("database is locked")
    busy_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    try:
        async with factory() as unit_of_work:
            repository = cast(SQLAlchemyScheduleRepository, unit_of_work.schedules)
            original_execute = repository._session.execute

            async def busy_on_update(statement: Any, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                if getattr(statement, "is_update", False):
                    raise OperationalError("UPDATE", {}, busy_error)
                return await original_execute(statement, *args, **kwargs)

            monkeypatch.setattr(repository._session, "execute", busy_on_update)
            assert (
                await repository.try_claim(
                    schedule_id=schedule.id,
                    expected_version=1,
                    expected_due_at_utc=schedule.next_run_at_utc,
                    lease_owner="worker.sched-02.busy",
                    claimed_at_utc=NOW,
                    lease_expires_at_utc=NOW + LEASE_DURATION,
                )
                is None
            )

        async with factory() as unit_of_work:
            assert await unit_of_work.schedules.get_claim(schedule.id) is None
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_02_invalid_owner_clock_duration_and_expiry_fail_before_write(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "invalid.db")
    factory = _uow_factory(runtime)
    schedule = _schedule()
    await _persist(factory, schedule)
    try:
        with pytest.raises(ValueError):
            ScheduleClaimService(
                OrchestrationDependencies(MutableClock(), UnusedIds(), factory),
                lease_duration=timedelta(0),
            )
        with pytest.raises(ValueError):
            ScheduleClaimService(
                OrchestrationDependencies(MutableClock(), UnusedIds(), factory),
                lease_duration=timedelta(minutes=11),
            )
        with pytest.raises(ValueError):
            ScheduleClaimService(
                OrchestrationDependencies(MutableClock(), UnusedIds(), factory),
                batch_size=True,
            )
        with pytest.raises(ValueError):
            await _service(factory, MutableClock()).claim_due_once(lease_owner="")
        with pytest.raises(ValueError):
            await _service(factory, MutableClock(datetime(2026, 8, 25, 12))).claim_due_once(
                lease_owner="worker.sched-02.naive"
            )
        with pytest.raises(ValueError):
            await _service(
                factory,
                MutableClock(
                    datetime(
                        2026,
                        8,
                        25,
                        13,
                        tzinfo=timezone(timedelta(hours=1)),
                    )
                ),
            ).claim_due_once(lease_owner="worker.sched-02.offset")

        async with factory() as unit_of_work:
            with pytest.raises(ValueError):
                await unit_of_work.schedules.try_claim(
                    schedule_id=schedule.id,
                    expected_version=1,
                    expected_due_at_utc=schedule.next_run_at_utc,
                    lease_owner="worker.sched-02.bad-expiry",
                    claimed_at_utc=NOW,
                    lease_expires_at_utc=NOW,
                )
        async with factory() as unit_of_work:
            assert await unit_of_work.schedules.get_claim(schedule.id) is None
    finally:
        await runtime.dispose()
