"""RUN-05 durable dispatch; ORCH-06/ORCH-07 controlled delivery witnesses."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import (
    OrchestrationDependencies,
    RoutingResult,
)
from marketing_agents.application.orchestration.effect_planner import EffectPlanRequest
from marketing_agents.application.policies.write_authorization import (
    ApprovalReservation,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.connectors import (
    ConnectorPortError,
    ConnectorWriteResult,
)
from marketing_agents.application.ports.external_writes import (
    ConnectorDeliveryContract,
    ConnectorDeliveryFailure,
    ExternalWriteConnectorGateway,
)
from marketing_agents.application.ports.id_generator import IdGenerator
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.repositories import (
    ExecutionControlRepositoryConflict,
    ReleaseAuthority,
    ReleaseCallMode,
)
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.application.services import (
    ApprovalBoundaryDisposition,
    ApprovalBoundaryService,
    ApprovalDecisionService,
    AuditedPlanPersistenceService,
    ControlledReadCommand,
    ControlledReadExecutor,
    DispatchDisposition,
    ExternalActionDispatcher,
    ExternalActionDispatchError,
    ExternalActionDispatchResult,
    ExternalActionRegistrationDisposition,
    ExternalActionRegistrationService,
    RunStepLifecycleService,
    TerminalExecutionCleanupService,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.action_hash import canonical_action_hash
from marketing_agents.domain.entities import (
    MAX_DELIVERY_ATTEMPTS,
    ConnectorActionReceipt,
    ExternalAction,
)
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
    RunState,
    StepState,
)
from marketing_agents.domain.execution_control import (
    AttemptReservationCommand,
    DeliveryCallPermit,
    DeliveryCallReservationCommand,
    OperationExecutionPolicy,
    fixed_window_start,
)
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.runtime_policy import canonical_payload_size_bytes
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    transition_step,
)
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.adapters.connectors.mock import (
    DurableMockReceiptLedger,
    MockConnectorBundle,
)
from marketing_agents.infrastructure.adapters.connectors.registry import (
    ConnectorBundleConfigurationError,
)
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    ExternalActionPersistenceConflict,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditRepository,
    SQLAlchemyConnectorReceiptRepository,
    SQLAlchemyExecutionControlRepository,
    SQLAlchemyExternalActionRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models import (
    ConnectorActionReceiptRecord,
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
    RateLimitWindowRecord,
    RunExecutionControlRecord,
    RunRecord,
    RunStepRecord,
    WorkItemRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.execution_control_digest import (
    execution_control_record_digest,
)
from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.db.test_orch_08_approval_boundary import (
    _current,
    _decision,
    _principal,
    _receive_and_validate,
)
from tests.integration.db.test_run_08_approval_persistence import (
    APPROVAL_INTEGRITY_KEY,
    IncrementingIds,
    _context,
)
from tests.support.execution_control import (
    TEST_EXECUTION_CONTROL_KEY,
    execution_control_repository,
)
from tests.support.read_adapter import ExactReadContractAdapter, observation_for
from tests.unit.application.test_run_02_effect_aware_planning import (
    CATALOG,
    COMMUNITY_BINDING,
    REGISTRY,
    WORKER_TEMPLATE,
    WORKFLOW_HASH,
    RecordingIds,
    _planner,
    _request,
    _route,
    _write_step,
)

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
RUN_ID = "run.run-05.0001"


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def tick(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class UnusedIds:
    def new(self, namespace: str) -> str:  # pragma: no cover - negative control
        raise AssertionError(f"RUN-05 must not generate {namespace!r} during persistence")


def _approval_repository(session):  # type: ignore[no-untyped-def]
    return SQLAlchemyApprovalRepository(session, APPROVAL_INTEGRITY_KEY)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _uow_factory(runtime: DatabaseRuntime) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            artifacts=SQLAlchemyArtifactRepository,
            approvals=_approval_repository,
            run_steps=SQLAlchemyRunStepRepository,
            external_actions=SQLAlchemyExternalActionRepository,
            connector_receipts=SQLAlchemyConnectorReceiptRepository,
            execution_control=execution_control_repository,
        ),
    )


def _dependencies(
    runtime: DatabaseRuntime,
    clock: MutableClock,
    *,
    ids: IdGenerator | None = None,
) -> OrchestrationDependencies:
    return OrchestrationDependencies(clock, ids or UnusedIds(), _uow_factory(runtime))


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(_sqlite_url(path))
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


async def _seed_parent(runtime: DatabaseRuntime, run_id: str = RUN_ID) -> None:
    async with runtime.session_factory() as session, session.begin():
        session.add(
            WorkItemRecord(
                id=f"work.{run_id}",
                source="manual",
                event_id=f"event.{run_id}",
                agent_instance_id="inst.community.education.material-builder.01",
                trigger_id="trigger.manual.1",
                workflow_id="workflow.community.onboarding",
                mode="mock_execution",
                campaign_brief_id=None,
                campaign_brief_revision=None,
                configuration_revision=1,
                admitted_payload={"safe": True},
                redacted_input_projection={"safe": True},
                input_schema_id="schema.test.seed.v1",
                input_schema_hash="schema-sha256-v1:" + "e" * 64,
                input_classification="internal",
                input_projection_created_at=NOW,
                input_projection_expires_at=NOW + timedelta(days=90),
                input_projection_integrity_digest="f" * 64,
                input_digest="a" * 64,
                admission_digest="b" * 64,
                digest_key_version="admission-hmac-sha256-v1:" + "c" * 64,
                created_at=NOW,
            )
        )
        await session.flush()
        session.add(
            RunRecord(
                id=run_id,
                work_item_id=f"work.{run_id}",
                state="executing",
                catalog_hash="d" * 64,
                configuration_revision=1,
                approval_required=True,
                terminal_reason_code=None,
                created_at=NOW,
                updated_at=NOW,
                version=5,
            )
        )


def _plan(*, seed: int, body: str = "private welcome body"):
    planner, _, _ = _planner(ids=RecordingIds(seed=seed))
    return planner.plan(
        _request(
            include_write=True,
            run_id=RUN_ID,
            write_step=_write_step(body=body),
        )
    )


def _multi_plan(*, seed: int, second_body: str = "second private body"):
    templates = tuple(
        item.model_copy(
            update={"budget_policy": item.budget_policy.model_copy(update={"max_tool_calls": 2})}
        )
        if item.id == WORKER_TEMPLATE
        else item
        for item in CATALOG.templates
    )
    planner, _, _ = _planner(ids=RecordingIds(seed=seed), templates=templates)
    first = _write_step(key="welcome-a", runtime_step_id="runtime-step.welcome-a")
    second = _write_step(
        key="welcome-b",
        runtime_step_id="runtime-step.welcome-b",
        body=second_body,
    )
    return planner.plan(
        EffectPlanRequest(
            run_id=RUN_ID,
            workflow_definition_hash=WORKFLOW_HASH,
            graph=DependencyGraph.build(
                (
                    TopologyStep("welcome-a", 1),
                    TopologyStep("welcome-b", 2, ("welcome-a",), terminal_result=True),
                ),
                workflow_max_steps=10,
                global_max_steps=20,
            ),
            routing=_route(include_write=True),  # type: ignore[arg-type]
            steps=(first, second),
            requested_by="principal.local.operator",
        )
    )


class _SuccessfulReadAdapter(ExactReadContractAdapter):
    def output_contract_for(
        self,
        operation: OperationExecutionPolicy,
    ) -> RuntimeOutputContract:
        if operation.result_schema_id is None:
            raise ValueError("callable test operation requires a result schema")
        result_type = REGISTRY.resolve(operation.capability_id).result_type
        return RuntimeOutputContract(
            schema_id=operation.result_schema_id,
            schema_version="v1",
            schema=result_type.model_json_schema(),  # type: ignore[union-attr]
            classification=operation.data_classification,
            provider_kind="connector",
            provider_mode="mock",
            provider_name=operation.connector_family,
            provider_version=result_type.__name__,
        )

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        assert request.input_payload == {"query": "release-write-dependency"}
        return observation_for(request, {"records": []})


async def _complete_controlled_read_dependency(
    dependencies: OrchestrationDependencies,
    clock: MutableClock,
    run_id: str,
) -> None:
    _, steps, _, _, _ = await _current(dependencies, run_id)
    read = steps[0]
    clock.tick(1)
    ready = await RunStepLifecycleService(dependencies).advance(
        read.id,
        read.version,
        StepLifecycleCommand.MARK_READY,
        NoStepTransitionContext(),
        audit_context=_context("dispatch.read.mark_ready"),
    )
    clock.tick(1)
    result = await ControlledReadExecutor(dependencies, _SuccessfulReadAdapter()).execute(
        ControlledReadCommand(
            ready.step.id,
            {"query": "release-write-dependency"},
        ),
        audit_context=_context("dispatch.read.controlled"),
    )
    assert result.step.state is StepState.SUCCEEDED


async def _released_action(
    runtime: DatabaseRuntime,
    clock: MutableClock,
    *,
    seed: int,
    delivery_attempt_limit: int = 2,
    body: str = "private welcome body",
    templates: tuple[object, ...] | None = None,
    operations: tuple[object, ...] | None = None,
) -> ExternalAction:
    """Create one real plan, approve its complete set, and release its WRITE step."""

    dependencies = _dependencies(
        runtime,
        clock,
        ids=IncrementingIds(10_000 + seed * 100),
    )
    event_id = f"event.run-05.released.{seed}"
    validated = await _receive_and_validate(dependencies, event_id=event_id)
    request = _request(
        include_write=True,
        run_id=validated.run.id,
        write_step=_write_step(
            runtime_step_id=f"runtime-step.welcome.{seed}",
            body=body,
        ),
    )
    request = replace(
        request,
        steps=(
            replace(
                request.steps[0],
                runtime_step_id=f"runtime-step.membership.{seed}",
            ),
            request.steps[1],
        ),
    )
    planner, _, _ = _planner(
        ids=RecordingIds(seed=seed),
        templates=templates,
        operations=operations,
    )
    plan = planner.plan(request)
    persisted = await AuditedPlanPersistenceService(dependencies).persist(
        plan,
        request.graph,
        cast(RoutingResult, request.routing),
        expected_run_version=validated.run.version,
        audit_context=_context(f"{event_id}.persist"),
    )
    assert persisted.run.state is RunState.AWAITING_APPROVAL

    _, _, _, requests, actions = await _current(dependencies, plan.run_id)
    assert len(requests) == len(actions) == 1
    action = actions[0]
    assert action is not None
    assert action.state is ExternalActionState.AWAITING_APPROVAL
    if delivery_attempt_limit != action.delivery_attempt_limit:
        # Plan persistence currently owns the default delivery policy. Apply the
        # test-specific policy before approval; the real release still derives
        # and persists every reservation/authority field atomically.
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(ExternalActionRecord)
                .where(ExternalActionRecord.id == action.id)
                .values(delivery_attempt_limit=delivery_attempt_limit)
            )

    clock.tick(1)
    decided = await ApprovalDecisionService(dependencies).decide(
        _decision(
            requests[0].request,
            ApprovalDecisionKind.APPROVE,
            suffix=f"run-05.{seed}",
        ),
        principal=_principal(f"run-05.{seed}"),
    )
    # The decision result is the immutable approval mutation; the boundary
    # service then consumes that same request in its atomic release transaction.
    assert decided.request.status is ApprovalStatus.APPROVED
    await _complete_controlled_read_dependency(dependencies, clock, plan.run_id)

    run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
    action = cast(ExternalAction, actions[0])
    assert run.state is RunState.EXECUTING
    assert selected.authorization_set.status.value == "released"
    assert all(request.status is ApprovalStatus.CONSUMED for request in requests)
    assert [step.state for step in steps] == [StepState.SUCCEEDED, StepState.READY]
    assert action.state is ExternalActionState.DISPATCH_RESERVED
    assert action.reservation is not None
    assert action.delivery_attempt_limit == delivery_attempt_limit
    clock.tick(1)
    return action


async def _released_parallel_actions(
    runtime: DatabaseRuntime,
    clock: MutableClock,
    *,
    seed: int,
) -> tuple[ExternalAction, ExternalAction, ExternalAction]:
    """Release three independent WRITE siblings behind one controlled READ."""

    dependencies = _dependencies(
        runtime,
        clock,
        ids=IncrementingIds(20_000 + seed * 100),
    )
    event_id = f"event.run-05.parallel.{seed}"
    validated = await _receive_and_validate(dependencies, event_id=event_id)
    base = _request(
        include_write=True,
        run_id=validated.run.id,
        write_step=_write_step(
            key="welcome-a",
            runtime_step_id=f"runtime-step.parallel-a.{seed}",
        ),
    )
    read = replace(
        base.steps[0],
        runtime_step_id=f"runtime-step.parallel-read.{seed}",
    )
    writes = (
        base.steps[1],
        _write_step(
            key="welcome-b",
            runtime_step_id=f"runtime-step.parallel-b.{seed}",
            body="second private body",
        ),
        _write_step(
            key="welcome-c",
            runtime_step_id=f"runtime-step.parallel-c.{seed}",
            body="third private body",
        ),
    )
    graph = DependencyGraph.build(
        (
            TopologyStep("membership", 1),
            TopologyStep("welcome-a", 2, ("membership",)),
            TopologyStep("welcome-b", 3, ("membership",)),
            TopologyStep(
                "welcome-c",
                4,
                ("welcome-a", "welcome-b"),
                terminal_result=True,
            ),
        ),
        workflow_max_steps=10,
        global_max_steps=20,
    )
    request = replace(base, graph=graph, steps=(read, *writes))
    templates = tuple(
        item.model_copy(
            update={"budget_policy": item.budget_policy.model_copy(update={"max_tool_calls": 4})}
        )
        if item.id == WORKER_TEMPLATE
        else item
        for item in CATALOG.templates
    )
    planner, _, _ = _planner(ids=RecordingIds(seed=seed), templates=templates)
    plan = planner.plan(request)
    await AuditedPlanPersistenceService(dependencies).persist(
        plan,
        graph,
        cast(RoutingResult, request.routing),
        expected_run_version=validated.run.version,
        audit_context=_context(f"{event_id}.persist"),
    )
    _, _, _, requests, _ = await _current(dependencies, plan.run_id)
    assert len(requests) == 3
    for index, stored in enumerate(requests, start=1):
        clock.tick(1)
        await ApprovalDecisionService(dependencies).decide(
            _decision(
                stored.request,
                ApprovalDecisionKind.APPROVE,
                suffix=f"parallel.{seed}.{index}",
            ),
            principal=_principal(f"parallel.{seed}.{index}"),
        )
    await _complete_controlled_read_dependency(dependencies, clock, plan.run_id)
    run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
    assert run.state is RunState.EXECUTING
    assert selected.authorization_set.status.value == "released"
    assert all(request.status is ApprovalStatus.CONSUMED for request in requests)
    assert [step.state for step in steps] == [
        StepState.SUCCEEDED,
        StepState.READY,
        StepState.READY,
        StepState.READY,
    ]
    released = tuple(cast(ExternalAction, action) for action in actions)
    assert len(released) == 3
    assert all(action.state is ExternalActionState.DISPATCH_RESERVED for action in released)
    clock.tick(1)
    return cast(tuple[ExternalAction, ExternalAction, ExternalAction], released)


async def _release_authority(unit_of_work, action_id: str) -> ReleaseAuthority:  # type: ignore[no-untyped-def]
    authority = await unit_of_work.approvals.get_release_authority(action_id)
    assert authority is not None
    return authority


async def _claim_reserved(
    unit_of_work,  # type: ignore[no-untyped-def]
    action: ExternalAction,
    clock: MutableClock,
    *,
    lease_owner: str,
    lease_seconds: int,
    authority: ReleaseAuthority | None = None,
) -> ExternalAction | None:
    trusted = authority or await _release_authority(unit_of_work, action.id)
    return await unit_of_work.external_actions.claim_reserved(
        action_id=action.id,
        expected_version=action.version,
        authority=trusted,
        lease_owner=lease_owner,
        claimed_at=clock.now(),
        lease_expires_at=clock.now() + timedelta(seconds=lease_seconds),
    )


async def _mark_call_started(
    unit_of_work,  # type: ignore[no-untyped-def]
    action: ExternalAction,
    clock: MutableClock,
    *,
    lease_owner: str,
    authority: ReleaseAuthority | None = None,
    call_deadline_at: datetime | None = None,
) -> ExternalAction | None:
    trusted = authority or await _release_authority(unit_of_work, action.id)
    step_transition = None
    if trusted.call_mode is ReleaseCallMode.FIRST_CALL:
        step = await unit_of_work.run_steps.get(trusted.step_id)
        assert step is not None
        step_transition = transition_step(
            step,
            StepLifecycleCommand.START_RESERVED_WRITE,
            NoStepTransitionContext(),
            clock.now(),
        )
    else:
        assert trusted.call_mode is ReleaseCallMode.PROVIDER_RETRY
    result = await unit_of_work.external_actions.mark_call_started(
        action_id=action.id,
        expected_version=action.version,
        authority=trusted,
        lease_owner=lease_owner,
        attempt_number=action.delivery_attempt_count,
        started_at=clock.now(),
        call_deadline_at=(
            clock.now() + timedelta(seconds=1) if call_deadline_at is None else call_deadline_at
        ),
        step_transition=step_transition,
    )
    return None if result is None else result.action


def _authorize_action(action, idempotency_key: str):  # type: ignore[no-untyped-def]
    reservation = action.reservation
    assert reservation is not None
    return WriteAuthorizationGuard().authorize(
        action.envelope,
        ApprovalReservation(
            reservation_id=reservation.reservation_id,
            authorization_set_id=reservation.authorization_set_id,
            state="dispatch_reserved",
            action_id=action.id,
            action_hash=reservation.action_hash,
            capability_id=reservation.capability_id,
            binding_id=reservation.binding_id,
            approval_request_id=reservation.approval_request_id,
            approval_decision_id=reservation.approval_decision_id,
            idempotency_key=idempotency_key,
            reserved_at=reservation.reserved_at,
        ),
        idempotency_key,
    )


def _gateway(
    runtime: DatabaseRuntime, clock: MutableClock
) -> tuple[RegistryConnectorWriteGateway, DurableMockReceiptLedger]:
    ledger = DurableMockReceiptLedger(_uow_factory(runtime), clock)
    bundle = MockConnectorBundle.create(REGISTRY, ledger)
    return (
        RegistryConnectorWriteGateway(
            REGISTRY,
            bundle,
            binding_configuration_revisions={COMMUNITY_BINDING: 1},
        ),
        ledger,
    )


async def _counts(runtime: DatabaseRuntime) -> tuple[int, int]:
    async with runtime.session_factory() as session:
        actions = int(
            (await session.execute(select(func.count(ExternalActionRecord.id)))).scalar_one()
        )
        receipts = int(
            (
                await session.execute(select(func.count(ConnectorActionReceiptRecord.receipt_id)))
            ).scalar_one()
        )
    return actions, receipts


@pytest.mark.asyncio
async def test_run_05_atomic_set_replay_returns_original_exact_identity_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registration-restart.db"
    runtime = await _runtime(path)
    await _seed_parent(runtime)
    clock = MutableClock()
    first_plan = _multi_plan(seed=10)
    regenerated = _multi_plan(seed=100)
    assert tuple(item.envelope.action_id for item in first_plan.proposed_actions) != tuple(
        item.envelope.action_id for item in regenerated.proposed_actions
    )
    first = ExternalActionRegistrationService(_dependencies(runtime, clock))
    try:
        created = await first.register_plan_actions(first_plan)
        await runtime.dispose()

        restarted = await _runtime(path)
        replay = ExternalActionRegistrationService(_dependencies(restarted, clock))
        replayed = await replay.register_plan_actions(regenerated)

        assert created.disposition is ExternalActionRegistrationDisposition.CREATED
        assert replayed.disposition is ExternalActionRegistrationDisposition.REPLAYED
        assert [item.action.envelope.step_key for item in replayed.actions] == [
            "welcome-a",
            "welcome-b",
        ]
        assert [item.action.id for item in replayed.actions] == [
            item.action.id for item in created.actions
        ]
        assert [item.action.action_hash for item in replayed.actions] == [
            item.action.action_hash for item in created.actions
        ]
        assert [item.action.idempotency_key for item in replayed.actions] == [
            item.action.idempotency_key for item in created.actions
        ]
        assert len({item.action.envelope.authorization_set_id for item in replayed.actions}) == 1
        assert "private welcome body" not in repr(replayed.actions[0].action)
        assert await _counts(restarted) == (2, 0)

        changed = _multi_plan(seed=200, second_body="changed private body")
        with pytest.raises(ExternalActionPersistenceConflict) as collision:
            await replay.register_plan_actions(changed)
        assert collision.value.code == "action_key_collision"
        assert await _counts(restarted) == (2, 0)
        await restarted.dispose()
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_set_registration_race_is_all_created_or_authoritative_replay(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "registration-race.db")
    await _seed_parent(runtime)
    clock = MutableClock()
    first = ExternalActionRegistrationService(_dependencies(runtime, clock))
    second = ExternalActionRegistrationService(_dependencies(runtime, clock))
    try:
        results = await asyncio.gather(
            first.register_plan_actions(_multi_plan(seed=1)),
            second.register_plan_actions(_multi_plan(seed=101)),
        )
        assert {item.disposition for item in results} == {
            ExternalActionRegistrationDisposition.CREATED,
            ExternalActionRegistrationDisposition.REPLAYED,
        }
        assert [item.action.id for item in results[0].actions] == [
            item.action.id for item in results[1].actions
        ]
        assert [item.action.envelope.step_key for item in results[0].actions] == [
            "welcome-a",
            "welcome-b",
        ]
        assert await _counts(runtime) == (2, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_outer_rollback_removes_the_entire_registered_set(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "registration-rollback.db")
    await _seed_parent(runtime)
    service = ExternalActionRegistrationService(_dependencies(runtime, MutableClock()))
    try:
        with pytest.raises(RuntimeError, match="injected fault"):
            async with _uow_factory(runtime)() as unit_of_work:
                await service.register_plan_actions_in_uow(unit_of_work, _multi_plan(seed=1))
                raise RuntimeError("injected fault")
        assert await _counts(runtime) == (0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_preexisting_partial_set_fails_closed_without_filling_members(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "partial-set.db")
    await _seed_parent(runtime)
    service = ExternalActionRegistrationService(_dependencies(runtime, MutableClock()))
    plan = _multi_plan(seed=1)
    candidates = service._candidates(plan)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            inserted = await unit_of_work.external_actions.add_proposed_set_or_get(candidates[:1])
            assert inserted.inserted
            await unit_of_work.commit()
        with pytest.raises(ExternalActionPersistenceConflict) as conflict:
            await service.register_plan_actions(plan)
        assert conflict.value.code == "partial_action_set"
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_three_phase_dispatch_commits_one_receipt_and_replays_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dispatch-restart.db"
    runtime = await _runtime(path)
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=101)
    gateway, ledger = _gateway(runtime, clock)
    dispatcher = ExternalActionDispatcher(
        _dependencies(runtime, clock), gateway, WriteAuthorizationGuard()
    )
    try:
        completed = await dispatcher.dispatch_once(action.id, lease_owner="worker.1")
        assert completed.disposition is DispatchDisposition.SUCCEEDED
        assert completed.action.state is ExternalActionState.SUCCEEDED
        assert ledger.side_effect_count == 1
        assert await _counts(runtime) == (1, 1)
        await runtime.dispose()

        restarted = await _runtime(path)
        restarted_gateway, restarted_ledger = _gateway(restarted, clock)
        restarted_dispatcher = ExternalActionDispatcher(
            _dependencies(restarted, clock),
            restarted_gateway,
            WriteAuthorizationGuard(),
        )
        replayed = await restarted_dispatcher.dispatch_once(action.id, lease_owner="worker.2")
        assert replayed.disposition is DispatchDisposition.ALREADY_SUCCEEDED
        assert restarted_ledger.side_effect_count == 0
        assert await _counts(restarted) == (1, 1)
        await restarted.dispose()
    finally:
        await runtime.dispose()


class LostResponseGateway:
    def __init__(self, delegate: ExternalWriteConnectorGateway) -> None:
        self.delegate = delegate
        self.calls = 0

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        self.calls += 1
        await self.delegate.execute(authorization)
        raise ConnectorDeliveryFailure(
            "connector_delivery_uncertain",
            "sanitized lost response",
            request_may_have_left_process=True,
        )


class CountingGateway:
    def __init__(self, delegate: ExternalWriteConnectorGateway) -> None:
        self.delegate = delegate
        self.calls = 0

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        self.calls += 1
        return await self.delegate.execute(authorization)


class BlockingFirstGateway:
    """Hold the first provider call open while allowing a recovery replay."""

    def __init__(self, delegate: ExternalWriteConnectorGateway) -> None:
        self.delegate = delegate
        self.calls = 0
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
        return await self.delegate.execute(authorization)


class ReceiptThenBlockingGateway:
    """Commit the provider receipt, then hold its successful response open."""

    def __init__(self, delegate: ExternalWriteConnectorGateway) -> None:
        self.delegate = delegate
        self.calls = 0
        self.receipt_committed = asyncio.Event()
        self.release_response = asyncio.Event()

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        self.calls += 1
        result = await self.delegate.execute(authorization)
        self.receipt_committed.set()
        await self.release_response.wait()
        return result


class NoReceiptUncertainGateway:
    """Provider-like gateway that leaves no receipt and records physical calls."""

    def __init__(self) -> None:
        self.calls = 0

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        contract = action.delivery_contract
        return ConnectorDeliveryContract(
            capability_id=contract.capability_id,
            connector_family=contract.connector_family,
            binding_id=contract.binding_id,
            binding_configuration_revision=contract.binding_configuration_revision,
            request_schema_id=contract.request_schema_id,
            idempotency_support=contract.idempotency_support,
            timeout_seconds=contract.timeout_seconds,
        )

    async def execute(self, _authorization):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise ConnectorDeliveryFailure(
            "connector_delivery_uncertain",
            "sanitized provider outcome",
            request_may_have_left_process=True,
        )


class CancellationSwallowingLateWriteGateway:
    """Return a valid-looking result after swallowing the dispatch timeout."""

    def __init__(
        self,
        clock: MutableClock,
        permits: list[DeliveryCallPermit],
        *,
        past_deadline: bool,
    ) -> None:
        self.clock = clock
        self.permits = permits
        self.past_deadline = past_deadline
        self.calls = 0
        self.swallowed_cancellation = False

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        contract = action.delivery_contract
        return ConnectorDeliveryContract(
            capability_id=contract.capability_id,
            connector_family=contract.connector_family,
            binding_id=contract.binding_id,
            binding_configuration_revision=contract.binding_configuration_revision,
            request_schema_id=contract.request_schema_id,
            idempotency_support=contract.idempotency_support,
            timeout_seconds=contract.timeout_seconds,
        )

    async def execute(self, _authorization):  # type: ignore[no-untyped-def]
        self.calls += 1
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            self.swallowed_cancellation = True
            assert len(self.permits) == 1
            self.clock.current = self.permits[0].call_deadline_at + (
                timedelta(microseconds=1) if self.past_deadline else timedelta(0)
            )
            return ConnectorWriteResult(
                receipt_id="receipt.orch-06.late-valid-looking",
                status="mock_succeeded",
                safe_metadata={"late": True},
            )
        raise AssertionError("bounded dispatcher failed to cancel the late gateway")


class PermitRecordingExecutionControlRepository(SQLAlchemyExecutionControlRepository):
    def __init__(self, session, permits: list[DeliveryCallPermit]) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session, TEST_EXECUTION_CONTROL_KEY)
        self._permits = permits

    async def reserve_delivery_call(self, command):  # type: ignore[no-untyped-def]
        result = await super().reserve_delivery_call(command)
        self._permits.append(result.permit)
        return result


class DeadlineAdvancingUnitOfWork:
    def __init__(
        self,
        delegate,  # type: ignore[no-untyped-def]
        clock: MutableClock,
        permits: list[DeliveryCallPermit],
        state: dict[str, bool],
    ) -> None:
        self._delegate = delegate
        self._clock = clock
        self._permits = permits
        self._state = state

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._delegate, name)

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
        return await self._delegate.__aexit__(*args)

    async def commit(self) -> None:
        await self._delegate.commit()
        if self._permits and not self._state["advanced"]:
            self._clock.current = self._permits[-1].call_deadline_at
            self._state["advanced"] = True


class DeadlineAdvancingUnitOfWorkFactory:
    def __init__(
        self,
        delegate,  # type: ignore[no-untyped-def]
        clock: MutableClock,
        permits: list[DeliveryCallPermit],
    ) -> None:
        self._delegate = delegate
        self._clock = clock
        self._permits = permits
        self._state = {"advanced": False}

    def __call__(self):  # type: ignore[no-untyped-def]
        return DeadlineAdvancingUnitOfWork(
            self._delegate(),
            self._clock,
            self._permits,
            self._state,
        )


def _permit_recording_dependencies(
    runtime: DatabaseRuntime,
    clock: MutableClock,
    permits: list[DeliveryCallPermit],
) -> OrchestrationDependencies:
    unit_of_work = SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            artifacts=SQLAlchemyArtifactRepository,
            approvals=_approval_repository,
            run_steps=SQLAlchemyRunStepRepository,
            external_actions=SQLAlchemyExternalActionRepository,
            connector_receipts=SQLAlchemyConnectorReceiptRepository,
            execution_control=lambda session: PermitRecordingExecutionControlRepository(
                session, permits
            ),
        ),
    )
    return OrchestrationDependencies(clock, UnusedIds(), unit_of_work)


async def _control_and_write_step(
    dependencies: OrchestrationDependencies,
    action: ExternalAction,
):  # type: ignore[no-untyped-def]
    async with dependencies.unit_of_work() as unit_of_work:
        control = await unit_of_work.execution_control.get(action.run_id)
        step = await unit_of_work.run_steps.get(action.step_id)
    assert control is not None and step is not None
    return control, step


def _execution_control_material(record: RunExecutionControlRecord) -> dict[str, object]:
    def timestamp(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat(timespec="microseconds")

    return {
        "run_id": record.run_id,
        "policy_hash": record.policy_hash,
        "run_timeout_seconds": record.run_timeout_seconds,
        "max_model_calls": record.max_model_calls,
        "max_tool_calls": record.max_tool_calls,
        "model_calls": record.model_calls,
        "tool_calls": record.tool_calls,
        "started_at": timestamp(record.started_at),
        "deadline_at": timestamp(record.deadline_at),
        "cancel_requested_at": timestamp(record.cancel_requested_at),
        "cancel_actor_digest": record.cancel_actor_digest,
        "created_at": timestamp(record.created_at),
        "updated_at": timestamp(record.updated_at),
        "version": record.version,
    }


class SecretFailureGateway:
    def __init__(self, delegate: ExternalWriteConnectorGateway, secret: str) -> None:
        self.delegate = delegate
        self.secret = secret

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, _authorization):  # type: ignore[no-untyped-def]
        raise RuntimeError(self.secret)


class CompletionFaultExternalActions:
    def __init__(self, delegate) -> None:  # type: ignore[no-untyped-def]
        self.delegate = delegate

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.delegate, name)

    async def mark_outcome_unknown(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("sanitized completion persistence fault")


class CompletionFaultUnitOfWork:
    def __init__(self, delegate) -> None:  # type: ignore[no-untyped-def]
        self.delegate = delegate

    @property
    def works(self):  # type: ignore[no-untyped-def]
        return self.delegate.works

    @property
    def runs(self):  # type: ignore[no-untyped-def]
        return self.delegate.runs

    @property
    def audits(self):  # type: ignore[no-untyped-def]
        return self.delegate.audits

    @property
    def approvals(self):  # type: ignore[no-untyped-def]
        return self.delegate.approvals

    @property
    def run_steps(self):  # type: ignore[no-untyped-def]
        return self.delegate.run_steps

    @property
    def execution_control(self):  # type: ignore[no-untyped-def]
        return self.delegate.execution_control

    @property
    def external_actions(self):  # type: ignore[no-untyped-def]
        return CompletionFaultExternalActions(self.delegate.external_actions)

    @property
    def connector_receipts(self):  # type: ignore[no-untyped-def]
        return self.delegate.connector_receipts

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        await self.delegate.__aenter__()
        return self

    async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
        return await self.delegate.__aexit__(*args)

    async def commit(self) -> None:
        await self.delegate.commit()

    async def rollback(self) -> None:
        await self.delegate.rollback()


class CompletionFaultUnitOfWorkFactory:
    def __init__(self, delegate) -> None:  # type: ignore[no-untyped-def]
        self.delegate = delegate

    def __call__(self):  # type: ignore[no-untyped-def]
        return CompletionFaultUnitOfWork(self.delegate())


class AppendThenFaultAuditRepository:
    def __init__(self, delegate) -> None:  # type: ignore[no-untyped-def]
        self.delegate = delegate

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.delegate, name)

    async def append_many(self, events):  # type: ignore[no-untyped-def]
        await self.delegate.append_many(events)
        raise RuntimeError("injected terminal audit fault")


class AuditAppendFaultUnitOfWork:
    def __init__(self, delegate) -> None:  # type: ignore[no-untyped-def]
        self.delegate = delegate

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.delegate, name)

    @property
    def audits(self):  # type: ignore[no-untyped-def]
        return AppendThenFaultAuditRepository(self.delegate.audits)

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        await self.delegate.__aenter__()
        return self

    async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
        return await self.delegate.__aexit__(*args)


class AuditAppendFaultUnitOfWorkFactory:
    def __init__(self, delegate) -> None:  # type: ignore[no-untyped-def]
        self.delegate = delegate

    def __call__(self):  # type: ignore[no-untyped-def]
        return AuditAppendFaultUnitOfWork(self.delegate())


class TrackingUnitOfWork:
    def __init__(self, delegate, tracker):  # type: ignore[no-untyped-def]
        self.delegate = delegate
        self.tracker = tracker

    @property
    def works(self):  # type: ignore[no-untyped-def]
        return self.delegate.works

    @property
    def runs(self):  # type: ignore[no-untyped-def]
        return self.delegate.runs

    @property
    def audits(self):  # type: ignore[no-untyped-def]
        return self.delegate.audits

    @property
    def approvals(self):  # type: ignore[no-untyped-def]
        return self.delegate.approvals

    @property
    def run_steps(self):  # type: ignore[no-untyped-def]
        return self.delegate.run_steps

    @property
    def execution_control(self):  # type: ignore[no-untyped-def]
        return self.delegate.execution_control

    @property
    def external_actions(self):  # type: ignore[no-untyped-def]
        return self.delegate.external_actions

    @property
    def connector_receipts(self):  # type: ignore[no-untyped-def]
        return self.delegate.connector_receipts

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        await self.delegate.__aenter__()
        self.tracker.active += 1
        return self

    async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
        try:
            return await self.delegate.__aexit__(*args)
        finally:
            self.tracker.active -= 1

    async def commit(self) -> None:
        await self.delegate.commit()

    async def rollback(self) -> None:
        await self.delegate.rollback()


class TrackingUnitOfWorkFactory:
    def __init__(self, delegate):  # type: ignore[no-untyped-def]
        self.delegate = delegate
        self.active = 0

    def __call__(self):  # type: ignore[no-untyped-def]
        return TrackingUnitOfWork(self.delegate(), self)


class AssertNoActiveUnitOfWorkGateway:
    def __init__(self, delegate, tracker):  # type: ignore[no-untyped-def]
        self.delegate = delegate
        self.tracker = tracker

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        assert self.tracker.active == 0
        return await self.delegate.execute(authorization)


class TamperedResponseGateway:
    def __init__(self, delegate: ExternalWriteConnectorGateway) -> None:
        self.delegate = delegate

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        result = await self.delegate.execute(authorization)
        return ConnectorWriteResult(
            receipt_id=result.receipt_id,
            status=result.status,
            safe_metadata={"token": "RUN05_SECRET_CANARY" * 512},
        )


class MalformedResponseGateway:
    def __init__(self, delegate: ExternalWriteConnectorGateway) -> None:
        self.delegate = delegate

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        result = await self.delegate.execute(authorization)
        return ConnectorWriteResult.model_construct(
            receipt_id=result.receipt_id,
            status=result.status,
            safe_metadata=cast(dict[str, JsonValue], {"invalid": object()}),
        )


class _MalformedCommunityConnectorResponse:
    def __init__(self, delegate: object, corruption: str) -> None:
        self._delegate = delegate
        self._corruption = corruption

    async def send_message(self, request):  # type: ignore[no-untyped-def]
        result = await self._delegate.send_message(request)  # type: ignore[attr-defined]
        if self._corruption == "invalid_metadata":
            return ConnectorWriteResult.model_construct(
                receipt_id=result.receipt_id,
                status=result.status,
                safe_metadata={"invalid": object()},
            )

        def hostile_dump(**_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("api-08-provider-secret-canary")

        object.__setattr__(result, "model_dump", hostile_dump)
        return result

    async def share_material(self, request):  # type: ignore[no-untyped-def]
        return await self._delegate.share_material(request)  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("invalid_metadata", "hostile_model_dump"))
async def test_api_08_registry_gateway_rejects_malformed_write_result_after_one_effect(
    tmp_path: Path,
    corruption: str,
) -> None:
    runtime = await _runtime(tmp_path / f"api-08-malformed-write-result-{corruption}.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=1042)
    ledger = DurableMockReceiptLedger(_uow_factory(runtime), clock)
    original = MockConnectorBundle.create(REGISTRY, ledger)
    bundle = replace(
        original,
        community=_MalformedCommunityConnectorResponse(  # type: ignore[arg-type]
            original.community,
            corruption,
        ),
    )
    gateway = RegistryConnectorWriteGateway(
        REGISTRY,
        bundle,
        binding_configuration_revisions={COMMUNITY_BINDING: 1},
    )
    try:
        with pytest.raises(ConnectorDeliveryFailure) as captured:
            await gateway.execute(_authorize_action(action, action.idempotency_key))
        assert captured.value.code == "schema_invalid_response"
        assert captured.value.request_may_have_left_process is True
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert "provider-secret-canary" not in str(captured.value)
        assert "provider-secret-canary" not in repr(captured.value)
        assert ledger.side_effect_count == 1
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_two_dispatch_workers_make_exactly_one_connector_call(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "dispatch-race.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=102)
    gateway, ledger = _gateway(runtime, clock)
    counted = CountingGateway(gateway)
    first = ExternalActionDispatcher(
        _dependencies(runtime, clock), counted, WriteAuthorizationGuard()
    )
    second = ExternalActionDispatcher(
        _dependencies(runtime, clock), counted, WriteAuthorizationGuard()
    )
    try:
        outcomes = await asyncio.gather(
            first.dispatch_once(action.id, lease_owner="worker.race.1"),
            second.dispatch_once(action.id, lease_owner="worker.race.2"),
            return_exceptions=True,
        )
        successes = [
            item
            for item in outcomes
            if not isinstance(item, BaseException)
            and item.disposition is DispatchDisposition.SUCCEEDED
        ]
        assert len(successes) == 1
        assert counted.calls == 1
        assert ledger.side_effect_count == 1
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_connector_await_has_no_dispatcher_transaction_open(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "transaction-lifetime.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=103)
    gateway, _ = _gateway(runtime, clock)
    tracker = TrackingUnitOfWorkFactory(_uow_factory(runtime))
    dependencies = OrchestrationDependencies(clock, UnusedIds(), tracker)
    dispatcher = ExternalActionDispatcher(
        dependencies,
        AssertNoActiveUnitOfWorkGateway(gateway, tracker),
        WriteAuthorizationGuard(),
    )
    try:
        result = await dispatcher.dispatch_once(action.id, lease_owner="worker.tx")
        assert result.disposition is DispatchDisposition.SUCCEEDED
        assert tracker.active == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_completion_uses_authoritative_receipt_metadata(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "authoritative-receipt.db")
    clock = MutableClock()
    action = await _released_action(
        runtime,
        clock,
        seed=104,
        templates=_runtime_templates(max_output_bytes=4_096),
    )
    gateway, _ = _gateway(runtime, clock)
    dispatcher = ExternalActionDispatcher(
        _dependencies(runtime, clock),
        TamperedResponseGateway(gateway),
        WriteAuthorizationGuard(),
    )
    try:
        completed = await dispatcher.dispatch_once(action.id, lease_owner="worker.metadata")
        assert completed.disposition is DispatchDisposition.SUCCEEDED
        assert completed.action.result is not None
        assert completed.action.result.safe_metadata["mode"] == "mock"
        assert "RUN05_SECRET_CANARY" not in repr(completed.action.result.safe_metadata)
        async with runtime.session_factory() as session:
            row = await session.get(ExternalActionRecord, action.id)
            assert row is not None
            assert "RUN05_SECRET_CANARY" not in repr(row.connector_safe_metadata)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_malformed_gateway_output_cannot_corrupt_the_action_result(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-malformed-gateway-output.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=1041)
    gateway, _ = _gateway(runtime, clock)
    try:
        completed = await ExternalActionDispatcher(
            _dependencies(runtime, clock),
            MalformedResponseGateway(gateway),
            WriteAuthorizationGuard(),
        ).dispatch_once(action.id, lease_owner="worker.orch-06.malformed-output")
        assert completed.disposition is DispatchDisposition.SUCCEEDED
        assert completed.action.result is not None
        assert completed.action.result.safe_metadata["mode"] == "mock"
        assert "invalid" not in completed.action.result.safe_metadata
        async with runtime.session_factory() as session:
            row = await session.get(ExternalActionRecord, action.id)
        assert row is not None
        assert row.connector_safe_metadata == dict(completed.action.result.safe_metadata)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_oversized_durable_receipt_keeps_success_with_bounded_projection(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-bounded-receipt-output.db")
    clock = MutableClock()
    max_output_bytes = 4_096
    action = await _released_action(
        runtime,
        clock,
        seed=105,
        templates=_runtime_templates(max_output_bytes=max_output_bytes),
    )
    dependencies = _dependencies(runtime, clock)
    oversized_metadata = {"provider_payload": "x" * (max_output_bytes * 2)}
    try:
        async with dependencies.unit_of_work() as unit_of_work:
            authority = await _release_authority(unit_of_work, action.id)
            claimed = await _claim_reserved(
                unit_of_work,
                action,
                clock,
                lease_owner="worker.orch-06.bounded-output",
                lease_seconds=1,
                authority=authority,
            )
            assert claimed is not None
            marked = await _mark_call_started(
                unit_of_work,
                claimed,
                clock,
                lease_owner="worker.orch-06.bounded-output",
                authority=authority,
                call_deadline_at=clock.now() + timedelta(seconds=2),
            )
            assert marked is not None and marked.call_deadline_at is not None
            stored = await unit_of_work.connector_receipts.add_or_get(
                ConnectorActionReceipt(
                    external_action_id=marked.id,
                    connector_binding_id=marked.connector_binding_id,
                    idempotency_key=marked.idempotency_key,
                    action_hash=marked.action_hash,
                    capability_id=marked.envelope.capability_id,
                    receipt_id="receipt.orch-06.oversized-output",
                    status="mock_succeeded",
                    safe_metadata=oversized_metadata,
                    created_at=clock.now(),
                )
            )
            assert stored.inserted
            await unit_of_work.commit()

        gateway = NoReceiptUncertainGateway()
        clock.current = marked.call_deadline_at
        recovered = await ExternalActionDispatcher(
            dependencies,
            gateway,
            WriteAuthorizationGuard(),
        ).recover_stale(
            lease_owner="worker.orch-06.bounded-output.recovery",
            limit=1,
        )
        assert len(recovered) == 1
        assert recovered[0].disposition is DispatchDisposition.SUCCEEDED
        assert recovered[0].action.result is not None
        assert recovered[0].action.result.receipt_id == "receipt.orch-06.oversized-output"
        assert recovered[0].action.result.status == "mock_succeeded"
        assert recovered[0].action.result.safe_metadata == {"omitted": "output_payload_too_large"}
        assert (
            canonical_payload_size_bytes(recovered[0].action.result.safe_metadata)
            <= max_output_bytes
        )
        assert "provider_payload" not in recovered[0].action.result.safe_metadata
        assert gateway.calls == 0

        async with dependencies.unit_of_work() as unit_of_work:
            receipt = await unit_of_work.connector_receipts.get(
                action.connector_binding_id,
                action.idempotency_key,
            )
        assert receipt is not None
        assert receipt.external_action_id == action.id
        assert receipt.receipt_id == "receipt.orch-06.oversized-output"
        assert receipt.status == "mock_succeeded"
    finally:
        await runtime.dispose()


def test_run_05_dispatch_gateway_rejects_process_local_receipt_ledger() -> None:
    with pytest.raises(ConnectorBundleConfigurationError, match="durable"):
        RegistryConnectorWriteGateway(
            REGISTRY,
            MockConnectorBundle.create(REGISTRY),
            binding_configuration_revisions={COMMUNITY_BINDING: 1},
        )


@pytest.mark.asyncio
async def test_run_05_gateway_suppresses_validation_and_provider_secret_causes(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "gateway-redaction.db")
    clock = MutableClock()
    released = await _released_action(runtime, clock, seed=105)
    gateway, _ = _gateway(runtime, clock)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            action = cast(
                ExternalAction,
                await unit_of_work.external_actions.get(released.id),
            )
        secret = "RUN05_SECRET_VALIDATION_CANARY"
        invalid_envelope = action.envelope.model_copy(
            update={
                "minimized_payload": {
                    "recipient_refs": secret,
                    "body": "invalid command",
                }
            }
        )
        invalid_reservation = ApprovalReservation(
            reservation_id="reservation.invalid-payload",
            authorization_set_id=invalid_envelope.authorization_set_id,
            state="dispatch_reserved",
            action_id=invalid_envelope.action_id,
            action_hash=canonical_action_hash(invalid_envelope),
            capability_id=invalid_envelope.capability_id,
            binding_id=invalid_envelope.binding_id,
            approval_request_id="approval-request.invalid-payload",
            approval_decision_id="approval-decision.invalid-payload",
            idempotency_key=action.idempotency_key,
            reserved_at=NOW,
        )
        invalid_authorization = WriteAuthorizationGuard().authorize(
            invalid_envelope, invalid_reservation, action.idempotency_key
        )
        with pytest.raises(ConnectorDeliveryFailure) as validation_failure:
            await gateway.execute(invalid_authorization)
        assert validation_failure.value.code == "connector_request_rejected"
        assert validation_failure.value.__cause__ is None
        assert validation_failure.value.__context__ is None
        assert secret not in str(validation_failure.value)
        assert secret not in repr(validation_failure.value)

        provider_secret = "RUN05_SECRET_PROVIDER_BODY"

        async def leak_provider_body(_command):  # type: ignore[no-untyped-def]
            raise RuntimeError(provider_secret)

        gateway._bundle.community.send_message = leak_provider_body  # type: ignore[method-assign]
        with pytest.raises(ConnectorDeliveryFailure) as provider_failure:
            await gateway.execute(_authorize_action(action, action.idempotency_key))
        assert provider_failure.value.code == "connector_delivery_uncertain"
        assert provider_failure.value.__cause__ is None
        assert provider_failure.value.__context__ is None
        assert provider_secret not in str(provider_failure.value)
        assert provider_secret not in repr(provider_failure.value)
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_provider_secret_is_not_context_for_completion_fault(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "completion-fault-redaction.db")
    clock = MutableClock()
    action = await _released_action(
        runtime,
        clock,
        seed=106,
        delivery_attempt_limit=1,
    )
    gateway, _ = _gateway(runtime, clock)
    secret = "RUN05_SECRET_PROVIDER_CONTEXT"
    fault_factory = CompletionFaultUnitOfWorkFactory(_uow_factory(runtime))
    dispatcher = ExternalActionDispatcher(
        OrchestrationDependencies(clock, UnusedIds(), fault_factory),  # type: ignore[arg-type]
        SecretFailureGateway(gateway, secret),
        WriteAuthorizationGuard(),
    )
    try:
        with pytest.raises(RuntimeError, match="sanitized completion") as fault:
            await dispatcher.dispatch_once(action.id, lease_owner="worker.fault")
        assert fault.value.__cause__ is None
        assert fault.value.__context__ is None
        assert secret not in str(fault.value)
        assert secret not in repr(fault.value)
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_durable_receipt_race_restart_and_collision_guards(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "receipt-race.db")
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    registered = await registration.register_plan_actions(_multi_plan(seed=1))
    first = registered.actions[0].action
    second = registered.actions[1].action
    ledger = DurableMockReceiptLedger(_uow_factory(runtime), clock)
    kwargs = {
        "external_action_id": first.id,
        "binding_id": first.connector_binding_id,
        "idempotency_key": first.idempotency_key,
        "action_hash": first.action_hash,
        "capability_id": first.envelope.capability_id,
    }
    try:
        receipts = await asyncio.gather(ledger.record(**kwargs), ledger.record(**kwargs))
        assert receipts[0] == receipts[1]
        assert ledger.side_effect_count == 1
        assert await _counts(runtime) == (2, 1)

        restarted = DurableMockReceiptLedger(_uow_factory(runtime), clock)
        replay = await restarted.record(**kwargs)
        assert replay == receipts[0]
        assert restarted.side_effect_count == 0

        with pytest.raises(ConnectorPortError) as same_key:
            await restarted.record(
                external_action_id=second.id,
                binding_id=first.connector_binding_id,
                idempotency_key=first.idempotency_key,
                action_hash=second.action_hash,
                capability_id=second.envelope.capability_id,
            )
        assert same_key.value.code == "idempotency_conflict"
        assert same_key.value.__cause__ is None
        assert same_key.value.__context__ is None
        with pytest.raises(ConnectorPortError) as same_action:
            await restarted.record(
                external_action_id=first.id,
                binding_id=first.connector_binding_id,
                idempotency_key="action-idempotency-v1:" + "f" * 64,
                action_hash=first.action_hash,
                capability_id=first.envelope.capability_id,
            )
        assert same_action.value.code == "idempotency_conflict"
        assert await _counts(runtime) == (2, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_receipt_replay_requires_exact_result_projection(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "receipt-result-parity.db")
    await _seed_parent(runtime)
    clock = MutableClock()
    registered = await ExternalActionRegistrationService(
        _dependencies(runtime, clock)
    ).register_plan_actions(_plan(seed=1))
    action = registered.actions[0].action
    ledger = DurableMockReceiptLedger(_uow_factory(runtime), clock)
    await ledger.record(
        external_action_id=action.id,
        binding_id=action.connector_binding_id,
        idempotency_key=action.idempotency_key,
        action_hash=action.action_hash,
        capability_id=action.envelope.capability_id,
    )
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            stored = await unit_of_work.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
        assert stored is not None
        drifts = (
            replace(stored, receipt_id="receipt.result-drift"),
            replace(stored, status="result-drift"),
            replace(stored, safe_metadata={"mode": "drifted"}),
        )
        for candidate in drifts:
            async with _uow_factory(runtime)() as unit_of_work:
                with pytest.raises(ExternalActionPersistenceConflict) as conflict:
                    await unit_of_work.connector_receipts.add_or_get(candidate)
                assert conflict.value.code == "connector_receipt_collision"
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_mapper_rejects_scalar_identity_drift(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "action-identity-drift.db")
    await _seed_parent(runtime)
    registered = await ExternalActionRegistrationService(
        _dependencies(runtime, MutableClock())
    ).register_plan_actions(_plan(seed=1))
    action = registered.actions[0].action
    try:
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(ExternalActionRecord)
                .where(ExternalActionRecord.id == action.id)
                .values(step_id="runtime-step.tampered")
            )
        async with _uow_factory(runtime)() as unit_of_work:
            with pytest.raises(ExternalActionPersistenceConflict) as conflict:
                await unit_of_work.external_actions.get(action.id)
            assert conflict.value.code == "action_identity_corrupt"
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_attempt_bound_is_enforced_by_domain_and_database(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "attempt-bound.db")
    await _seed_parent(runtime)
    registered = await ExternalActionRegistrationService(
        _dependencies(runtime, MutableClock())
    ).register_plan_actions(_plan(seed=1))
    action = registered.actions[0].action
    try:
        with pytest.raises(ValueError, match="cannot exceed"):
            replace(
                action,
                delivery_attempt_limit=MAX_DELIVERY_ATTEMPTS + 1,
            )
        with pytest.raises(IntegrityError):
            async with runtime.session_factory() as session, session.begin():
                await session.execute(
                    update(ExternalActionRecord)
                    .where(ExternalActionRecord.id == action.id)
                    .values(delivery_attempt_limit=MAX_DELIVERY_ATTEMPTS + 1)
                )
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_reservation_snapshot_blocks_action_hash_rewrite(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "reservation-tamper.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=107)
    _, ledger = _gateway(runtime, clock)
    try:
        with pytest.raises(IntegrityError):
            async with runtime.session_factory() as session, session.begin():
                await session.execute(
                    update(ExternalActionRecord)
                    .where(ExternalActionRecord.id == action.id)
                    .values(action_hash="f" * 64)
                )
        assert ledger.side_effect_count == 0
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_final_attempt_lost_response_reconciles_durable_receipt(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "lost-response.db")
    clock = MutableClock()
    action = await _released_action(
        runtime,
        clock,
        seed=108,
        delivery_attempt_limit=1,
    )
    gateway, ledger = _gateway(runtime, clock)
    lost = LostResponseGateway(gateway)
    dispatcher = ExternalActionDispatcher(
        _dependencies(runtime, clock), lost, WriteAuthorizationGuard()
    )
    try:
        completed = await dispatcher.dispatch_once(action.id, lease_owner="worker.crash")
        assert completed.disposition is DispatchDisposition.SUCCEEDED
        assert completed.action.state is ExternalActionState.SUCCEEDED
        assert completed.action.result is not None
        assert ledger.side_effect_count == 1
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_stale_crash_after_receipt_reconciles_without_provider_call(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "receipt-before-crash.db")
    clock = MutableClock()
    action = await _released_action(
        runtime,
        clock,
        seed=109,
        delivery_attempt_limit=1,
    )
    gateway, ledger = _gateway(runtime, clock)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            claimed = await _claim_reserved(
                unit_of_work,
                current,
                clock,
                lease_owner="worker.crashed-after-receipt",
                lease_seconds=1,
            )
            assert claimed is not None
            marked = await _mark_call_started(
                unit_of_work,
                claimed,
                clock,
                lease_owner="worker.crashed-after-receipt",
            )
            assert marked is not None
            await unit_of_work.commit()

        await gateway.execute(_authorize_action(marked, marked.idempotency_key))
        assert ledger.side_effect_count == 1
        assert await _counts(runtime) == (1, 1)

        clock.tick(2)
        counted = CountingGateway(gateway)
        dispatcher = ExternalActionDispatcher(
            _dependencies(runtime, clock), counted, WriteAuthorizationGuard()
        )
        recovered = await dispatcher.recover_stale(lease_owner="worker.recovery")
        assert len(recovered) == 1
        assert recovered[0].disposition is DispatchDisposition.SUCCEEDED
        assert recovered[0].action.state is ExternalActionState.SUCCEEDED
        assert counted.calls == 0
        assert ledger.side_effect_count == 1
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_exhausted_pre_call_claim_fails_without_connector_call(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "pre-call-exhausted.db")
    clock = MutableClock()
    action = await _released_action(
        runtime,
        clock,
        seed=110,
        delivery_attempt_limit=1,
    )
    gateway, ledger = _gateway(runtime, clock)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(
                ExternalAction,
                await unit_of_work.external_actions.get(action.id),
            )
            claimed = await _claim_reserved(
                unit_of_work,
                current,
                clock,
                lease_owner="worker.crashed-before-call",
                lease_seconds=1,
            )
            assert claimed is not None
            assert claimed.state is ExternalActionState.DISPATCHING
            assert claimed.version == current.version + 1
            await unit_of_work.commit()

        clock.tick(2)
        dispatcher = ExternalActionDispatcher(
            _dependencies(runtime, clock), gateway, WriteAuthorizationGuard()
        )
        recovered = await dispatcher.recover_stale(lease_owner="worker.recovery", limit=1)
        assert recovered[0].disposition is DispatchDisposition.FAILED
        assert recovered[0].action.terminal_reason_code == "pre_call_attempts_exhausted"
        assert ledger.side_effect_count == 0
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_unavailable_post_call_stale_never_blind_retries(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "unavailable-stale.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=111)
    async with runtime.session_factory() as session, session.begin():
        await session.execute(
            update(ExternalActionRecord)
            .where(ExternalActionRecord.id == action.id)
            .values(idempotency_support="unavailable")
        )
    gateway, ledger = _gateway(runtime, clock)
    counted = CountingGateway(gateway)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            claimed = await _claim_reserved(
                unit_of_work,
                current,
                clock,
                lease_owner="worker.unavailable",
                lease_seconds=1,
            )
            assert claimed is not None
            marked = await _mark_call_started(
                unit_of_work,
                claimed,
                clock,
                lease_owner="worker.unavailable",
            )
            assert marked is not None
            await unit_of_work.commit()
        clock.tick(2)
        async with _uow_factory(runtime)() as unit_of_work:
            malicious = await unit_of_work.external_actions.release_stale_for_retry(
                action_id=action.id,
                expected_version=cast(ExternalAction, marked).version,
                attempt_number=cast(ExternalAction, marked).delivery_attempt_count,
                occurred_at=clock.now(),
                conclusion="provider_retry",
            )
            assert malicious is None

        dispatcher = ExternalActionDispatcher(
            _dependencies(runtime, clock), counted, WriteAuthorizationGuard()
        )
        recovered = await dispatcher.recover_stale(lease_owner="worker.recovery", limit=1)
        assert recovered[0].disposition is DispatchDisposition.OUTCOME_UNKNOWN
        assert recovered[0].action.state is ExternalActionState.OUTCOME_UNKNOWN
        assert counted.calls == 0
        assert ledger.side_effect_count == 0
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_stale_attempt_cannot_overwrite_new_lease_generation(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "lease-generation.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=112)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            first = await _claim_reserved(
                unit_of_work,
                current,
                clock,
                lease_owner="worker.generation.1",
                lease_seconds=1,
            )
            assert first is not None
            first_marked = await _mark_call_started(
                unit_of_work,
                first,
                clock,
                lease_owner="worker.generation.1",
            )
            assert first_marked is not None
            await unit_of_work.commit()
        clock.tick(2)
        async with _uow_factory(runtime)() as unit_of_work:
            released = await unit_of_work.external_actions.release_stale_for_retry(
                action_id=action.id,
                expected_version=cast(ExternalAction, first_marked).version,
                attempt_number=1,
                occurred_at=clock.now(),
                conclusion="provider_retry",
            )
            assert released is not None
            await unit_of_work.commit()
        async with _uow_factory(runtime)() as unit_of_work:
            second = await _claim_reserved(
                unit_of_work,
                cast(ExternalAction, released),
                clock,
                lease_owner="worker.generation.2",
                lease_seconds=10,
            )
            assert second is not None
            second_marked = await _mark_call_started(
                unit_of_work,
                second,
                clock,
                lease_owner="worker.generation.2",
            )
            assert second_marked is not None
            await unit_of_work.commit()
        async with _uow_factory(runtime)() as unit_of_work:
            stale_completion = await unit_of_work.external_actions.complete_failed(
                action_id=action.id,
                expected_version=cast(ExternalAction, first_marked).version,
                lease_owner="worker.generation.1",
                attempt_number=1,
                reason_code="stale_worker_failure",
                occurred_at=clock.now(),
            )
            assert stale_completion is None
            latest = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            assert latest.state is ExternalActionState.DISPATCHING
            assert latest.lease is not None
            assert latest.lease.owner == "worker.generation.2"
            assert latest.delivery_attempt_count == 2
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_05_cancelled_parent_blocks_claim_and_pre_call_marker(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "cancel-fences.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=113)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            authority = await _release_authority(unit_of_work, action.id)
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(RunRecord)
                .where(RunRecord.id == action.run_id)
                .values(
                    state="cancelled",
                    terminal_reason_code="operator_cancelled",
                    updated_at=clock.now(),
                    version=authority.released_run_version + 1,
                )
            )
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            claimed = await _claim_reserved(
                unit_of_work,
                current,
                clock,
                lease_owner="worker.cancelled",
                lease_seconds=10,
                authority=authority,
            )
            assert claimed is None
        async with runtime.session_factory() as session:
            row = await session.get(ExternalActionRecord, action.id)
            assert row is not None
            assert row.state == ExternalActionState.DISPATCH_RESERVED.value
            assert row.delivery_attempt_count == 0

        call_start_action = await _released_action(runtime, clock, seed=114)
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(
                ExternalAction,
                await unit_of_work.external_actions.get(call_start_action.id),
            )
            claimed = await _claim_reserved(
                unit_of_work,
                current,
                clock,
                lease_owner="worker.claimed",
                lease_seconds=10,
            )
            assert claimed is not None
            await unit_of_work.commit()
        async with _uow_factory(runtime)() as unit_of_work:
            call_start_authority = await _release_authority(
                unit_of_work,
                call_start_action.id,
            )
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(RunRecord)
                .where(RunRecord.id == call_start_action.run_id)
                .values(
                    state="cancelled",
                    terminal_reason_code="operator_cancelled_before_call",
                    updated_at=clock.now(),
                    version=call_start_authority.released_run_version + 1,
                )
            )
        async with _uow_factory(runtime)() as unit_of_work:
            blocked = await _mark_call_started(
                unit_of_work,
                cast(ExternalAction, claimed),
                clock,
                lease_owner="worker.claimed",
                authority=call_start_authority,
            )
            assert blocked is None
        assert await _counts(runtime) == (2, 0)
    finally:
        await runtime.dispose()


def _runtime_templates(
    *,
    step_timeout_seconds: int | None = None,
    rate_max_calls: int | None = None,
    max_input_bytes: int | None = None,
    max_input_field_bytes: int | None = None,
    max_output_bytes: int | None = None,
) -> tuple[object, ...]:
    templates: list[object] = []
    for item in CATALOG.templates:
        if item.id != WORKER_TEMPLATE:
            templates.append(item)
            continue
        updates: dict[str, object] = {}
        if step_timeout_seconds is not None:
            updates["timeout_policy"] = item.timeout_policy.model_copy(
                update={"step_seconds": step_timeout_seconds}
            )
        if rate_max_calls is not None:
            updates["rate_limit_policy"] = item.rate_limit_policy.model_copy(
                update={"max_calls": rate_max_calls}
            )
        budget_updates = {
            key: value
            for key, value in {
                "max_input_bytes": max_input_bytes,
                "max_input_field_bytes": max_input_field_bytes,
                "max_output_bytes": max_output_bytes,
            }.items()
            if value is not None
        }
        if budget_updates:
            updates["budget_policy"] = item.budget_policy.model_copy(update=budget_updates)
        templates.append(item.model_copy(update=updates))
    return tuple(templates)


def _operations_with_timeout(timeout_seconds: int) -> tuple[object, ...]:
    return tuple(
        replace(
            item,
            metadata=replace(
                item.metadata,
                default_timeout_seconds=timeout_seconds,
            ),
        )
        if item.metadata.capability_id == "cap.messaging.send-message"
        else item
        for item in REGISTRY.operations
    )


@pytest.mark.asyncio
async def test_orch_06_write_permit_requires_exact_step_action_and_control_binding(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-write-permit-binding.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=180)
    dependencies = _dependencies(runtime, clock)
    try:
        control, step = await _control_and_write_step(dependencies, action)
        with pytest.raises(ExecutionControlRepositoryConflict) as generic_rejected:
            async with dependencies.unit_of_work() as unit_of_work:
                await unit_of_work.execution_control.reserve_attempt(
                    AttemptReservationCommand(
                        attempt_id="execution-attempt.orch-06.write-rejected",
                        run_id=action.run_id,
                        step_id=action.step_id,
                        operation_key=step.runtime_policy.operation_key,
                        expected_control_version=control.version,
                        expected_step_version=step.version,
                        reserved_at=clock.now(),
                    )
                )
        assert generic_rejected.value.code == "step_fence_invalid"

        async with dependencies.unit_of_work() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            authority = await _release_authority(unit_of_work, action.id)
            claimed = await _claim_reserved(
                unit_of_work,
                current,
                clock,
                lease_owner="worker.orch-06.binding",
                lease_seconds=30,
                authority=authority,
            )
            assert claimed is not None
            await unit_of_work.commit()

        base = DeliveryCallReservationCommand(
            run_id=claimed.run_id,
            step_id=claimed.step_id,
            action_id=claimed.id,
            delivery_attempt_number=claimed.delivery_attempt_count,
            expected_control_version=control.version,
            expected_step_version=authority.step_version,
            expected_action_version=claimed.version,
            reserved_at=clock.now(),
        )
        rejected_commands = (
            (
                replace(base, expected_control_version=base.expected_control_version + 1),
                "stale_execution_control",
            ),
            (replace(base, step_id="run-step.orch-06.wrong"), "delivery_step_fence_invalid"),
            (
                replace(base, action_id="external-action.orch-06.wrong"),
                "delivery_action_fence_invalid",
            ),
            (
                replace(base, expected_action_version=base.expected_action_version + 1),
                "delivery_action_fence_invalid",
            ),
        )
        for command, expected_code in rejected_commands:
            with pytest.raises(ExecutionControlRepositoryConflict) as rejected:
                async with dependencies.unit_of_work() as unit_of_work:
                    await unit_of_work.execution_control.reserve_delivery_call(command)
            assert rejected.value.code == expected_code

        async with dependencies.unit_of_work() as unit_of_work:
            exact = await unit_of_work.execution_control.reserve_delivery_call(base)
        assert exact.permit.run_id == claimed.run_id
        assert exact.permit.step_id == claimed.step_id
        assert exact.permit.action_id == claimed.id
        assert exact.permit.source_control_version == control.version
        assert exact.permit.source_step_version == authority.step_version
        assert exact.permit.source_action_version == claimed.version
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_first_write_call_consumes_one_logical_tool_and_rate_slot(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-first-write-accounting.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=181)
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    gateway, ledger = _gateway(runtime, clock)
    try:
        before, step = await _control_and_write_step(dependencies, action)
        window_start = fixed_window_start(
            clock.now(), step.runtime_policy.rate_limit.window_seconds
        )
        async with dependencies.unit_of_work() as unit_of_work:
            before_window = await unit_of_work.execution_control.get_rate_window(
                step.runtime_policy.rate_limit.scope,
                step.runtime_policy.rate_limit.key,
                window_start,
            )

        result = await ExternalActionDispatcher(
            dependencies, gateway, WriteAuthorizationGuard()
        ).dispatch_once(action.id, lease_owner="worker.orch-06.first-call")
        assert result.disposition is DispatchDisposition.SUCCEEDED
        assert ledger.side_effect_count == 1
        assert len(permits) == 1
        permit = permits[0]
        after, _ = await _control_and_write_step(dependencies, action)
        async with dependencies.unit_of_work() as unit_of_work:
            after_window = await unit_of_work.execution_control.get_rate_window(
                permit.rate_limit_scope,
                permit.rate_limit_key,
                permit.rate_window_started_at,
            )
        assert permit.logical_budget_consumed is True
        assert after.tool_calls == before.tool_calls + 1
        assert after.model_calls == before.model_calls
        assert after_window is not None
        assert after_window.used == (0 if before_window is None else before_window.used) + 1
        assert permit.call_deadline_at == min(
            permit.reserved_at + timedelta(seconds=step.runtime_policy.timeout.step_seconds),
            permit.reserved_at + timedelta(seconds=action.delivery_contract.timeout_seconds),
            cast(datetime, before.deadline_at),
        )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_provider_recovery_consumes_rate_but_not_logical_tool_budget(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-provider-recovery-accounting.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=182, delivery_attempt_limit=2)
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    gateway = NoReceiptUncertainGateway()
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
        lease_duration=timedelta(seconds=1),
    )
    try:
        before, step = await _control_and_write_step(dependencies, action)
        before_window_start = fixed_window_start(
            clock.now(), step.runtime_policy.rate_limit.window_seconds
        )
        async with dependencies.unit_of_work() as unit_of_work:
            before_window = await unit_of_work.execution_control.get_rate_window(
                step.runtime_policy.rate_limit.scope,
                step.runtime_policy.rate_limit.key,
                before_window_start,
            )
        first = await dispatcher.dispatch_once(
            action.id, lease_owner="worker.orch-06.recovery.first"
        )
        assert first.disposition is DispatchDisposition.RECOVERY_PENDING
        assert len(permits) == 1
        clock.current = permits[0].call_deadline_at
        recovered = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.recovery.second",
            limit=1,
        )
        assert len(recovered) == 1
        assert gateway.calls == 2
        assert len(permits) == 2
        assert [permit.logical_budget_consumed for permit in permits] == [True, False]
        after, _ = await _control_and_write_step(dependencies, action)
        assert after.tool_calls == before.tool_calls + 1
        async with dependencies.unit_of_work() as unit_of_work:
            window = await unit_of_work.execution_control.get_rate_window(
                permits[0].rate_limit_scope,
                permits[0].rate_limit_key,
                permits[0].rate_window_started_at,
            )
        assert window is not None
        assert window.used == (0 if before_window is None else before_window.used) + 2
        assert permits[0].rate_window_started_at == permits[1].rate_window_started_at
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_stale_recovery_waits_for_durable_call_deadline(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-call-deadline-recovery-fence.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=183, delivery_attempt_limit=2)
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    delegate, ledger = _gateway(runtime, clock)
    gateway = BlockingFirstGateway(delegate)
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
        lease_duration=timedelta(seconds=1),
    )
    first_dispatch: asyncio.Task[ExternalActionDispatchResult] | None = None
    try:
        first_dispatch = asyncio.create_task(
            dispatcher.dispatch_once(
                action.id,
                lease_owner="worker.orch-06.deadline.first",
            )
        )
        await asyncio.wait_for(gateway.first_started.wait(), timeout=2)

        assert gateway.calls == 1
        assert len(permits) == 1
        first_permit = permits[0]
        async with dependencies.unit_of_work() as unit_of_work:
            in_flight = await unit_of_work.external_actions.get(action.id)
        assert in_flight is not None and in_flight.lease is not None
        assert in_flight.call_started_at == first_permit.reserved_at
        assert in_flight.call_deadline_at == first_permit.call_deadline_at
        assert in_flight.lease.expires_at < first_permit.call_deadline_at
        async with runtime.session_factory() as session:
            first_attempt = await session.get(
                ExternalActionDispatchAttemptRecord,
                (action.id, 1),
            )
            assert first_attempt is not None
            assert first_attempt.call_started_at == first_permit.reserved_at
            assert first_attempt.call_deadline_at == first_permit.call_deadline_at
            assert first_attempt.completed_at is None
            assert first_attempt.conclusion is None

        clock.current = in_flight.lease.expires_at
        assert clock.now() < first_permit.call_deadline_at
        early = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.deadline.early",
            limit=1,
        )
        assert early == ()
        assert gateway.calls == 1
        assert not first_dispatch.done()
        async with dependencies.unit_of_work() as unit_of_work:
            unchanged = await unit_of_work.external_actions.get(action.id)
        assert unchanged == in_flight
        async with runtime.session_factory() as session:
            unchanged_attempt = await session.get(
                ExternalActionDispatchAttemptRecord,
                (action.id, 1),
            )
            assert unchanged_attempt is not None
            assert unchanged_attempt.completed_at is None
            assert unchanged_attempt.conclusion is None

        clock.current = first_permit.call_deadline_at
        recovered = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.deadline.recovery",
            limit=1,
        )
        assert len(recovered) == 1
        assert recovered[0].disposition is DispatchDisposition.SUCCEEDED
        assert recovered[0].action.state is ExternalActionState.SUCCEEDED
        assert recovered[0].action.call_started_at is None
        assert recovered[0].action.call_deadline_at is None
        assert gateway.calls == 2
        assert len(permits) == 2
        assert ledger.side_effect_count == 1
        async with runtime.session_factory() as session:
            attempts = tuple(
                (
                    await session.execute(
                        select(ExternalActionDispatchAttemptRecord)
                        .where(ExternalActionDispatchAttemptRecord.external_action_id == action.id)
                        .order_by(ExternalActionDispatchAttemptRecord.attempt_number)
                    )
                ).scalars()
            )
        assert len(attempts) == 2
        assert attempts[0].call_deadline_at == first_permit.call_deadline_at
        assert attempts[0].completed_at == first_permit.call_deadline_at
        assert attempts[0].conclusion == "provider_retry"
        assert attempts[1].call_deadline_at == permits[1].call_deadline_at
        assert attempts[1].conclusion == "succeeded"

        gateway.release_first.set()
        stale_completion = await first_dispatch
        assert stale_completion.disposition is DispatchDisposition.ALREADY_SUCCEEDED
        assert ledger.side_effect_count == 1
    finally:
        gateway.release_first.set()
        if first_dispatch is not None and not first_dispatch.done():
            first_dispatch.cancel()
            await asyncio.gather(first_dispatch, return_exceptions=True)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_recovery_rejects_action_attempt_call_deadline_drift(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-call-deadline-binding.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=184, delivery_attempt_limit=2)
    dependencies = _dependencies(runtime, clock)
    delegate, _ = _gateway(runtime, clock)
    gateway = CountingGateway(delegate)
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
        lease_duration=timedelta(seconds=1),
    )
    lease_owner = "worker.orch-06.deadline-binding"
    try:
        async with dependencies.unit_of_work() as unit_of_work:
            claimed = await _claim_reserved(
                unit_of_work,
                action,
                clock,
                lease_owner=lease_owner,
                lease_seconds=1,
            )
            assert claimed is not None
            call_deadline_at = clock.now() + timedelta(seconds=10)
            marked = await _mark_call_started(
                unit_of_work,
                claimed,
                clock,
                lease_owner=lease_owner,
                call_deadline_at=call_deadline_at,
            )
            assert marked is not None
            await unit_of_work.commit()

        drifted_deadline = call_deadline_at - timedelta(microseconds=1)
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(ExternalActionDispatchAttemptRecord)
                .where(
                    ExternalActionDispatchAttemptRecord.external_action_id == action.id,
                    ExternalActionDispatchAttemptRecord.attempt_number == 1,
                )
                .values(call_deadline_at=drifted_deadline)
            )

        clock.current = call_deadline_at
        with pytest.raises(ExternalActionPersistenceConflict) as conflict:
            await dispatcher.recover_stale(
                lease_owner="worker.orch-06.deadline-binding.recovery",
                limit=1,
            )
        assert conflict.value.code == "dispatch_attempt_corrupt"
        assert gateway.calls == 0

        async with runtime.session_factory() as session:
            action_record = await session.get(ExternalActionRecord, action.id)
            attempt_record = await session.get(
                ExternalActionDispatchAttemptRecord,
                (action.id, 1),
            )
        assert action_record is not None
        assert action_record.state == ExternalActionState.DISPATCHING.value
        assert action_record.version == marked.version
        assert action_record.connector_call_deadline_at == call_deadline_at
        assert attempt_record is not None
        assert attempt_record.call_deadline_at == drifted_deadline
        assert attempt_record.completed_at is None
        assert attempt_record.conclusion is None
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_cancelled_long_call_terminalizes_only_at_deadline(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-cancelled-long-call.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=185, delivery_attempt_limit=2)
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    delegate, ledger = _gateway(runtime, clock)
    gateway = BlockingFirstGateway(delegate)
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
        lease_duration=timedelta(seconds=1),
    )
    first_dispatch: asyncio.Task[ExternalActionDispatchResult] | None = None
    try:
        first_dispatch = asyncio.create_task(
            dispatcher.dispatch_once(
                action.id,
                lease_owner="worker.orch-06.cancelled-long-call",
            )
        )
        await asyncio.wait_for(gateway.first_started.wait(), timeout=2)
        assert len(permits) == 1
        permit = permits[0]

        clock.tick(1)
        cancelled = await ApprovalBoundaryService(dependencies).cancel(
            action.run_id,
            audit_context=_context("orch-06.cancelled-long-call"),
        )
        assert cancelled.disposition is ApprovalBoundaryDisposition.CANCELLED
        async with dependencies.unit_of_work() as unit_of_work:
            before_deadline = await unit_of_work.external_actions.get(action.id)
            before_timeline = await unit_of_work.audits.list_run(action.run_id)
        assert before_deadline is not None and before_deadline.lease is not None
        assert before_deadline.state is ExternalActionState.DISPATCHING
        assert before_deadline.call_deadline_at == permit.call_deadline_at
        assert clock.now() == before_deadline.lease.expires_at < permit.call_deadline_at

        early = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.cancelled-long-call.early",
            limit=1,
        )
        assert early == ()
        assert gateway.calls == 1
        assert ledger.side_effect_count == 0
        assert not first_dispatch.done()
        async with dependencies.unit_of_work() as unit_of_work:
            unchanged = await unit_of_work.external_actions.get(action.id)
            unchanged_timeline = await unit_of_work.audits.list_run(action.run_id)
        assert unchanged == before_deadline
        assert unchanged_timeline == before_timeline

        clock.current = permit.call_deadline_at
        terminal = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.cancelled-long-call.deadline",
            limit=1,
        )
        assert len(terminal) == 1
        assert terminal[0].disposition is DispatchDisposition.OUTCOME_UNKNOWN
        assert terminal[0].action.state is ExternalActionState.OUTCOME_UNKNOWN
        assert terminal[0].action.terminal_reason_code == "run_cancelled_after_call_start"
        assert gateway.calls == 1
        assert ledger.side_effect_count == 0
        async with dependencies.unit_of_work() as unit_of_work:
            timeline = await unit_of_work.audits.list_run(action.run_id)
            terminal_step = await unit_of_work.run_steps.get(action.step_id)
        outcome_event = timeline[-2]
        assert outcome_event.event_type == "action.outcome_unknown"
        assert outcome_event.action_id == action.id
        assert outcome_event.action_attempt_number == 1
        assert outcome_event.mutation_version == terminal[0].action.version
        assert outcome_event.previous_state == ExternalActionState.DISPATCHING.value
        assert outcome_event.new_state == ExternalActionState.OUTCOME_UNKNOWN.value
        assert outcome_event.reason_code == "run_cancelled_after_call_start"
        assert timeline[-1].event_type == "step.transitioned"
        assert terminal_step is not None and terminal_step.state is StepState.FAILED
        assert terminal_step.terminal_reason_code == "run_cancelled_after_call_start"
    finally:
        gateway.release_first.set()
        if first_dispatch is not None:
            await asyncio.gather(first_dispatch, return_exceptions=True)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_cancelled_long_call_reconciles_receipt_before_unknown(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-cancelled-receipt-first.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=186, delivery_attempt_limit=2)
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    delegate, ledger = _gateway(runtime, clock)
    gateway = ReceiptThenBlockingGateway(delegate)
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
        lease_duration=timedelta(seconds=1),
    )
    first_dispatch: asyncio.Task[ExternalActionDispatchResult] | None = None
    try:
        first_dispatch = asyncio.create_task(
            dispatcher.dispatch_once(
                action.id,
                lease_owner="worker.orch-06.cancelled-receipt-first",
            )
        )
        await asyncio.wait_for(gateway.receipt_committed.wait(), timeout=2)
        assert len(permits) == 1
        assert gateway.calls == ledger.side_effect_count == 1

        clock.tick(1)
        await ApprovalBoundaryService(dependencies).cancel(
            action.run_id,
            audit_context=_context("orch-06.cancelled-receipt-first"),
        )
        permit = permits[0]
        early = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.cancelled-receipt-first.early",
            limit=1,
        )
        assert early == ()

        clock.current = permit.call_deadline_at
        reconciled = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.cancelled-receipt-first.deadline",
            limit=1,
        )
        assert len(reconciled) == 1
        assert reconciled[0].disposition is DispatchDisposition.SUCCEEDED
        assert reconciled[0].action.state is ExternalActionState.SUCCEEDED
        assert reconciled[0].action.result is not None
        assert gateway.calls == ledger.side_effect_count == 1
        async with dependencies.unit_of_work() as unit_of_work:
            timeline = await unit_of_work.audits.list_run(action.run_id)
            terminal_step = await unit_of_work.run_steps.get(action.step_id)
        receipt_event = timeline[-2]
        assert receipt_event.event_type == "action.receipt_reconciled"
        assert receipt_event.action_id == action.id
        assert receipt_event.action_attempt_number == 1
        assert receipt_event.receipt_id == reconciled[0].action.result.receipt_id
        assert receipt_event.mutation_version == reconciled[0].action.version
        assert receipt_event.previous_state == ExternalActionState.DISPATCHING.value
        assert receipt_event.new_state == ExternalActionState.SUCCEEDED.value
        assert timeline[-1].event_type == "step.transitioned"
        assert terminal_step is not None and terminal_step.state is StepState.SUCCEEDED
    finally:
        gateway.release_response.set()
        if first_dispatch is not None:
            await asyncio.gather(first_dispatch, return_exceptions=True)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_terminal_write_denial_closes_mixed_siblings_and_inflight_at_deadline(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-terminal-denial-mixed-siblings.db")
    clock = MutableClock()
    first, denied, queued = await _released_parallel_actions(runtime, clock, seed=187)
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    delegate, ledger = _gateway(runtime, clock)
    gateway = BlockingFirstGateway(delegate)
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
        lease_duration=timedelta(seconds=1),
    )
    first_dispatch: asyncio.Task[ExternalActionDispatchResult] | None = None
    try:
        first_dispatch = asyncio.create_task(
            dispatcher.dispatch_once(
                first.id,
                lease_owner="worker.orch-06.mixed.first",
            )
        )
        await asyncio.wait_for(gateway.first_started.wait(), timeout=2)
        assert len(permits) == 1
        async with runtime.session_factory() as session, session.begin():
            record = await session.get(RunExecutionControlRecord, first.run_id)
            assert record is not None
            record.tool_calls = record.max_tool_calls
            record.updated_at = clock.now()
            record.version += 1
            record.integrity_digest = execution_control_record_digest(
                _execution_control_material(record),
                TEST_EXECUTION_CONTROL_KEY,
            )

        with pytest.raises(ExternalActionDispatchError) as rejected:
            await dispatcher.dispatch_once(
                denied.id,
                lease_owner="worker.orch-06.mixed.denied",
            )
        assert rejected.value.code == "tool_budget_exhausted"
        assert gateway.calls == 1
        assert ledger.side_effect_count == 0

        async with dependencies.unit_of_work() as unit_of_work:
            actions = await unit_of_work.external_actions.list_run_plan(
                first.run_id,
                first.envelope.plan_hash,
            )
            steps = await unit_of_work.run_steps.list_for_run(first.run_id)
            run = await unit_of_work.runs.get(first.run_id)
            timeline = await unit_of_work.audits.list_run(first.run_id)
        action_by_id = {action.id: action for action in actions}
        step_by_id = {step.id: step for step in steps}
        assert action_by_id[first.id].state is ExternalActionState.DISPATCHING
        assert action_by_id[first.id].call_started_at is not None
        assert action_by_id[denied.id].state is ExternalActionState.CANCELLED
        assert action_by_id[queued.id].state is ExternalActionState.CANCELLED
        assert step_by_id[first.step_id].state is StepState.EXECUTING
        assert step_by_id[denied.step_id].state is StepState.FAILED
        assert step_by_id[denied.step_id].terminal_reason_code == "tool_budget_exhausted"
        assert step_by_id[queued.step_id].state is StepState.SKIPPED
        assert step_by_id[queued.step_id].terminal_reason_code == "runtime_control_denied"
        assert run is not None and run.state is RunState.FAILED
        assert run.terminal_reason_code == "tool_budget_exhausted"
        assert tuple(event.event_type for event in timeline[-6:]) == (
            "runtime.control_denied",
            "action.cancelled",
            "action.cancelled",
            "step.transitioned",
            "step.transitioned",
            "run.transitioned",
        )

        early = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.mixed.early",
            limit=3,
        )
        assert early == ()
        permit = permits[0]
        clock.current = permit.call_deadline_at
        terminal = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.mixed.deadline",
            limit=3,
        )
        assert len(terminal) == 1
        assert terminal[0].disposition is DispatchDisposition.OUTCOME_UNKNOWN
        assert terminal[0].action.terminal_reason_code == "runtime_control_denied_after_call_start"
        assert gateway.calls == 1
        assert ledger.side_effect_count == 0
    finally:
        gateway.release_first.set()
        if first_dispatch is not None:
            await asyncio.gather(first_dispatch, return_exceptions=True)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_read_output_denial_code_closes_inflight_write_without_replay(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-read-denial-inflight-write.db")
    clock = MutableClock()
    first, denied, _ = await _released_parallel_actions(runtime, clock, seed=1871)
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    delegate, ledger = _gateway(runtime, clock)
    gateway = BlockingFirstGateway(delegate)
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
        lease_duration=timedelta(seconds=1),
    )
    first_dispatch: asyncio.Task[ExternalActionDispatchResult] | None = None
    try:
        first_dispatch = asyncio.create_task(
            dispatcher.dispatch_once(
                first.id,
                lease_owner="worker.orch-06.read-denial.first",
            )
        )
        await asyncio.wait_for(gateway.first_started.wait(), timeout=2)
        assert len(permits) == 1

        occurred_at = clock.now()
        audit_context = _context("orch-06.read-output-denial")
        async with dependencies.unit_of_work() as unit_of_work:
            denied_step = await unit_of_work.run_steps.get(denied.step_id)
            assert denied_step is not None
            cleanup = await TerminalExecutionCleanupService().fail_runtime_control_in_uow(
                unit_of_work,
                run_id=first.run_id,
                denied_step_id=denied.step_id,
                plan_hash=first.envelope.plan_hash,
                denial_code="output_payload_too_large",
                occurred_at=occurred_at,
                audit_context=audit_context,
            )
            denial_event = AuditEventFactory(audit_context).runtime_control_denied(
                run_id=first.run_id,
                step_id=denied.step_id,
                operation_key=denied_step.runtime_policy.operation_key,
                denial_code="output_payload_too_large",
                occurred_at=occurred_at,
            )
            await unit_of_work.audits.append_many((denial_event, *cleanup.audit_events))
            await unit_of_work.commit()

        early = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.read-denial.early",
            limit=3,
        )
        assert early == ()
        clock.current = permits[0].call_deadline_at
        terminal = await dispatcher.recover_stale(
            lease_owner="worker.orch-06.read-denial.deadline",
            limit=3,
        )
        assert len(terminal) == 1
        assert terminal[0].disposition is DispatchDisposition.OUTCOME_UNKNOWN
        assert terminal[0].action.terminal_reason_code == "runtime_control_denied_after_call_start"
        assert gateway.calls == 1
        assert ledger.side_effect_count == 0
    finally:
        gateway.release_first.set()
        if first_dispatch is not None:
            await asyncio.gather(first_dispatch, return_exceptions=True)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_terminal_sibling_cleanup_and_audits_rollback_atomically(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-terminal-cleanup-atomicity.db")
    clock = MutableClock()
    first, denied, queued = await _released_parallel_actions(runtime, clock, seed=188)
    dependencies = _dependencies(runtime, clock)
    gateway = NoReceiptUncertainGateway()
    try:
        async with runtime.session_factory() as session, session.begin():
            record = await session.get(RunExecutionControlRecord, first.run_id)
            assert record is not None
            record.tool_calls = record.max_tool_calls
            record.updated_at = clock.now()
            record.version += 1
            record.integrity_digest = execution_control_record_digest(
                _execution_control_material(record),
                TEST_EXECUTION_CONTROL_KEY,
            )

        async with dependencies.unit_of_work() as unit_of_work:
            before_actions = await unit_of_work.external_actions.list_run_plan(
                first.run_id,
                first.envelope.plan_hash,
            )
            before_steps = await unit_of_work.run_steps.list_for_run(first.run_id)
            before_run = await unit_of_work.runs.get(first.run_id)
            before_control = await unit_of_work.execution_control.get(first.run_id)
            before_timeline = await unit_of_work.audits.list_run(first.run_id)

        faulting_dependencies = OrchestrationDependencies(
            clock,
            UnusedIds(),
            AuditAppendFaultUnitOfWorkFactory(_uow_factory(runtime)),  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="injected terminal audit fault"):
            await ExternalActionDispatcher(
                faulting_dependencies,
                gateway,
                WriteAuthorizationGuard(),
            ).dispatch_once(
                denied.id,
                lease_owner="worker.orch-06.atomic-terminal-cleanup",
            )

        async with dependencies.unit_of_work() as unit_of_work:
            after_actions = await unit_of_work.external_actions.list_run_plan(
                first.run_id,
                first.envelope.plan_hash,
            )
            after_steps = await unit_of_work.run_steps.list_for_run(first.run_id)
            after_run = await unit_of_work.runs.get(first.run_id)
            after_control = await unit_of_work.execution_control.get(first.run_id)
            after_timeline = await unit_of_work.audits.list_run(first.run_id)
        assert {action.id: action for action in after_actions} == {
            action.id: action for action in before_actions
        }
        assert {step.id: step for step in after_steps} == {step.id: step for step in before_steps}
        assert after_run == before_run
        assert after_control == before_control
        assert after_timeline == before_timeline
        assert {action.id for action in after_actions} == {first.id, denied.id, queued.id}
        assert all(
            action.state is ExternalActionState.DISPATCH_RESERVED for action in after_actions
        )
        assert gateway.calls == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial",
    ("cancelled", "deadline", "tool", "rate", "payload", "field"),
)
async def test_orch_06_write_control_denials_make_zero_gateway_calls(
    tmp_path: Path,
    denial: str,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-06-write-denial-{denial}.db")
    clock = MutableClock()
    if denial == "rate":
        templates = _runtime_templates(rate_max_calls=1)
    elif denial == "payload":
        templates = _runtime_templates(max_input_bytes=64, max_input_field_bytes=64)
    elif denial == "field":
        templates = _runtime_templates(max_input_bytes=1_024, max_input_field_bytes=64)
    else:
        templates = None
    action = await _released_action(
        runtime,
        clock,
        seed=190,
        body=("x" * 200 if denial in {"payload", "field"} else "private welcome body"),
        templates=templates,
    )
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    gateway = NoReceiptUncertainGateway()
    expected_code = {
        "cancelled": "run_cancelled",
        "deadline": "deadline_exceeded",
        "tool": "tool_budget_exhausted",
        "rate": "rate_limit_exhausted",
        "payload": "input_payload_too_large",
        "field": "input_field_too_large",
    }[denial]
    try:
        control, _ = await _control_and_write_step(dependencies, action)
        if denial == "cancelled":
            async with dependencies.unit_of_work() as unit_of_work:
                await unit_of_work.execution_control.request_cancel(
                    run_id=action.run_id,
                    expected_control_version=control.version,
                    actor_digest="a" * 64,
                    requested_at=clock.now(),
                )
                await unit_of_work.commit()
        elif denial == "deadline":
            clock.current = cast(datetime, control.deadline_at)
        elif denial == "tool":
            # Model a control whose trusted earlier operations consumed the
            # final logical slot; keep the fixture HMAC-valid so hydration and
            # the repository's budget fence remain the production path.
            async with runtime.session_factory() as session, session.begin():
                record = await session.get(RunExecutionControlRecord, action.run_id)
                assert record is not None
                record.tool_calls = record.max_tool_calls
                record.updated_at = clock.now()
                record.version += 1
                record.integrity_digest = execution_control_record_digest(
                    _execution_control_material(record),
                    TEST_EXECUTION_CONTROL_KEY,
                )

        dispatch_control, step = await _control_and_write_step(dependencies, action)
        window_start = fixed_window_start(
            clock.now(), step.runtime_policy.rate_limit.window_seconds
        )
        async with dependencies.unit_of_work() as unit_of_work:
            before_timeline = await unit_of_work.audits.list_run(action.run_id)
            before_run = await unit_of_work.runs.get(action.run_id)
            before_step = await unit_of_work.run_steps.get(action.step_id)
            before_window = await unit_of_work.execution_control.get_rate_window(
                step.runtime_policy.rate_limit.scope,
                step.runtime_policy.rate_limit.key,
                window_start,
            )
        async with runtime.session_factory() as session:
            before_dispatch_attempts = int(
                (
                    await session.execute(
                        select(
                            func.count(ExternalActionDispatchAttemptRecord.attempt_number)
                        ).where(ExternalActionDispatchAttemptRecord.external_action_id == action.id)
                    )
                ).scalar_one()
            )
        dispatcher = ExternalActionDispatcher(dependencies, gateway, WriteAuthorizationGuard())
        retry_after_seconds: int | None = None
        terminal_denial = denial in {"deadline", "tool", "payload", "field"}
        for _ in range(1 if terminal_denial else 2):
            with pytest.raises(ExternalActionDispatchError) as rejected:
                await dispatcher.dispatch_once(
                    action.id,
                    lease_owner=f"worker.orch-06.denied.{denial}",
                )
            assert rejected.value.code == expected_code
            if denial == "rate":
                assert rejected.value.retry_after_seconds is not None
                assert 1 <= rejected.value.retry_after_seconds <= 3_600
            else:
                assert rejected.value.retry_after_seconds is None
            if retry_after_seconds is None:
                retry_after_seconds = rejected.value.retry_after_seconds
            else:
                assert rejected.value.retry_after_seconds == retry_after_seconds
        assert gateway.calls == 0
        assert permits == []
        async with dependencies.unit_of_work() as unit_of_work:
            latest = await unit_of_work.external_actions.get(action.id)
            after_control = await unit_of_work.execution_control.get(action.run_id)
            after_run = await unit_of_work.runs.get(action.run_id)
            after_step = await unit_of_work.run_steps.get(action.step_id)
            after_window = await unit_of_work.execution_control.get_rate_window(
                step.runtime_policy.rate_limit.scope,
                step.runtime_policy.rate_limit.key,
                window_start,
            )
            after_timeline = await unit_of_work.audits.list_run(action.run_id)
        async with runtime.session_factory() as session:
            after_dispatch_attempts = int(
                (
                    await session.execute(
                        select(
                            func.count(ExternalActionDispatchAttemptRecord.attempt_number)
                        ).where(ExternalActionDispatchAttemptRecord.external_action_id == action.id)
                    )
                ).scalar_one()
            )
        assert latest is not None
        assert latest.delivery_attempt_count == 0
        assert latest.lease is None and latest.call_started_at is None
        assert after_control == dispatch_control
        assert after_window == before_window
        assert after_dispatch_attempts == before_dispatch_attempts == 0
        denial_events = tuple(
            event for event in after_timeline if event.event_type == "runtime.control_denied"
        )
        assert len(denial_events) == 1
        denial_event = denial_events[0]
        expected_metadata: dict[str, object] = {
            "denial_code": expected_code,
            "operation_key": step.runtime_policy.operation_key,
        }
        if retry_after_seconds is not None:
            expected_metadata["retry_after_seconds"] = retry_after_seconds
        assert denial_event.action_id == action.id
        assert denial_event.step_id == action.step_id
        assert denial_event.outcome.value == "rejected"
        assert denial_event.mutation_version is None
        assert denial_event.safe_metadata.values == expected_metadata
        assert f"worker.orch-06.denied.{denial}" not in str(denial_event.safe_metadata.values)
        if terminal_denial:
            assert before_run is not None and before_run.state is RunState.EXECUTING
            assert after_run is not None and after_run.state is RunState.FAILED
            assert after_run.terminal_reason_code == expected_code
            assert before_step is not None and before_step.state is StepState.READY
            assert after_step is not None and after_step.state is StepState.FAILED
            assert after_step.terminal_reason_code == expected_code
            assert latest.state is ExternalActionState.CANCELLED
            assert latest.version == action.version + 1
            assert latest.terminal_reason_code == "runtime_control_denied"
            assert tuple(event.event_type for event in after_timeline[-4:]) == (
                "runtime.control_denied",
                "action.cancelled",
                "step.transitioned",
                "run.transitioned",
            )
            assert after_timeline[:-4] == before_timeline
        else:
            assert latest == action
            assert latest.state is ExternalActionState.DISPATCH_RESERVED
            assert after_run == before_run
            assert after_step == before_step
            assert denial_event == after_timeline[-1]
            assert after_timeline[:-1] == before_timeline
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_write_dispatch_rejects_sibling_runtime_policy_tamper_pre_claim(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-write-sibling-policy-tamper.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=195)
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    gateway = NoReceiptUncertainGateway()
    try:
        async with dependencies.unit_of_work() as unit_of_work:
            steps = await unit_of_work.run_steps.list_for_run(action.run_id)
        sibling = next(step for step in steps if step.id != action.step_id)
        async with runtime.session_factory() as session, session.begin():
            sibling_record = await session.get(RunStepRecord, sibling.id)
            assert sibling_record is not None
            sibling_record.runtime_policy_snapshot = {
                **sibling_record.runtime_policy_snapshot,
                "operation_key": "operation.orch-06.tampered-sibling",
            }

        async with dependencies.unit_of_work() as unit_of_work:
            before_action = await unit_of_work.external_actions.get(action.id)
            before_control = await unit_of_work.execution_control.get(action.run_id)
            before_target_step = await unit_of_work.run_steps.get(action.step_id)
            before_timeline = await unit_of_work.audits.list_run(action.run_id)
        async with runtime.session_factory() as session:
            before_attempts = int(
                (
                    await session.execute(
                        select(func.count()).select_from(ExternalActionDispatchAttemptRecord)
                    )
                ).scalar_one()
            )
            before_windows = int(
                (
                    await session.execute(select(func.count()).select_from(RateLimitWindowRecord))
                ).scalar_one()
            )
        assert before_action == action

        with pytest.raises(ExternalActionDispatchError) as rejected:
            await ExternalActionDispatcher(
                dependencies,
                gateway,
                WriteAuthorizationGuard(),
            ).dispatch_once(
                action.id,
                lease_owner="worker.orch-06.sibling-policy-tamper",
            )
        assert rejected.value.code == "execution_plan_invalid"

        async with dependencies.unit_of_work() as unit_of_work:
            after_action = await unit_of_work.external_actions.get(action.id)
            after_control = await unit_of_work.execution_control.get(action.run_id)
            after_target_step = await unit_of_work.run_steps.get(action.step_id)
            after_timeline = await unit_of_work.audits.list_run(action.run_id)
        async with runtime.session_factory() as session:
            after_attempts = int(
                (
                    await session.execute(
                        select(func.count()).select_from(ExternalActionDispatchAttemptRecord)
                    )
                ).scalar_one()
            )
            after_windows = int(
                (
                    await session.execute(select(func.count()).select_from(RateLimitWindowRecord))
                ).scalar_one()
            )

        assert after_action == before_action == action
        assert after_action is not None
        assert after_action.state is ExternalActionState.DISPATCH_RESERVED
        assert after_action.delivery_attempt_count == 0
        assert after_action.lease is None and after_action.call_started_at is None
        assert after_control == before_control
        assert after_target_step == before_target_step
        assert after_attempts == before_attempts == 0
        assert after_windows == before_windows
        assert after_timeline == before_timeline
        assert permits == []
        assert gateway.calls == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limiter", "expected_seconds"),
    (("template", 5), ("delivery", 4), ("remaining_run", 3)),
)
async def test_orch_06_write_timeout_is_minimum_of_all_durable_bounds(
    tmp_path: Path,
    limiter: str,
    expected_seconds: int,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-06-write-timeout-{limiter}.db")
    clock = MutableClock()
    templates = _runtime_templates(step_timeout_seconds=5) if limiter == "template" else None
    operations = _operations_with_timeout(4) if limiter == "delivery" else None
    action = await _released_action(
        runtime,
        clock,
        seed=200,
        templates=templates,
        operations=operations,
    )
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    gateway = NoReceiptUncertainGateway()
    try:
        control, _ = await _control_and_write_step(dependencies, action)
        if limiter == "remaining_run":
            clock.current = cast(datetime, control.deadline_at) - timedelta(seconds=3)
        await ExternalActionDispatcher(
            dependencies, gateway, WriteAuthorizationGuard()
        ).dispatch_once(action.id, lease_owner=f"worker.orch-06.timeout.{limiter}")
        assert len(permits) == 1
        assert permits[0].effective_timeout == timedelta(seconds=expected_seconds)
        assert gateway.calls == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("past_deadline", (False, True))
async def test_orch_06_write_gateway_cannot_swallow_timeout_and_return_success(
    tmp_path: Path,
    past_deadline: bool,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-06-write-swallowed-timeout-{past_deadline}.db")
    clock = MutableClock()
    action = await _released_action(
        runtime,
        clock,
        seed=202,
        templates=_runtime_templates(step_timeout_seconds=1),
    )
    permits: list[DeliveryCallPermit] = []
    dependencies = _permit_recording_dependencies(runtime, clock, permits)
    gateway = CancellationSwallowingLateWriteGateway(
        clock,
        permits,
        past_deadline=past_deadline,
    )
    try:
        result = await ExternalActionDispatcher(
            dependencies,
            gateway,
            WriteAuthorizationGuard(),
        ).dispatch_once(
            action.id,
            lease_owner=f"worker.orch-06.swallowed-timeout.{past_deadline}",
        )

        assert len(permits) == 1
        assert gateway.calls == 1
        assert gateway.swallowed_cancellation
        assert clock.now() >= permits[0].call_deadline_at
        assert result.disposition is DispatchDisposition.RECOVERY_PENDING
        assert result.action.state is ExternalActionState.DISPATCHING
        assert result.action.result is None
        async with dependencies.unit_of_work() as unit_of_work:
            stored_action = await unit_of_work.external_actions.get(action.id)
            stored_step = await unit_of_work.run_steps.get(action.step_id)
        async with runtime.session_factory() as session:
            attempt = await session.get(
                ExternalActionDispatchAttemptRecord,
                (action.id, 1),
            )
            receipt_count = int(
                (
                    await session.execute(
                        select(func.count()).select_from(ConnectorActionReceiptRecord)
                    )
                ).scalar_one()
            )
        assert stored_action == result.action
        assert stored_step is not None and stored_step.state is StepState.EXECUTING
        assert attempt is not None and attempt.call_started_at is not None
        assert attempt.completed_at is None and attempt.conclusion is None
        assert receipt_count == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_call_start_commit_latency_cannot_extend_absolute_deadline(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-write-commit-latency.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=205)
    permits: list[DeliveryCallPermit] = []
    recorded = _permit_recording_dependencies(runtime, clock, permits)
    dependencies = OrchestrationDependencies(
        clock,
        UnusedIds(),
        DeadlineAdvancingUnitOfWorkFactory(
            recorded.unit_of_work,
            clock,
            permits,
        ),
    )
    gateway = NoReceiptUncertainGateway()
    try:
        result = await ExternalActionDispatcher(
            dependencies,
            gateway,
            WriteAuthorizationGuard(),
        ).dispatch_once(
            action.id,
            lease_owner="worker.orch-06.commit-latency",
        )
        assert len(permits) == 1
        assert clock.now() == permits[0].call_deadline_at
        assert gateway.calls == 0
        assert result.disposition is DispatchDisposition.FAILED
        assert result.action.state is ExternalActionState.FAILED
        assert result.action.terminal_reason_code == "connector_timeout"
        async with runtime.session_factory() as session:
            attempt = await session.get(
                ExternalActionDispatchAttemptRecord,
                (action.id, 1),
            )
        assert attempt is not None and attempt.call_started_at is not None
        assert attempt.conclusion == "failed"
        assert attempt.reason_code == "connector_timeout"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_cancellation_and_call_start_control_cas_have_one_winner(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-06-cancel-call-start-race.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=210)
    dependencies = _dependencies(runtime, clock)
    gateway = NoReceiptUncertainGateway()
    try:
        control, _ = await _control_and_write_step(dependencies, action)
        dispatcher = ExternalActionDispatcher(dependencies, gateway, WriteAuthorizationGuard())

        async def call_start():  # type: ignore[no-untyped-def]
            return await dispatcher._claim_and_mark_call_started(
                action.id,
                "worker.orch-06.race",
            )

        async def cancel():  # type: ignore[no-untyped-def]
            async with dependencies.unit_of_work() as unit_of_work:
                result = await unit_of_work.execution_control.request_cancel(
                    run_id=action.run_id,
                    expected_control_version=control.version,
                    actor_digest="b" * 64,
                    requested_at=clock.now(),
                )
                await unit_of_work.commit()
                return result

        outcomes = await asyncio.gather(call_start(), cancel(), return_exceptions=True)
        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
        assert sum(isinstance(item, BaseException) for item in outcomes) == 1
        failure = next(item for item in outcomes if isinstance(item, BaseException))
        assert isinstance(
            failure,
            (ExternalActionDispatchError, ExecutionControlRepositoryConflict),
        )
        assert gateway.calls == 0
        final_control, final_step = await _control_and_write_step(dependencies, action)
        async with dependencies.unit_of_work() as unit_of_work:
            final_action = await unit_of_work.external_actions.get(action.id)
        assert final_action is not None
        call_started = final_action.call_started_at is not None
        cancelled = final_control.cancel_requested_at is not None
        assert call_started is not cancelled
        assert final_step.state is (StepState.EXECUTING if call_started else StepState.READY)
        if cancelled:
            assert final_action == action
            assert final_action.delivery_attempt_count == 0
            assert final_action.lease is None
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_pre_call", (False, True))
async def test_orch_06_post_release_cancellation_closes_every_unstarted_write(
    tmp_path: Path,
    claimed_pre_call: bool,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-06-post-release-cancel-{claimed_pre_call}.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=220)
    dependencies = _dependencies(runtime, clock)
    gateway = NoReceiptUncertainGateway()
    try:
        if claimed_pre_call:
            async with dependencies.unit_of_work() as unit_of_work:
                current = cast(
                    ExternalAction,
                    await unit_of_work.external_actions.get(action.id),
                )
                claimed = await _claim_reserved(
                    unit_of_work,
                    current,
                    clock,
                    lease_owner="worker.orch-06.cancel-pre-call",
                    lease_seconds=30,
                )
                assert claimed is not None
                assert claimed.call_started_at is None
                await unit_of_work.commit()

        clock.tick(1)
        cancelled = await ApprovalBoundaryService(dependencies).cancel(
            action.run_id,
            audit_context=_context(f"orch-06.post-release-cancel.{claimed_pre_call}"),
        )
        assert cancelled.disposition is ApprovalBoundaryDisposition.CANCELLED
        async with dependencies.unit_of_work() as unit_of_work:
            closed_action = await unit_of_work.external_actions.get(action.id)
            closed_step = await unit_of_work.run_steps.get(action.step_id)
            control = await unit_of_work.execution_control.get(action.run_id)
        assert closed_action is not None
        assert closed_action.state is ExternalActionState.CANCELLED
        assert closed_action.call_started_at is None
        assert closed_step is not None and closed_step.state is StepState.CANCELLED
        assert control is not None and control.cancel_requested_at == clock.now()

        with pytest.raises(ExternalActionDispatchError) as blocked:
            await ExternalActionDispatcher(
                dependencies,
                gateway,
                WriteAuthorizationGuard(),
            ).dispatch_once(
                action.id,
                lease_owner="worker.orch-06.cancelled-dispatch",
            )
        assert blocked.value.code == "action_not_dispatchable"
        assert gateway.calls == 0
    finally:
        await runtime.dispose()
