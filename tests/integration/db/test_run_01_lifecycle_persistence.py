"""RUN-01: durable primary-Run lifecycle, CAS, restart, and fault rollback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.repositories import (
    AuditRepository,
    RunInsertResult,
    RunRepository,
)
from marketing_agents.application.services import (
    AdmissionDisposition,
    ReceiveRunRequest,
    ReceiveRunResult,
    RunLifecycleService,
    RunLifecycleServiceError,
    WorkAdmissionService,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.entities import Run
from marketing_agents.domain.enums import RunState, WorkMode
from marketing_agents.domain.run_lifecycle import (
    CancellationContext,
    CompletionContext,
    NoRunTransitionContext,
    PlanDispositionContext,
    RunLifecycleCommand,
    RunStateTransition,
    RunTransitionContext,
    RunTransitionResult,
)
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models import (
    RunRecord,
    RunStateTransitionRecord,
    WorkItemRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.incoming_work import validate_incoming_for_test

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)
CATALOG_HASH = "c" * 64


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def tick(self, seconds: int = 1) -> None:
        self.current += timedelta(seconds=seconds)


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


class BarrierRunRepository:
    def __init__(self, delegate: RunRepository, barrier: AsyncBarrier) -> None:
        self._delegate = delegate
        self._barrier = barrier

    async def get(self, run_id: str) -> Run | None:
        return await self._delegate.get(run_id)

    async def get_by_work_item_id(self, work_item_id: str) -> Run | None:
        return await self._delegate.get_by_work_item_id(work_item_id)

    async def add_received_or_get(
        self,
        run: Run,
        initial_transition: RunStateTransition,
    ) -> RunInsertResult:
        return await self._delegate.add_received_or_get(run, initial_transition)

    async def apply_transition(
        self,
        *,
        expected_version: int,
        expected_state: RunState,
        result: RunTransitionResult,
    ) -> bool:
        await self._barrier.wait()
        return await self._delegate.apply_transition(
            expected_version=expected_version,
            expected_state=expected_state,
            result=result,
        )

    async def list_transitions(self, run_id: str) -> tuple[RunStateTransition, ...]:
        return await self._delegate.list_transitions(run_id)


class ReceiptBarrierRunRepository:
    def __init__(self, delegate: RunRepository, barrier: AsyncBarrier) -> None:
        self._delegate = delegate
        self._barrier = barrier

    async def get(self, run_id: str) -> Run | None:
        return await self._delegate.get(run_id)

    async def get_by_work_item_id(self, work_item_id: str) -> Run | None:
        return await self._delegate.get_by_work_item_id(work_item_id)

    async def add_received_or_get(
        self,
        run: Run,
        initial_transition: RunStateTransition,
    ) -> RunInsertResult:
        await self._barrier.wait()
        return await self._delegate.add_received_or_get(run, initial_transition)

    async def apply_transition(
        self,
        *,
        expected_version: int,
        expected_state: RunState,
        result: RunTransitionResult,
    ) -> bool:
        return await self._delegate.apply_transition(
            expected_version=expected_version,
            expected_state=expected_state,
            result=result,
        )

    async def list_transitions(self, run_id: str) -> tuple[RunStateTransition, ...]:
        return await self._delegate.list_transitions(run_id)


def _unused_audit_repository(_session: AsyncSession) -> AuditRepository:
    return cast(AuditRepository, object())


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _key() -> DigestKey:
    return DigestKey(bytes(range(32)))


def _envelope() -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id="event.run-01.0001",
        instance_id="instance.email.01",
        trigger_id="trigger.manual.01",
        workflow_id="workflow.lifecycle.v1",
        mode=WorkMode.MOCK_EXECUTION,
        brief_id="brief.lifecycle.01",
        brief_revision=1,
        configuration_revision=2,
        admitted_payload={"campaign": "restart-safe"},
    )


def _unit_of_work_factory(
    runtime: DatabaseRuntime,
    *,
    run_factory: Callable[[AsyncSession], RunRepository] = SQLAlchemyRunRepository,
) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=run_factory,
            audits=_unused_audit_repository,
        ),
    )


def _services(
    runtime: DatabaseRuntime,
    clock: MutableClock,
    *,
    ids: IncrementingIds | None = None,
    run_factory: Callable[[AsyncSession], RunRepository] = SQLAlchemyRunRepository,
) -> tuple[WorkAdmissionService, RunLifecycleService, SQLAlchemyUnitOfWorkFactory]:
    unit_of_work_factory = _unit_of_work_factory(runtime, run_factory=run_factory)
    dependencies = OrchestrationDependencies(
        clock=clock,
        ids=ids or IncrementingIds(),
        unit_of_work_factory=unit_of_work_factory,
    )
    return (
        WorkAdmissionService(dependencies, _key()),
        RunLifecycleService(dependencies),
        unit_of_work_factory,
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(_sqlite_url(path))
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


async def _admit_and_receive(
    work_service: WorkAdmissionService,
    run_service: RunLifecycleService,
) -> Run:
    admitted = await work_service.admit(validate_incoming_for_test(_envelope()))
    received = await run_service.receive(ReceiveRunRequest(admitted.work_item, CATALOG_HASH))
    assert admitted.disposition is AdmissionDisposition.CREATED
    assert received.created is True
    assert received.initial_transition is not None
    return received.run


async def _record_counts(runtime: DatabaseRuntime) -> tuple[int, int, int]:
    async with runtime.session_factory() as session:
        values = []
        for column in (
            WorkItemRecord.id,
            RunRecord.id,
            RunStateTransitionRecord.run_id,
        ):
            values.append(int((await session.execute(select(func.count(column)))).scalar_one()))
    return cast(tuple[int, int, int], tuple(values))


async def _advance(
    service: RunLifecycleService,
    clock: MutableClock,
    run: Run,
    command: RunLifecycleCommand,
    context: RunTransitionContext,
) -> Run:
    clock.tick()
    return (await service.advance(run.id, run.version, command, context)).run


@pytest.mark.asyncio
async def test_run_01_lifecycle_survives_restart_with_ordered_terminal_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart.db"
    first_runtime = await _runtime(database_path)
    first_clock = MutableClock()
    first_work, first_lifecycle, _ = _services(first_runtime, first_clock)
    run = await _admit_and_receive(first_work, first_lifecycle)
    run = await _advance(
        first_lifecycle,
        first_clock,
        run,
        RunLifecycleCommand.MARK_VALIDATED,
        NoRunTransitionContext(),
    )
    await first_runtime.dispose()

    restarted_runtime = await _runtime(database_path)
    restarted_clock = MutableClock(first_clock.current)
    restarted_work, restarted_lifecycle, _ = _services(
        restarted_runtime,
        restarted_clock,
        ids=IncrementingIds(100),
    )
    try:
        replayed_work = await restarted_work.admit(validate_incoming_for_test(_envelope()))
        replayed_run = await restarted_lifecycle.receive(
            ReceiveRunRequest(replayed_work.work_item, CATALOG_HASH)
        )
        assert replayed_work.disposition is AdmissionDisposition.REPLAYED
        assert replayed_run.created is False
        assert replayed_run.initial_transition is None
        assert replayed_run.run == run

        run = await _advance(
            restarted_lifecycle,
            restarted_clock,
            run,
            RunLifecycleCommand.RECORD_PLAN,
            PlanDispositionContext(False),
        )
        run = await _advance(
            restarted_lifecycle,
            restarted_clock,
            run,
            RunLifecycleCommand.ACTIVATE_PLAN,
            NoRunTransitionContext(),
        )
        run = await _advance(
            restarted_lifecycle,
            restarted_clock,
            run,
            RunLifecycleCommand.COMPLETE,
            CompletionContext(2, 2, 0, 0),
        )

        history = await restarted_lifecycle.history(run.id)
        assert tuple(item.sequence for item in history) == (1, 2, 3, 4, 5)
        assert tuple(item.new_state for item in history) == (
            RunState.RECEIVED,
            RunState.VALIDATED,
            RunState.PLANNED,
            RunState.EXECUTING,
            RunState.COMPLETED,
        )
        assert tuple(item.resulting_version for item in history) == (1, 2, 3, 4, 5)
        assert run.state is RunState.COMPLETED
        assert run.terminal_reason_code == "execution_completed"
        assert await _record_counts(restarted_runtime) == (1, 1, 5)
    finally:
        await restarted_runtime.dispose()


@pytest.mark.asyncio
async def test_run_01_work_and_initial_run_share_caller_transaction(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "atomic-receipt.db")
    clock = MutableClock()
    work_service, lifecycle, unit_of_work_factory = _services(runtime, clock)
    try:
        async with unit_of_work_factory() as unit_of_work:
            admitted = await work_service.admit_in_uow(
                unit_of_work,
                validate_incoming_for_test(_envelope()),
            )
            received = await lifecycle.receive_in_uow(
                unit_of_work,
                ReceiveRunRequest(admitted.work_item, CATALOG_HASH),
            )
            assert received.created is True

        assert await _record_counts(runtime) == (0, 0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_01_repository_rejects_non_initial_transition_on_receipt(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "invalid-initial.db")
    clock = MutableClock()
    work_service, _, unit_of_work_factory = _services(runtime, clock)
    try:
        admitted = await work_service.admit(validate_incoming_for_test(_envelope()))
        candidate = Run(
            id="run.invalid-initial",
            work_item_id=admitted.work_item.id,
            state=RunState.RECEIVED,
            catalog_hash=CATALOG_HASH,
            configuration_revision=admitted.work_item.configuration_revision,
            created_at=NOW,
            version=1,
            updated_at=NOW,
        )
        sequence_two = RunStateTransition(
            run_id=candidate.id,
            sequence=2,
            command=RunLifecycleCommand.MARK_VALIDATED,
            previous_state=RunState.RECEIVED,
            new_state=RunState.VALIDATED,
            reason_code="input_validated",
            occurred_at=NOW,
            expected_version=1,
            resulting_version=2,
        )

        async with unit_of_work_factory() as unit_of_work:
            with pytest.raises(ValueError, match="initial received transition"):
                await unit_of_work.runs.add_received_or_get(candidate, sequence_two)

        assert await _record_counts(runtime) == (1, 0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_01_two_sessions_receive_one_primary_run_and_initial_transition(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "receipt-race.db")
    clock = MutableClock()
    base_work, _, _ = _services(runtime, clock)
    admitted = await base_work.admit(validate_incoming_for_test(_envelope()))
    barrier = AsyncBarrier(2)

    def run_factory(session: AsyncSession) -> RunRepository:
        return ReceiptBarrierRunRepository(SQLAlchemyRunRepository(session), barrier)

    first_ids = IncrementingIds(100)
    second_ids = IncrementingIds(200)
    _, first_lifecycle, _ = _services(
        runtime,
        clock,
        ids=first_ids,
        run_factory=run_factory,
    )
    _, second_lifecycle, _ = _services(
        runtime,
        clock,
        ids=second_ids,
        run_factory=run_factory,
    )
    request = ReceiveRunRequest(admitted.work_item, CATALOG_HASH)
    try:
        outcomes = await asyncio.gather(
            first_lifecycle.receive(request),
            second_lifecycle.receive(request),
        )

        assert all(isinstance(item, ReceiveRunResult) for item in outcomes)
        assert {item.created for item in outcomes} == {True, False}
        assert len({item.run.id for item in outcomes}) == 1
        assert sum(item.initial_transition is not None for item in outcomes) == 1
        assert first_ids.generated == ["run.0101"]
        assert second_ids.generated == ["run.0201"]
        assert first_ids.generated != second_ids.generated
        assert await _record_counts(runtime) == (1, 1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_01_two_sessions_allow_exactly_one_expected_version_transition(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "cas-race.db")
    barrier = AsyncBarrier(2)

    def run_factory(session: AsyncSession) -> RunRepository:
        return BarrierRunRepository(SQLAlchemyRunRepository(session), barrier)

    clock = MutableClock()
    work_service, lifecycle, _ = _services(runtime, clock, run_factory=run_factory)
    run = await _admit_and_receive(work_service, lifecycle)
    clock.tick()
    try:
        outcomes = await asyncio.gather(
            lifecycle.advance(
                run.id,
                run.version,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
            ),
            lifecycle.advance(
                run.id,
                run.version,
                RunLifecycleCommand.CANCEL,
                CancellationContext("concurrent_operator_cancel"),
            ),
            return_exceptions=True,
        )

        accepted = [item for item in outcomes if isinstance(item, RunTransitionResult)]
        rejected = [item for item in outcomes if isinstance(item, RunLifecycleServiceError)]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert rejected[0].code == "stale_run_version"
        assert accepted[0].run.version == 2
        assert accepted[0].run.state in {RunState.VALIDATED, RunState.CANCELLED}

        history = await lifecycle.history(run.id)
        assert tuple(item.sequence for item in history) == (1, 2)
        assert history[-1].new_state is accepted[0].run.state
        assert await _record_counts(runtime) == (1, 1, 2)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_01_transition_insert_fault_rolls_back_primary_run_update(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "transition-fault.db")
    clock = MutableClock()
    work_service, lifecycle, _ = _services(runtime, clock)
    run = await _admit_and_receive(work_service, lifecycle)
    async with runtime.engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TRIGGER run_01_fail_transition_insert
                BEFORE INSERT ON run_state_transitions
                WHEN NEW.sequence > 1
                BEGIN
                    SELECT RAISE(ABORT, 'injected transition failure');
                END
                """
            )
        )

    clock.tick()
    try:
        with pytest.raises(IntegrityError, match="injected transition failure"):
            await lifecycle.advance(
                run.id,
                run.version,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
            )

        history = await lifecycle.history(run.id)
        async with runtime.session_factory() as session:
            stored = await session.get(RunRecord, run.id)
        assert stored is not None
        assert stored.state == RunState.RECEIVED.value
        assert stored.version == 1
        assert tuple(item.sequence for item in history) == (1,)
        assert await _record_counts(runtime) == (1, 1, 1)
    finally:
        await runtime.dispose()
