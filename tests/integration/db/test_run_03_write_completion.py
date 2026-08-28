"""RUN-03 atomic WRITE completion and post-cancellation outcome witnesses."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.write_authorization import (
    AuthorizedExternalWrite,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.application.ports.external_writes import (
    ConnectorDeliveryContract,
    ConnectorDeliveryFailure,
    ExternalWriteConnectorGateway,
)
from marketing_agents.application.ports.repositories import AuditRepository
from marketing_agents.application.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from marketing_agents.application.services import (
    ApprovalBoundaryDisposition,
    ApprovalBoundaryService,
    DispatchDisposition,
    ExternalActionDispatcher,
    RunCancellationCoordinator,
    RunCancellationCoordinatorError,
)
from marketing_agents.domain.audit import AuditEvent, AuditEventDraft
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState

from tests.integration.db.test_run_05_external_action_idempotency import (
    BlockingFirstGateway,
    CountingGateway,
    MutableClock,
    UnusedIds,
    _dependencies,
    _gateway,
    _released_action,
    _runtime,
    _uow_factory,
)
from tests.integration.db.test_run_08_approval_persistence import _context

_ACTION_OUTCOME_EVENTS = frozenset(
    {
        "action.succeeded",
        "action.failed",
        "action.outcome_unknown",
        "action.receipt_reconciled",
    }
)


class _TerminalFailureGateway:
    def __init__(
        self,
        delegate: ExternalWriteConnectorGateway,
        *,
        outcome_unknown: bool,
    ) -> None:
        self._delegate = delegate
        self._outcome_unknown = outcome_unknown
        self.calls = 0

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self._delegate.contract_for(action)

    async def execute(self, _authorization: AuthorizedExternalWrite) -> ConnectorWriteResult:
        self.calls += 1
        raise ConnectorDeliveryFailure(
            "connector_delivery_uncertain"
            if self._outcome_unknown
            else "connector_request_rejected",
            "sanitized terminal provider failure",
            request_may_have_left_process=self._outcome_unknown,
        )


class _CompletionAuditFaultRepository:
    def __init__(self, delegate: AuditRepository) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def append_many(
        self,
        events: tuple[AuditEventDraft, ...],
    ) -> tuple[AuditEvent, ...]:
        persisted = await self._delegate.append_many(events)
        if any(event.event_type in _ACTION_OUTCOME_EVENTS for event in events):
            raise RuntimeError("injected WRITE completion audit fault")
        return persisted


class _CompletionAuditFaultUnitOfWork:
    def __init__(self, delegate: UnitOfWork) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    @property
    def audits(self) -> AuditRepository:
        return cast(AuditRepository, _CompletionAuditFaultRepository(self._delegate.audits))

    async def __aenter__(self) -> _CompletionAuditFaultUnitOfWork:
        await self._delegate.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._delegate.__aexit__(exc_type, exc, traceback)


class _CompletionAuditFaultUnitOfWorkFactory:
    def __init__(self, delegate: UnitOfWorkFactory) -> None:
        self._delegate = delegate

    def __call__(self) -> UnitOfWork:
        return cast(UnitOfWork, _CompletionAuditFaultUnitOfWork(self._delegate()))


@pytest.mark.asyncio
async def test_run_03_post_cancel_timely_provider_success_closes_write_step(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-03-post-cancel-success.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=301)
    dependencies = _dependencies(runtime, clock)
    delegate, ledger = _gateway(runtime, clock)
    gateway = BlockingFirstGateway(delegate)
    dispatcher = ExternalActionDispatcher(dependencies, gateway, WriteAuthorizationGuard())
    dispatch_task: asyncio.Task[Any] | None = None
    try:
        dispatch_task = asyncio.create_task(
            dispatcher.dispatch_once(
                action.id,
                lease_owner="worker.run-03.post-cancel-success",
            )
        )
        await asyncio.wait_for(gateway.first_started.wait(), timeout=2)

        clock.tick(1)
        cancelled = await ApprovalBoundaryService(dependencies).cancel(
            action.run_id,
            audit_context=_context("run-03.post-cancel-success"),
        )
        assert cancelled.disposition is ApprovalBoundaryDisposition.CANCELLED
        assert cancelled.run.state is RunState.CANCELLED

        gateway.release_first.set()
        completed = await asyncio.wait_for(dispatch_task, timeout=2)
        assert completed.disposition is DispatchDisposition.SUCCEEDED
        assert completed.action.state is ExternalActionState.SUCCEEDED
        assert gateway.calls == 1
        assert ledger.side_effect_count == 1

        async with dependencies.unit_of_work() as unit_of_work:
            stored_run = await unit_of_work.runs.get(action.run_id)
            stored_step = await unit_of_work.run_steps.get(action.step_id)
            stored_action = await unit_of_work.external_actions.get(action.id)
            timeline = await unit_of_work.audits.list_run(action.run_id)

        assert stored_run == cancelled.run
        assert stored_step is not None and stored_step.state is StepState.SUCCEEDED
        assert stored_step.terminal_reason_code == "step_succeeded"
        assert stored_action == completed.action
        assert [event.event_type for event in timeline[-2:]] == [
            "action.succeeded",
            "step.transitioned",
        ]
        assert timeline[-2].run_sequence + 1 == timeline[-1].run_sequence
        assert timeline[-2].action_id == action.id
        assert timeline[-2].receipt_id == completed.action.result.receipt_id
        assert timeline[-2].occurred_at == timeline[-1].occurred_at
        cancel_event = next(
            event
            for event in timeline
            if event.event_type == "run.transitioned"
            and event.safe_metadata.values["command"] == "cancel"
        )
        assert cancel_event.run_sequence < timeline[-2].run_sequence
    finally:
        gateway.release_first.set()
        if dispatch_task is not None:
            await asyncio.gather(dispatch_task, return_exceptions=True)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_03_preexisting_success_remains_immutable_and_precedes_cancellation(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-03-success-before-cancel.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=305)
    dependencies = _dependencies(runtime, clock)
    gateway, ledger = _gateway(runtime, clock)
    try:
        succeeded = await ExternalActionDispatcher(
            dependencies,
            gateway,
            WriteAuthorizationGuard(),
        ).dispatch_once(
            action.id,
            lease_owner="worker.run-03.success-before-cancel",
        )
        assert succeeded.disposition is DispatchDisposition.SUCCEEDED
        assert succeeded.action.state is ExternalActionState.SUCCEEDED
        assert ledger.side_effect_count == 1

        clock.tick(1)
        cancelled = await RunCancellationCoordinator(dependencies).request(
            action.run_id,
            audit_context=_context("run-03.success-before-cancel"),
        )

        assert cancelled.run.state is RunState.CANCELLED
        assert cancelled.cancelled_action_ids == ()
        assert cancelled.preserved_action_ids == (action.id,)
        assert cancelled.cancelled_step_ids == ()
        assert action.step_id in cancelled.preserved_step_ids
        assert cancelled.succeeded_effect_count == 1
        assert cancelled.outcome_unknown_effect_count == 0
        async with dependencies.unit_of_work() as unit_of_work:
            stored_action = await unit_of_work.external_actions.get(action.id)
            stored_step = await unit_of_work.run_steps.get(action.step_id)
            transitions = await unit_of_work.runs.list_transitions(action.run_id)
            timeline = await unit_of_work.audits.list_run(action.run_id)

        assert stored_action == succeeded.action
        assert stored_step is not None and stored_step.state is StepState.SUCCEEDED
        assert transitions[-1].completed_effect_count == 1
        succeeded_events = tuple(
            event
            for event in timeline
            if event.event_type == "action.succeeded" and event.action_id == action.id
        )
        cancel_events = tuple(
            event
            for event in timeline
            if event.event_type == "run.transitioned"
            and event.safe_metadata.values["command"] == "cancel"
        )
        assert len(succeeded_events) == len(cancel_events) == 1
        assert succeeded_events[0].run_sequence < cancel_events[0].run_sequence
        assert not any(
            event.event_type == "action.cancelled" and event.action_id == action.id
            for event in timeline
        )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome_unknown", "attempt_limit", "disposition", "action_state", "event_type", "reason"),
    (
        (
            False,
            2,
            DispatchDisposition.FAILED,
            ExternalActionState.FAILED,
            "action.failed",
            "connector_request_rejected",
        ),
        (
            True,
            1,
            DispatchDisposition.OUTCOME_UNKNOWN,
            ExternalActionState.OUTCOME_UNKNOWN,
            "action.outcome_unknown",
            "connector_delivery_uncertain",
        ),
    ),
)
async def test_run_03_terminal_provider_failure_closes_write_step_atomically(
    tmp_path: Path,
    outcome_unknown: bool,
    attempt_limit: int,
    disposition: DispatchDisposition,
    action_state: ExternalActionState,
    event_type: str,
    reason: str,
) -> None:
    runtime = await _runtime(tmp_path / f"run-03-terminal-failure-{outcome_unknown}.db")
    clock = MutableClock()
    action = await _released_action(
        runtime,
        clock,
        seed=302 + int(outcome_unknown),
        delivery_attempt_limit=attempt_limit,
    )
    dependencies = _dependencies(runtime, clock)
    delegate, ledger = _gateway(runtime, clock)
    gateway = _TerminalFailureGateway(delegate, outcome_unknown=outcome_unknown)
    try:
        completed = await ExternalActionDispatcher(
            dependencies,
            gateway,
            WriteAuthorizationGuard(),
        ).dispatch_once(
            action.id,
            lease_owner=f"worker.run-03.failure.{outcome_unknown}",
        )

        assert completed.disposition is disposition
        assert completed.action.state is action_state
        assert completed.action.terminal_reason_code == reason
        assert gateway.calls == 1
        assert ledger.side_effect_count == 0
        async with dependencies.unit_of_work() as unit_of_work:
            stored_run = await unit_of_work.runs.get(action.run_id)
            stored_step = await unit_of_work.run_steps.get(action.step_id)
            stored_action = await unit_of_work.external_actions.get(action.id)
            timeline = await unit_of_work.audits.list_run(action.run_id)

        assert stored_run is not None and stored_run.state is RunState.FAILED
        assert stored_run.terminal_reason_code == reason
        assert stored_step is not None and stored_step.state is StepState.FAILED
        assert stored_step.terminal_reason_code == reason
        assert stored_action == completed.action
        assert [event.event_type for event in timeline[-3:]] == [
            event_type,
            "step.transitioned",
            "run.transitioned",
        ]
        assert timeline[-3].reason_code == reason
        assert timeline[-2].reason_code == reason
        assert timeline[-1].reason_code == reason
        assert timeline[-3].occurred_at == timeline[-2].occurred_at == timeline[-1].occurred_at

        clock.tick(1)
        with pytest.raises(RunCancellationCoordinatorError) as rejected:
            await RunCancellationCoordinator(dependencies).request(
                action.run_id,
                audit_context=_context(f"run-03.failure.{outcome_unknown}.cancel"),
            )
        assert rejected.value.code == "terminal_state_immutable"
        async with dependencies.unit_of_work() as unit_of_work:
            preserved_action = await unit_of_work.external_actions.get(action.id)
            cancelled_timeline = await unit_of_work.audits.list_run(action.run_id)
        assert preserved_action == completed.action
        assert len(cancelled_timeline) == len(timeline) + 1
        assert cancelled_timeline[-1].event_type == "run.transition_rejected"
        assert not any(
            event.event_type == "action.cancelled" and event.action_id == action.id
            for event in cancelled_timeline
        )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_03_completion_audit_fault_rolls_back_action_and_step_then_recovers_receipt(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-03-completion-audit-rollback.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=304)
    faulting_dependencies = OrchestrationDependencies(
        clock,
        UnusedIds(),
        _CompletionAuditFaultUnitOfWorkFactory(_uow_factory(runtime)),
    )
    delegate, ledger = _gateway(runtime, clock)
    try:
        with pytest.raises(RuntimeError, match="injected WRITE completion audit fault"):
            await ExternalActionDispatcher(
                faulting_dependencies,
                delegate,
                WriteAuthorizationGuard(),
            ).dispatch_once(
                action.id,
                lease_owner="worker.run-03.audit-rollback",
            )

        dependencies = _dependencies(runtime, clock)
        async with dependencies.unit_of_work() as unit_of_work:
            rolled_back_action = await unit_of_work.external_actions.get(action.id)
            rolled_back_step = await unit_of_work.run_steps.get(action.step_id)
            durable_receipt = await unit_of_work.connector_receipts.get(
                action.connector_binding_id,
                action.idempotency_key,
            )
            timeline_before_recovery = await unit_of_work.audits.list_run(action.run_id)

        assert rolled_back_action is not None
        assert rolled_back_action.state is ExternalActionState.DISPATCHING
        assert rolled_back_action.call_started_at is not None
        assert rolled_back_action.call_deadline_at is not None
        assert rolled_back_step is not None and rolled_back_step.state is StepState.EXECUTING
        assert durable_receipt is not None
        assert ledger.side_effect_count == 1
        assert not any(
            event.event_type in _ACTION_OUTCOME_EVENTS for event in timeline_before_recovery
        )

        clock.current = rolled_back_action.call_deadline_at
        recovery_delegate, recovery_ledger = _gateway(runtime, clock)
        recovery_gateway = CountingGateway(recovery_delegate)
        recovered = await ExternalActionDispatcher(
            dependencies,
            recovery_gateway,
            WriteAuthorizationGuard(),
        ).recover_stale(
            lease_owner="worker.run-03.audit-rollback.recovery",
            limit=1,
        )

        assert len(recovered) == 1
        assert recovered[0].disposition is DispatchDisposition.SUCCEEDED
        assert recovered[0].action.state is ExternalActionState.SUCCEEDED
        assert recovery_gateway.calls == 0
        assert recovery_ledger.side_effect_count == 0
        async with dependencies.unit_of_work() as unit_of_work:
            recovered_step = await unit_of_work.run_steps.get(action.step_id)
            timeline = await unit_of_work.audits.list_run(action.run_id)
        assert recovered_step is not None and recovered_step.state is StepState.SUCCEEDED
        assert [event.event_type for event in timeline[-2:]] == [
            "action.receipt_reconciled",
            "step.transitioned",
        ]
        assert timeline[-2].occurred_at == timeline[-1].occurred_at
    finally:
        await runtime.dispose()
