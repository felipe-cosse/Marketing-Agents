"""A completed real READ retry history must not permanently block detail expiry."""

from __future__ import annotations

from datetime import timedelta

import pytest
from marketing_agents.application.ports.read_adapter import ReadAdapterTransientError
from marketing_agents.application.services.controlled_read_executor import (
    ControlledReadCommand,
    ControlledReadExecutor,
    ReadExecutionClassification,
)
from marketing_agents.application.services.retention_maintenance import RetentionMaintenanceService
from marketing_agents.application.services.run_lifecycle import RunLifecycleService
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.retained_details import DetailExpired, RetainedWorkItem
from marketing_agents.domain.retention import RetentionPolicy
from marketing_agents.domain.run_lifecycle import CompletionContext, RunLifecycleCommand
from marketing_agents.infrastructure.db import (
    SQLAlchemyExecutionControlRepository,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.migrations import upgrade_database
from marketing_agents.infrastructure.db.models import ArtifactRecord, ExecutionAttemptRecord
from marketing_agents.infrastructure.db.repositories.retention import SQLAlchemyRetentionRepository
from sqlalchemy import select

from tests.integration.db import test_orch_06_controlled_read_executor as read_fixture


@pytest.mark.asyncio
async def test_exec_06_completed_retry_history_purges_without_rewriting_attempt_facts(
    tmp_path, monkeypatch
):
    async def database(path):
        runtime = create_database_runtime(
            f"sqlite+aiosqlite:///{path}",
            retention_policy=RetentionPolicy(),
            retention_key=read_fixture.RECEIPT_KEY,
        )
        await upgrade_database(runtime)
        return runtime

    # Reuse the real admission/planner/activation fixture, with one installation
    # key for work and execution integrity as the real runtime uses. No validator
    # or repository behavior is replaced; only the fixture's key wiring differs.
    monkeypatch.setattr(read_fixture, "_runtime", database)
    monkeypatch.setattr(
        read_fixture,
        "execution_control_repository",
        lambda session: SQLAlchemyExecutionControlRepository(session, read_fixture.RECEIPT_KEY),
    )
    prepared = await read_fixture._prepare(tmp_path / "retention-retry.db", max_attempts=2)
    adapter = read_fixture.SequenceAdapter(
        [
            ReadAdapterTransientError("temporary_failure", "safe transient failure"),
            {"retry": "ok"},
        ]
    )
    executor = ControlledReadExecutor(prepared.dependencies, adapter)
    repository = SQLAlchemyRetentionRepository(
        prepared.runtime.session_factory, read_fixture.RECEIPT_KEY
    )
    service = RetentionMaintenanceService(
        repository, prepared.clock, owner="worker.retention.retry", batch_size=100
    )
    try:
        first = await executor.execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=read_fixture._audit_context("retention-transient"),
        )
        assert first.classification is ReadExecutionClassification.TRANSIENT_FAILURE
        assert first.retryable and first.retry_not_before is not None
        assert first.attempt.terminal_reason_code is None
        async with prepared.runtime.session_factory() as session:
            assert prepared.run_id not in set(
                (await session.scalars(repository._eligible_runs(prepared.clock.now()))).all()
            )

        prepared.clock.current = first.retry_not_before
        second = await executor.execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=read_fixture._audit_context("retention-retry-success"),
        )
        assert second.classification is ReadExecutionClassification.SUCCEEDED
        assert [call.attempt_number for call in adapter.calls] == [1, 2]
        async with prepared.dependencies.unit_of_work() as uow:
            attempts = await uow.execution_control.list_attempts(
                prepared.step_id, prepared.operation_key
            )
            assert attempts == (first.attempt, second.attempt)
            run = await uow.runs.get(prepared.run_id)
            assert run is not None and run.state is RunState.EXECUTING
        completed = await RunLifecycleService(prepared.dependencies).advance(
            run.id,
            run.version,
            RunLifecycleCommand.COMPLETE,
            CompletionContext(1, 1, 0, 0),
            audit_context=read_fixture._audit_context("retention-complete"),
        )
        assert completed.run.state is RunState.COMPLETED
        async with prepared.dependencies.unit_of_work() as uow:
            before = await uow.runs.get_inspectable(prepared.run_id)
            plan = await uow.run_steps.get_inspectable_plan(prepared.run_id)
            events = await uow.audits.list_run(prepared.run_id, limit=100)
            assert before is not None and events and len(events) < 100

        prepared.clock.current += timedelta(days=6)
        early = await service.run_once(occurrence_key="retention.retry.before-expiry")
        assert early is not None and sum(early.counts.values()) == 0
        prepared.clock.current += timedelta(days=91)
        result = await service.run_once(occurrence_key="retention.retry.after-expiry")
        assert result is not None
        # The first attempt still carries its original retry timestamp. Only the
        # later successful attempt makes that retry historical rather than open.
        assert result.counts["admitted_payload"] == 3  # Work and both attempts.
        assert result.counts["artifact_detail"] == 1
        assert result.counts["audit_metadata"] >= len(events)
        async with prepared.dependencies.unit_of_work() as uow:
            after = await uow.runs.get_inspectable(prepared.run_id)
            assert after is not None and isinstance(after.work_item, RetainedWorkItem)
            assert after.run == before.run and after.transitions == before.transitions
            assert await uow.run_steps.get_inspectable_plan(prepared.run_id) == plan
            history = await uow.audits.list_run(prepared.run_id, limit=100)
            assert [(item.id, item.run_sequence, item.event_type) for item in history] == [
                (item.id, item.run_sequence, item.event_type) for item in events
            ]
            failed_attempt_event = next(
                item
                for item in history
                if item.event_type == "attempt.completed" and item.attempt_id == first.attempt.id
            )
            assert (
                failed_attempt_event.safe_metadata.values["safe_error_code"]
                == first.attempt.safe_error_code
            )
            with pytest.raises(DetailExpired):
                await uow.execution_control.get_attempt(first.attempt.id)
        async with prepared.runtime.session_factory() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(ExecutionAttemptRecord)
                        .where(ExecutionAttemptRecord.run_id == prepared.run_id)
                        .order_by(ExecutionAttemptRecord.attempt_number)
                    )
                ).all()
            )
            assert len(rows) == 2 and all(row.redacted_input == {} for row in rows)
            assert rows[0].retry_not_before == first.retry_not_before
            assert rows[0].terminal_reason_code is None
            assert [row.outcome for row in rows] == ["transient_failure", "succeeded"]
            assert rows[0].completed_at == first.attempt.completed_at
            assert rows[1].completed_at == second.attempt.completed_at
            artifacts = tuple(
                (
                    await session.scalars(
                        select(ArtifactRecord).where(ArtifactRecord.run_id == prepared.run_id)
                    )
                ).all()
            )
            assert len(artifacts) == 1 and artifacts[0].payload == {}
        for attempt in attempts:
            assert await repository.inspect("attempt", attempt.id) is not None
        again = await service.run_once(occurrence_key="retention.retry.after-purge")
        assert again is not None and sum(again.counts.values()) == 0
    finally:
        await prepared.runtime.dispose()
