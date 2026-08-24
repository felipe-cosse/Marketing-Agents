"""ORCH-09: atomic persisted plan/step state and fail-closed Run timelines."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from marketing_agents.application.orchestration import (
    EffectPlan,
    OrchestrationDependencies,
    RoutingResult,
)
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.repositories import AuditRepository, RunRepository
from marketing_agents.application.services import (
    AuditedPlanPersistenceService,
    ControlledReadCommand,
    ControlledReadExecutor,
    ControlledReadExecutorError,
    ExecutionActivationService,
    IdempotentWorkRunReceiptService,
    PersistedRunPlan,
    PlanPersistenceError,
    RunAdvanceDisposition,
    RunCancellationService,
    RunLifecycleService,
    RunLifecycleServiceError,
    RunStepLifecycleService,
    RunStepLifecycleServiceError,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext, AuditOutcome
from marketing_agents.domain.entities import Run
from marketing_agents.domain.enums import RunState, StepState, WorkMode
from marketing_agents.domain.graph import DependencyGraph
from marketing_agents.domain.run_lifecycle import (
    CompletionContext,
    NoRunTransitionContext,
    RunLifecycleCommand,
    RunTransitionError,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
)
from marketing_agents.infrastructure.db import (
    AuditPersistenceInvariantError,
    Base,
    DatabaseRuntime,
    SQLAlchemyAuditRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyUnitOfWorkFactory,
    StepPersistenceConflict,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models import (
    AuditEventRecord,
    RunPlanRecord,
    RunRecord,
    RunStepDependencyRecord,
    RunStepRecord,
    RunStepStateTransitionRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.execution_control import execution_control_repository
from tests.support.incoming_work import TEST_CATALOG_HASH, validate_incoming_for_test
from tests.support.orch_09_planning import build_read_only_plan
from tests.support.read_adapter import ExactReadContractAdapter, observation_for

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


class IncrementingClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class IncrementingIds:
    def __init__(self, seed: int = 0) -> None:
        self._next = seed

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.orch-09.{self._next:04d}"


class SuccessfulReadAdapter(ExactReadContractAdapter):
    def __init__(self) -> None:
        self.calls: list[ReadAdapterRequest] = []

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        return observation_for(request, {"attempt_id": request.attempt_id, "completed": True})


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

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def apply_transition(self, **kwargs: object) -> bool:
        await self._barrier.wait()
        return await self._delegate.apply_transition(**kwargs)  # type: ignore[arg-type]


class FaultAfterAuditAppend:
    def __init__(self, delegate: AuditRepository) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def append(self, event):  # type: ignore[no-untyped-def]
        await self._delegate.append(event)
        raise RuntimeError("injected fault after audit flush")

    async def append_many(self, events):  # type: ignore[no-untyped-def]
        await self._delegate.append_many(events)
        raise RuntimeError("injected fault after audit flush")


def _audit_context(label: str) -> AuditContext:
    return AuditContext.system("test.orch-09", correlation_id=f"request.{label}")


def _key() -> DigestKey:
    return DigestKey(bytes(range(32)))


def _envelope(event_id: str) -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id=event_id,
        instance_id="instance.orch-09.target",
        trigger_id="trigger.orch-09.manual",
        workflow_id="workflow.orch-09.audit",
        mode=WorkMode.MOCK_EXECUTION,
        brief_id=None,
        brief_revision=None,
        configuration_revision=1,
        admitted_payload={"safe": True},
    )


def _uow_factory(
    runtime: DatabaseRuntime,
    *,
    run_factory: Callable[[AsyncSession], RunRepository] = SQLAlchemyRunRepository,
    audit_factory: Callable[[AsyncSession], AuditRepository] = SQLAlchemyAuditRepository,
) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=run_factory,
            audits=audit_factory,
            run_steps=SQLAlchemyRunStepRepository,
            execution_control=execution_control_repository,
        ),
    )


def _dependencies(
    runtime: DatabaseRuntime,
    *,
    clock: IncrementingClock | None = None,
    ids: IncrementingIds | None = None,
    run_factory: Callable[[AsyncSession], RunRepository] = SQLAlchemyRunRepository,
    audit_factory: Callable[[AsyncSession], AuditRepository] = SQLAlchemyAuditRepository,
) -> OrchestrationDependencies:
    return OrchestrationDependencies(
        clock or IncrementingClock(),
        ids or IncrementingIds(),
        _uow_factory(runtime, run_factory=run_factory, audit_factory=audit_factory),
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


async def _validated_run(
    dependencies: OrchestrationDependencies,
    event_id: str,
) -> tuple[Run, AdmissionEnvelope]:
    envelope = _envelope(event_id)
    received = await IdempotentWorkRunReceiptService(
        dependencies,
        _key(),
        current_catalog_hash=TEST_CATALOG_HASH,
    ).receive(
        validate_incoming_for_test(envelope),
        audit_context=_audit_context(f"{event_id}.receive"),
    )
    validated = await RunLifecycleService(dependencies).advance(
        received.run.id,
        received.run.version,
        RunLifecycleCommand.MARK_VALIDATED,
        NoRunTransitionContext(),
        audit_context=_audit_context(f"{event_id}.validate"),
    )
    return validated.run, envelope


async def _persist_plan(
    dependencies: OrchestrationDependencies,
    run: Run,
    envelope: AdmissionEnvelope,
    *,
    dependent_steps: bool = False,
    parallel_steps: bool = False,
    context_label: str = "plan",
) -> tuple[PersistedRunPlan, EffectPlan, DependencyGraph, RoutingResult]:
    plan, graph, routing = build_read_only_plan(
        run_id=run.id,
        workflow_id=envelope.workflow_id,
        target_instance_id=envelope.instance_id,
        configuration_revision=envelope.configuration_revision,
        catalog_hash=run.catalog_hash,
        dependent_steps=dependent_steps,
        parallel_steps=parallel_steps,
    )
    persisted = await AuditedPlanPersistenceService(dependencies).persist(
        plan,
        graph,
        routing,
        expected_run_version=run.version,
        audit_context=_audit_context(context_label),
    )
    return persisted, plan, graph, routing


async def _timeline(dependencies: OrchestrationDependencies, run_id: str):  # type: ignore[no-untyped-def]
    async with dependencies.unit_of_work() as unit_of_work:
        return await unit_of_work.audits.list_run(run_id, limit=100)


@pytest.mark.asyncio
async def test_orch_09_restart_stable_complete_plan_step_timeline(tmp_path: Path) -> None:
    path = tmp_path / "complete-timeline.db"
    runtime = await _runtime(path)
    dependencies = _dependencies(runtime)
    run, envelope = await _validated_run(dependencies, "event.orch-09.complete")
    persisted, plan, graph, routing = await _persist_plan(dependencies, run, envelope)
    lifecycle = RunLifecycleService(dependencies)
    activated = await ExecutionActivationService(dependencies).activate(
        persisted.run.id,
        audit_context=_audit_context("complete.activate"),
    )
    running = activated.run
    executed = await ControlledReadExecutor(
        dependencies,
        SuccessfulReadAdapter(),
    ).execute(
        ControlledReadCommand(activated.steps[0].id, {"input": "complete"}),
        audit_context=_audit_context("complete.step.execute"),
    )
    step = executed.step
    completed = await lifecycle.advance(
        running.id,
        running.version,
        RunLifecycleCommand.COMPLETE,
        CompletionContext(1, 1, 0, 0),
        audit_context=_audit_context("complete.run"),
    )
    assert completed.run.state is RunState.COMPLETED
    assert step.state is StepState.SUCCEEDED
    timeline = await _timeline(dependencies, run.id)
    assert tuple(item.run_sequence for item in timeline) == tuple(range(1, 10))
    assert tuple(item.event_type for item in timeline) == (
        "run.received",
        "run.transitioned",
        "run.plan_recorded",
        "step.recorded",
        "run.transitioned",
        "step.transitioned",
        "step.transitioned",
        "step.transitioned",
        "run.transitioned",
    )
    await runtime.dispose()

    restarted = await _runtime(path)
    restarted_dependencies = _dependencies(restarted, ids=IncrementingIds(100))
    try:
        async with restarted_dependencies.unit_of_work() as unit_of_work:
            first = await unit_of_work.audits.list_run(run.id, limit=4)
            second = await unit_of_work.audits.list_run(run.id, after_sequence=4, limit=4)
            third = await unit_of_work.audits.list_run(run.id, after_sequence=8, limit=4)
        assert tuple(item.run_sequence for item in (*first, *second, *third)) == tuple(range(1, 10))
        replayed = await AuditedPlanPersistenceService(restarted_dependencies).persist(
            plan,
            graph,
            routing,
            expected_run_version=2,
            audit_context=_audit_context("complete.replay.new-context"),
        )
        assert replayed.created is False
        assert replayed.run.state is RunState.COMPLETED
        assert len(await _timeline(restarted_dependencies, run.id)) == 9
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_orch_09_plan_and_audit_fault_roll_back_every_row(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "plan-rollback.db")
    normal = _dependencies(runtime)
    run, envelope = await _validated_run(normal, "event.orch-09.rollback")

    def faulting_audits(session: AsyncSession) -> AuditRepository:
        return FaultAfterAuditAppend(SQLAlchemyAuditRepository(session))  # type: ignore[return-value]

    faulting = _dependencies(
        runtime,
        clock=IncrementingClock(run.updated_at + timedelta(seconds=1)),
        audit_factory=faulting_audits,
    )
    plan, graph, routing = build_read_only_plan(
        run_id=run.id,
        workflow_id=envelope.workflow_id,
        target_instance_id=envelope.instance_id,
        configuration_revision=envelope.configuration_revision,
        catalog_hash=run.catalog_hash,
    )
    try:
        with pytest.raises(RuntimeError, match="injected fault"):
            await AuditedPlanPersistenceService(faulting).persist(
                plan,
                graph,
                routing,
                expected_run_version=run.version,
                audit_context=_audit_context("rollback.plan"),
            )
        async with normal.unit_of_work() as unit_of_work:
            stored = await unit_of_work.runs.get(run.id)
            assert stored is not None and stored.state is RunState.VALIDATED
            assert await unit_of_work.run_steps.get_plan(run.id) is None
            assert await unit_of_work.run_steps.list_for_run(run.id) == ()
            assert len(await unit_of_work.audits.list_run(run.id)) == 2
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_single_audit_append_fault_rolls_back_run_and_step_mutations(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "ordinary-mutation-rollback.db")
    normal = _dependencies(runtime)
    run, envelope = await _validated_run(normal, "event.orch-09.mutation-rollback")
    persisted, _, _, _ = await _persist_plan(normal, run, envelope)
    before = await _timeline(normal, run.id)

    def faulting_audits(session: AsyncSession) -> AuditRepository:
        return FaultAfterAuditAppend(SQLAlchemyAuditRepository(session))  # type: ignore[return-value]

    faulting_activation = _dependencies(
        runtime,
        clock=IncrementingClock(persisted.run.updated_at + timedelta(seconds=1)),
        audit_factory=faulting_audits,
    )
    step = persisted.steps[0]
    try:
        with pytest.raises(RuntimeError, match="injected fault"):
            await ExecutionActivationService(faulting_activation).activate(
                persisted.run.id,
                audit_context=_audit_context("mutation-rollback.activate-fault"),
            )

        async with normal.unit_of_work() as unit_of_work:
            rolled_back_run = await unit_of_work.runs.get(run.id)
            rolled_back_step = await unit_of_work.run_steps.get(step.id)
            rolled_back_history = await unit_of_work.run_steps.list_transitions(step.id)
            rolled_back_timeline = await unit_of_work.audits.list_run(run.id)
        assert rolled_back_run == persisted.run
        assert rolled_back_step == step
        assert len(rolled_back_history) == 1
        assert rolled_back_timeline == before

        activated = await ExecutionActivationService(normal).activate(
            persisted.run.id,
            audit_context=_audit_context("mutation-rollback.activate"),
        )
        executing = activated.run
        ready_step = activated.steps[0]
        before_cancel = await _timeline(normal, run.id)
        faulting_cancel = _dependencies(
            runtime,
            clock=IncrementingClock(executing.updated_at + timedelta(seconds=1)),
            audit_factory=faulting_audits,
        )
        with pytest.raises(RuntimeError, match="injected fault"):
            await RunCancellationService(faulting_cancel).request(
                executing.id,
                audit_context=_audit_context("mutation-rollback.run"),
            )

        async with normal.unit_of_work() as unit_of_work:
            stored_run = await unit_of_work.runs.get(run.id)
            stored_step = await unit_of_work.run_steps.get(step.id)
            step_history = await unit_of_work.run_steps.list_transitions(step.id)
            timeline = await unit_of_work.audits.list_run(run.id)
        assert stored_run is not None and stored_run.state is RunState.EXECUTING
        assert stored_run.version == executing.version
        assert stored_step == ready_step
        assert len(step_history) == 2
        assert timeline == before_cancel
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_completion_uses_persisted_steps_and_audits_rejection(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "completion-barrier.db")
    dependencies = _dependencies(runtime)
    run, envelope = await _validated_run(dependencies, "event.orch-09.incomplete")
    persisted, _, _, _ = await _persist_plan(dependencies, run, envelope)
    activated = await ExecutionActivationService(dependencies).activate(
        persisted.run.id,
        audit_context=_audit_context("incomplete.activate"),
    )
    running = activated.run
    try:
        with pytest.raises(RunTransitionError) as captured:
            await RunLifecycleService(dependencies).advance(
                running.id,
                running.version,
                RunLifecycleCommand.COMPLETE,
                CompletionContext(1, 1, 0, 0),
                audit_context=_audit_context("incomplete.forged-counts"),
            )
        assert captured.value.code == "execution_incomplete"
        async with dependencies.unit_of_work() as unit_of_work:
            current = await unit_of_work.runs.get(run.id)
            assert current is not None and current.state is RunState.EXECUTING
            assert (
                await unit_of_work.run_steps.get(persisted.steps[0].id)
            ).state is StepState.READY  # type: ignore[union-attr]
            timeline = await unit_of_work.audits.list_run(run.id)
        assert timeline[-1].event_type == "run.transition_rejected"
        assert timeline[-1].outcome is AuditOutcome.REJECTED
        assert timeline[-1].aggregate_id == timeline[-1].attempt_id
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_missing_dependency_row_cannot_turn_child_into_root(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "dependency-corruption.db")
    dependencies = _dependencies(runtime)
    run, envelope = await _validated_run(dependencies, "event.orch-09.dependency")
    persisted, _, _, _ = await _persist_plan(
        dependencies,
        run,
        envelope,
        dependent_steps=True,
    )
    running = await ExecutionActivationService(dependencies).activate(
        persisted.run.id,
        audit_context=_audit_context("dependency.activate"),
    )
    assert running.run.state is RunState.EXECUTING
    child = persisted.steps[1]
    before = len(await _timeline(dependencies, run.id))
    async with runtime.session_factory() as session, session.begin():
        await session.execute(
            delete(RunStepDependencyRecord).where(
                RunStepDependencyRecord.step_id == child.id,
                RunStepDependencyRecord.dependency_key == "read",
            )
        )
    try:
        with pytest.raises(RunStepLifecycleServiceError) as captured:
            await RunStepLifecycleService(dependencies).advance(
                child.id,
                child.version,
                StepLifecycleCommand.MARK_READY,
                NoStepTransitionContext(),
                audit_context=_audit_context("dependency.corrupt-ready"),
            )
        assert captured.value.code == "step_plan_snapshot_invalid"
        async with dependencies.unit_of_work() as unit_of_work:
            stored = await unit_of_work.run_steps.get(child.id)
            assert stored is not None and stored.state is StepState.PENDING
            assert len(await unit_of_work.audits.list_run(run.id)) == before
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("column", "forged_value"),
    [
        ("capability_id", "cap.model.forged"),
        ("request_schema_id", "schema.model.forged"),
        ("approval_policy_id", "approval.forged"),
    ],
)
@pytest.mark.asyncio
async def test_orch_09_structural_step_tamper_blocks_activation_without_healing(
    tmp_path: Path,
    column: str,
    forged_value: str,
) -> None:
    runtime = await _runtime(tmp_path / f"plan-tamper-{column}.db")
    dependencies = _dependencies(runtime)
    run, envelope = await _validated_run(dependencies, f"event.orch-09.tamper.{column}")
    persisted, _, _, _ = await _persist_plan(dependencies, run, envelope)
    step = persisted.steps[0]
    before = await _timeline(dependencies, run.id)
    async with runtime.session_factory() as session, session.begin():
        await session.execute(
            update(RunStepRecord).where(RunStepRecord.id == step.id).values({column: forged_value})
        )
    try:
        with pytest.raises(StepPersistenceConflict) as captured:
            await ExecutionActivationService(dependencies).activate(
                persisted.run.id,
                audit_context=_audit_context(f"tamper.{column}.activate"),
            )
        assert captured.value.code == "plan_snapshot_corrupt"
        async with dependencies.unit_of_work() as unit_of_work:
            stored_run = await unit_of_work.runs.get(run.id)
            stored_step = await unit_of_work.run_steps.get(step.id)
            timeline = await unit_of_work.audits.list_run(run.id)
        assert stored_run is not None and stored_run == persisted.run
        assert stored_step is not None and stored_step.state is StepState.PENDING
        assert stored_step.version == step.version
        assert timeline == before
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_structural_tamper_after_ready_blocks_start_without_healing(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "plan-tamper-after-ready.db")
    dependencies = _dependencies(runtime)
    run, envelope = await _validated_run(dependencies, "event.orch-09.tamper.after-ready")
    persisted, _, _, _ = await _persist_plan(dependencies, run, envelope)
    activated = await ExecutionActivationService(dependencies).activate(
        persisted.run.id,
        audit_context=_audit_context("tamper.after-ready.activate"),
    )
    executing = activated.run
    step = activated.steps[0]
    before = await _timeline(dependencies, run.id)
    async with runtime.session_factory() as session, session.begin():
        await session.execute(
            update(RunStepRecord)
            .where(RunStepRecord.id == step.id)
            .values(capability_id="cap.model.forged-after-ready")
        )
    try:
        adapter = SuccessfulReadAdapter()
        with pytest.raises(ControlledReadExecutorError) as captured:
            await ControlledReadExecutor(dependencies, adapter).execute(
                ControlledReadCommand(step.id, {"input": "tampered"}),
                audit_context=_audit_context("tamper.after-ready.execute"),
            )
        assert captured.value.code == "execution_policy_invalid"
        assert adapter.calls == []
        async with dependencies.unit_of_work() as unit_of_work:
            stored_run = await unit_of_work.runs.get(run.id)
            stored_step = await unit_of_work.run_steps.get(step.id)
            timeline = await unit_of_work.audits.list_run(run.id)
            attempts = await unit_of_work.execution_control.list_attempts(
                step.id,
                step.runtime_policy.operation_key,
            )
            control = await unit_of_work.execution_control.get(run.id)
        assert stored_run is not None and stored_run == executing
        assert stored_step is not None and stored_step.state is StepState.READY
        assert stored_step.version == step.version
        assert attempts == ()
        assert control is not None and control.model_calls == 0
        assert timeline == before
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_same_run_plan_race_has_one_complete_canonical_winner(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "plan-race.db")
    normal = _dependencies(runtime)
    run, envelope = await _validated_run(normal, "event.orch-09.plan-race")
    plan, graph, routing = build_read_only_plan(
        run_id=run.id,
        workflow_id=envelope.workflow_id,
        target_instance_id=envelope.instance_id,
        configuration_revision=envelope.configuration_revision,
        catalog_hash=run.catalog_hash,
    )
    barrier = AsyncBarrier(2)

    def barrier_runs(session: AsyncSession) -> RunRepository:
        return BarrierRunRepository(SQLAlchemyRunRepository(session), barrier)  # type: ignore[return-value]

    first = AuditedPlanPersistenceService(
        _dependencies(
            runtime,
            clock=IncrementingClock(run.updated_at + timedelta(seconds=1)),
            run_factory=barrier_runs,
        )
    )
    second = AuditedPlanPersistenceService(
        _dependencies(
            runtime,
            clock=IncrementingClock(run.updated_at + timedelta(seconds=1)),
            run_factory=barrier_runs,
        )
    )
    results = await asyncio.gather(
        first.persist(
            plan,
            graph,
            routing,
            expected_run_version=run.version,
            audit_context=_audit_context("plan-race.first"),
        ),
        second.persist(
            plan,
            graph,
            routing,
            expected_run_version=run.version,
            audit_context=_audit_context("plan-race.second"),
        ),
        return_exceptions=True,
    )
    try:
        assert sum(not isinstance(item, BaseException) for item in results) == 1
        loser = next(item for item in results if isinstance(item, BaseException))
        assert isinstance(loser, PlanPersistenceError)
        assert loser.code == "stale_run_version"
        replayed = await AuditedPlanPersistenceService(normal).persist(
            plan,
            graph,
            routing,
            expected_run_version=run.version,
            audit_context=_audit_context("plan-race.replay"),
        )
        assert replayed.created is False
        async with normal.unit_of_work() as unit_of_work:
            assert len(await unit_of_work.run_steps.list_for_run(run.id)) == 1
            assert len(await unit_of_work.audits.list_run(run.id)) == 4
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_parallel_steps_allocate_contiguous_same_run_audit_sequences(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "parallel-step-sequences.db")
    dependencies = _dependencies(runtime)
    run, envelope = await _validated_run(dependencies, "event.orch-09.parallel-steps")
    persisted, _, _, _ = await _persist_plan(
        dependencies,
        run,
        envelope,
        parallel_steps=True,
    )
    activated = await ExecutionActivationService(dependencies).activate(
        persisted.run.id,
        audit_context=_audit_context("parallel-steps.activate"),
    )
    try:
        executing = activated.run
        assert all(step.state is StepState.READY for step in activated.steps)
        timeline = await _timeline(dependencies, run.id)
        assert tuple(item.run_sequence for item in timeline) == tuple(range(1, 9))
        assert tuple(item.event_type for item in timeline[-2:]) == (
            "step.transitioned",
            "step.transitioned",
        )
        assert {item.step_id for item in timeline[-2:]} == {step.id for step in activated.steps}
        async with dependencies.unit_of_work() as unit_of_work:
            stored_run = await unit_of_work.runs.get(run.id)
        assert stored_run is not None and stored_run == executing
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_identical_structural_plan_hash_is_scoped_per_run(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "two-runs.db")
    first_dependencies = _dependencies(runtime, ids=IncrementingIds(0))
    second_dependencies = _dependencies(runtime, ids=IncrementingIds(100))
    first_run, first_envelope = await _validated_run(
        first_dependencies, "event.orch-09.same-plan.first"
    )
    second_run, second_envelope = await _validated_run(
        second_dependencies, "event.orch-09.same-plan.second"
    )
    first_plan = build_read_only_plan(
        run_id=first_run.id,
        workflow_id=first_envelope.workflow_id,
        target_instance_id=first_envelope.instance_id,
        configuration_revision=first_envelope.configuration_revision,
        catalog_hash=first_run.catalog_hash,
    )
    second_plan = build_read_only_plan(
        run_id=second_run.id,
        workflow_id=second_envelope.workflow_id,
        target_instance_id=second_envelope.instance_id,
        configuration_revision=second_envelope.configuration_revision,
        catalog_hash=second_run.catalog_hash,
    )
    assert first_plan[0].plan_hash == second_plan[0].plan_hash
    try:
        await AuditedPlanPersistenceService(first_dependencies).persist(
            *first_plan,
            expected_run_version=first_run.version,
            audit_context=_audit_context("same-plan.first"),
        )
        await AuditedPlanPersistenceService(second_dependencies).persist(
            *second_plan,
            expected_run_version=second_run.version,
            audit_context=_audit_context("same-plan.second"),
        )
        async with runtime.session_factory() as session:
            assert (
                int((await session.execute(select(func.count(RunPlanRecord.run_id)))).scalar_one())
                == 2
            )
            assert (
                int((await session.execute(select(func.count(RunStepRecord.id)))).scalar_one()) == 2
            )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_caller_owned_rejection_is_typed_and_committable(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "caller-rejection.db")
    dependencies = _dependencies(runtime)
    run, _ = await _validated_run(dependencies, "event.orch-09.caller-reject")
    lifecycle = RunLifecycleService(dependencies)
    try:
        async with dependencies.unit_of_work() as unit_of_work:
            attempt = await lifecycle.attempt_advance_in_uow(
                unit_of_work,
                run.id,
                run.version,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
                audit_context=_audit_context("caller-reject.invalid"),
            )
            assert attempt.disposition is RunAdvanceDisposition.REJECTED
            assert isinstance(attempt.error, RunTransitionError)
            await unit_of_work.commit()
        timeline = await _timeline(dependencies, run.id)
        assert timeline[-1].event_type == "run.transition_rejected"
        assert timeline[-1].reason_code == "invalid_transition"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_rejected_attempt_identity_replays_without_consuming_sequence(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "rejected-attempts.db")
    dependencies = _dependencies(runtime)
    run, _ = await _validated_run(dependencies, "event.orch-09.reject-replay")
    lifecycle = RunLifecycleService(dependencies)

    async def reject(label: str, expected_version: int) -> None:
        with pytest.raises(RunTransitionError):
            await lifecycle.advance(
                run.id,
                expected_version,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
                audit_context=_audit_context(label),
            )

    try:
        await reject("reject-replay.first", run.version)
        await reject("reject-replay.second", run.version)
        timeline = await _timeline(dependencies, run.id)
        rejections = tuple(
            item for item in timeline if item.event_type == "run.transition_rejected"
        )
        assert len(rejections) == 2
        assert len({item.attempt_id for item in rejections}) == 2
        assert {item.observed_version for item in rejections} == {run.version}

        await reject("reject-replay.first", run.version)
        assert len(await _timeline(dependencies, run.id)) == len(timeline)

        with pytest.raises(RunLifecycleServiceError) as collision:
            await lifecycle.advance(
                run.id,
                run.version + 10,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
                audit_context=_audit_context("reject-replay.first"),
            )
        assert getattr(collision.value, "code", None) == "audit_attempt_identity_conflict"
        assert len(await _timeline(dependencies, run.id)) == len(timeline)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_timeline_gap_rejects_reads_and_cannot_be_extended(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "timeline-gap.db")
    dependencies = _dependencies(runtime)
    run, _ = await _validated_run(dependencies, "event.orch-09.timeline-gap")
    async with runtime.session_factory() as session, session.begin():
        await session.execute(
            delete(AuditEventRecord).where(
                AuditEventRecord.run_id == run.id,
                AuditEventRecord.run_sequence == 1,
            )
        )
    try:
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(AuditPersistenceInvariantError) as corrupt:
                await unit_of_work.audits.list_run(run.id)
        assert corrupt.value.code == "audit_timeline_not_contiguous"

        with pytest.raises(AuditPersistenceInvariantError) as append_failure:
            await RunLifecycleService(dependencies).advance(
                run.id,
                run.version,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
                audit_context=_audit_context("timeline-gap.extend"),
            )
        assert append_failure.value.code == "audit_timeline_not_contiguous"
        async with runtime.session_factory() as session:
            counter = (
                await session.execute(
                    select(RunRecord.next_timeline_sequence).where(RunRecord.id == run.id)
                )
            ).scalar_one()
            count = int(
                (
                    await session.execute(
                        select(func.count(AuditEventRecord.global_sequence)).where(
                            AuditEventRecord.run_id == run.id
                        )
                    )
                ).scalar_one()
            )
        assert counter == 2
        assert count == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_09_plan_replay_rejects_missing_initial_step_history(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "missing-step-history.db")
    dependencies = _dependencies(runtime)
    run, envelope = await _validated_run(dependencies, "event.orch-09.missing-history")
    persisted, plan, graph, routing = await _persist_plan(dependencies, run, envelope)
    step = persisted.steps[0]
    before = await _timeline(dependencies, run.id)
    assert before[-1].event_type == "step.recorded"
    async with runtime.session_factory() as session, session.begin():
        await session.execute(
            delete(AuditEventRecord).where(
                AuditEventRecord.aggregate_type == "step",
                AuditEventRecord.aggregate_id == step.id,
            )
        )
        await session.execute(
            delete(RunStepStateTransitionRecord).where(
                RunStepStateTransitionRecord.step_id == step.id,
                RunStepStateTransitionRecord.sequence == 1,
            )
        )
    try:
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(AuditPersistenceInvariantError) as corrupt:
                await unit_of_work.audits.list_run(run.id)
        assert corrupt.value.code == "audit_timeline_not_contiguous"

        with pytest.raises(PlanPersistenceError) as captured:
            await AuditedPlanPersistenceService(dependencies).persist(
                plan,
                graph,
                routing,
                expected_run_version=run.version,
                audit_context=_audit_context("missing-history.replay"),
            )
        assert captured.value.code == "initial_step_history_missing"
        async with runtime.session_factory() as session:
            counter = (
                await session.execute(
                    select(RunRecord.next_timeline_sequence).where(RunRecord.id == run.id)
                )
            ).scalar_one()
            audit_count = int(
                (
                    await session.execute(
                        select(func.count(AuditEventRecord.global_sequence)).where(
                            AuditEventRecord.run_id == run.id
                        )
                    )
                ).scalar_one()
            )
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count(RunStepStateTransitionRecord.step_id)).where(
                                RunStepStateTransitionRecord.step_id == step.id
                            )
                        )
                    ).scalar_one()
                )
                == 0
            )
        assert counter == len(before)
        assert audit_count == len(before) - 1
    finally:
        await runtime.dispose()
