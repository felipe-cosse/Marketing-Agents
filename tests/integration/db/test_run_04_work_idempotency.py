"""RUN-04 persistence compatibility after the ORCH-02 sealed validation boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.repositories import (
    AuditRepository,
    RunRepository,
    WorkInsertResult,
    WorkRepository,
)
from marketing_agents.application.services import (
    AdmissionDisposition,
    WorkAdmissionResult,
    WorkAdmissionService,
    WorkIdempotencyError,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.entities import WorkItem
from marketing_agents.domain.enums import WorkMode
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models import WorkItemRecord
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.incoming_work import validate_incoming_for_test

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class IncrementingIds:
    def __init__(self, start: int = 0) -> None:
        self._next = start
        self.generated: list[str] = []

    def new(self, namespace: str) -> str:
        self._next += 1
        value = f"{namespace}.{self._next:04d}"
        self.generated.append(value)
        return value


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


class BarrierWorkRepository:
    def __init__(self, delegate: WorkRepository, barrier: AsyncBarrier) -> None:
        self._delegate = delegate
        self._barrier = barrier

    async def get(self, work_item_id: str) -> WorkItem | None:
        return await self._delegate.get(work_item_id)

    async def get_by_source_key(
        self,
        source: str,
        event_id: str,
        instance_id: str,
    ) -> WorkItem | None:
        return await self._delegate.get_by_source_key(source, event_id, instance_id)

    async def add(self, work_item: WorkItem) -> None:
        await self._delegate.add(work_item)

    async def add_or_get(self, work_item: WorkItem) -> WorkInsertResult:
        await self._barrier.wait()
        return await self._delegate.add_or_get(work_item)


def _unused_run_repository(_session: AsyncSession) -> RunRepository:
    return cast(RunRepository, object())


def _unused_audit_repository(_session: AsyncSession) -> AuditRepository:
    return cast(AuditRepository, object())


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _envelope(*, payload: dict[str, object] | None = None) -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id="event.0001",
        instance_id="instance.email.01",
        trigger_id="trigger.manual.01",
        workflow_id="workflow.email-signup.v1",
        mode=WorkMode.MOCK_EXECUTION,
        brief_id="brief.signup.01",
        brief_revision=1,
        configuration_revision=2,
        admitted_payload=payload or {"email": "person@example.test"},
    )


def _key(offset: int = 0) -> DigestKey:
    return DigestKey(bytes((value + offset) % 256 for value in range(32)))


def _service(
    runtime: DatabaseRuntime,
    *,
    key: DigestKey | None = None,
    ids: IncrementingIds | None = None,
    work_factory: Callable[[AsyncSession], WorkRepository] = SQLAlchemyWorkRepository,
) -> WorkAdmissionService:
    unit_of_work_factory = _unit_of_work_factory(runtime, work_factory=work_factory)
    dependencies = OrchestrationDependencies(
        clock=FixedClock(),
        ids=ids or IncrementingIds(),
        unit_of_work_factory=unit_of_work_factory,
    )
    return WorkAdmissionService(dependencies, key or _key())


def _unit_of_work_factory(
    runtime: DatabaseRuntime,
    *,
    work_factory: Callable[[AsyncSession], WorkRepository] = SQLAlchemyWorkRepository,
) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=work_factory,
            runs=_unused_run_repository,
            audits=_unused_audit_repository,
        ),
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(_sqlite_url(path))
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


async def _work_count(runtime: DatabaseRuntime) -> int:
    async with runtime.session_factory() as session:
        return int((await session.execute(select(func.count(WorkItemRecord.id)))).scalar_one())


@pytest.mark.asyncio
async def test_run_04_identical_replay_returns_original_and_changed_payload_conflicts(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "sequential.db")
    service = _service(runtime)
    try:
        created = await service.admit(validate_incoming_for_test(_envelope()))
        replayed = await service.admit(validate_incoming_for_test(_envelope()))

        assert created.disposition is AdmissionDisposition.CREATED
        assert replayed.disposition is AdmissionDisposition.REPLAYED
        assert replayed.work_item.id == created.work_item.id
        assert replayed.work_item.admitted_payload == created.work_item.admitted_payload
        assert "person@example.test" not in repr(created.work_item)
        assert created.work_item.admission_digest not in repr(created.work_item)
        with pytest.raises(WorkIdempotencyError) as collision:
            await service.admit(
                validate_incoming_for_test(_envelope(payload={"email": "changed@example.test"}))
            )
        assert collision.value.code == "idempotency_conflict"
        assert collision.value.existing_work_item_id == created.work_item.id
        different_key_service = _service(runtime, key=_key(1), ids=IncrementingIds(200))
        with pytest.raises(WorkIdempotencyError) as key_mismatch:
            await different_key_service.admit(validate_incoming_for_test(_envelope()))
        assert key_mismatch.value.code == "digest_key_version_mismatch"
        assert key_mismatch.value.existing_work_item_id == created.work_item.id
        assert await _work_count(runtime) == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_04_same_source_key_rejects_changed_routing_mode_or_context(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "context-conflicts.db")
    service = _service(runtime)
    original = _envelope()
    changes = (
        replace(original, trigger_id="trigger.manual.02"),
        replace(original, workflow_id="workflow.email-review.v1"),
        replace(original, mode=WorkMode.DRY_RUN),
        replace(original, brief_id="brief.signup.02"),
        replace(original, brief_revision=2),
        replace(original, configuration_revision=3),
    )
    try:
        created = await service.admit(validate_incoming_for_test(original))
        for changed in changes:
            with pytest.raises(WorkIdempotencyError) as collision:
                await service.admit(validate_incoming_for_test(changed))
            assert collision.value.code == "idempotency_conflict"
            assert collision.value.existing_work_item_id == created.work_item.id
        assert await _work_count(runtime) == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_04_two_sessions_resolve_identical_race_to_created_and_replayed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "identical-race.db")
    barrier = AsyncBarrier(2)

    def work_factory(session: AsyncSession) -> WorkRepository:
        return BarrierWorkRepository(SQLAlchemyWorkRepository(session), barrier)

    first_ids = IncrementingIds(0)
    second_ids = IncrementingIds(100)
    first_service = _service(runtime, ids=first_ids, work_factory=work_factory)
    second_service = _service(runtime, ids=second_ids, work_factory=work_factory)
    try:
        results = await asyncio.gather(
            first_service.admit(validate_incoming_for_test(_envelope())),
            second_service.admit(validate_incoming_for_test(_envelope())),
        )

        assert {item.disposition for item in results} == {
            AdmissionDisposition.CREATED,
            AdmissionDisposition.REPLAYED,
        }
        assert len({item.work_item.id for item in results}) == 1
        assert first_ids.generated == ["work.0001"]
        assert second_ids.generated == ["work.0101"]
        assert first_ids.generated != second_ids.generated
        assert await _work_count(runtime) == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_04_two_sessions_resolve_changed_race_to_one_row_and_conflict(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "changed-race.db")
    barrier = AsyncBarrier(2)

    def work_factory(session: AsyncSession) -> WorkRepository:
        return BarrierWorkRepository(SQLAlchemyWorkRepository(session), barrier)

    first_ids = IncrementingIds(0)
    second_ids = IncrementingIds(100)
    first_service = _service(runtime, ids=first_ids, work_factory=work_factory)
    second_service = _service(runtime, ids=second_ids, work_factory=work_factory)
    try:
        outcomes = await asyncio.gather(
            first_service.admit(validate_incoming_for_test(_envelope(payload={"choice": "first"}))),
            second_service.admit(
                validate_incoming_for_test(_envelope(payload={"choice": "second"}))
            ),
            return_exceptions=True,
        )

        created = [item for item in outcomes if isinstance(item, WorkAdmissionResult)]
        errors = [item for item in outcomes if isinstance(item, WorkIdempotencyError)]
        assert len(created) == 1
        assert created[0].disposition is AdmissionDisposition.CREATED
        assert len(errors) == 1
        assert errors[0].code == "idempotency_conflict"
        assert first_ids.generated == ["work.0001"]
        assert second_ids.generated == ["work.0101"]
        assert first_ids.generated != second_ids.generated
        assert await _work_count(runtime) == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_04_restart_with_same_database_and_key_returns_original_work(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart.db"
    first_runtime = await _runtime(database_path)
    first_service = _service(first_runtime, key=_key())
    created = await first_service.admit(validate_incoming_for_test(_envelope()))
    await first_runtime.dispose()

    restarted_runtime = await _runtime(database_path)
    restarted_service = _service(restarted_runtime, key=_key())
    try:
        replayed = await restarted_service.admit(validate_incoming_for_test(_envelope()))
        assert replayed.disposition is AdmissionDisposition.REPLAYED
        assert replayed.work_item.id == created.work_item.id
        assert replayed.work_item.input_digest == created.work_item.input_digest
        assert replayed.work_item.admission_digest == created.work_item.admission_digest
        assert await _work_count(restarted_runtime) == 1
    finally:
        await restarted_runtime.dispose()


@pytest.mark.asyncio
async def test_run_04_admit_in_uow_never_commits_the_outer_transaction(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "rollback.db")
    service = _service(runtime)
    unit_of_work = _unit_of_work_factory(runtime)()
    try:
        async with unit_of_work:
            result = await service.admit_in_uow(
                unit_of_work,
                validate_incoming_for_test(_envelope()),
            )
            assert result.disposition is AdmissionDisposition.CREATED

        assert await _work_count(runtime) == 0
    finally:
        await runtime.dispose()
