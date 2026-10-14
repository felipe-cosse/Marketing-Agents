"""Recorded receipt validation keeps stable replay errors and never heals evidence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from marketing_agents.application.services import (
    ScheduleClaimProcessingDisposition,
    ScheduleClaimProcessingError,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.run_lifecycle import initial_received_transition
from marketing_agents.infrastructure.db import SQLAlchemyAuditRepository

from tests.integration.scheduler.test_sched_06_restart_recovery import (
    DUE,
    CommitForbiddenUnitOfWork,
    ForbiddenClock,
    ForbiddenIds,
    MutableClock,
    _audit_snapshot,
    _command,
    _counts,
    _custom_uow_factory,
    _dependencies,
    _runtime,
    _schedule_snapshot,
    _seed_claim,
    _service,
)


@pytest.mark.parametrize("fault", ["tampered", "nonauthoritative"])
@pytest.mark.asyncio
async def test_exec_06_committed_receipt_rejects_invalid_or_substituted_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    runtime = await _runtime(tmp_path / f"receipt-{fault}.db")
    try:
        factory, claim = await _seed_claim(runtime, MutableClock(DUE))
        created = await _service(_dependencies(factory, MutableClock(DUE))).process_claimed_once(
            _command(claim)
        )
        assert created.disposition is ScheduleClaimProcessingDisposition.PROCESSED
        assert created.run is not None
        run = created.run
        committed_schedule = await _schedule_snapshot(runtime)
        committed_audits = await _audit_snapshot(runtime)
        assert await _counts(runtime) == (1, 1, 1, 1, 3)
        original_get = SQLAlchemyAuditRepository.get_mutation_event

        async def substituted_receipt(
            repository: SQLAlchemyAuditRepository,
            aggregate_type: str,
            aggregate_id: str,
            mutation_version: int,
        ) -> AuditEvent | None:
            event = await original_get(repository, aggregate_type, aggregate_id, mutation_version)
            if (aggregate_type, aggregate_id, mutation_version) != ("run", run.id, 1):
                return event
            assert event is not None
            if fault == "tampered":
                # Simulate an invalid typed port response after normal persistence
                # validation; no ordinary constructor admits this broken seal.
                object.__setattr__(event.draft, "correlation_id", "other.correlation")
                with pytest.raises(ValueError):
                    event.draft.verify_integrity()
                return event
            alternate = AuditEventFactory.for_recorded_event(
                AuditContext.worker("worker.other", correlation_id=created.occurrence.id),
                event.draft,
            ).run_transition(run, initial_received_transition(run))
            alternate.verify_integrity()
            return replace(event, draft=alternate)

        monkeypatch.setattr(SQLAlchemyAuditRepository, "get_mutation_event", substituted_receipt)
        read_only_factory = _custom_uow_factory(
            runtime, factory.repository_factories, CommitForbiddenUnitOfWork
        )
        with pytest.raises(ScheduleClaimProcessingError) as failure:
            await _service(
                _dependencies(read_only_factory, ForbiddenClock(), ids=ForbiddenIds())
            ).process_claimed_once(_command(claim))

        assert failure.value.code == "committed_outcome_incomplete"
        assert await _counts(runtime) == (1, 1, 1, 1, 3)
        assert await _schedule_snapshot(runtime) == committed_schedule
        assert await _audit_snapshot(runtime) == committed_audits
    finally:
        await runtime.dispose()
