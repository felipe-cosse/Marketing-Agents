"""ORCH-06: runtime-control denial witnesses persist and replay fail closed."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext, AuditOutcome
from marketing_agents.infrastructure.db import AuditPersistenceInvariantError
from marketing_agents.infrastructure.db.models import AuditEventRecord, RunRecord
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from tests.integration.db.test_orch_06_execution_control import _prepare


def _factory() -> AuditEventFactory:
    return AuditEventFactory(
        AuditContext.worker(
            "worker.orch-06.runtime-control",
            correlation_id="request.orch-06.runtime-denial.persistence",
        )
    )


def test_orch_06_runtime_denial_constraint_compiles_for_sqlite_and_postgresql() -> None:
    for dialect in (sqlite.dialect(), postgresql.dialect()):  # type: ignore[no-untyped-call]
        ddl = " ".join(
            str(
                CreateTable(AuditEventRecord.__table__).compile(  # type: ignore[arg-type]
                    dialect=dialect
                )
            )
            .lower()
            .split()
        )
        assert "aggregate_type = 'runtime_control_denial'" in ddl
        assert "event_type = 'runtime.control_denied'" in ddl
        assert "outcome = 'rejected'" in ddl
        assert "mutation_version is null" in ddl
        assert "step_id is not null" in ddl


@pytest.mark.asyncio
async def test_orch_06_runtime_denial_persists_redacted_and_replay_rolls_back(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(tmp_path / "runtime-denial-audit.db")
    operation = prepared.policy.operations[0]
    occurred_at = prepared.started_at + timedelta(seconds=1)
    event = _factory().runtime_control_denied(
        run_id=prepared.policy.run_id,
        step_id=prepared.step_ids[0],
        operation_key=operation.operation_key,
        denial_code="rate_limit_exhausted",
        retry_after_seconds=59,
        occurred_at=occurred_at,
    )
    try:
        async with prepared.uow_factory() as unit_of_work:
            before = await unit_of_work.audits.list_run(prepared.policy.run_id)
            appended = await unit_of_work.audits.append(event)
            await unit_of_work.commit()

        assert appended.id == event.id
        assert appended.outcome is AuditOutcome.REJECTED
        assert appended.mutation_version is None
        assert appended.step_id == prepared.step_ids[0]
        assert appended.action_id is None

        async with prepared.uow_factory() as unit_of_work:
            stored = await unit_of_work.audits.get(event.id)
            timeline = await unit_of_work.audits.list_run(prepared.policy.run_id)
        assert stored is not None
        assert stored.draft == event
        assert stored.safe_metadata.values == {
            "denial_code": "rate_limit_exhausted",
            "operation_key": operation.operation_key,
            "retry_after_seconds": 59,
        }
        assert len(timeline) == len(before) + 1

        async with prepared.runtime.session_factory() as session:
            persisted_metadata, before_replay_counter = (
                await session.execute(
                    select(
                        AuditEventRecord.safe_metadata,
                        RunRecord.next_timeline_sequence,
                    )
                    .join(RunRecord, RunRecord.id == AuditEventRecord.run_id)
                    .where(AuditEventRecord.id == event.id)
                )
            ).one()
        assert persisted_metadata == {
            "denial_code": "rate_limit_exhausted",
            "operation_key": operation.operation_key,
            "retry_after_seconds": 59,
        }
        assert "provider" not in str(persisted_metadata).lower()

        replay = _factory().runtime_control_denied(
            run_id=prepared.policy.run_id,
            step_id=prepared.step_ids[0],
            operation_key=operation.operation_key,
            denial_code="rate_limit_exhausted",
            retry_after_seconds=59,
            occurred_at=occurred_at,
        )
        assert replay.id == event.id
        assert replay.aggregate_id == event.aggregate_id
        with pytest.raises(AuditPersistenceInvariantError) as conflict:
            async with prepared.uow_factory() as unit_of_work:
                await unit_of_work.audits.append(replay)
                await unit_of_work.commit()
        assert conflict.value.code == "audit_append_conflict"

        async with prepared.uow_factory() as unit_of_work:
            after_replay = await unit_of_work.audits.list_run(prepared.policy.run_id)
        async with prepared.runtime.session_factory() as session:
            after_replay_counter = (
                await session.execute(
                    select(RunRecord.next_timeline_sequence).where(
                        RunRecord.id == prepared.policy.run_id
                    )
                )
            ).scalar_one()
        assert after_replay == timeline
        assert after_replay_counter == before_replay_counter
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_runtime_denial_action_link_must_reference_exact_run_step(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(tmp_path / "runtime-denial-action-link.db")
    operation = prepared.policy.operations[0]
    event = _factory().runtime_control_denied(
        run_id=prepared.policy.run_id,
        step_id=prepared.step_ids[0],
        action_id="action.orch-06.not-persisted",
        operation_key=operation.operation_key,
        denial_code="tool_budget_exhausted",
        occurred_at=prepared.started_at + timedelta(seconds=1),
    )
    try:
        async with prepared.uow_factory() as unit_of_work:
            before = await unit_of_work.audits.list_run(prepared.policy.run_id)
        with pytest.raises(AuditPersistenceInvariantError) as rejected:
            async with prepared.uow_factory() as unit_of_work:
                await unit_of_work.audits.append(event)
                await unit_of_work.commit()
        assert rejected.value.code == "audit_append_conflict"
        async with prepared.uow_factory() as unit_of_work:
            assert await unit_of_work.audits.list_run(prepared.policy.run_id) == before
    finally:
        await prepared.runtime.dispose()
