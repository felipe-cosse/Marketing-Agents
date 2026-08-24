"""ORCH-06: atomic plan, activation, deadline, and cancellation composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import (
    OrchestrationDependencies,
    RoutingResult,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWorkFactory
from marketing_agents.application.services import (
    ApprovalBoundaryDisposition,
    ApprovalBoundaryService,
    AuditedPlanPersistenceService,
    ExecutionActivationService,
    PlanPersistenceError,
    RunCancellationService,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.run_lifecycle import RunLifecycleCommand
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    transition_step,
)
from marketing_agents.infrastructure.db import (
    DatabaseRuntime,
    ExecutionControlPersistenceConflict,
    SQLAlchemyAuditRepository,
    SQLAlchemyExecutionControlRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyUnitOfWorkFactory,
)
from marketing_agents.infrastructure.db.models import (
    ExecutionOperationPolicyRecord,
    RunExecutionControlRecord,
    RunPlanRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.db.test_orch_08_approval_boundary import (
    _approve_complete_set,
    _current,
    _receive_and_validate,
)
from tests.integration.db.test_orch_09_audited_step_state import (
    IncrementingIds,
    _audit_context,
    _runtime,
    _validated_run,
)
from tests.integration.db.test_run_08_approval_persistence import (
    IncrementingIds as ApprovalIds,
)
from tests.integration.db.test_run_08_approval_persistence import (
    MutableClock as ApprovalClock,
)
from tests.integration.db.test_run_08_approval_persistence import (
    _context as approval_context,
)
from tests.integration.db.test_run_08_approval_persistence import (
    _dependencies as approval_dependencies,
)
from tests.integration.db.test_run_08_approval_persistence import _plan as build_write_plan
from tests.support.execution_control import (
    TEST_EXECUTION_CONTROL_KEY,
    execution_control_repository,
)
from tests.support.orch_09_planning import build_read_only_plan
from tests.unit.application.test_run_02_effect_aware_planning import _request

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


class ManualClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class _FaultAfterInitializeRepository(SQLAlchemyExecutionControlRepository):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, TEST_EXECUTION_CONTROL_KEY)

    async def initialize(self, policy):  # type: ignore[no-untyped-def]
        await super().initialize(policy)
        raise RuntimeError("injected fault after execution-control initialization")


class _RecordingExecutionControlRepository(SQLAlchemyExecutionControlRepository):
    def __init__(self, session: AsyncSession, order: list[str]) -> None:
        super().__init__(session, TEST_EXECUTION_CONTROL_KEY)
        self._order = order

    async def request_cancel(self, **kwargs):  # type: ignore[no-untyped-def]
        self._order.append("fence")
        return await super().request_cancel(**kwargs)


@dataclass(slots=True)
class _StaleCancellationState:
    remaining: int = 1
    calls: int = 0


class _StaleOnceExecutionControlRepository:
    def __init__(self, delegate: object, state: _StaleCancellationState) -> None:
        self._delegate = delegate
        self._state = state

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def request_cancel(self, **kwargs):  # type: ignore[no-untyped-def]
        self._state.calls += 1
        if self._state.remaining:
            self._state.remaining -= 1
            raise ExecutionControlPersistenceConflict(
                "stale_execution_control",
                "injected competing completion bumped the Run control",
            )
        return await self._delegate.request_cancel(**kwargs)  # type: ignore[attr-defined,no-any-return]


class _StaleOnceCancellationUnitOfWork:
    def __init__(self, delegate: object, state: _StaleCancellationState) -> None:
        self._delegate = delegate
        self._state = state
        self._execution_control: _StaleOnceExecutionControlRepository | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    @property
    def execution_control(self) -> _StaleOnceExecutionControlRepository:
        if self._execution_control is None:
            raise RuntimeError("test unit of work has not been entered")
        return self._execution_control

    async def __aenter__(self) -> _StaleOnceCancellationUnitOfWork:
        await self._delegate.__aenter__()  # type: ignore[attr-defined]
        self._execution_control = _StaleOnceExecutionControlRepository(
            self._delegate.execution_control,  # type: ignore[attr-defined]
            self._state,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._delegate.__aexit__(*args)  # type: ignore[attr-defined]


class _StaleOnceCancellationFactory:
    def __init__(
        self,
        delegate: UnitOfWorkFactory,
        state: _StaleCancellationState,
    ) -> None:
        self._delegate = delegate
        self._state = state

    def __call__(self) -> _StaleOnceCancellationUnitOfWork:
        return _StaleOnceCancellationUnitOfWork(self._delegate(), self._state)


def _with_stale_once_cancellation(
    dependencies: OrchestrationDependencies,
    state: _StaleCancellationState,
) -> OrchestrationDependencies:
    return OrchestrationDependencies(
        dependencies.clock,
        dependencies.ids,
        cast(
            UnitOfWorkFactory,
            _StaleOnceCancellationFactory(dependencies.unit_of_work_factory, state),
        ),
    )


class _RecordingRunRepository(SQLAlchemyRunRepository):
    def __init__(self, session: AsyncSession, order: list[str]) -> None:
        super().__init__(session)
        self._order = order

    async def apply_transition(self, **kwargs):  # type: ignore[no-untyped-def]
        result = kwargs["result"]
        if result.transition.command is RunLifecycleCommand.CANCEL:
            self._order.append("run")
        return await super().apply_transition(**kwargs)


class _RecordingRunStepRepository(SQLAlchemyRunStepRepository):
    def __init__(self, session: AsyncSession, order: list[str]) -> None:
        super().__init__(session)
        self._order = order

    async def apply_transition(self, **kwargs):  # type: ignore[no-untyped-def]
        result = kwargs["result"]
        if result.transition.command is StepLifecycleCommand.CANCEL:
            self._order.append(f"step:{result.step.id}")
        return await super().apply_transition(**kwargs)


def _read_dependencies(
    runtime: DatabaseRuntime,
    clock: ManualClock,
    *,
    run_factory: Callable[[AsyncSession], SQLAlchemyRunRepository] = SQLAlchemyRunRepository,
    step_factory: Callable[
        [AsyncSession], SQLAlchemyRunStepRepository
    ] = SQLAlchemyRunStepRepository,
    execution_factory: Callable[
        [AsyncSession], SQLAlchemyExecutionControlRepository
    ] = execution_control_repository,
) -> OrchestrationDependencies:
    return OrchestrationDependencies(
        clock,
        IncrementingIds(),
        SQLAlchemyUnitOfWorkFactory(
            runtime.session_factory,
            SQLAlchemyRepositoryFactories(
                works=SQLAlchemyWorkRepository,
                runs=run_factory,
                audits=SQLAlchemyAuditRepository,
                run_steps=step_factory,
                execution_control=execution_factory,
            ),
        ),
    )


async def _persist_read_plan(
    dependencies: OrchestrationDependencies,
    *,
    event_id: str,
    dependent_steps: bool = False,
    parallel_steps: bool = False,
):  # type: ignore[no-untyped-def]
    run, envelope = await _validated_run(dependencies, event_id)
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
        audit_context=_audit_context(f"{event_id}.plan"),
    )
    return run, persisted, plan, graph, routing


async def _execution_control(dependencies: OrchestrationDependencies, run_id: str):  # type: ignore[no-untyped-def]
    async with dependencies.unit_of_work() as unit_of_work:
        control = await unit_of_work.execution_control.get(run_id)
    assert control is not None
    return control


async def _compose_single_write_plan(
    dependencies: OrchestrationDependencies,
    *,
    event_id: str,
    seed: int,
):  # type: ignore[no-untyped-def]
    validated = await _receive_and_validate(dependencies, event_id=event_id)
    request = _request(include_write=True, run_id=validated.run.id)
    plan = build_write_plan(validated.run.id, seed=seed)
    persisted = await AuditedPlanPersistenceService(dependencies).persist(
        plan,
        request.graph,
        cast(RoutingResult, request.routing),
        expected_run_version=validated.run.version,
        audit_context=approval_context(f"{event_id}.persist"),
    )
    return plan, persisted


@pytest.mark.asyncio
async def test_orch_06_plan_creation_installs_exact_control_and_replay_is_a_noop(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-plan-control.db")
    clock = ManualClock()
    dependencies = _read_dependencies(runtime, clock)
    try:
        validated, persisted, plan, graph, routing = await _persist_read_plan(
            dependencies,
            event_id="event.orch-06.plan-control",
            dependent_steps=True,
        )
        assert persisted.created is True
        async with dependencies.unit_of_work() as unit_of_work:
            control = await unit_of_work.execution_control.get(persisted.run.id)
            operations = tuple(
                [
                    await unit_of_work.execution_control.get_operation(
                        step.id,
                        step.runtime_policy.operation_key,
                    )
                    for step in persisted.steps
                ]
            )
        assert control is not None
        assert control.policy_hash == persisted.plan.plan_hash == plan.plan_hash
        assert control.run_timeout_seconds == persisted.plan.runtime_policy.run_timeout_seconds
        assert control.max_model_calls == persisted.plan.runtime_policy.max_model_calls
        assert control.max_tool_calls == persisted.plan.runtime_policy.max_tool_calls
        assert control.model_calls == control.tool_calls == 0
        assert control.started_at is control.deadline_at is None
        assert control.cancel_requested_at is control.cancel_actor_digest is None
        assert control.created_at == persisted.plan.created_at
        assert control.version == 1
        assert all(operation is not None for operation in operations)
        for step, operation in zip(persisted.steps, operations, strict=True):
            assert operation is not None
            assert operation.run_id == persisted.run.id
            assert operation.step_id == step.id
            assert operation.operation_key == step.runtime_policy.operation_key
            assert operation.policy_hash == plan.plan_hash
            assert operation.kind is step.runtime_policy.attempt_kind
            assert operation.max_attempts == step.runtime_policy.retry.max_attempts
            assert operation.retry_backoff is step.runtime_policy.retry.backoff
            assert operation.step_timeout_seconds == step.runtime_policy.timeout.step_seconds
            assert operation.rate_limit_key == step.runtime_policy.rate_limit.key

        replayed = await AuditedPlanPersistenceService(dependencies).persist(
            plan,
            graph,
            routing,
            expected_run_version=validated.version,
            audit_context=_audit_context("orch-06.plan-control.replay"),
        )
        assert replayed.created is False
        assert replayed.plan == persisted.plan
        assert await _execution_control(dependencies, persisted.run.id) == control
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_plan_creation_fault_rolls_back_plan_and_control(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-plan-atomic-rollback.db")
    clock = ManualClock()
    dependencies = _read_dependencies(
        runtime,
        clock,
        execution_factory=_FaultAfterInitializeRepository,
    )
    try:
        run, envelope = await _validated_run(
            dependencies,
            "event.orch-06.plan-atomic-rollback",
        )
        plan, graph, routing = build_read_only_plan(
            run_id=run.id,
            workflow_id=envelope.workflow_id,
            target_instance_id=envelope.instance_id,
            configuration_revision=envelope.configuration_revision,
            catalog_hash=run.catalog_hash,
        )
        with pytest.raises(PlanPersistenceError) as failed:
            await AuditedPlanPersistenceService(dependencies).persist(
                plan,
                graph,
                routing,
                expected_run_version=run.version,
                audit_context=_audit_context("orch-06.plan-atomic-rollback.plan"),
            )
        assert failed.value.code == "execution_policy_conflict"
        async with runtime.session_factory() as session:
            counts = (
                int((await session.scalar(select(func.count()).select_from(RunPlanRecord))) or 0),
                int(
                    (
                        await session.scalar(
                            select(func.count()).select_from(RunExecutionControlRecord)
                        )
                    )
                    or 0
                ),
                int(
                    (
                        await session.scalar(
                            select(func.count()).select_from(ExecutionOperationPolicyRecord)
                        )
                    )
                    or 0
                ),
            )
        assert counts == (0, 0, 0)
        async with dependencies.unit_of_work() as unit_of_work:
            current = await unit_of_work.runs.get(run.id)
            history = await unit_of_work.runs.list_transitions(run.id)
        assert current == run
        assert all(item.command is not RunLifecycleCommand.RECORD_PLAN for item in history)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("missing", "tampered"))
async def test_orch_06_plan_replay_requires_exact_control_and_never_heals(
    tmp_path: Path,
    corruption: str,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-06-plan-replay-{corruption}.db")
    clock = ManualClock()
    dependencies = _read_dependencies(runtime, clock)
    try:
        validated, _persisted, plan, graph, routing = await _persist_read_plan(
            dependencies,
            event_id=f"event.orch-06.plan-replay-{corruption}",
        )
        original = await _execution_control(dependencies, plan.run_id)
        async with runtime.session_factory() as session, session.begin():
            if corruption == "missing":
                await session.execute(
                    delete(ExecutionOperationPolicyRecord).where(
                        ExecutionOperationPolicyRecord.run_id == plan.run_id
                    )
                )
                await session.execute(
                    delete(RunExecutionControlRecord).where(
                        RunExecutionControlRecord.run_id == plan.run_id
                    )
                )
            else:
                await session.execute(
                    update(RunExecutionControlRecord)
                    .where(RunExecutionControlRecord.run_id == plan.run_id)
                    .values(run_timeout_seconds=original.run_timeout_seconds + 1)
                )

        with pytest.raises(PlanPersistenceError) as rejected:
            await AuditedPlanPersistenceService(dependencies).persist(
                plan,
                graph,
                routing,
                expected_run_version=validated.version,
                audit_context=_audit_context(f"orch-06.plan-replay-{corruption}.replay"),
            )
        assert rejected.value.code in {
            "execution_control_integrity_corrupt",
            "execution_policy_replay_mismatch",
        }
        async with runtime.session_factory() as session:
            raw_control = await session.get(RunExecutionControlRecord, plan.run_id)
            operation_count = int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(ExecutionOperationPolicyRecord)
                        .where(ExecutionOperationPolicyRecord.run_id == plan.run_id)
                    )
                )
                or 0
            )
        if corruption == "missing":
            assert raw_control is None
            assert operation_count == 0
        else:
            assert raw_control is not None
            assert raw_control.run_timeout_seconds == original.run_timeout_seconds + 1
            assert operation_count == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_direct_activation_starts_deadline_and_roots_at_one_instant(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-direct-activation.db")
    clock = ManualClock()
    dependencies = _read_dependencies(runtime, clock)
    try:
        _, persisted, _, _, _ = await _persist_read_plan(
            dependencies,
            event_id="event.orch-06.direct-activation",
            dependent_steps=True,
        )
        before = await _execution_control(dependencies, persisted.run.id)
        assert before.started_at is None
        activated_at = persisted.plan.created_at + timedelta(seconds=45)
        clock.current = activated_at
        activated = await ExecutionActivationService(dependencies).activate(
            persisted.run.id,
            audit_context=_audit_context("orch-06.direct-activation.activate"),
        )
        assert activated.activated is True
        assert activated.run.state is RunState.EXECUTING
        roots = tuple(step for step in activated.steps if not step.dependency_keys)
        dependents = tuple(step for step in activated.steps if step.dependency_keys)
        assert roots and dependents
        assert all(step.state is StepState.READY for step in roots)
        assert all(step.updated_at == activated_at for step in roots)
        assert all(step.state is StepState.PENDING for step in dependents)
        control = await _execution_control(dependencies, persisted.run.id)
        assert control.started_at == activated_at
        assert control.deadline_at == activated_at + timedelta(seconds=control.run_timeout_seconds)
        assert control.version == before.version + 1

        async with dependencies.unit_of_work() as unit_of_work:
            before_replay_events = await unit_of_work.audits.list_run(
                persisted.run.id,
                limit=100,
            )
        clock.current += timedelta(seconds=30)
        replayed = await ExecutionActivationService(dependencies).activate(
            persisted.run.id,
            audit_context=_audit_context("orch-06.direct-activation.replay"),
        )
        assert replayed.activated is False
        assert replayed.run == activated.run
        assert await _execution_control(dependencies, persisted.run.id) == control
        async with dependencies.unit_of_work() as unit_of_work:
            after_replay_events = await unit_of_work.audits.list_run(
                persisted.run.id,
                limit=100,
            )
        assert after_replay_events == before_replay_events
    finally:
        await runtime.dispose()


async def _start_step_for_cancellation(
    dependencies: OrchestrationDependencies,
    *,
    run_id: str,
    step_id: str,
    started_at: datetime,
):  # type: ignore[no-untyped-def]
    async with dependencies.unit_of_work() as unit_of_work:
        run = await unit_of_work.runs.get(run_id)
        step = await unit_of_work.run_steps.get(step_id)
        assert run is not None and step is not None
        result = transition_step(
            step,
            StepLifecycleCommand.START,
            NoStepTransitionContext(),
            started_at,
        )
        applied = await unit_of_work.run_steps.apply_transition(
            expected_run_version=run.version,
            expected_run_state=run.state,
            expected_version=step.version,
            expected_state=step.state,
            result=result,
        )
        assert applied
        await unit_of_work.audits.append(
            AuditEventFactory(
                AuditContext.system(
                    "test.orch-06",
                    correlation_id="request.orch-06.cancel.start",
                )
            ).step_transition(result.step, result.transition)
        )
        await unit_of_work.commit()
        return result.step


@pytest.mark.asyncio
async def test_orch_06_direct_cancellation_fences_first_and_preserves_in_flight_step(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-direct-cancel.db")
    clock = ManualClock()
    order: list[str] = []
    dependencies = _read_dependencies(
        runtime,
        clock,
        run_factory=lambda session: _RecordingRunRepository(session, order),
        step_factory=lambda session: _RecordingRunStepRepository(session, order),
        execution_factory=lambda session: _RecordingExecutionControlRepository(session, order),
    )
    try:
        _, persisted, _, _, _ = await _persist_read_plan(
            dependencies,
            event_id="event.orch-06.direct-cancel",
            parallel_steps=True,
        )
        clock.current = persisted.plan.created_at + timedelta(seconds=1)
        activated = await ExecutionActivationService(dependencies).activate(
            persisted.run.id,
            audit_context=_audit_context("orch-06.direct-cancel.activate"),
        )
        assert all(step.state is StepState.READY for step in activated.steps)
        executing = await _start_step_for_cancellation(
            dependencies,
            run_id=activated.run.id,
            step_id=activated.steps[0].id,
            started_at=clock.current + timedelta(seconds=1),
        )
        queued = activated.steps[1]
        order.clear()
        cancelled_at = clock.current + timedelta(seconds=2)
        clock.current = cancelled_at
        cancelled = await RunCancellationService(dependencies).request(
            activated.run.id,
            audit_context=_audit_context("orch-06.direct-cancel.request"),
        )
        assert order == ["fence", f"step:{queued.id}", "run"]
        assert cancelled.run.state is RunState.CANCELLED
        assert tuple(step.id for step in cancelled.cancelled_steps) == (queued.id,)
        assert cancelled.preserved_executing_step_ids == (executing.id,)
        control = await _execution_control(dependencies, activated.run.id)
        assert control.cancel_requested_at == cancelled_at
        assert control.cancel_actor_digest is not None
        async with dependencies.unit_of_work() as unit_of_work:
            steps = await unit_of_work.run_steps.list_for_run(activated.run.id)
        states = {step.id: step.state for step in steps}
        assert states[executing.id] is StepState.EXECUTING
        assert states[queued.id] is StepState.CANCELLED
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_read_cancellation_reloads_whole_uow_after_stale_control(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-read-cancel-stale-control.db")
    clock = ManualClock()
    dependencies = _read_dependencies(runtime, clock)
    try:
        _, persisted, _, _, _ = await _persist_read_plan(
            dependencies,
            event_id="event.orch-06.read-cancel-stale-control",
        )
        activated = await ExecutionActivationService(dependencies).activate(
            persisted.run.id,
            audit_context=_audit_context("orch-06.read-cancel-stale-control.activate"),
        )
        state = _StaleCancellationState()
        retrying_dependencies = _with_stale_once_cancellation(dependencies, state)
        clock.current += timedelta(seconds=1)

        cancelled = await RunCancellationService(retrying_dependencies).request(
            activated.run.id,
            audit_context=_audit_context("orch-06.read-cancel-stale-control.request"),
        )

        assert state.calls == 2
        assert cancelled.run.state is RunState.CANCELLED
        assert all(step.state is StepState.CANCELLED for step in cancelled.cancelled_steps)
        control = await _execution_control(dependencies, activated.run.id)
        assert control.cancel_requested_at == clock.current
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_approval_wait_does_not_consume_timeout_and_release_starts_it(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-approval-release.db")
    clock = ApprovalClock()
    dependencies = approval_dependencies(runtime, clock=clock, ids=ApprovalIds(1600))
    try:
        plan, persisted = await _compose_single_write_plan(
            dependencies,
            event_id="event.orch-06.approval-release",
            seed=1600,
        )
        unstarted = await _execution_control(dependencies, plan.run_id)
        assert persisted.run.state is RunState.AWAITING_APPROVAL
        assert unstarted.started_at is unstarted.deadline_at is None
        clock.current = persisted.plan.created_at + timedelta(seconds=30)
        await _approve_complete_set(
            dependencies,
            clock,
            plan.run_id,
            suffix="orch-06.approval-release",
        )
        run, _, selected, _, _ = await _current(dependencies, plan.run_id)
        released_at = selected.authorization_set.released_at
        assert run.state is RunState.EXECUTING
        assert released_at is not None
        control = await _execution_control(dependencies, plan.run_id)
        assert control.started_at == released_at
        assert control.deadline_at == released_at + timedelta(seconds=control.run_timeout_seconds)
        assert control.deadline_at > persisted.plan.created_at + timedelta(
            seconds=control.run_timeout_seconds
        )
        assert control.version == unstarted.version + 1

        clock.current += timedelta(seconds=5)
        replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=approval_context("orch-06.approval-release.replay"),
        )
        assert replay.disposition is ApprovalBoundaryDisposition.RELEASED
        assert await _execution_control(dependencies, plan.run_id) == control
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("pre_release", "post_release"))
async def test_orch_06_approval_cancellation_installs_pre_or_post_release_fence(
    tmp_path: Path,
    phase: str,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-06-approval-cancel-{phase}.db")
    clock = ApprovalClock()
    dependencies = approval_dependencies(runtime, clock=clock, ids=ApprovalIds(1700))
    try:
        plan, persisted = await _compose_single_write_plan(
            dependencies,
            event_id=f"event.orch-06.approval-cancel-{phase}",
            seed=1700,
        )
        released_at = None
        if phase == "post_release":
            await _approve_complete_set(
                dependencies,
                clock,
                plan.run_id,
                suffix="orch-06.approval-cancel-post",
            )
            _, _, selection, _, _ = await _current(dependencies, plan.run_id)
            released_at = selection.authorization_set.released_at
            assert released_at is not None
        else:
            assert persisted.run.state is RunState.AWAITING_APPROVAL

        clock.current += timedelta(seconds=1)
        cancelled_at = clock.current
        cancelled = await ApprovalBoundaryService(dependencies).cancel(
            plan.run_id,
            audit_context=approval_context(f"orch-06.approval-cancel-{phase}"),
        )
        assert cancelled.disposition is ApprovalBoundaryDisposition.CANCELLED
        assert cancelled.run.state is RunState.CANCELLED
        control = await _execution_control(dependencies, plan.run_id)
        assert control.cancel_requested_at == cancelled_at
        assert control.cancel_actor_digest is not None
        if phase == "pre_release":
            assert control.started_at is control.deadline_at is None
        else:
            assert control.started_at == released_at
            assert control.deadline_at == released_at + timedelta(
                seconds=control.run_timeout_seconds
            )

        replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=approval_context(f"orch-06.approval-cancel-{phase}.replay"),
        )
        assert replay.disposition is ApprovalBoundaryDisposition.CANCELLED
        assert await _execution_control(dependencies, plan.run_id) == control
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_released_write_cancellation_reloads_after_stale_control(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-write-cancel-stale-control.db")
    clock = ApprovalClock()
    dependencies = approval_dependencies(runtime, clock=clock, ids=ApprovalIds(1750))
    try:
        plan, _ = await _compose_single_write_plan(
            dependencies,
            event_id="event.orch-06.write-cancel-stale-control",
            seed=1750,
        )
        await _approve_complete_set(
            dependencies,
            clock,
            plan.run_id,
            suffix="orch-06.write-cancel-stale-control.release",
        )
        state = _StaleCancellationState()
        retrying_dependencies = _with_stale_once_cancellation(dependencies, state)
        clock.current += timedelta(seconds=1)

        cancelled = await ApprovalBoundaryService(retrying_dependencies).cancel(
            plan.run_id,
            audit_context=approval_context("orch-06.write-cancel-stale-control.request"),
        )

        assert state.calls == 2
        assert cancelled.disposition is ApprovalBoundaryDisposition.CANCELLED
        assert cancelled.run.state is RunState.CANCELLED
        control = await _execution_control(dependencies, plan.run_id)
        assert control.cancel_requested_at == clock.current
    finally:
        await runtime.dispose()
