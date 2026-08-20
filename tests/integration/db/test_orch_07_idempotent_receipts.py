"""ORCH-07: atomic WorkItem plus primary-Run receipt and replay."""

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
    RunInsertResult,
    RunRepository,
    WorkInsertResult,
    WorkRepository,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services import (
    AuditedPlanPersistenceService,
    IdempotentWorkRunReceiptService,
    IncomingWorkValidationError,
    RunLifecycleService,
    RunStepLifecycleService,
    WorkAdmissionService,
    WorkIdempotencyError,
    WorkRunReceiptDisposition,
    WorkRunReceiptError,
)
from marketing_agents.application.services.incoming_work_validation import (
    ValidatedIncomingWork,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, WorkMode
from marketing_agents.domain.run_lifecycle import (
    CompletionContext,
    NoRunTransitionContext,
    RunLifecycleCommand,
    RunStateTransition,
    RunTransitionContext,
    RunTransitionResult,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
)
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    SQLAlchemyAuditRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
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
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.incoming_work import TEST_CATALOG_HASH, validate_incoming_for_test
from tests.support.orch_09_planning import build_read_only_plan

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)
CATALOG_B = "catalog-sha256-v1:" + ("b" * 64)


class FixedClock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return NOW


class IncrementingIds:
    def __init__(self, start: int = 0) -> None:
        self._next = start
        self.generated: list[str] = []

    def new(self, namespace: str) -> str:
        self._next += 1
        value = f"{namespace}.orch-07.{self._next:04d}"
        self.generated.append(value)
        return value


class ProbeUnitOfWorkFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> UnitOfWork:
        self.calls += 1
        raise AssertionError("invalid incoming work must fail before opening a unit of work")


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


class FaultingRunRepository:
    def __init__(self, delegate: RunRepository, *, after_flush: bool) -> None:
        self._delegate = delegate
        self._after_flush = after_flush

    async def get(self, run_id: str) -> Run | None:
        return await self._delegate.get(run_id)

    async def get_by_work_item_id(self, work_item_id: str) -> Run | None:
        return await self._delegate.get_by_work_item_id(work_item_id)

    async def add_received_or_get(
        self,
        run: Run,
        initial_transition: RunStateTransition,
    ) -> RunInsertResult:
        if not self._after_flush:
            raise RuntimeError("injected fault before Run insert")
        await self._delegate.add_received_or_get(run, initial_transition)
        raise RuntimeError("injected fault after Run and transition flush")

    async def fence(
        self,
        *,
        run_id: str,
        expected_version: int,
        expected_state: RunState,
    ) -> bool:
        return await self._delegate.fence(
            run_id=run_id,
            expected_version=expected_version,
            expected_state=expected_state,
        )

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


class ExistingRunRepository(FaultingRunRepository):
    def __init__(self, delegate: RunRepository, existing_run_id: str) -> None:
        super().__init__(delegate, after_flush=False)
        self._existing_run_id = existing_run_id

    async def add_received_or_get(
        self,
        run: Run,
        initial_transition: RunStateTransition,
    ) -> RunInsertResult:
        del run, initial_transition
        existing = await self._delegate.get(self._existing_run_id)
        assert existing is not None
        return RunInsertResult(existing, inserted=False)


class WrongCatalogRunRepository(FaultingRunRepository):
    def __init__(self, delegate: RunRepository) -> None:
        super().__init__(delegate, after_flush=False)

    async def add_received_or_get(
        self,
        run: Run,
        initial_transition: RunStateTransition,
    ) -> RunInsertResult:
        stored = await self._delegate.add_received_or_get(run, initial_transition)
        return RunInsertResult(
            replace(stored.run, catalog_hash=CATALOG_B),
            inserted=stored.inserted,
        )


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _key() -> DigestKey:
    return DigestKey(bytes(range(32)))


def _audit_context(correlation_id: str) -> AuditContext:
    return AuditContext.system(
        "test.orch-07.receipt",
        correlation_id=correlation_id,
    )


def _envelope(
    *,
    event_id: str = "event.orch-07.0001",
    payload: dict[str, object] | None = None,
) -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id=event_id,
        instance_id="instance.email.01",
        trigger_id="trigger.manual.01",
        workflow_id="workflow.orch-07.v1",
        mode=WorkMode.MOCK_EXECUTION,
        brief_id=None,
        brief_revision=None,
        configuration_revision=2,
        admitted_payload=payload or {"campaign": "restart-safe"},
    )


def _uow_factory(
    runtime: DatabaseRuntime,
    *,
    work_factory: Callable[[AsyncSession], WorkRepository] = SQLAlchemyWorkRepository,
    run_factory: Callable[[AsyncSession], RunRepository] = SQLAlchemyRunRepository,
) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=work_factory,
            runs=run_factory,
            audits=SQLAlchemyAuditRepository,
            run_steps=SQLAlchemyRunStepRepository,
        ),
    )


def _dependencies(
    runtime: DatabaseRuntime,
    *,
    ids: IncrementingIds | None = None,
    clock: FixedClock | None = None,
    work_factory: Callable[[AsyncSession], WorkRepository] = SQLAlchemyWorkRepository,
    run_factory: Callable[[AsyncSession], RunRepository] = SQLAlchemyRunRepository,
) -> OrchestrationDependencies:
    return OrchestrationDependencies(
        clock or FixedClock(),
        ids or IncrementingIds(),
        _uow_factory(runtime, work_factory=work_factory, run_factory=run_factory),
    )


def _service(
    runtime: DatabaseRuntime,
    *,
    catalog_hash: str = TEST_CATALOG_HASH,
    ids: IncrementingIds | None = None,
    clock: FixedClock | None = None,
    work_factory: Callable[[AsyncSession], WorkRepository] = SQLAlchemyWorkRepository,
    run_factory: Callable[[AsyncSession], RunRepository] = SQLAlchemyRunRepository,
) -> IdempotentWorkRunReceiptService:
    return IdempotentWorkRunReceiptService(
        _dependencies(
            runtime,
            ids=ids,
            clock=clock,
            work_factory=work_factory,
            run_factory=run_factory,
        ),
        _key(),
        current_catalog_hash=catalog_hash,
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(_sqlite_url(path))
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


async def _counts(runtime: DatabaseRuntime) -> tuple[int, int, int]:
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
    run: Run,
    command: RunLifecycleCommand,
    context: RunTransitionContext,
) -> Run:
    return (
        await service.advance(
            run.id,
            run.version,
            command,
            context,
            audit_context=_audit_context(f"orch-07.advance.{run.id}.{run.version}.{command.value}"),
        )
    ).run


@pytest.mark.asyncio
async def test_orch_07_atomic_receipt_replays_original_after_terminal_and_catalog_release(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "replay.db")
    dependencies = _dependencies(runtime)
    service_a = IdempotentWorkRunReceiptService(
        dependencies,
        _key(),
        current_catalog_hash=TEST_CATALOG_HASH,
    )
    marker_a = validate_incoming_for_test(_envelope())
    try:
        created = await service_a.receive(
            marker_a,
            audit_context=_audit_context("orch-07.replay.create"),
        )
        lifecycle = RunLifecycleService(dependencies)
        run = await _advance(
            lifecycle,
            created.run,
            RunLifecycleCommand.MARK_VALIDATED,
            NoRunTransitionContext(),
        )
        plan, graph, routing = build_read_only_plan(
            run_id=run.id,
            workflow_id=created.work_item.workflow_id,
            target_instance_id=created.work_item.instance_id,
            configuration_revision=created.work_item.configuration_revision,
            catalog_hash=run.catalog_hash,
        )
        persisted = await AuditedPlanPersistenceService(dependencies).persist(
            plan,
            graph,
            routing,
            expected_run_version=run.version,
            audit_context=_audit_context("orch-07.persist-plan"),
        )
        run = persisted.run
        run = await _advance(
            lifecycle,
            run,
            RunLifecycleCommand.ACTIVATE_PLAN,
            NoRunTransitionContext(),
        )
        step_service = RunStepLifecycleService(dependencies)
        step = persisted.steps[0]
        for command in (
            StepLifecycleCommand.MARK_READY,
            StepLifecycleCommand.START,
            StepLifecycleCommand.SUCCEED,
        ):
            step = (
                await step_service.advance(
                    step.id,
                    step.version,
                    command,
                    NoStepTransitionContext(),
                    audit_context=_audit_context(
                        f"orch-07.step.{step.id}.{step.version}.{command.value}"
                    ),
                )
            ).step
        terminal = await _advance(
            lifecycle,
            run,
            RunLifecycleCommand.COMPLETE,
            CompletionContext(1, 1, 0, 0),
        )

        replayed_terminal = await service_a.receive(
            marker_a,
            audit_context=_audit_context("orch-07.replay.terminal"),
        )
        service_b = _service(runtime, catalog_hash=CATALOG_B, ids=IncrementingIds(100))
        marker_b = validate_incoming_for_test(_envelope(), catalog_hash=CATALOG_B)
        replayed_release = await service_b.receive(
            marker_b,
            audit_context=_audit_context("orch-07.replay.catalog-release"),
        )

        assert created.disposition is WorkRunReceiptDisposition.CREATED
        assert created.initial_transition is not None
        assert replayed_terminal.disposition is WorkRunReceiptDisposition.REPLAYED
        assert replayed_terminal.initial_transition is None
        assert replayed_terminal.run == terminal
        assert replayed_release.disposition is WorkRunReceiptDisposition.REPLAYED
        assert replayed_release.work_item.id == created.work_item.id
        assert replayed_release.run.id == created.run.id
        assert replayed_release.run.catalog_hash == TEST_CATALOG_HASH
        assert (
            replayed_release.run.configuration_revision == created.work_item.configuration_revision
        )
        assert await _counts(runtime) == (1, 1, 5)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_07_faults_and_caller_rollback_never_leave_partial_receipt(
    tmp_path: Path,
) -> None:
    for label, after_flush in (("before-run", False), ("after-run-flush", True)):
        runtime = await _runtime(tmp_path / f"{label}.db")

        def run_factory(session: AsyncSession, *, mode: bool = after_flush) -> RunRepository:
            return FaultingRunRepository(SQLAlchemyRunRepository(session), after_flush=mode)

        try:
            with pytest.raises(RuntimeError, match="injected fault"):
                await _service(runtime, run_factory=run_factory).receive(
                    validate_incoming_for_test(_envelope(event_id=f"event.{label}")),
                    audit_context=_audit_context(f"orch-07.fault.{label}"),
                )
            assert await _counts(runtime) == (0, 0, 0)
        finally:
            await runtime.dispose()

    rollback_runtime = await _runtime(tmp_path / "caller-rollback.db")
    service = _service(rollback_runtime)
    try:
        async with _uow_factory(rollback_runtime)() as unit_of_work:
            result = await service.receive_in_uow(
                unit_of_work,
                validate_incoming_for_test(_envelope(event_id="event.caller-rollback")),
                audit_context=_audit_context("orch-07.caller-rollback"),
            )
            assert result.disposition is WorkRunReceiptDisposition.CREATED
        assert await _counts(rollback_runtime) == (0, 0, 0)
    finally:
        await rollback_runtime.dispose()


@pytest.mark.asyncio
async def test_orch_07_orphan_work_and_mixed_or_misdirected_runs_fail_closed(
    tmp_path: Path,
) -> None:
    orphan_runtime = await _runtime(tmp_path / "orphan-work.db")
    dependencies = _dependencies(orphan_runtime)
    work_service = WorkAdmissionService(dependencies, _key())
    marker = validate_incoming_for_test(_envelope(event_id="event.orphan-work"))
    try:
        orphan = await work_service.admit(marker)
        with pytest.raises(WorkRunReceiptError) as mixed:
            await IdempotentWorkRunReceiptService(
                dependencies,
                _key(),
                current_catalog_hash=TEST_CATALOG_HASH,
            ).receive(
                marker,
                audit_context=_audit_context("orch-07.orphan-work"),
            )
        assert mixed.value.code == "mixed_receipt_disposition"
        assert mixed.value.work_item_id == orphan.work_item.id
        assert await _counts(orphan_runtime) == (1, 0, 0)
    finally:
        await orphan_runtime.dispose()

    mixed_runtime = await _runtime(tmp_path / "mixed-run.db")
    existing = await _service(mixed_runtime).receive(
        validate_incoming_for_test(_envelope(event_id="event.existing")),
        audit_context=_audit_context("orch-07.mixed.existing"),
    )

    def existing_run_factory(session: AsyncSession) -> RunRepository:
        return ExistingRunRepository(SQLAlchemyRunRepository(session), existing.run.id)

    try:
        with pytest.raises(WorkRunReceiptError) as mixed:
            await _service(
                mixed_runtime,
                ids=IncrementingIds(100),
                run_factory=existing_run_factory,
            ).receive(
                validate_incoming_for_test(_envelope(event_id="event.new-work")),
                audit_context=_audit_context("orch-07.mixed.new-work"),
            )
        assert mixed.value.code == "mixed_receipt_disposition"
        assert await _counts(mixed_runtime) == (1, 1, 1)
    finally:
        await mixed_runtime.dispose()

    wrong_runtime = await _runtime(tmp_path / "wrong-run.db")
    first = await _service(wrong_runtime).receive(
        validate_incoming_for_test(_envelope(event_id="event.first")),
        audit_context=_audit_context("orch-07.wrong.first"),
    )
    second = await _service(wrong_runtime, ids=IncrementingIds(100)).receive(
        validate_incoming_for_test(_envelope(event_id="event.second")),
        audit_context=_audit_context("orch-07.wrong.second"),
    )

    def wrong_run_factory(session: AsyncSession) -> RunRepository:
        return ExistingRunRepository(SQLAlchemyRunRepository(session), second.run.id)

    try:
        with pytest.raises(WorkRunReceiptError) as mismatched:
            await _service(wrong_runtime, run_factory=wrong_run_factory).receive(
                validate_incoming_for_test(_envelope(event_id="event.first")),
                audit_context=_audit_context("orch-07.wrong.replay"),
            )
        assert mismatched.value.code == "receipt_correlation_mismatch"
        assert mismatched.value.work_item_id == first.work_item.id
        assert await _counts(wrong_runtime) == (2, 2, 2)
    finally:
        await wrong_runtime.dispose()


@pytest.mark.asyncio
async def test_orch_07_created_run_must_retain_marker_catalog_and_fk_blocks_orphan_run(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "catalog-correlation.db")

    def wrong_catalog_factory(session: AsyncSession) -> RunRepository:
        return WrongCatalogRunRepository(SQLAlchemyRunRepository(session))

    try:
        with pytest.raises(WorkRunReceiptError) as mismatched:
            await _service(runtime, run_factory=wrong_catalog_factory).receive(
                validate_incoming_for_test(_envelope()),
                audit_context=_audit_context("orch-07.wrong-catalog"),
            )
        assert mismatched.value.code == "receipt_correlation_mismatch"
        assert await _counts(runtime) == (0, 0, 0)

        async with runtime.session_factory() as session:
            session.add(
                RunRecord(
                    id="run.orphan",
                    work_item_id="work.missing",
                    state="received",
                    catalog_hash=TEST_CATALOG_HASH,
                    configuration_revision=1,
                    approval_required=None,
                    terminal_reason_code=None,
                    created_at=NOW,
                    updated_at=NOW,
                    version=1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
        assert await _counts(runtime) == (0, 0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_07_identical_and_changed_two_session_races_are_pair_atomic(
    tmp_path: Path,
) -> None:
    identical_runtime = await _runtime(tmp_path / "identical-race.db")
    barrier = AsyncBarrier(2)

    def work_factory(session: AsyncSession) -> WorkRepository:
        return BarrierWorkRepository(SQLAlchemyWorkRepository(session), barrier)

    first_ids = IncrementingIds()
    second_ids = IncrementingIds(100)
    try:
        results = await asyncio.gather(
            _service(
                identical_runtime,
                ids=first_ids,
                work_factory=work_factory,
            ).receive(
                validate_incoming_for_test(_envelope()),
                audit_context=_audit_context("orch-07.identical-race.first"),
            ),
            _service(
                identical_runtime,
                ids=second_ids,
                work_factory=work_factory,
            ).receive(
                validate_incoming_for_test(_envelope()),
                audit_context=_audit_context("orch-07.identical-race.second"),
            ),
        )
        assert {item.disposition for item in results} == {
            WorkRunReceiptDisposition.CREATED,
            WorkRunReceiptDisposition.REPLAYED,
        }
        assert len({item.work_item.id for item in results}) == 1
        assert len({item.run.id for item in results}) == 1
        assert {item.run.catalog_hash for item in results} == {TEST_CATALOG_HASH}
        assert await _counts(identical_runtime) == (1, 1, 1)
    finally:
        await identical_runtime.dispose()

    changed_runtime = await _runtime(tmp_path / "changed-race.db")
    changed_barrier = AsyncBarrier(2)

    def changed_work_factory(session: AsyncSession) -> WorkRepository:
        return BarrierWorkRepository(SQLAlchemyWorkRepository(session), changed_barrier)

    try:
        outcomes = await asyncio.gather(
            _service(changed_runtime, work_factory=changed_work_factory).receive(
                validate_incoming_for_test(_envelope(payload={"choice": "first"})),
                audit_context=_audit_context("orch-07.changed-race.first"),
            ),
            _service(
                changed_runtime,
                ids=IncrementingIds(100),
                work_factory=changed_work_factory,
            ).receive(
                validate_incoming_for_test(_envelope(payload={"choice": "second"})),
                audit_context=_audit_context("orch-07.changed-race.second"),
            ),
            return_exceptions=True,
        )
        created = [item for item in outcomes if not isinstance(item, BaseException)]
        conflicts = [item for item in outcomes if isinstance(item, WorkIdempotencyError)]
        assert len(created) == 1
        assert len(conflicts) == 1
        assert conflicts[0].code == "idempotency_conflict"
        assert await _counts(changed_runtime) == (1, 1, 1)
    finally:
        await changed_runtime.dispose()


@pytest.mark.asyncio
async def test_orch_07_unsealed_tampered_or_stale_marker_fails_before_authority_use() -> None:
    clock = FixedClock()
    ids = IncrementingIds()
    unit_of_work_factory = ProbeUnitOfWorkFactory()
    service = IdempotentWorkRunReceiptService(
        OrchestrationDependencies(clock, ids, unit_of_work_factory),
        _key(),
        current_catalog_hash=CATALOG_B,
    )
    envelope = _envelope(event_id="event.pre-uow")
    audit_context = _audit_context("orch-07.pre-uow")

    with pytest.raises(IncomingWorkValidationError) as raw:
        await service.receive(
            cast(ValidatedIncomingWork, envelope),
            audit_context=audit_context,
        )
    assert raw.value.code == "incoming_work_not_validated"

    stale = validate_incoming_for_test(envelope)
    with pytest.raises(IncomingWorkValidationError) as drift:
        await service.receive(stale, audit_context=audit_context)
    assert drift.value.code == "catalog_drift"

    tampered = validate_incoming_for_test(envelope, catalog_hash=CATALOG_B)
    object.__setattr__(
        tampered,
        "snapshot",
        replace(tampered.snapshot, workflow_id="workflow.forged"),
    )
    with pytest.raises(IncomingWorkValidationError) as invalid:
        await service.receive(tampered, audit_context=audit_context)
    assert invalid.value.code == "incoming_work_not_validated"

    catalog_tampered = validate_incoming_for_test(envelope)
    object.__setattr__(
        catalog_tampered,
        "snapshot",
        replace(catalog_tampered.snapshot, catalog_hash=CATALOG_B),
    )
    with pytest.raises(IncomingWorkValidationError) as invalid_catalog:
        await service.receive(catalog_tampered, audit_context=audit_context)
    assert invalid_catalog.value.code == "incoming_work_not_validated"
    assert unit_of_work_factory.calls == 0
    assert ids.generated == []
    assert clock.calls == 0
