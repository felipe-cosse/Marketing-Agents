"""API-07 immutable, fixed-snapshot global audit-feed persistence."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from marketing_agents.application.ports.repositories import AuditFeedPage
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext, AuditEvent, AuditEventDraft
from marketing_agents.infrastructure.db.models import (
    AuditEventRecord,
    AuditFeedSequenceRecord,
)
from marketing_agents.infrastructure.db.repositories import (
    AuditPersistenceInvariantError,
    SQLAlchemyAuditRepository,
)
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import delete, update

from tests.integration.db.test_run_06_artifact_persistence import (
    NOW,
    _prepare_run,
    _runtime,
)


def _configuration_event(index: int) -> AuditEventDraft:
    return AuditEventFactory(
        AuditContext.authenticated_user(
            f"local-operator-{index}",
            authentication_method="local_fixed",
            correlation_id=f"request.api-07.audit.{index}",
        ),
        configuration_pseudonym_key=DigestKey(bytes(range(32))),
    ).instance_configuration_changed(
        instance_id=f"inst.api-07.audit.{index}",
        previous_configuration={
            "enabled": True,
            "variant_label": None,
            "trigger_bindings": [],
            "connector_bindings": {},
            "schedule": None,
        },
        new_configuration={
            "enabled": False,
            "variant_label": None,
            "trigger_bindings": [],
            "connector_bindings": {},
            "schedule": None,
        },
        previous_revision=1,
        new_revision=2,
        occurred_at=NOW + timedelta(seconds=index),
    )


async def _list_feed(
    repository: SQLAlchemyAuditRepository,
    **overrides: Any,
) -> AuditFeedPage:
    arguments: dict[str, Any] = {
        "high_watermark": None,
        "before_feed_sequence": None,
        "run_id": None,
        "step_id": None,
        "action_id": None,
        "approval_id": None,
        "event_type": None,
        "occurred_at_from": None,
        "occurred_at_to": None,
        "limit": 101,
    }
    arguments.update(overrides)
    return await repository.list_feed(**arguments)


@pytest.mark.asyncio
async def test_api_07_feed_allocation_is_serialized_and_rollback_safe(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "audit-allocation.db")
    try:

        async def append(event: AuditEventDraft) -> AuditEvent:
            async with runtime.session_factory() as session, session.begin():
                return await SQLAlchemyAuditRepository(session).append_global(event)

        first, second = await asyncio.gather(
            append(_configuration_event(1)),
            append(_configuration_event(2)),
        )
        assert {first.feed_sequence, second.feed_sequence} == {1, 2}
        assert first.global_sequence != second.global_sequence

        async with runtime.session_factory() as session:
            transaction = await session.begin()
            rolled_back = await SQLAlchemyAuditRepository(session).append_global(
                _configuration_event(3)
            )
            assert rolled_back.feed_sequence == 3
            await transaction.rollback()

        committed = await append(_configuration_event(4))
        assert committed.feed_sequence == 3
        async with runtime.session_factory() as session:
            counter = await session.get(AuditFeedSequenceRecord, 1)
            assert counter is not None and counter.last_sequence == 3
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_07_feed_pages_use_one_fixed_high_watermark_and_filters(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "audit-pages.db")
    try:
        _, persisted = await _prepare_run(runtime, "api-07-feed-run")
        async with runtime.session_factory() as session:
            first_page = await _list_feed(
                SQLAlchemyAuditRepository(session),
                limit=2,
            )
        assert len(first_page.events) == 2
        assert first_page.events[0].feed_sequence == first_page.high_watermark
        boundary = first_page.events[-1].feed_sequence
        assert boundary is not None

        async with runtime.session_factory() as session, session.begin():
            appended = await SQLAlchemyAuditRepository(session).append_global(
                _configuration_event(10)
            )
        assert appended.feed_sequence == first_page.high_watermark + 1

        async with runtime.session_factory() as session:
            repository = SQLAlchemyAuditRepository(session)
            second_page = await _list_feed(
                repository,
                high_watermark=first_page.high_watermark,
                before_feed_sequence=boundary,
                limit=101,
            )
            run_page = await _list_feed(
                repository,
                run_id=persisted.run.id,
                event_type="run.plan_recorded",
                occurred_at_from=persisted.run.created_at - timedelta(seconds=1),
                occurred_at_to=persisted.run.updated_at + timedelta(seconds=1),
                limit=101,
            )
        assert second_page.high_watermark == first_page.high_watermark
        assert all(
            event.feed_sequence is not None and event.feed_sequence <= first_page.high_watermark
            for event in second_page.events
        )
        assert tuple(event.event_type for event in run_page.events) == ("run.plan_recorded",)
        assert all(event.run_id == persisted.run.id for event in run_page.events)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_07_feed_detects_counter_drift_selected_gaps_and_tampering(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "audit-corruption.db")
    try:
        async with runtime.session_factory() as session, session.begin():
            repository = SQLAlchemyAuditRepository(session)
            for index in range(1, 6):
                await repository.append_global(_configuration_event(index))

        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(AuditFeedSequenceRecord)
                .where(AuditFeedSequenceRecord.singleton_id == 1)
                .values(last_sequence=6)
            )
        async with runtime.session_factory() as session:
            with pytest.raises(AuditPersistenceInvariantError, match="counter"):
                await _list_feed(SQLAlchemyAuditRepository(session))

        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(AuditFeedSequenceRecord)
                .where(AuditFeedSequenceRecord.singleton_id == 1)
                .values(last_sequence=5)
            )
            await session.execute(
                delete(AuditEventRecord).where(AuditEventRecord.feed_sequence == 3)
            )
        async with runtime.session_factory() as session:
            with pytest.raises(AuditPersistenceInvariantError, match="sequence gap"):
                await _list_feed(SQLAlchemyAuditRepository(session), limit=5)

        async with runtime.session_factory() as session, session.begin():
            row = await session.get(AuditEventRecord, 4)
            assert row is not None
            row.safe_metadata = {"previous_revision": 999}
        async with runtime.session_factory() as session:
            with pytest.raises(AuditPersistenceInvariantError, match="metadata"):
                await _list_feed(
                    SQLAlchemyAuditRepository(session),
                    high_watermark=5,
                    before_feed_sequence=5,
                    limit=1,
                )
    finally:
        await runtime.dispose()
