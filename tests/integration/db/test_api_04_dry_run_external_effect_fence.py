"""API-04 durable dry-run fence at the final external-effect boundary."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.write_authorization import (
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.external_writes import (
    ConnectorDeliveryContract,
    ExternalWriteConnectorGateway,
)
from marketing_agents.application.services.external_action_dispatcher import (
    DispatchDisposition,
    ExternalActionDispatcher,
    ExternalActionDispatchError,
)
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState, WorkMode
from marketing_agents.infrastructure.db.models import (
    ConnectorActionReceiptRecord,
    ExternalActionDispatchAttemptRecord,
    WorkItemRecord,
)
from sqlalchemy import func, select, update

from tests.integration.db.test_run_05_external_action_idempotency import (
    MutableClock,
    UnusedIds,
    _dependencies,
    _gateway,
    _released_action,
    _runtime,
    _uow_factory,
)


class _CountingGateway:
    def __init__(self, delegate: ExternalWriteConnectorGateway) -> None:
        self._delegate = delegate
        self.contract_calls = 0
        self.execute_calls = 0

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        self.contract_calls += 1
        return self._delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        self.execute_calls += 1
        return await self._delegate.execute(authorization)


class _GetFaultRepository:
    def __init__(self, delegate: Any, *, missing: bool, mismatch: bool) -> None:
        self._delegate = delegate
        self._missing = missing
        self._mismatch = mismatch

    async def get(self, item_id: str):  # type: ignore[no-untyped-def]
        item = await self._delegate.get(item_id)
        if self._missing:
            return None
        if self._mismatch and item is not None:
            return replace(item, id=f"{item.id}.mismatch")
        return item

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _ContextFaultUnitOfWork:
    def __init__(self, delegate: Any, fault: str) -> None:
        self._delegate = delegate
        self._fault = fault

    @property
    def runs(self) -> object:
        return _GetFaultRepository(
            self._delegate.runs,
            missing=self._fault == "run_missing",
            mismatch=self._fault == "run_mismatch",
        )

    @property
    def works(self) -> object:
        return _GetFaultRepository(
            self._delegate.works,
            missing=self._fault == "work_missing",
            mismatch=self._fault == "work_mismatch",
        )

    async def __aenter__(self) -> _ContextFaultUnitOfWork:
        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        await self._delegate.__aexit__(exc_type, exc, traceback)

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _ContextFaultUnitOfWorkFactory:
    def __init__(self, delegate: Any, fault: str) -> None:
        self._delegate = delegate
        self._fault = fault

    def __call__(self) -> _ContextFaultUnitOfWork:
        return _ContextFaultUnitOfWork(self._delegate(), self._fault)


async def _dispatch_attempt_and_receipt_counts(
    runtime: Any,
    action_id: str,
) -> tuple[int, int]:
    async with runtime.session_factory() as session:
        attempts = int(
            (
                await session.execute(
                    select(func.count(ExternalActionDispatchAttemptRecord.attempt_number)).where(
                        ExternalActionDispatchAttemptRecord.external_action_id == action_id
                    )
                )
            ).scalar_one()
        )
        receipts = int(
            (
                await session.execute(
                    select(func.count(ConnectorActionReceiptRecord.receipt_id)).where(
                        ConnectorActionReceiptRecord.external_action_id == action_id
                    )
                )
            ).scalar_one()
        )
    return attempts, receipts


@pytest.mark.asyncio
async def test_api_04_dry_run_is_terminally_denied_before_any_connector_boundary(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "api-04-dry-run-effect-fence.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=404)
    dependencies = _dependencies(runtime, clock)
    delegate, ledger = _gateway(runtime, clock)
    gateway = _CountingGateway(delegate)
    try:
        async with dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(action.run_id)
            assert run is not None
            work = await unit_of_work.works.get(run.work_item_id)
            assert work is not None and work.mode is WorkMode.MOCK_EXECUTION
            before_timeline = await unit_of_work.audits.list_run(action.run_id)

        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(WorkItemRecord)
                .where(WorkItemRecord.id == work.id)
                .values(mode=WorkMode.DRY_RUN.value)
            )

        with pytest.raises(ExternalActionDispatchError) as denied:
            await ExternalActionDispatcher(
                dependencies,
                gateway,
                WriteAuthorizationGuard(),
            ).dispatch_once(action.id, lease_owner="worker.api-04.dry-run")
        assert denied.value.code == "dry_run_external_effect_forbidden"
        assert denied.value.retry_after_seconds is None
        assert gateway.contract_calls == gateway.execute_calls == 0
        assert ledger.side_effect_count == 0
        assert await _dispatch_attempt_and_receipt_counts(runtime, action.id) == (0, 0)

        async with dependencies.unit_of_work() as unit_of_work:
            latest = await unit_of_work.external_actions.get(action.id)
            latest_run = await unit_of_work.runs.get(action.run_id)
            latest_step = await unit_of_work.run_steps.get(action.step_id)
            timeline = await unit_of_work.audits.list_run(action.run_id)
        assert latest is not None
        assert latest.state is ExternalActionState.CANCELLED
        assert latest.delivery_attempt_count == 0
        assert latest.lease is None and latest.call_started_at is None
        assert latest.terminal_reason_code == "runtime_control_denied"
        assert latest_run is not None and latest_run.state is RunState.FAILED
        assert latest_run.terminal_reason_code == "dry_run_external_effect_forbidden"
        assert latest_step is not None and latest_step.state is StepState.FAILED
        assert latest_step.terminal_reason_code == "dry_run_external_effect_forbidden"
        assert tuple(event.event_type for event in timeline[-4:]) == (
            "runtime.control_denied",
            "action.cancelled",
            "step.transitioned",
            "run.transitioned",
        )
        assert timeline[:-4] == before_timeline
        denial_event = timeline[-4]
        assert denial_event.action_id == action.id
        assert denial_event.step_id == action.step_id
        assert denial_event.safe_metadata.values == {
            "denial_code": "dry_run_external_effect_forbidden",
            "operation_key": latest_step.runtime_policy.operation_key,
        }
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_mock_execution_preserves_the_durable_connector_path(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "api-04-mock-execution.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=405)
    delegate, ledger = _gateway(runtime, clock)
    gateway = _CountingGateway(delegate)
    try:
        completed = await ExternalActionDispatcher(
            _dependencies(runtime, clock),
            gateway,
            WriteAuthorizationGuard(),
        ).dispatch_once(action.id, lease_owner="worker.api-04.mock-execution")

        assert completed.disposition is DispatchDisposition.SUCCEEDED
        assert completed.action.state is ExternalActionState.SUCCEEDED
        assert gateway.contract_calls == gateway.execute_calls == 1
        assert ledger.side_effect_count == 1
        assert await _dispatch_attempt_and_receipt_counts(runtime, action.id) == (1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected_code"),
    (
        ("run_missing", "execution_run_missing"),
        ("run_mismatch", "execution_policy_source_corrupt"),
        ("work_missing", "execution_policy_source_corrupt"),
        ("work_mismatch", "execution_policy_source_corrupt"),
    ),
)
async def test_api_04_dispatch_fails_closed_on_missing_or_mismatched_parent_context(
    tmp_path: Path,
    fault: str,
    expected_code: str,
) -> None:
    runtime = await _runtime(tmp_path / f"api-04-{fault}.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=406)
    delegate, ledger = _gateway(runtime, clock)
    gateway = _CountingGateway(delegate)
    dependencies = OrchestrationDependencies(
        clock,
        UnusedIds(),
        _ContextFaultUnitOfWorkFactory(_uow_factory(runtime), fault),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(ExternalActionDispatchError) as denied:
            await ExternalActionDispatcher(
                dependencies,
                gateway,
                WriteAuthorizationGuard(),
            ).dispatch_once(action.id, lease_owner=f"worker.api-04.{fault}")
        assert denied.value.code == expected_code
        assert gateway.contract_calls == gateway.execute_calls == 0
        assert ledger.side_effect_count == 0
        assert await _dispatch_attempt_and_receipt_counts(runtime, action.id) == (0, 0)

        async with _dependencies(runtime, clock).unit_of_work() as unit_of_work:
            latest = await unit_of_work.external_actions.get(action.id)
            latest_run = await unit_of_work.runs.get(action.run_id)
            latest_step = await unit_of_work.run_steps.get(action.step_id)
            timeline = await unit_of_work.audits.list_run(action.run_id)
        assert latest == action
        assert latest is not None and latest.state is ExternalActionState.DISPATCH_RESERVED
        assert latest.delivery_attempt_count == 0
        assert latest.lease is None and latest.call_started_at is None
        assert latest_run is not None and latest_run.state is RunState.EXECUTING
        assert latest_step is not None and latest_step.state is StepState.READY
        denial_events = tuple(
            event for event in timeline if event.event_type == "runtime.control_denied"
        )
        assert len(denial_events) == 1
        assert denial_events[0].safe_metadata.values["denial_code"] == expected_code
    finally:
        await runtime.dispose()
