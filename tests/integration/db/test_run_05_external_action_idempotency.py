"""RUN-05: durable exact-action registration, dispatch, and recovery."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
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
from marketing_agents.application.ports.repositories import AuditRepository
from marketing_agents.application.services import (
    DispatchDisposition,
    ExternalActionDispatcher,
    ExternalActionRegistrationDisposition,
    ExternalActionRegistrationService,
)
from marketing_agents.domain.action_hash import canonical_action_hash
from marketing_agents.domain.entities import MAX_DELIVERY_ATTEMPTS, ExternalAction
from marketing_agents.domain.enums import ExternalActionState
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
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
    SQLAlchemyConnectorReceiptRepository,
    SQLAlchemyExternalActionRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models import (
    ConnectorActionReceiptRecord,
    ExternalActionRecord,
    RunRecord,
    WorkItemRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.application.test_run_02_effect_aware_planning import (
    COMMUNITY_BINDING,
    REGISTRY,
    WORKFLOW_HASH,
    RecordingIds,
    _planner,
    _read_step,
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


def _unused_audit_repository(_session: AsyncSession) -> AuditRepository:
    return cast(AuditRepository, object())


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _uow_factory(runtime: DatabaseRuntime) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=_unused_audit_repository,
            external_actions=SQLAlchemyExternalActionRepository,
            connector_receipts=SQLAlchemyConnectorReceiptRepository,
        ),
    )


def _dependencies(runtime: DatabaseRuntime, clock: MutableClock) -> OrchestrationDependencies:
    return OrchestrationDependencies(clock, UnusedIds(), _uow_factory(runtime))


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
    planner, _, _ = _planner(ids=RecordingIds(seed=seed))
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
                    TopologyStep("membership", 1),
                    TopologyStep("welcome-a", 2, ("membership",)),
                    TopologyStep("welcome-b", 3, ("welcome-a",), terminal_result=True),
                ),
                workflow_max_steps=10,
                global_max_steps=20,
            ),
            routing=_route(include_write=True),  # type: ignore[arg-type]
            steps=(_read_step(), first, second),
            requested_by="principal.local.operator",
        )
    )


async def _reserve(runtime: DatabaseRuntime, action: ExternalAction) -> None:
    """Model the future RUN-08 atomic approval output, not a public RUN-05 bypass."""

    async with runtime.session_factory() as session, session.begin():
        await session.execute(
            update(ExternalActionRecord)
            .where(ExternalActionRecord.id == action.id)
            .values(
                state=ExternalActionState.DISPATCH_RESERVED.value,
                reservation_id=f"reservation.{action.id}",
                reservation_authorization_set_id=action.envelope.authorization_set_id,
                approval_request_id=f"approval-request.persisted.{action.id}",
                approval_decision_id=f"approval-decision.persisted.{action.id}",
                reservation_action_hash=action.action_hash,
                reservation_capability_id=action.envelope.capability_id,
                reservation_binding_id=action.connector_binding_id,
                reservation_idempotency_key=action.idempotency_key,
                reserved_at=NOW,
                updated_at=NOW,
                version=2,
            )
        )


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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
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
            safe_metadata={"token": "RUN05_SECRET_CANARY"},
        )


@pytest.mark.asyncio
async def test_run_05_two_dispatch_workers_make_exactly_one_connector_call(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "dispatch-race.db")
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    created = await registration.register_plan_actions(_plan(seed=1))
    await _reserve(runtime, created.actions[0].action)
    gateway, _ = _gateway(runtime, clock)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            action = cast(
                ExternalAction,
                await unit_of_work.external_actions.get(created.actions[0].action.id),
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(
        _dependencies(runtime, clock), delivery_attempt_limit=1
    )
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registered = await ExternalActionRegistrationService(
        _dependencies(runtime, clock)
    ).register_plan_actions(_plan(seed=1))
    action = registered.actions[0].action
    await _reserve(runtime, action)
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(
        _dependencies(runtime, clock), delivery_attempt_limit=1
    )
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(
        _dependencies(runtime, clock), delivery_attempt_limit=1
    )
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
    gateway, ledger = _gateway(runtime, clock)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=current.version,
                expected_run_version=5,
                lease_owner="worker.crashed-after-receipt",
                claimed_at=clock.now(),
                lease_expires_at=clock.now() + timedelta(seconds=1),
            )
            assert claimed is not None
            marked = await unit_of_work.external_actions.mark_call_started(
                action_id=action.id,
                expected_version=claimed.version,
                expected_run_version=5,
                lease_owner="worker.crashed-after-receipt",
                attempt_number=claimed.delivery_attempt_count,
                started_at=clock.now(),
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(
        _dependencies(runtime, clock), delivery_attempt_limit=1
    )
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
    gateway, ledger = _gateway(runtime, clock)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(
                ExternalAction,
                await unit_of_work.external_actions.get(action.id),
            )
            run = cast(object, await unit_of_work.runs.get(action.run_id))
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=current.version,
                expected_run_version=run.version,  # type: ignore[attr-defined]
                lease_owner="worker.crashed-before-call",
                claimed_at=clock.now(),
                lease_expires_at=clock.now() + timedelta(seconds=1),
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
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
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=current.version,
                expected_run_version=5,
                lease_owner="worker.unavailable",
                claimed_at=clock.now(),
                lease_expires_at=clock.now() + timedelta(seconds=1),
            )
            assert claimed is not None
            marked = await unit_of_work.external_actions.mark_call_started(
                action_id=action.id,
                expected_version=claimed.version,
                expected_run_version=5,
                lease_owner="worker.unavailable",
                attempt_number=claimed.delivery_attempt_count,
                started_at=clock.now(),
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
    try:
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            first = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=current.version,
                expected_run_version=5,
                lease_owner="worker.generation.1",
                claimed_at=clock.now(),
                lease_expires_at=clock.now() + timedelta(seconds=1),
            )
            assert first is not None
            first_marked = await unit_of_work.external_actions.mark_call_started(
                action_id=action.id,
                expected_version=first.version,
                expected_run_version=5,
                lease_owner="worker.generation.1",
                attempt_number=first.delivery_attempt_count,
                started_at=clock.now(),
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
            second = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=cast(ExternalAction, released).version,
                expected_run_version=5,
                lease_owner="worker.generation.2",
                claimed_at=clock.now(),
                lease_expires_at=clock.now() + timedelta(seconds=10),
            )
            assert second is not None
            second_marked = await unit_of_work.external_actions.mark_call_started(
                action_id=action.id,
                expected_version=second.version,
                expected_run_version=5,
                lease_owner="worker.generation.2",
                attempt_number=second.delivery_attempt_count,
                started_at=clock.now(),
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
    await _seed_parent(runtime)
    clock = MutableClock()
    registration = ExternalActionRegistrationService(_dependencies(runtime, clock))
    created = await registration.register_plan_actions(_plan(seed=1))
    action = created.actions[0].action
    await _reserve(runtime, action)
    try:
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(RunRecord)
                .where(RunRecord.id == RUN_ID)
                .values(
                    state="cancelled",
                    terminal_reason_code="operator_cancelled",
                    updated_at=clock.now(),
                    version=6,
                )
            )
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=current.version,
                expected_run_version=6,
                lease_owner="worker.cancelled",
                claimed_at=clock.now(),
                lease_expires_at=clock.now() + timedelta(seconds=10),
            )
            assert claimed is None
        async with runtime.session_factory() as session:
            row = await session.get(ExternalActionRecord, action.id)
            assert row is not None
            assert row.state == ExternalActionState.DISPATCH_RESERVED.value
            assert row.delivery_attempt_count == 0
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(RunRecord)
                .where(RunRecord.id == RUN_ID)
                .values(
                    state="executing",
                    terminal_reason_code=None,
                    updated_at=clock.now(),
                    version=7,
                )
            )
        async with _uow_factory(runtime)() as unit_of_work:
            current = cast(ExternalAction, await unit_of_work.external_actions.get(action.id))
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=current.version,
                expected_run_version=7,
                lease_owner="worker.claimed",
                claimed_at=clock.now(),
                lease_expires_at=clock.now() + timedelta(seconds=10),
            )
            assert claimed is not None
            await unit_of_work.commit()
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(RunRecord)
                .where(RunRecord.id == RUN_ID)
                .values(
                    state="cancelled",
                    terminal_reason_code="operator_cancelled_before_call",
                    updated_at=clock.now(),
                    version=8,
                )
            )
        async with _uow_factory(runtime)() as unit_of_work:
            blocked = await unit_of_work.external_actions.mark_call_started(
                action_id=action.id,
                expected_version=cast(ExternalAction, claimed).version,
                expected_run_version=8,
                lease_owner="worker.claimed",
                attempt_number=cast(ExternalAction, claimed).delivery_attempt_count,
                started_at=clock.now(),
            )
            assert blocked is None
        assert await _counts(runtime) == (1, 0)
    finally:
        await runtime.dispose()
