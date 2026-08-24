"""RUN-03: one authoritative cancellation entry point for every Run phase."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.application.services import (
    ExecutionActivationService,
    IdempotentWorkRunReceiptService,
    RunCancellationCoordinator,
    RunCancellationCoordinatorError,
)
from marketing_agents.domain.audit import AuditOutcome
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.run_lifecycle import RunLifecycleCommand

from tests.integration.db.test_orch_06_runtime_composition import (
    _compose_single_write_plan,
    _execution_control,
    _persist_read_plan,
)
from tests.integration.db.test_orch_08_approval_boundary import _approve_complete_set
from tests.integration.db.test_orch_09_audited_step_state import (
    _audit_context,
    _envelope,
    _key,
    _validated_run,
)
from tests.integration.db.test_orch_09_audited_step_state import (
    _dependencies as read_dependencies,
)
from tests.integration.db.test_orch_09_audited_step_state import (
    _runtime as read_runtime,
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
from tests.integration.db.test_run_08_approval_persistence import (
    _runtime as approval_runtime,
)
from tests.support.incoming_work import TEST_CATALOG_HASH, validate_incoming_for_test


@pytest.mark.asyncio
@pytest.mark.parametrize("validated", (False, True))
async def test_run_03_early_run_routes_through_audited_lifecycle(
    tmp_path: Path,
    validated: bool,
) -> None:
    runtime = await read_runtime(tmp_path / f"run-03-early-{validated}.db")
    dependencies = read_dependencies(runtime)
    try:
        if validated:
            run, _ = await _validated_run(dependencies, f"event.run-03.early.{validated}")
        else:
            event_id = f"event.run-03.early.{validated}"
            envelope = _envelope(event_id)
            received = await IdempotentWorkRunReceiptService(
                dependencies,
                _key(),
                current_catalog_hash=TEST_CATALOG_HASH,
            ).receive(
                validate_incoming_for_test(envelope),
                audit_context=_audit_context(f"{event_id}.receive"),
            )
            run = received.run

        outcome = await RunCancellationCoordinator(dependencies).request(
            run.id,
            audit_context=_audit_context(f"run-03.early.{validated}.cancel"),
        )

        assert outcome.run.state is RunState.CANCELLED
        assert outcome.cancelled_at == outcome.run.updated_at
        assert outcome.cancelled_step_ids == outcome.preserved_step_ids == ()
        assert outcome.cancelled_action_ids == outcome.preserved_action_ids == ()
        assert outcome.succeeded_effect_count == outcome.outcome_unknown_effect_count == 0
        async with dependencies.unit_of_work() as unit_of_work:
            transitions = await unit_of_work.runs.list_transitions(run.id)
        assert transitions[-1].command is RunLifecycleCommand.CANCEL
        assert transitions[-1].new_state is RunState.CANCELLED
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("activated", (False, True))
async def test_run_03_controlled_read_routes_planned_or_executing_run(
    tmp_path: Path,
    activated: bool,
) -> None:
    runtime = await read_runtime(tmp_path / f"run-03-controlled-read-{activated}.db")
    dependencies = read_dependencies(runtime)
    try:
        _, persisted, plan, _, _ = await _persist_read_plan(
            dependencies,
            event_id=f"event.run-03.controlled-read.{activated}",
            parallel_steps=True,
        )
        if activated:
            await ExecutionActivationService(dependencies).activate(
                persisted.run.id,
                audit_context=_audit_context("run-03.controlled-read.activate"),
            )

        outcome = await RunCancellationCoordinator(dependencies).request(
            persisted.run.id,
            audit_context=_audit_context(f"run-03.controlled-read.{activated}.cancel"),
        )

        assert outcome.run.state is RunState.CANCELLED
        assert len(outcome.cancelled_step_ids) == len(plan.steps)
        assert outcome.preserved_step_ids == ()
        assert outcome.cancelled_action_ids == outcome.preserved_action_ids == ()
        control = await _execution_control(dependencies, persisted.run.id)
        assert control.cancel_requested_at == outcome.cancelled_at
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("released", (False, True))
async def test_run_03_write_routes_pre_or_post_release_approval_boundary(
    tmp_path: Path,
    released: bool,
) -> None:
    runtime = await approval_runtime(tmp_path / f"run-03-write-{released}.db")
    clock = ApprovalClock()
    dependencies = approval_dependencies(runtime, clock=clock, ids=ApprovalIds(3_000))
    try:
        plan, persisted = await _compose_single_write_plan(
            dependencies,
            event_id=f"event.run-03.write.{released}",
            seed=3_000,
        )
        if released:
            await _approve_complete_set(
                dependencies,
                clock,
                plan.run_id,
                suffix="run-03.write.release",
            )
        else:
            assert persisted.run.state is RunState.AWAITING_APPROVAL
        clock.current = max(clock.current, persisted.run.updated_at) + timedelta(seconds=1)

        outcome = await RunCancellationCoordinator(dependencies).request(
            plan.run_id,
            audit_context=approval_context(f"run-03.write.{released}.cancel"),
        )

        assert outcome.run.state is RunState.CANCELLED
        assert outcome.cancelled_step_ids
        assert outcome.preserved_step_ids == ()
        assert outcome.cancelled_action_ids
        assert outcome.preserved_action_ids == ()
        assert outcome.succeeded_effect_count == outcome.outcome_unknown_effect_count == 0
        control = await _execution_control(dependencies, plan.run_id)
        assert control.cancel_requested_at == outcome.cancelled_at
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_03_terminal_rejection_is_audited_and_stable(tmp_path: Path) -> None:
    runtime = await read_runtime(tmp_path / "run-03-terminal-rejection.db")
    dependencies = read_dependencies(runtime)
    coordinator = RunCancellationCoordinator(dependencies)
    try:
        run, _ = await _validated_run(dependencies, "event.run-03.terminal")
        await coordinator.request(
            run.id,
            audit_context=_audit_context("run-03.terminal.setup"),
        )
        retry_context = _audit_context("run-03.terminal.retry")

        for _ in range(2):
            with pytest.raises(RunCancellationCoordinatorError) as rejected:
                await coordinator.request(run.id, audit_context=retry_context)
            assert rejected.value.code == "terminal_state_immutable"
            assert rejected.value.run_id == run.id

        async with dependencies.unit_of_work() as unit_of_work:
            current = await unit_of_work.runs.get(run.id)
            timeline = await unit_of_work.audits.list_run(run.id)
        rejections = tuple(
            event
            for event in timeline
            if event.event_type == "run.transition_rejected"
            and event.attempted_command == RunLifecycleCommand.CANCEL.value
            and event.correlation_id == retry_context.correlation_id
        )
        assert current is not None and current.state is RunState.CANCELLED
        assert len(rejections) == 1
        assert rejections[0].outcome is AuditOutcome.REJECTED
        assert rejections[0].observed_state == RunState.CANCELLED.value
        assert rejections[0].reason_code == "terminal_state_immutable"
    finally:
        await runtime.dispose()
