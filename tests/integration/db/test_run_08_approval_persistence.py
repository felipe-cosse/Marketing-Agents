"""RUN-08 exact approval records, lifecycle CAS, replay, and redacted audit."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import (
    EffectPlan,
    EffectPlanRequest,
    OrchestrationDependencies,
    RoutingResult,
)
from marketing_agents.application.ports.repositories import ApprovalRepository
from marketing_agents.application.services.approval_integrity import renew_expired_request
from marketing_agents.application.services.approval_records import (
    ApprovalRecordService,
    ApprovalRecordServiceError,
)
from marketing_agents.application.services.idempotent_work_receipt import (
    IdempotentWorkRunReceiptService,
)
from marketing_agents.application.services.plan_persistence import _materialize_plan
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalDecision,
    ApprovalRenewal,
    StoredActionApprovalRequest,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
    WorkMode,
)
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.infrastructure.db import (
    ApprovalPersistenceConflict,
    AuditPersistenceInvariantError,
    Base,
    DatabaseRuntime,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditRepository,
    SQLAlchemyExternalActionRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ApprovalUseRecord,
    AuditEventRecord,
    ExternalActionRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.infrastructure.db.repositories.approval import (
    _seal_request_record,
    _seal_use_record,
)
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from tests.support.execution_control import execution_control_repository
from tests.support.incoming_work import validate_incoming_for_test
from tests.unit.application.test_run_02_effect_aware_planning import (
    CATALOG,
    TARGET_INSTANCE,
    WORKER_TEMPLATE,
    WORKFLOW_HASH,
    RecordingClock,
    RecordingIds,
    _planner,
    _read_step,
    _request,
    _route,
    _write_step,
)

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
APPROVAL_INTEGRITY_KEY = DigestKey(bytes(range(32)))


def _approval_repository(session: AsyncSession) -> SQLAlchemyApprovalRepository:
    return SQLAlchemyApprovalRepository(session, APPROVAL_INTEGRITY_KEY)


def test_run_08_approval_schema_compiles_for_sqlite_and_postgresql() -> None:
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        for table in Base.metadata.sorted_tables:
            str(CreateTable(table).compile(dialect=dialect))

    sqlite_request = str(
        CreateTable(ApprovalRequestRecord.__table__).compile(dialect=sqlite.dialect())
    ).lower()
    postgres_request = str(
        CreateTable(ApprovalRequestRecord.__table__).compile(dialect=postgresql.dialect())
    ).lower()
    use_ddl = " ".join(
        str(CreateTable(ApprovalUseRecord.__table__).compile(dialect=sqlite.dialect()))
        .lower()
        .split()
    )
    assert "bool_approval_request_self_approval" in sqlite_request
    assert "allow_self_approval in (0, 1)" in sqlite_request
    assert "allow_self_approval boolean not null" in postgres_request
    assert "integrity_digest" in sqlite_request
    assert "integrity_digest" in postgres_request
    assert "foreign key(action_id, reservation_id, request_id, decision_id" in use_ddl
    assert "reserved_at" in use_ddl


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class IncrementingIds:
    def __init__(self, seed: int = 0) -> None:
        self._next = seed

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.run-08.{self._next:04d}"


class FaultAfterAuditAppend:
    def __init__(self, delegate: SQLAlchemyAuditRepository) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def append_many(self, events):  # type: ignore[no-untyped-def]
        await self._delegate.append_many(events)
        raise RuntimeError("injected approval audit failure")


class TwoPartyBarrier:
    def __init__(self) -> None:
        self._arrivals = 0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._arrivals += 1
            if self._arrivals == 2:
                self._ready.set()
        await self._ready.wait()


class RenewalBarrierApprovalRepository:
    def __init__(self, session: AsyncSession, barrier: TwoPartyBarrier) -> None:
        self._delegate = SQLAlchemyApprovalRepository(session, APPROVAL_INTEGRITY_KEY)
        self._barrier = barrier

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def renew_expired(
        self,
        *,
        expected_version: int,
        expected_action_version: int,
        renewal: ApprovalRenewal,
    ) -> StoredActionApprovalRequest:
        await self._barrier.wait()
        return await self._delegate.renew_expired(
            expected_version=expected_version,
            expected_action_version=expected_action_version,
            renewal=renewal,
        )


def _context(label: str) -> AuditContext:
    return AuditContext.system("test.run-08", correlation_id=f"request.{label}")


def _uow_factory(
    runtime: DatabaseRuntime,
    *,
    audit_factory=SQLAlchemyAuditRepository,  # type: ignore[no-untyped-def]
    approval_factory=_approval_repository,  # type: ignore[no-untyped-def]
) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=audit_factory,
            artifacts=SQLAlchemyArtifactRepository,
            approvals=approval_factory,
            run_steps=SQLAlchemyRunStepRepository,
            external_actions=SQLAlchemyExternalActionRepository,
            execution_control=execution_control_repository,
        ),
    )


def _dependencies(
    runtime: DatabaseRuntime,
    *,
    clock: MutableClock | None = None,
    ids: IncrementingIds | None = None,
    audit_factory=SQLAlchemyAuditRepository,  # type: ignore[no-untyped-def]
    approval_factory=_approval_repository,  # type: ignore[no-untyped-def]
) -> OrchestrationDependencies:
    return OrchestrationDependencies(
        clock or MutableClock(),
        ids or IncrementingIds(),
        _uow_factory(
            runtime,
            audit_factory=audit_factory,
            approval_factory=approval_factory,
        ),
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


def _envelope(event_id: str) -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id=event_id,
        instance_id=TARGET_INSTANCE,
        trigger_id="trigger.manual.run-08",
        workflow_id="workflow.community.onboarding",
        mode=WorkMode.MOCK_EXECUTION,
        brief_id=None,
        brief_revision=None,
        configuration_revision=1,
        admitted_payload={"safe": True},
    )


def _plan(
    run_id: str,
    *,
    seed: int,
    body: str = "private welcome body",
    planned_at: datetime = NOW,
) -> EffectPlan:
    planner, _, _ = _planner(
        clock=cast(RecordingClock, MutableClock(planned_at)),
        ids=RecordingIds(seed=seed),
    )
    return planner.plan(
        _request(
            include_write=True,
            run_id=run_id,
            write_step=_write_step(body=body),
        )
    )


def _multi_plan(run_id: str, *, seed: int) -> tuple[EffectPlan, EffectPlanRequest]:
    templates = tuple(
        item.model_copy(
            update={"budget_policy": item.budget_policy.model_copy(update={"max_tool_calls": 3})}
        )
        if item.id == WORKER_TEMPLATE
        else item
        for item in CATALOG.templates
    )
    planner, _, _ = _planner(ids=RecordingIds(seed=seed), templates=templates)
    request = EffectPlanRequest(
        run_id=run_id,
        workflow_definition_hash=WORKFLOW_HASH,
        graph=DependencyGraph.build(
            (
                TopologyStep("membership", 1),
                TopologyStep("welcome-z", 2, ("membership",)),
                TopologyStep("welcome-a", 3, ("welcome-z",), terminal_result=True),
            ),
            workflow_max_steps=10,
            global_max_steps=20,
        ),
        routing=_route(include_write=True),  # type: ignore[arg-type]
        steps=(
            _read_step(),
            _write_step(
                key="welcome-z",
                runtime_step_id=f"runtime-step.welcome-z.{seed}",
                body="first private body",
            ),
            _write_step(
                key="welcome-a",
                runtime_step_id=f"runtime-step.welcome-a.{seed}",
                body="second private body",
            ),
        ),
        requested_by="principal.local.operator",
    )
    return planner.plan(request), request


async def _seed_run_and_plan(
    dependencies: OrchestrationDependencies,
    *,
    event_id: str,
    seed: int = 10,
    multiple_writes: bool = False,
) -> EffectPlan:
    received = await IdempotentWorkRunReceiptService(
        dependencies,
        APPROVAL_INTEGRITY_KEY,
        current_catalog_hash=CATALOG.content_hash,
    ).receive(
        validate_incoming_for_test(
            _envelope(event_id),
            catalog_hash=CATALOG.content_hash,
        ),
        audit_context=_context(f"{event_id}.receive"),
    )
    if multiple_writes:
        plan, request = _multi_plan(received.run.id, seed=seed)
    else:
        request = _request(include_write=True, run_id=received.run.id)
        planner, _, _ = _planner(ids=RecordingIds(seed=seed))
        plan = planner.plan(request)
    materialized = _materialize_plan(
        plan,
        request.graph,
        cast(RoutingResult, request.routing),
        created_at=NOW,
    )
    async with dependencies.unit_of_work() as unit_of_work:
        inserted = await unit_of_work.run_steps.add_plan(*materialized)
        assert inserted.inserted is True
        await unit_of_work.commit()
    return plan


async def _timeline(
    dependencies: OrchestrationDependencies,
    run_id: str,
) -> tuple[str, ...]:
    async with dependencies.unit_of_work() as unit_of_work:
        events = await unit_of_work.audits.list_run(run_id, limit=100)
    return tuple(event.event_type for event in events)


@pytest.mark.asyncio
async def test_run_08_create_replay_restart_preserves_exact_set_and_audits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval-replay.db"
    runtime = await _runtime(path)
    dependencies = _dependencies(runtime)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-08.replay")
    regenerated = _plan(
        plan.run_id,
        seed=100,
        planned_at=NOW + timedelta(days=1),
    )
    service = ApprovalRecordService(dependencies)
    try:
        created = await service.register_plan(
            plan,
            audit_context=_context("register.created"),
        )
        replayed = await service.register_plan(
            regenerated,
            audit_context=_context("register.replayed"),
        )
        assert created.actions.disposition.value == "created"
        assert replayed.actions.disposition.value == "replayed"
        assert replayed.actions.actions == created.actions.actions
        assert replayed.requests == created.requests
        assert regenerated.approval_requests[0].requested_at != (
            created.requests[0].request.requested_at
        )
        assert created.actions.actions[0].action.state is ExternalActionState.AWAITING_APPROVAL
        assert created.actions.actions[0].action.version == 2
        assert created.requests[0].status is ApprovalStatus.PENDING
        async with runtime.session_factory() as session:
            stored_request = await session.get(
                ApprovalRequestRecord,
                created.requests[0].request.id,
            )
            approval_audits = tuple(
                (
                    await session.execute(
                        select(AuditEventRecord).where(
                            AuditEventRecord.event_type.like("approval.%")
                        )
                    )
                ).scalars()
            )
        assert stored_request is not None
        persisted_projection = json.dumps(
            stored_request.redacted_projection,
            sort_keys=True,
        )
        persisted_audit = json.dumps(
            [event.safe_metadata for event in approval_audits],
            sort_keys=True,
        )
        assert "private welcome body" not in persisted_projection
        assert "private welcome body" not in persisted_audit
        assert "[REDACTED]" in persisted_projection
        assert await _timeline(dependencies, plan.run_id) == (
            "run.received",
            "action.proposed",
            "action.awaiting_approval",
            "approval.requested",
        )
    finally:
        await runtime.dispose()

    restarted = await _runtime(path)
    restarted_dependencies = _dependencies(restarted, ids=IncrementingIds(200))
    try:
        after_restart = await ApprovalRecordService(restarted_dependencies).register_plan(
            _plan(
                plan.run_id,
                seed=200,
                planned_at=NOW + timedelta(days=2),
            ),
            audit_context=_context("register.restart"),
        )
        assert after_restart.requests == created.requests
        assert after_restart.actions.actions == created.actions.actions
        assert len(await _timeline(restarted_dependencies, plan.run_id)) == 4
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_run_08_two_write_set_replays_in_plan_order_and_never_heals_partial(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-complete-set.db")
    dependencies = _dependencies(runtime)
    plan = await _seed_run_and_plan(
        dependencies,
        event_id="event.run-08.complete-set",
        multiple_writes=True,
    )
    regenerated, _ = _multi_plan(plan.run_id, seed=100)
    service = ApprovalRecordService(dependencies)
    try:
        created = await service.register_plan(
            plan,
            audit_context=_context("complete-set.created"),
        )
        replayed = await service.register_plan(
            regenerated,
            audit_context=_context("complete-set.replayed"),
        )
        assert [item.action.envelope.step_key for item in created.actions.actions] == [
            "welcome-z",
            "welcome-a",
        ]
        assert [item.envelope.step_id for item in regenerated.proposed_actions] != [
            item.action.step_id for item in created.actions.actions
        ]
        assert replayed.actions.actions == created.actions.actions
        assert replayed.requests == created.requests
        assert len({item.request.authorization_set_id for item in created.requests}) == 1
        assert [item.request.action_id for item in created.requests] == [
            item.action.id for item in created.actions.actions
        ]

        removed = created.requests[1].request.id
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                delete(AuditEventRecord).where(AuditEventRecord.approval_request_id == removed)
            )
            await session.execute(
                delete(ApprovalRequestRecord).where(ApprovalRequestRecord.id == removed)
            )
        with pytest.raises(ApprovalPersistenceConflict) as captured:
            await service.register_plan(
                _multi_plan(plan.run_id, seed=200)[0],
                audit_context=_context("complete-set.partial"),
            )
        assert captured.value.code == "partial_approval_set"
        async with runtime.session_factory() as session:
            counts = (
                int(
                    (
                        await session.execute(select(func.count(ApprovalRequestRecord.id)))
                    ).scalar_one()
                ),
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
            )
        assert counts == (1, 6)

        missing_action = created.actions.actions[1].action.id
        with pytest.raises(IntegrityError):
            async with runtime.session_factory() as session, session.begin():
                await session.execute(
                    delete(AuditEventRecord).where(AuditEventRecord.action_id == missing_action)
                )
                await session.execute(
                    delete(ExternalActionRecord).where(ExternalActionRecord.id == missing_action)
                )
        async with runtime.session_factory() as session:
            final_counts = (
                int(
                    (
                        await session.execute(select(func.count(ExternalActionRecord.id)))
                    ).scalar_one()
                ),
                int(
                    (
                        await session.execute(select(func.count(ApprovalRequestRecord.id)))
                    ).scalar_one()
                ),
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
            )
        assert final_counts == (2, 1, 6)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_audit_fault_rolls_back_actions_requests_and_timeline(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-audit-fault.db")
    normal = _dependencies(runtime)
    plan = await _seed_run_and_plan(normal, event_id="event.run-08.audit-fault")

    def faulting(session: AsyncSession):  # type: ignore[no-untyped-def]
        return FaultAfterAuditAppend(SQLAlchemyAuditRepository(session))

    faulting_dependencies = _dependencies(runtime, audit_factory=faulting)
    try:
        with pytest.raises(RuntimeError, match="injected approval audit failure"):
            await ApprovalRecordService(faulting_dependencies).register_plan(
                plan,
                audit_context=_context("register.fault"),
            )
        async with runtime.session_factory() as session:
            action_count = int(
                (await session.execute(select(func.count(ExternalActionRecord.id)))).scalar_one()
            )
            request_count = int(
                (await session.execute(select(func.count(ApprovalRequestRecord.id)))).scalar_one()
            )
            audit_count = int(
                (await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()
            )
        assert (action_count, request_count, audit_count) == (0, 0, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_expiry_audit_fault_rolls_back_request_and_action(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-expiry-audit-fault.db")
    clock = MutableClock()
    normal = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(normal, event_id="event.run-08.expiry-fault")
    registered = await ApprovalRecordService(normal).register_plan(
        plan,
        audit_context=_context("expiry-fault.register"),
    )
    request = registered.requests[0]
    source = request.request
    decision = ApprovalDecision(
        id="approval-decision.expiry-fault",
        request_id=source.id,
        action_id=source.action_id,
        action_hash=source.action_hash,
        authorization_set_id=source.authorization_set_id,
        run_id=source.run_id,
        plan_hash=source.plan_hash,
        proposal_revision=source.proposal_revision,
        step_id=source.step_id,
        step_key=source.step_key,
        actor_id="principal.approver.expiry-fault",
        authentication_method="internal",
        correlation_id="request.decision.expiry-fault",
        decision=ApprovalDecisionKind.APPROVE,
        authority_roles=source.policy.required_roles,
        authority_scopes=source.policy.required_scopes,
        reason_code="approval_granted",
        decided_at=source.requested_at + timedelta(seconds=1),
    )
    async with normal.unit_of_work() as unit_of_work:
        approved = await unit_of_work.approvals.record_decision(
            expected_version=1,
            expected_action_version=2,
            decision=decision,
        )
        await unit_of_work.commit()

    def faulting(session: AsyncSession):  # type: ignore[no-untyped-def]
        return FaultAfterAuditAppend(SQLAlchemyAuditRepository(session))

    faulting_dependencies = _dependencies(
        runtime,
        clock=clock,
        audit_factory=faulting,
    )
    clock.current = source.expires_at
    try:
        with pytest.raises(RuntimeError, match="injected approval audit failure"):
            await ApprovalRecordService(faulting_dependencies).mark_expired(
                request_id=request.request.id,
                expected_version=2,
                audit_context=_context("expiry-fault.expire"),
            )
        async with normal.unit_of_work() as unit_of_work:
            stored = await unit_of_work.approvals.get(request.request.id)
            action = await unit_of_work.external_actions.get(request.request.action_id)
        assert stored == approved.request
        assert action is not None
        assert action.state is ExternalActionState.APPROVED
        assert action.version == 3
        assert await _timeline(normal, plan.run_id) == (
            "run.received",
            "action.proposed",
            "action.awaiting_approval",
            "approval.requested",
        )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_replay_rejects_missing_request_audit_without_healing(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-missing-audit.db")
    dependencies = _dependencies(runtime)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-08.missing-audit")
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("missing-audit.register"),
    )
    request_id = registered.requests[0].request.id
    try:
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                delete(AuditEventRecord).where(
                    AuditEventRecord.approval_request_id == request_id,
                    AuditEventRecord.event_type == "approval.requested",
                )
            )
        with pytest.raises(AuditPersistenceInvariantError) as captured:
            await ApprovalRecordService(dependencies).register_plan(
                _plan(plan.run_id, seed=100),
                audit_context=_context("missing-audit.replay"),
            )
        assert captured.value.code == "audit_timeline_not_contiguous"
        async with runtime.session_factory() as session:
            counts = (
                int(
                    (
                        await session.execute(select(func.count(ApprovalRequestRecord.id)))
                    ).scalar_one()
                ),
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
            )
        assert counts == (1, 3)
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("missing_event", ["approval.expired", "approval.renewed"])
@pytest.mark.asyncio
async def test_run_08_replay_rejects_missing_renewal_history_without_healing(
    tmp_path: Path,
    missing_event: str,
) -> None:
    runtime = await _runtime(tmp_path / f"approval-missing-{missing_event}.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(
        dependencies,
        event_id=f"event.run-08.missing-{missing_event}",
    )
    service = ApprovalRecordService(dependencies)
    registered = await service.register_plan(
        plan,
        audit_context=_context(f"missing-{missing_event}.register"),
    )
    source = registered.requests[0].request
    clock.current = source.expires_at + timedelta(seconds=1)
    await service.renew_expired(
        request_id=source.id,
        expected_version=1,
        expected_action_hash=source.action_hash,
        audit_context=_context(f"missing-{missing_event}.renew"),
    )
    try:
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                delete(AuditEventRecord).where(
                    AuditEventRecord.run_id == source.run_id,
                    AuditEventRecord.event_type == missing_event,
                )
            )
        async with runtime.session_factory() as session:
            before = (
                int(
                    (
                        await session.execute(select(func.count(ApprovalRequestRecord.id)))
                    ).scalar_one()
                ),
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
            )
        with pytest.raises(AuditPersistenceInvariantError) as captured:
            await service.register_plan(
                _plan(plan.run_id, seed=100),
                audit_context=_context(f"missing-{missing_event}.replay"),
            )
        assert captured.value.code == "audit_timeline_not_contiguous"
        async with runtime.session_factory() as session:
            after = (
                int(
                    (
                        await session.execute(select(func.count(ApprovalRequestRecord.id)))
                    ).scalar_one()
                ),
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
            )
        assert after == before == (2, 6)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_expire_renew_and_response_loss_replay_keep_one_leaf(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-renewal.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-08.renew")
    service = ApprovalRecordService(dependencies)
    try:
        registered = await service.register_plan(
            plan,
            audit_context=_context("renew.register"),
        )
        request = registered.requests[0]
        clock.current = request.request.expires_at
        expired = await service.mark_expired(
            request_id=request.request.id,
            expected_version=1,
            audit_context=_context("renew.expire"),
        )
        assert expired.status is ApprovalStatus.EXPIRED and expired.version == 2
        clock.current += timedelta(seconds=1)
        renewed = await service.renew_expired(
            request_id=request.request.id,
            expected_version=2,
            expected_action_hash=request.request.action_hash,
            audit_context=_context("renew.create"),
        )
        replayed = await service.renew_expired(
            request_id=request.request.id,
            expected_version=2,
            expected_action_hash=request.request.action_hash,
            audit_context=_context("renew.replay"),
        )
        assert replayed == renewed
        assert renewed.expired.version == 3
        assert renewed.replacement.request.generation == 2
        assert renewed.replacement.request.action_id == request.request.action_id
        assert renewed.replacement.request.authorization_set_id == (
            request.request.authorization_set_id
        )
        async with runtime.session_factory() as session:
            request_count = int(
                (await session.execute(select(func.count(ApprovalRequestRecord.id)))).scalar_one()
            )
        assert request_count == 2
        assert (await _timeline(dependencies, plan.run_id))[-3:] == (
            "approval.expired",
            "approval.requested",
            "approval.renewed",
        )
        with pytest.raises(ApprovalRecordServiceError, match="positive integer"):
            await service.renew_expired(
                request_id=request.request.id,
                expected_version=True,  # type: ignore[arg-type]
                expected_action_hash=request.request.action_hash,
                audit_context=_context("renew.bool"),
            )

        shift = timedelta(hours=1)
        async with runtime.session_factory() as session, session.begin():
            source_record = await session.get(ApprovalRequestRecord, request.request.id)
            replacement_record = await session.get(
                ApprovalRequestRecord, renewed.replacement.request.id
            )
            assert source_record is not None and replacement_record is not None
            assert source_record.renewed_at is not None
            source_record.updated_at += shift
            source_record.renewed_at += shift
            replacement_record.requested_at += shift
            replacement_record.expires_at += shift
            replacement_record.updated_at += shift
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as shifted:
                await unit_of_work.approvals.get(request.request.id)
            assert shifted.value.code == "approval_integrity_corrupt"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_approved_renewal_replay_uses_historical_action_version(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-approved-renewal.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(
        dependencies,
        event_id="event.run-08.approved-renewal",
    )
    service = ApprovalRecordService(dependencies)
    registered = await service.register_plan(
        plan,
        audit_context=_context("approved-renewal.register"),
    )
    source = registered.requests[0].request

    def decision_for(
        request: ActionApprovalRequest,
        *,
        suffix: str,
        decided_at: datetime,
    ) -> ApprovalDecision:
        return ApprovalDecision(
            id=f"approval-decision.{suffix}",
            request_id=request.id,
            action_id=request.action_id,
            action_hash=request.action_hash,
            authorization_set_id=request.authorization_set_id,
            run_id=request.run_id,
            plan_hash=request.plan_hash,
            proposal_revision=request.proposal_revision,
            step_id=request.step_id,
            step_key=request.step_key,
            actor_id=f"principal.approver.{suffix}",
            authentication_method="internal",
            correlation_id=f"request.decision.{suffix}",
            decision=ApprovalDecisionKind.APPROVE,
            authority_roles=request.policy.required_roles,
            authority_scopes=request.policy.required_scopes,
            reason_code="approval_granted",
            decided_at=decided_at,
        )

    try:
        async with dependencies.unit_of_work() as unit_of_work:
            decided = await unit_of_work.approvals.record_decision(
                expected_version=1,
                expected_action_version=2,
                decision=decision_for(
                    source,
                    suffix="source",
                    decided_at=source.requested_at + timedelta(seconds=1),
                ),
            )
            await unit_of_work.commit()
        assert decided.request.status is ApprovalStatus.APPROVED

        clock.current = source.expires_at
        expired = await service.mark_expired(
            request_id=source.id,
            expected_version=2,
            audit_context=_context("approved-renewal.expire"),
        )
        assert expired.status is ApprovalStatus.EXPIRED
        clock.current += timedelta(seconds=1)
        renewed = await service.renew_expired(
            request_id=source.id,
            expected_version=3,
            expected_action_hash=source.action_hash,
            audit_context=_context("approved-renewal.create"),
        )
        replacement = renewed.replacement.request
        async with dependencies.unit_of_work() as unit_of_work:
            replacement_decision = await unit_of_work.approvals.record_decision(
                expected_version=1,
                expected_action_version=4,
                decision=decision_for(
                    replacement,
                    suffix="replacement",
                    decided_at=replacement.requested_at + timedelta(seconds=1),
                ),
            )
            await unit_of_work.commit()
        assert replacement_decision.request.status is ApprovalStatus.APPROVED

        replayed = await service.renew_expired(
            request_id=source.id,
            expected_version=3,
            expected_action_hash=source.action_hash,
            audit_context=_context("approved-renewal.replay"),
        )
        assert replayed.expired == renewed.expired
        assert replayed.replacement.request == renewed.replacement.request
        assert replayed.replacement.status is ApprovalStatus.APPROVED
        assert replayed.replacement.version == 2
        async with dependencies.unit_of_work() as unit_of_work:
            action = await unit_of_work.external_actions.get(source.action_id)
        assert action is not None
        assert action.state is ExternalActionState.APPROVED
        assert action.version == 5
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_renewal_race_from_established_snapshots_has_one_leaf(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-renewal-race.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-08.renew-race")
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("renew-race.register"),
    )
    source = registered.requests[0]
    clock.current = source.request.expires_at
    expired = await ApprovalRecordService(dependencies).mark_expired(
        request_id=source.request.id,
        expected_version=1,
        audit_context=_context("renew-race.expire"),
    )
    clock.current += timedelta(seconds=1)
    barrier = asyncio.Event()
    arrival_lock = asyncio.Lock()
    arrivals = 0

    async def renew(suffix: str) -> object:
        nonlocal arrivals
        async with dependencies.unit_of_work() as unit_of_work:
            current = await unit_of_work.approvals.get(source.request.id)
            action = await unit_of_work.external_actions.get(source.request.action_id)
            assert current == expired and action is not None
            renewal = renew_expired_request(
                current=current,
                replacement_request_id=f"approval-request.race.{suffix}",
                exact_action=action.proposal,
                now=clock.current,
                expected_client_hash=source.request.action_hash,
            )
            async with arrival_lock:
                arrivals += 1
                if arrivals == 2:
                    barrier.set()
            await barrier.wait()
            result = await unit_of_work.approvals.renew_expired(
                expected_version=2,
                expected_action_version=2,
                renewal=renewal,
            )
            await unit_of_work.commit()
            return result

    try:
        outcomes = await asyncio.gather(
            renew("a"),
            renew("b"),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
        loser = next(item for item in outcomes if isinstance(item, BaseException))
        assert isinstance(loser, ApprovalPersistenceConflict)
        async with dependencies.unit_of_work() as unit_of_work:
            authoritative = await unit_of_work.approvals.get(source.request.id)
            current_set = await unit_of_work.approvals.list_current_set(
                source.request.run_id,
                source.request.plan_hash,
                source.request.proposal_revision,
            )
        assert authoritative is not None
        assert authoritative.replacement_request_id in {
            "approval-request.race.a",
            "approval-request.race.b",
        }
        assert len(current_set) == 1
        assert current_set[0].request.id == authoritative.replacement_request_id
        async with runtime.session_factory() as session:
            request_count = int(
                (await session.execute(select(func.count(ApprovalRequestRecord.id)))).scalar_one()
            )
        assert request_count == 2
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_service_renewal_race_commits_one_audited_leaf(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-service-renewal-race.db")
    clock = MutableClock()
    normal = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(
        normal,
        event_id="event.run-08.service-renew-race",
    )
    registered = await ApprovalRecordService(normal).register_plan(
        plan,
        audit_context=_context("service-renew-race.register"),
    )
    source = registered.requests[0].request
    clock.current = source.expires_at + timedelta(seconds=1)
    barrier = TwoPartyBarrier()

    def approvals(session: AsyncSession) -> ApprovalRepository:
        return cast(
            ApprovalRepository,
            RenewalBarrierApprovalRepository(session, barrier),
        )

    racing = _dependencies(
        runtime,
        clock=clock,
        approval_factory=approvals,
    )
    service = ApprovalRecordService(racing)
    try:
        outcomes = await asyncio.gather(
            service.renew_expired(
                request_id=source.id,
                expected_version=1,
                expected_action_hash=source.action_hash,
                audit_context=_context("service-renew-race.a"),
            ),
            service.renew_expired(
                request_id=source.id,
                expected_version=1,
                expected_action_hash=source.action_hash,
                audit_context=_context("service-renew-race.b"),
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
        loser = next(item for item in outcomes if isinstance(item, BaseException))
        assert isinstance(loser, ApprovalPersistenceConflict)
        async with runtime.session_factory() as session:
            request_count = int(
                (await session.execute(select(func.count(ApprovalRequestRecord.id)))).scalar_one()
            )
        assert request_count == 2
        assert await _timeline(normal, plan.run_id) == (
            "run.received",
            "action.proposed",
            "action.awaiting_approval",
            "approval.requested",
            "approval.expired",
            "approval.requested",
            "approval.renewed",
        )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_decision_cas_has_one_winner_and_updates_exact_action(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-decision-race.db")
    dependencies = _dependencies(runtime)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-08.decision")
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("decision.register"),
    )
    request = registered.requests[0].request
    barrier = asyncio.Event()
    arrivals = 0
    arrival_lock = asyncio.Lock()

    async def decide(kind: ApprovalDecisionKind, suffix: str) -> object:
        nonlocal arrivals
        decision = ApprovalDecision(
            id=f"approval-decision.{suffix}",
            request_id=request.id,
            action_id=request.action_id,
            action_hash=request.action_hash,
            authorization_set_id=request.authorization_set_id,
            run_id=request.run_id,
            plan_hash=request.plan_hash,
            proposal_revision=request.proposal_revision,
            step_id=request.step_id,
            step_key=request.step_key,
            actor_id=f"principal.approver.{suffix}",
            authentication_method="internal",
            correlation_id=f"request.decision.{suffix}",
            decision=kind,
            authority_roles=request.policy.required_roles,
            authority_scopes=request.policy.required_scopes,
            reason_code=(
                "approval_granted" if kind is ApprovalDecisionKind.APPROVE else "approval_rejected"
            ),
            decided_at=NOW + timedelta(seconds=1),
        )
        async with dependencies.unit_of_work() as unit_of_work:
            observed = await unit_of_work.approvals.get(request.id)
            observed_action = await unit_of_work.external_actions.get(request.action_id)
            assert observed is not None and observed.version == 1
            assert observed_action is not None and observed_action.version == 2
            async with arrival_lock:
                arrivals += 1
                if arrivals == 2:
                    barrier.set()
            await barrier.wait()
            result = await unit_of_work.approvals.record_decision(
                expected_version=1,
                expected_action_version=2,
                decision=decision,
            )
            await unit_of_work.commit()
            return result

    try:
        outcomes = await asyncio.gather(
            decide(ApprovalDecisionKind.APPROVE, "approve"),
            decide(ApprovalDecisionKind.REJECT, "reject"),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
        loser = next(item for item in outcomes if isinstance(item, BaseException))
        assert isinstance(loser, ApprovalPersistenceConflict)
        async with runtime.session_factory() as session:
            decisions = int(
                (await session.execute(select(func.count(ApprovalDecisionRecord.id)))).scalar_one()
            )
            action = (
                await session.execute(
                    select(ExternalActionRecord).where(ExternalActionRecord.id == request.action_id)
                )
            ).scalar_one()
        assert decisions == 1
        assert action.version == 3
        assert action.state in {
            ExternalActionState.APPROVED.value,
            ExternalActionState.REJECTED.value,
        }
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_hydration_rejects_policy_action_and_projection_corruption(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-corruption.db")
    dependencies = _dependencies(runtime)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-08.corruption")
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("corruption.register"),
    )
    request = registered.requests[0].request
    try:
        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            approval = await session.get(ApprovalRequestRecord, request.id)
            assert action is not None and approval is not None
            action.approval_policy_snapshot = {
                **action.approval_policy_snapshot,
                "allow_self_approval": "false",
            }
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as captured:
                await unit_of_work.approvals.get(request.id)
            assert captured.value.code == "approval_authority_corrupt"

        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            assert action is not None
            action.approval_policy_snapshot = {
                **action.approval_policy_snapshot,
                "allow_self_approval": False,
            }
            action.idempotency_key = "idempotency.forged"
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as captured:
                await unit_of_work.approvals.get(request.id)
            assert captured.value.code == "approval_authority_corrupt"

        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            approval = await session.get(ApprovalRequestRecord, request.id)
            assert action is not None and approval is not None
            action.idempotency_key = registered.actions.actions[0].action.idempotency_key
            forged = {
                **action.redacted_projection,
                "payload": {"token": "secret.canary"},
            }
            action.redacted_projection = forged
            approval.redacted_projection = forged
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as captured:
                await unit_of_work.approvals.get(request.id)
            assert captured.value.code == "approval_integrity_corrupt"

        async with runtime.session_factory() as session, session.begin():
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "UPDATE approval_requests SET allow_self_approval = 2 "
                        "WHERE id = :request_id"
                    ),
                    {"request_id": request.id},
                )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_keyed_integrity_digests_reject_fact_rewrites_and_wrong_key(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-integrity-digests.db")
    dependencies = _dependencies(runtime)
    plan = await _seed_run_and_plan(
        dependencies,
        event_id="event.run-08.integrity-digests",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("integrity-digests.register"),
    )
    request = registered.requests[0].request
    decision = ApprovalDecision(
        id="approval-decision.integrity-digests",
        request_id=request.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        step_id=request.step_id,
        step_key=request.step_key,
        actor_id="principal.approver.integrity-digests",
        authentication_method="internal",
        correlation_id="request.decision.integrity-digests",
        decision=ApprovalDecisionKind.APPROVE,
        authority_roles=request.policy.required_roles,
        authority_scopes=request.policy.required_scopes,
        reason_code="approval_granted",
        decided_at=request.requested_at + timedelta(seconds=1),
    )
    try:
        async with runtime.session_factory() as session, session.begin():
            record = await session.get(ApprovalRequestRecord, request.id)
            assert record is not None
            record.requested_by = "principal.forged.requester"
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as requester_rewrite:
                await unit_of_work.approvals.get(request.id)
            assert requester_rewrite.value.code == "approval_integrity_corrupt"

        async with runtime.session_factory() as session, session.begin():
            record = await session.get(ApprovalRequestRecord, request.id)
            assert record is not None
            record.requested_by = request.requested_by

        def wrong_key_repository(session: AsyncSession) -> ApprovalRepository:
            return SQLAlchemyApprovalRepository(
                session,
                DigestKey(bytes(reversed(range(32)))),
            )

        wrong_key_dependencies = _dependencies(
            runtime,
            approval_factory=wrong_key_repository,
        )
        async with wrong_key_dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as wrong_key:
                await unit_of_work.approvals.get(request.id)
            assert wrong_key.value.code == "approval_integrity_corrupt"

        async with dependencies.unit_of_work() as unit_of_work:
            await unit_of_work.approvals.record_decision(
                expected_version=1,
                expected_action_version=2,
                decision=decision,
            )
            await unit_of_work.commit()
        async with runtime.session_factory() as session, session.begin():
            record = await session.get(ApprovalDecisionRecord, decision.id)
            assert record is not None
            record.actor_id = "principal.forged.approver"
            record.authentication_method = "forged"
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as decision_rewrite:
                await unit_of_work.approvals.get(request.id)
            assert decision_rewrite.value.code == "approval_integrity_corrupt"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_use_roundtrip_binds_exact_reservation_and_time(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-use.db")
    dependencies = _dependencies(runtime)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-08.use")
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("use.register"),
    )
    request = registered.requests[0].request
    decision = ApprovalDecision(
        id="approval-decision.use",
        request_id=request.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        step_id=request.step_id,
        step_key=request.step_key,
        actor_id="principal.approver.use",
        authentication_method="internal",
        correlation_id="request.decision.use",
        decision=ApprovalDecisionKind.APPROVE,
        authority_roles=request.policy.required_roles,
        authority_scopes=request.policy.required_scopes,
        reason_code="approval_granted",
        decided_at=request.requested_at + timedelta(seconds=1),
    )
    used_at = decision.decided_at + timedelta(seconds=1)
    reservation_id = "reservation.run-08.use"
    try:
        async with dependencies.unit_of_work() as unit_of_work:
            await unit_of_work.approvals.record_decision(
                expected_version=1,
                expected_action_version=2,
                decision=decision,
            )
            await unit_of_work.commit()
        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            approval = await session.get(ApprovalRequestRecord, request.id)
            assert action is not None and approval is not None
            action.state = ExternalActionState.DISPATCH_RESERVED.value
            action.version = 4
            action.updated_at = used_at
            action.reservation_id = reservation_id
            action.reservation_authorization_set_id = request.authorization_set_id
            action.approval_request_id = request.id
            action.approval_decision_id = decision.id
            action.reservation_action_hash = request.action_hash
            action.reservation_capability_id = request.capability_id
            action.reservation_binding_id = request.binding_id
            action.reservation_idempotency_key = action.idempotency_key
            action.reserved_at = used_at
            approval.status = ApprovalStatus.CONSUMED.value
            approval.version = 3
            approval.updated_at = used_at
            _seal_request_record(approval, APPROVAL_INTEGRITY_KEY)
            await session.flush()
            use_record = ApprovalUseRecord(
                id="approval-use.run-08",
                request_id=request.id,
                decision_id=decision.id,
                action_id=request.action_id,
                action_hash=request.action_hash,
                authorization_set_id=request.authorization_set_id,
                run_id=request.run_id,
                plan_hash=request.plan_hash,
                proposal_revision=request.proposal_revision,
                step_id=request.step_id,
                step_key=request.step_key,
                reservation_id=reservation_id,
                used_at=used_at,
            )
            _seal_use_record(use_record, APPROVAL_INTEGRITY_KEY)
            session.add(use_record)
        async with dependencies.unit_of_work() as unit_of_work:
            stored = await unit_of_work.approvals.get(request.id)
        assert stored is not None
        assert stored.status is ApprovalStatus.CONSUMED
        assert stored.use is not None
        assert stored.use.reservation_id == reservation_id
        assert stored.use.used_at == used_at

        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            assert action is not None
            action.state = ExternalActionState.REJECTED.value
            action.terminal_reason_code = "approval_rejected"
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as state_conflict:
                await unit_of_work.approvals.get(request.id)
            assert state_conflict.value.code == "approval_request_corrupt"
        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            assert action is not None
            action.state = ExternalActionState.DISPATCH_RESERVED.value
            action.terminal_reason_code = None

        async with runtime.session_factory() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        update(ApprovalUseRecord)
                        .where(ApprovalUseRecord.request_id == request.id)
                        .values(used_at=used_at + timedelta(seconds=1))
                    )
        async with runtime.session_factory() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        update(ApprovalUseRecord)
                        .where(ApprovalUseRecord.request_id == request.id)
                        .values(authorization_set_id="authorization-set.forged")
                    )

        async with runtime.engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            await connection.commit()
            await connection.execute(
                update(ApprovalUseRecord)
                .where(ApprovalUseRecord.request_id == request.id)
                .values(used_at=used_at + timedelta(seconds=1))
            )
            await connection.commit()
            await connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            await connection.commit()
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as captured:
                await unit_of_work.approvals.get(request.id)
            assert captured.value.code == "approval_integrity_corrupt"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_08_hydration_rejects_request_action_lifecycle_contradictions(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "approval-lifecycle-corruption.db")
    dependencies = _dependencies(runtime)
    plan = await _seed_run_and_plan(
        dependencies,
        event_id="event.run-08.lifecycle-corruption",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("lifecycle-corruption.register"),
    )
    request = registered.requests[0].request
    decided_at = request.requested_at + timedelta(seconds=1)
    decision = ApprovalDecision(
        id="approval-decision.lifecycle-corruption",
        request_id=request.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        step_id=request.step_id,
        step_key=request.step_key,
        actor_id="principal.approver.lifecycle-corruption",
        authentication_method="internal",
        correlation_id="request.decision.lifecycle-corruption",
        decision=ApprovalDecisionKind.APPROVE,
        authority_roles=request.policy.required_roles,
        authority_scopes=request.policy.required_scopes,
        reason_code="approval_granted",
        decided_at=decided_at,
    )
    try:
        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            assert action is not None
            action.state = ExternalActionState.APPROVED.value
            action.version = 3
            action.updated_at = decided_at
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as pending_conflict:
                await unit_of_work.approvals.get(request.id)
            assert pending_conflict.value.code == "approval_request_corrupt"

        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            assert action is not None
            action.state = ExternalActionState.AWAITING_APPROVAL.value
            action.version = 3
            action.updated_at = request.requested_at
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as version_conflict:
                await unit_of_work.approvals.get(request.id)
            assert version_conflict.value.code == "approval_action_lifecycle_corrupt"

        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            assert action is not None
            action.version = 2
            action.updated_at = request.requested_at
        async with dependencies.unit_of_work() as unit_of_work:
            await unit_of_work.approvals.record_decision(
                expected_version=1,
                expected_action_version=2,
                decision=decision,
            )
            await unit_of_work.commit()

        async with runtime.session_factory() as session, session.begin():
            action = await session.get(ExternalActionRecord, request.action_id)
            assert action is not None
            action.state = ExternalActionState.AWAITING_APPROVAL.value
            action.version = 2
            action.updated_at = request.requested_at
        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as approved_conflict:
                await unit_of_work.approvals.get(request.id)
            assert approved_conflict.value.code == "approval_request_corrupt"
    finally:
        await runtime.dispose()
