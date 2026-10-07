"""AC-13 terminal-only worker lane over real persisted call and cancellation evidence."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from marketing_agents.application.policies.write_authorization import WriteAuthorizationGuard
from marketing_agents.application.services import (
    ControlledReadCommand,
    ControlledReadExecutor,
    ControlledReadExecutorError,
    DispatchDisposition,
    ExternalActionDispatcher,
    ExternalActionDispatchError,
    RunCancellationCoordinator,
    RunCancellationService,
)
from marketing_agents.application.services.cancelled_run_recovery import (
    _NoReadAdapter,
    _NoWriteGateway,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import ExternalActionState, StepState
from marketing_agents.domain.execution_control import AttemptOutcome
from marketing_agents.infrastructure.db import (
    Base,
    SQLAlchemyAuditRepository,
    SQLAlchemyExecutionControlRepository,
)
from marketing_agents.infrastructure.db.models import RunExecutionControlRecord, RunStepRecord
from marketing_agents.infrastructure.runtime.run_claims import (
    ACTIVE_STATES,
    RunClaims,
    RunRecoveryClaimLost,
)
from marketing_agents.workers.runtime.composition import LocalRuntime
from marketing_agents.workers.runtime.run_loop import RunWorker
from sqlalchemy import select, update

from tests.integration.db.test_orch_06_controlled_read_executor import (
    CrashAfterCommittedReservationAdapter,
    IncrementingIds,
    SimulatedWorkerCrash,
    _audit_context,
    _prepare,
)
from tests.integration.db.test_run_05_external_action_idempotency import (
    MutableClock,
    _dependencies,
    _gateway,
    _released_action,
    _runtime,
)


def _worker_runtime(database, dependencies):
    # Deliberately no workflow registry/providers: the focused terminal lane
    # must use only persisted evidence. Composed acceptance covers real startup.
    return cast(LocalRuntime, SimpleNamespace(database=database, dependencies=dependencies))


async def _business_snapshot(database):
    async with database.session_factory() as session:
        return {
            table.name: tuple(
                dict(row)
                for row in (
                    await session.execute(select(table).order_by(*table.primary_key.columns))
                ).mappings()
            )
            for table in Base.metadata.sorted_tables
            if table.name != "run_worker_claims"
        }


async def _orphan_read(tmp_path):
    prepared = await _prepare(tmp_path / "read.db", max_attempts=2, step_timeout_seconds=10)
    adapter = CrashAfterCommittedReservationAdapter()
    with pytest.raises(SimulatedWorkerCrash):
        await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("ac13-crash"),
        )
    assert len(adapter.calls) == 1
    async with prepared.dependencies.unit_of_work() as uow:
        (attempt,) = await uow.execution_control.list_attempts(
            prepared.step_id, prepared.operation_key
        )
    return prepared, attempt


@pytest.mark.asyncio
async def test_ac_13_read_lane_respects_deadline_claim_lease_and_single_owner(tmp_path: Path):
    prepared, attempt = await _orphan_read(tmp_path)
    runtime = _worker_runtime(prepared.runtime, prepared.dependencies)
    claims = RunClaims(runtime)
    try:
        active = await claims.claim_once("worker.ac13.active")
        assert active is not None and active.purpose == "advance"
        cancelled = await RunCancellationService(prepared.dependencies).request(
            prepared.run_id, audit_context=_audit_context("ac13-cancel")
        )
        before = await _business_snapshot(prepared.runtime)
        prepared.clock.current = attempt.call_deadline_at - timedelta(microseconds=1)
        assert await claims.claim_once("worker.ac13.early") is None
        prepared.clock.current = attempt.call_deadline_at
        assert await claims.claim_once("worker.ac13.lease-fenced") is None
        assert await _business_snapshot(prepared.runtime) == before
        assert await claims.release(active)
        assert await claims.claim_once("worker.ac13.available-fenced") is None
        prepared.clock.current += timedelta(seconds=1)
        winners = await asyncio.gather(
            claims.claim_once("worker.ac13.racer-a"), claims.claim_once("worker.ac13.racer-b")
        )
        owned = [item for item in winners if item is not None]
        assert len(owned) == 1 and owned[0].purpose == "cancelled_recovery"
        assert "cancelled" not in ACTIVE_STATES
        assert await claims.release(owned[0])
        prepared.clock.current += timedelta(seconds=1)
        worker = RunWorker(runtime, "worker.ac13.recover")
        assert await worker.drain_once()
        async with prepared.dependencies.unit_of_work() as uow:
            assert await uow.runs.get(prepared.run_id) == cancelled.run
            (closed,) = await uow.execution_control.list_attempts(
                prepared.step_id, prepared.operation_key
            )
            step = await uow.run_steps.get(prepared.step_id)
            control = await uow.execution_control.get(prepared.run_id)
            events = await uow.audits.list_run(prepared.run_id)
        assert closed.id == attempt.id and closed.outcome is AttemptOutcome.CANCELLED
        assert closed.retry_not_before is None and closed.output_artifact_id is None
        assert step.state is StepState.FAILED and step.terminal_reason_code == "run_cancelled"
        old_control = before["run_execution_controls"][0]
        assert (control.model_calls, control.tool_calls) == (
            old_control["model_calls"],
            old_control["tool_calls"],
        )
        assert [event.event_type for event in events[-2:]] == [
            "attempt.completed",
            "step.transitioned",
        ]
        assert {event.actor_id for event in events[-2:]} == {
            AuditContext.worker("worker.ac13.recover", correlation_id="proof.ac13").actor_id
        }
        after = await _business_snapshot(prepared.runtime)
        prepared.clock.current += timedelta(seconds=200)
        assert not await RunWorker(runtime, "worker.ac13.again").drain_once()
        assert await _business_snapshot(prepared.runtime) == after
    finally:
        await prepared.runtime.dispose()


class _CrashWrite:
    def __init__(self, delegate, *, receipt):
        self.delegate = delegate
        self.receipt = receipt
        self.calls = 0

    def contract_for(self, action):
        return self.delegate.contract_for(action)

    async def execute(self, authorization):
        self.calls += 1
        if self.receipt:
            await self.delegate.execute(authorization)
        raise SimulatedWorkerCrash("controlled process interruption")


async def _orphan_write(tmp_path, *, receipt, lease_seconds):
    database = await _runtime(tmp_path / "write.db")
    clock = MutableClock()
    action = await _released_action(database, clock, seed=1313)
    dependencies = _dependencies(database, clock, ids=IncrementingIds())
    delegate, ledger = _gateway(database, clock)
    gateway = _CrashWrite(delegate, receipt=receipt)
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
        lease_duration=timedelta(seconds=lease_seconds),
    )
    with pytest.raises(SimulatedWorkerCrash):
        await dispatcher.dispatch_once(action.id, lease_owner="worker.ac13.crashed")
    assert gateway.calls == 1 and ledger.side_effect_count == int(receipt)
    async with dependencies.unit_of_work() as uow:
        action = await uow.external_actions.get(action.id)
    return database, clock, dependencies, action, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", (True, False))
@pytest.mark.parametrize("lease_seconds", (1, 120))
async def test_ac_13_write_lane_closes_exact_receipt_or_unknown_without_retry(
    tmp_path: Path, receipt: bool, lease_seconds: int
):
    database, clock, dependencies, action, gateway = await _orphan_write(
        tmp_path, receipt=receipt, lease_seconds=lease_seconds
    )
    try:
        cancelled = await RunCancellationCoordinator(dependencies).request(
            action.run_id, audit_context=_audit_context("ac13-write-cancel")
        )
        before = await _business_snapshot(database)
        runtime = _worker_runtime(database, dependencies)
        worker = RunWorker(runtime, "worker.ac13.write-recovery")
        due = max(action.lease.expires_at, action.call_deadline_at)
        clock.current = due - timedelta(microseconds=1)
        assert not await worker.drain_once()
        direct = ExternalActionDispatcher(
            dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
        )
        assert (
            await direct.recover_cancelled_action(action.id)
        ).disposition is DispatchDisposition.RECOVERY_PENDING
        assert await _business_snapshot(database) == before
        clock.current = due
        assert await worker.drain_once()
        async with dependencies.unit_of_work() as uow:
            assert await uow.runs.get(action.run_id) == cancelled.run
            closed = await uow.external_actions.get(action.id)
            step = await uow.run_steps.get(action.step_id)
            events = await uow.audits.list_run(action.run_id)
        assert closed.state is (
            ExternalActionState.SUCCEEDED if receipt else ExternalActionState.OUTCOME_UNKNOWN
        )
        assert step.state is (StepState.SUCCEEDED if receipt else StepState.FAILED)
        assert closed.idempotency_key == action.idempotency_key
        assert closed.action_hash == action.action_hash
        assert gateway.calls == 1
        assert {event.actor_id for event in events[-2:]} == {
            AuditContext.worker("worker.ac13.write-recovery", correlation_id="proof.ac13").actor_id
        }
        after = await _business_snapshot(database)
        for table in (
            "approval_uses",
            "approval_decisions",
            "approval_requests",
            "authorization_sets",
            "authorization_set_members",
            "connector_action_receipts",
            "execution_attempts",
            "rate_limit_windows",
            "artifacts",
            "run_execution_controls",
        ):
            assert after[table] == before[table], table
        # The successful READ sibling remains identical; only the WRITE step closes.
        assert tuple(row for row in after["run_steps"] if row["id"] != action.step_id) == tuple(
            row for row in before["run_steps"] if row["id"] != action.step_id
        )
        clock.current += timedelta(seconds=200)
        assert not await RunWorker(runtime, "worker.ac13.write-again").drain_once()
        assert await _business_snapshot(database) == after
        replay = await direct.recover_cancelled_action(action.id)
        assert replay.disposition is (
            DispatchDisposition.ALREADY_SUCCEEDED
            if receipt
            else DispatchDisposition.OUTCOME_UNKNOWN
        )
        assert await _business_snapshot(database) == after
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("control", "step"))
async def test_ac_13_cancelled_recovery_corruption_fails_without_business_mutation(
    tmp_path: Path, tamper: str
):
    prepared, attempt = await _orphan_read(tmp_path)
    try:
        await RunCancellationService(prepared.dependencies).request(
            prepared.run_id, audit_context=_audit_context("ac13-tamper-cancel")
        )
        async with prepared.runtime.session_factory() as session, session.begin():
            if tamper == "control":
                await session.execute(
                    update(RunExecutionControlRecord).values(integrity_digest="0" * 64)
                )
            else:
                await session.execute(update(RunStepRecord).values(source_order=999))
        prepared.clock.current = attempt.call_deadline_at
        before = await _business_snapshot(prepared.runtime)
        with pytest.raises((RuntimeError, ValueError)):
            await RunWorker(
                _worker_runtime(prepared.runtime, prepared.dependencies), "worker.ac13.tamper"
            ).drain_once()
        assert await _business_snapshot(prepared.runtime) == before
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_ac_13_read_recovery_audit_failure_rolls_back_and_retries_only_closure(
    tmp_path: Path, monkeypatch
):
    prepared, attempt = await _orphan_read(tmp_path)
    try:
        await RunCancellationService(prepared.dependencies).request(
            prepared.run_id, audit_context=_audit_context("ac13-audit-cancel")
        )
        prepared.clock.current = attempt.call_deadline_at
        original = SQLAlchemyAuditRepository.append_many

        async def fail_after_audit(self, events):
            result = await original(self, events)
            if any(event.event_type == "attempt.completed" for event in events):
                raise RuntimeError("injected terminal audit failure")
            return result

        monkeypatch.setattr(SQLAlchemyAuditRepository, "append_many", fail_after_audit)
        before = await _business_snapshot(prepared.runtime)
        runtime = _worker_runtime(prepared.runtime, prepared.dependencies)
        with pytest.raises(RuntimeError, match="injected terminal audit failure"):
            await RunWorker(runtime, "worker.ac13.audit-fault").drain_once()
        assert await _business_snapshot(prepared.runtime) == before
        monkeypatch.setattr(SQLAlchemyAuditRepository, "append_many", original)
        prepared.clock.current += timedelta(seconds=1)
        assert await RunWorker(runtime, "worker.ac13.audit-restored").drain_once()
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_ac_13_terminal_methods_refuse_active_parent_without_reserving_retry(tmp_path: Path):
    prepared, attempt = await _orphan_read(tmp_path)
    try:
        prepared.clock.current = attempt.call_deadline_at
        before = await _business_snapshot(prepared.runtime)
        with pytest.raises(ControlledReadExecutorError, match="cancelled execution plan"):
            await ControlledReadExecutor(
                prepared.dependencies, _NoReadAdapter()
            ).recover_cancelled_attempt(
                prepared.step_id, audit_context=_audit_context("ac13-wrong-parent")
            )
        assert await _business_snapshot(prepared.runtime) == before
    finally:
        await prepared.runtime.dispose()
    database, clock, dependencies, action, gateway = await _orphan_write(
        tmp_path, receipt=False, lease_seconds=1
    )
    try:
        clock.current = max(action.lease.expires_at, action.call_deadline_at)
        before = await _business_snapshot(database)
        with pytest.raises(ExternalActionDispatchError, match="exact cancelled parent"):
            await ExternalActionDispatcher(
                dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
            ).recover_cancelled_action(action.id)
        assert await _business_snapshot(database) == before
        assert gateway.calls == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ("lease", "call_started_at", "call_deadline_at"))
async def test_ac_13_dedicated_write_recovery_rejects_missing_call_authority(
    tmp_path: Path, monkeypatch, missing
):
    database, _clock, dependencies, action, _ = await _orphan_write(
        tmp_path, receipt=False, lease_seconds=1
    )
    try:
        object.__setattr__(action, missing, None)
        dispatcher = ExternalActionDispatcher(
            dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
        )
        monkeypatch.setattr(dispatcher, "_load_required", AsyncMock(return_value=action))
        with pytest.raises(ExternalActionDispatchError, match="existing call"):
            await dispatcher.recover_cancelled_action(action.id)
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("read", "write"))
async def test_ac_13_stale_worker_cannot_close_evidence_after_claim_takeover(tmp_path: Path, kind):
    if kind == "read":
        prepared, attempt = await _orphan_read(tmp_path)
        database, clock, dependencies, run_id = (
            prepared.runtime,
            prepared.clock,
            prepared.dependencies,
            prepared.run_id,
        )
        await RunCancellationService(dependencies).request(
            run_id, audit_context=_audit_context("ac13-takeover-read")
        )
        clock.current = attempt.call_deadline_at
    else:
        database, clock, dependencies, action, _ = await _orphan_write(
            tmp_path, receipt=True, lease_seconds=1
        )
        run_id = action.run_id
        await RunCancellationCoordinator(dependencies).request(
            run_id, audit_context=_audit_context("ac13-takeover-write")
        )
        clock.current = max(action.call_deadline_at, action.lease.expires_at)
    try:
        runtime = _worker_runtime(database, dependencies)
        stale_worker = RunWorker(runtime, "worker.ac13.stale")
        current_worker = RunWorker(runtime, "worker.ac13.current")
        stale = await stale_worker.claims.claim_once(stale_worker.worker_id)
        assert stale is not None and stale.purpose == "cancelled_recovery"
        clock.current = stale.expires_at
        current = await current_worker.claims.claim_once(current_worker.worker_id)
        assert current is not None and current.token != stale.token
        before = await _business_snapshot(database)
        with pytest.raises(RunRecoveryClaimLost):
            await stale_worker._recover_cancelled(stale)
        assert await _business_snapshot(database) == before
        assert not await stale_worker.claims.release(stale)
        await current_worker._recover_cancelled(current)
        assert await current_worker.claims.release(current)
        async with dependencies.unit_of_work() as uow:
            events = await uow.audits.list_run(run_id)
        assert {event.actor_id for event in events[-2:]} == {
            AuditContext.worker(current_worker.worker_id, correlation_id="proof.ac13").actor_id
        }
        after = await _business_snapshot(database)
        clock.current += timedelta(seconds=1)
        assert not await current_worker.drain_once()
        assert await _business_snapshot(database) == after
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ac_13_missing_terminal_result_never_falls_through_to_retry(
    tmp_path: Path, monkeypatch
):
    database, clock, dependencies, action, _ = await _orphan_write(
        tmp_path, receipt=False, lease_seconds=1
    )
    try:
        clock.current = max(action.call_deadline_at, action.lease.expires_at)
        dispatcher = ExternalActionDispatcher(
            dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
        )
        monkeypatch.setattr(
            dispatcher, "_finalize_terminal_parent_call", AsyncMock(return_value=None)
        )
        before = await _business_snapshot(database)
        with pytest.raises(ExternalActionDispatchError, match="cannot resume execution"):
            await dispatcher.recover_cancelled_action(action.id)
        assert await _business_snapshot(database) == before
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ac_13_concurrent_real_receipt_closure_returns_winner_without_second_audit(
    tmp_path: Path, monkeypatch
):
    database, clock, dependencies, action, gateway = await _orphan_write(
        tmp_path, receipt=True, lease_seconds=1
    )
    try:
        await RunCancellationCoordinator(dependencies).request(
            action.run_id, audit_context=_audit_context("ac13-completion-race-cancel")
        )
        clock.current = max(action.call_deadline_at, action.lease.expires_at)
        winner = ExternalActionDispatcher(
            dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
        )
        observer = ExternalActionDispatcher(
            dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
        )
        original_load = observer._load_required
        winning_snapshot = None

        async def load_then_other_worker_finishes(action_id):
            nonlocal winning_snapshot
            stale = await original_load(action_id)
            completed = await winner.recover_cancelled_action(
                action_id, audit_context=_audit_context("ac13-real-winner")
            )
            assert completed.disposition is DispatchDisposition.SUCCEEDED
            winning_snapshot = await _business_snapshot(database)
            return stale

        monkeypatch.setattr(observer, "_load_required", load_then_other_worker_finishes)
        result = await observer.recover_cancelled_action(action.id)
        assert result.disposition is DispatchDisposition.ALREADY_SUCCEEDED
        assert await _business_snapshot(database) == winning_snapshot
        assert gateway.calls == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ("cancel_requested_at", "started_at"))
async def test_ac_13_terminal_write_rejects_in_memory_control_loss_without_retry(
    tmp_path: Path, monkeypatch, missing
):
    database, clock, dependencies, action, _ = await _orphan_write(
        tmp_path, receipt=False, lease_seconds=1
    )
    try:
        await RunCancellationCoordinator(dependencies).request(
            action.run_id, audit_context=_audit_context("ac13-control-loss-cancel")
        )
        clock.current = max(action.call_deadline_at, action.lease.expires_at)
        original = SQLAlchemyExecutionControlRepository.get

        async def corrupted_control(self, run_id):
            control = await original(self, run_id)
            object.__setattr__(control, missing, None)
            return control

        monkeypatch.setattr(SQLAlchemyExecutionControlRepository, "get", corrupted_control)
        before = await _business_snapshot(database)
        with pytest.raises(ExternalActionDispatchError, match="exact cancelled parent"):
            await ExternalActionDispatcher(
                dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
            ).recover_cancelled_action(action.id)
        assert await _business_snapshot(database) == before
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ac_13_terminal_write_finalizer_rechecks_live_action_lease(tmp_path: Path):
    database, clock, dependencies, action, _ = await _orphan_write(
        tmp_path, receipt=False, lease_seconds=120
    )
    try:
        await RunCancellationCoordinator(dependencies).request(
            action.run_id, audit_context=_audit_context("ac13-inner-lease-cancel")
        )
        clock.current = action.call_deadline_at
        assert clock.current < action.lease.expires_at
        before = await _business_snapshot(database)
        dispatcher = ExternalActionDispatcher(
            dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
        )
        with pytest.raises(ExternalActionDispatchError, match="expired call"):
            await dispatcher._finalize_terminal_parent_call(action, cancelled_only=True)
        assert await _business_snapshot(database) == before
    finally:
        await database.dispose()
