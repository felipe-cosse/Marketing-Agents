"""RUN-03: cancellation closes durable retry backoff before worker restart."""

from __future__ import annotations

from pathlib import Path

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.write_authorization import WriteAuthorizationGuard
from marketing_agents.application.ports.read_adapter import ReadAdapterTransientError
from marketing_agents.application.services import (
    ApprovalBoundaryDisposition,
    ApprovalBoundaryService,
    ControlledReadCommand,
    ControlledReadExecutor,
    ControlledReadExecutorError,
    ExternalActionDispatcher,
    ExternalActionDispatchError,
    ReadExecutionClassification,
    RunCancellationService,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState
from marketing_agents.infrastructure.db import DatabaseRuntime

from tests.integration.db.test_orch_06_controlled_read_executor import (
    IncrementingIds,
    SequenceAdapter,
    _audit_context,
    _prepare,
)
from tests.integration.db.test_orch_06_controlled_read_executor import (
    _runtime as _read_runtime,
)
from tests.integration.db.test_orch_06_controlled_read_executor import (
    _uow_factory as _read_uow_factory,
)
from tests.integration.db.test_run_05_external_action_idempotency import (
    MutableClock,
    NoReceiptUncertainGateway,
    _dependencies,
    _released_action,
)
from tests.integration.db.test_run_05_external_action_idempotency import (
    _runtime as _write_runtime,
)


def _write_context(label: str) -> AuditContext:
    return AuditContext.system("test.run-03", correlation_id=f"request.{label}")


@pytest.mark.asyncio
async def test_run_03_direct_cancel_closes_read_retry_backoff_before_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run-03-read-retry-cancel.db"
    prepared = await _prepare(path, max_attempts=2)
    original_runtime = prepared.runtime
    restarted_runtime: DatabaseRuntime | None = None
    first_adapter = SequenceAdapter(
        [ReadAdapterTransientError("temporary_failure", "safe transient failure")]
    )
    try:
        first = await ControlledReadExecutor(
            prepared.dependencies,
            first_adapter,
        ).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("run-03.retry.first"),
        )
        assert first.classification is ReadExecutionClassification.TRANSIENT_FAILURE
        assert first.retry_not_before is not None
        assert first.step.state is StepState.EXECUTING
        assert len(first_adapter.calls) == 1

        prepared.clock.current = first.retry_not_before
        cancelled = await RunCancellationService(prepared.dependencies).request(
            prepared.run_id,
            audit_context=_audit_context("run-03.retry.cancel"),
        )
        assert cancelled.run.state is RunState.CANCELLED
        assert cancelled.cancelled_steps == ()
        assert cancelled.preserved_executing_step_ids == ()

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            stored_step = await unit_of_work.run_steps.get(prepared.step_id)
            stored_control = await unit_of_work.execution_control.get(prepared.run_id)
            stored_attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            timeline = await unit_of_work.audits.list_run(prepared.run_id)
        assert stored_step is not None and stored_step.state is StepState.FAILED
        assert stored_step.terminal_reason_code == "run_cancelled"
        assert stored_control is not None
        assert stored_control.cancel_requested_at == prepared.clock.current
        assert stored_attempts == (first.attempt,)
        assert [event.event_type for event in timeline[-2:]] == [
            "step.transitioned",
            "run.transitioned",
        ]
        assert [event.safe_metadata.values["command"] for event in timeline[-2:]] == [
            "fail",
            "cancel",
        ]

        await original_runtime.dispose()
        restarted_runtime = await _read_runtime(path)
        restarted_dependencies = OrchestrationDependencies(
            prepared.clock,
            IncrementingIds(),
            _read_uow_factory(restarted_runtime),
        )
        restarted_adapter = SequenceAdapter([{"must_not": "execute"}])
        with pytest.raises(ControlledReadExecutorError) as blocked:
            await ControlledReadExecutor(
                restarted_dependencies,
                restarted_adapter,
            ).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("run-03.retry.restart"),
            )
        assert blocked.value.code == "run_cancelled"
        assert restarted_adapter.calls == []
    finally:
        await original_runtime.dispose()
        if restarted_runtime is not None:
            await restarted_runtime.dispose()


@pytest.mark.asyncio
async def test_run_03_released_cancel_closes_provider_retry_write_step(
    tmp_path: Path,
) -> None:
    runtime = await _write_runtime(tmp_path / "run-03-write-provider-retry-cancel.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=303, delivery_attempt_limit=2)
    dependencies = _dependencies(runtime, clock)
    gateway = NoReceiptUncertainGateway()
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
    )
    try:
        started = await dispatcher._claim_and_mark_call_started(
            action.id,
            "worker.run-03.provider-retry.first",
        )
        assert started.action.state is ExternalActionState.DISPATCHING
        assert started.action.call_started_at is not None
        assert started.action.call_deadline_at is not None
        assert gateway.calls == 0
        async with dependencies.unit_of_work() as unit_of_work:
            executing_step = await unit_of_work.run_steps.get(action.step_id)
        assert executing_step is not None and executing_step.state is StepState.EXECUTING

        clock.current = started.action.call_deadline_at
        lease = started.action.lease
        assert lease is not None
        async with dependencies.unit_of_work() as unit_of_work:
            retry_ready = await unit_of_work.external_actions.release_stale_for_retry(
                action_id=action.id,
                expected_version=started.action.version,
                attempt_number=lease.attempt_number,
                occurred_at=clock.now(),
                conclusion="provider_retry",
            )
            assert retry_ready is not None
            await unit_of_work.commit()
        assert retry_ready.state is ExternalActionState.DISPATCH_RESERVED
        assert retry_ready.call_started_at is None
        assert retry_ready.delivery_attempt_count == 1

        clock.tick(1)
        cancelled = await ApprovalBoundaryService(dependencies).cancel(
            action.run_id,
            audit_context=_write_context("run-03.provider-retry.cancel"),
        )
        assert cancelled.disposition is ApprovalBoundaryDisposition.CANCELLED
        assert cancelled.run.state is RunState.CANCELLED
        async with dependencies.unit_of_work() as unit_of_work:
            closed_action = await unit_of_work.external_actions.get(action.id)
            closed_step = await unit_of_work.run_steps.get(action.step_id)
            closed_control = await unit_of_work.execution_control.get(action.run_id)
            timeline = await unit_of_work.audits.list_run(action.run_id)
        assert closed_action is not None
        assert closed_action.state is ExternalActionState.CANCELLED
        assert closed_action.delivery_attempt_count == 1
        assert closed_action.call_started_at is None
        assert closed_step is not None and closed_step.state is StepState.FAILED
        assert closed_step.terminal_reason_code == "run_cancelled"
        assert closed_control is not None and closed_control.cancel_requested_at == clock.now()
        assert [event.event_type for event in timeline[-3:]] == [
            "action.cancelled",
            "step.transitioned",
            "run.transitioned",
        ]
        assert timeline[-2].safe_metadata.values["command"] == "fail"
        assert timeline[-1].safe_metadata.values["command"] == "cancel"

        with pytest.raises(ExternalActionDispatchError) as blocked:
            await dispatcher.dispatch_once(
                action.id,
                lease_owner="worker.run-03.provider-retry.restart",
            )
        assert blocked.value.code == "action_not_dispatchable"
        assert gateway.calls == 0

        replay = await ApprovalBoundaryService(dependencies).evaluate(
            action.run_id,
            audit_context=_write_context("run-03.provider-retry.replay"),
        )
        assert replay.disposition is ApprovalBoundaryDisposition.CANCELLED
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_03_cancellation_fence_prevents_stale_provider_retry_rearm(
    tmp_path: Path,
) -> None:
    runtime = await _write_runtime(tmp_path / "run-03-cancel-wins-provider-rearm.db")
    clock = MutableClock()
    action = await _released_action(runtime, clock, seed=304, delivery_attempt_limit=2)
    dependencies = _dependencies(runtime, clock)
    gateway = NoReceiptUncertainGateway()
    dispatcher = ExternalActionDispatcher(
        dependencies,
        gateway,
        WriteAuthorizationGuard(),
    )
    try:
        started = await dispatcher._claim_and_mark_call_started(
            action.id,
            "worker.run-03.cancellation-wins",
        )
        lease = started.action.lease
        assert lease is not None
        assert started.action.call_started_at is not None
        assert started.action.call_deadline_at is not None

        clock.tick(1)
        cancelled = await ApprovalBoundaryService(dependencies).cancel(
            action.run_id,
            audit_context=_write_context("run-03.cancellation-wins"),
        )
        assert cancelled.disposition is ApprovalBoundaryDisposition.CANCELLED
        assert cancelled.run.state is RunState.CANCELLED
        async with dependencies.unit_of_work() as unit_of_work:
            preserved_action = await unit_of_work.external_actions.get(action.id)
            preserved_step = await unit_of_work.run_steps.get(action.step_id)
            fenced_control = await unit_of_work.execution_control.get(action.run_id)
        assert preserved_action == started.action
        assert preserved_step is not None and preserved_step.state is StepState.EXECUTING
        assert fenced_control is not None and fenced_control.cancel_requested_at == clock.now()

        clock.current = started.action.call_deadline_at
        async with dependencies.unit_of_work() as unit_of_work:
            rearmed = await unit_of_work.external_actions.release_stale_for_retry(
                action_id=action.id,
                expected_version=started.action.version,
                attempt_number=lease.attempt_number,
                occurred_at=clock.now(),
                conclusion="provider_retry",
            )
            await unit_of_work.commit()
        assert rearmed is None
        async with dependencies.unit_of_work() as unit_of_work:
            unchanged_action = await unit_of_work.external_actions.get(action.id)
            unchanged_step = await unit_of_work.run_steps.get(action.step_id)
        assert unchanged_action == started.action
        assert unchanged_step == preserved_step
        assert gateway.calls == 0
    finally:
        await runtime.dispose()
