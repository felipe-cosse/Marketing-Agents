"""API-05: durable source-event receipts preserve complete webhook fan-out."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.ports.repositories import (
    AuditRepository,
    WebhookReceiptInsertResult,
)
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, WorkMode
from marketing_agents.domain.run_lifecycle import initial_received_transition
from marketing_agents.domain.webhook import (
    WEBHOOK_DIGEST_KEY_VERSION_PREFIX,
    WebhookReceipt,
    WebhookReceiptDelivery,
)
from marketing_agents.infrastructure.db import (
    AgentInstanceConfigurationRecord,
    Base,
    DatabaseRuntime,
    RunRecord,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyWebhookAdmissionUnitOfWorkFactory,
    SQLAlchemyWebhookReceiptRepository,
    SQLAlchemyWorkRepository,
    WebhookReceiptDeliveryRecord,
    WebhookReceiptPersistenceError,
    WebhookReceiptRecord,
    WorkItemRecord,
    create_database_runtime,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 8, 26, 20, tzinfo=UTC)
SOURCE = "local.events"
EVENT_ID = "event.api-05.0001"
TRIGGER_ID = "trigger.webhook.local-events.v1"
MAPPER_VERSION = "mapper.local-envelope.v1"
INSTANCE_IDS = (
    "inst.community.events.attendee-scheduler.01",
    "inst.community.events.attendee-scheduler.02",
)
TAMPER_INSTANCE_ID = "inst.email.newsletter.newsletter-subscriber.01"


def _unused_audit_repository(_session: AsyncSession) -> AuditRepository:
    return cast(AuditRepository, object())


def _factory(runtime: DatabaseRuntime) -> SQLAlchemyWebhookAdmissionUnitOfWorkFactory:
    return SQLAlchemyWebhookAdmissionUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=_unused_audit_repository,
            webhook_receipts=SQLAlchemyWebhookReceiptRepository,
        ),
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


async def _seed_instance_rows(runtime: DatabaseRuntime) -> None:
    async with runtime.session_factory() as session, session.begin():
        for instance_id in (*INSTANCE_IDS, TAMPER_INSTANCE_ID):
            session.add(
                AgentInstanceConfigurationRecord(
                    instance_id=instance_id,
                    enabled=True,
                    variant_label=None,
                    trigger_bindings_json="[]",
                    connector_bindings_json="{}",
                    schedule_json="null",
                    version=1,
                    integrity_digest="a" * 64,
                )
            )


def _work(instance_id: str, ordinal: int) -> WorkItem:
    return WorkItem(
        id=f"work.webhook.api-05.{ordinal:02d}",
        source=SOURCE,
        event_id=EVENT_ID,
        instance_id=instance_id,
        trigger_id=TRIGGER_ID,
        workflow_id="workflow.webhook.community-attendee-scheduler.v1",
        mode=WorkMode.DRY_RUN,
        brief_id=None,
        configuration_revision=1,
        input_digest=f"{ordinal}" * 64,
        admission_digest=f"{ordinal + 2}" * 64,
        created_at=NOW,
        brief_revision=None,
        digest_key_version="admission-hmac-sha256-v1:" + ("f" * 64),
        admitted_payload={
            "request_id": f"request-api-05-{ordinal:04d}",
            "source_content": "authenticated webhook content",
        },
        redacted_input_projection={
            "request_id": f"request-api-05-{ordinal:04d}",
            "source_content": "[REDACTED]",
        },
        input_schema_id="schema.webhook.api-05",
        input_schema_hash="schema-sha256-v1:" + ("b" * 64),
        input_projection_integrity_digest="c" * 64,
    )


def _run(work: WorkItem, ordinal: int) -> Run:
    return Run(
        id=f"run.webhook.api-05.{ordinal:02d}",
        work_item_id=work.id,
        state=RunState.RECEIVED,
        catalog_hash="catalog-sha256-v1:" + ("d" * 64),
        configuration_revision=work.configuration_revision,
        created_at=NOW,
        updated_at=NOW,
    )


def _receipt(
    works_and_runs: tuple[tuple[WorkItem, Run], ...],
    *,
    receipt_id: str = "webhook-receipt.api-05.0001",
    source: str = SOURCE,
    event_id: str = EVENT_ID,
    body_digest: str = "e" * 64,
) -> WebhookReceipt:
    return WebhookReceipt(
        id=receipt_id,
        source=source,
        event_id=event_id,
        trigger_id=TRIGGER_ID,
        body_digest=body_digest,
        digest_key_version=WEBHOOK_DIGEST_KEY_VERSION_PREFIX + ("f" * 64),
        mapper_version=MAPPER_VERSION,
        received_at=NOW,
        deliveries=tuple(
            WebhookReceiptDelivery(
                instance_id=work.instance_id,
                work_item_id=work.id,
                run_id=run.id,
            )
            for work, run in reversed(works_and_runs)
        ),
    )


async def _persist_work_runs(
    runtime: DatabaseRuntime,
) -> tuple[tuple[WorkItem, Run], ...]:
    works_and_runs = tuple(
        (work, _run(work, ordinal))
        for ordinal, instance_id in enumerate(INSTANCE_IDS, start=1)
        for work in (_work(instance_id, ordinal),)
    )
    async with _factory(runtime)() as unit_of_work:
        for work, run in works_and_runs:
            await unit_of_work.works.add(work)
            inserted = await unit_of_work.runs.add_received_or_get(
                run,
                initial_received_transition(run),
            )
            assert inserted.inserted is True
        await unit_of_work.commit()
    return works_and_runs


async def _counts(runtime: DatabaseRuntime) -> tuple[int, int, int, int]:
    models = (
        WebhookReceiptRecord,
        WebhookReceiptDeliveryRecord,
        WorkItemRecord,
        RunRecord,
    )
    async with runtime.session_factory() as session:
        values = [
            int((await session.execute(select(func.count()).select_from(model))).scalar_one())
            for model in models
        ]
    return cast(tuple[int, int, int, int], tuple(values))


@pytest.mark.asyncio
async def test_api_05_complete_fanout_round_trips_and_replays_authoritative_receipt(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "webhook-replay.db"
    runtime = await _runtime(database_path)
    await _seed_instance_rows(runtime)
    works_and_runs = await _persist_work_runs(runtime)
    candidate = _receipt(works_and_runs)
    try:
        async with _factory(runtime)() as unit_of_work:
            created = await unit_of_work.webhook_receipts.add_or_get(candidate)
            await unit_of_work.commit()
        assert created.inserted is True
        assert created.receipt == candidate
        assert tuple(item.instance_id for item in created.receipt.deliveries) == INSTANCE_IDS
        assert await _counts(runtime) == (1, 2, 2, 2)
    finally:
        await runtime.dispose()

    restarted = await _runtime(database_path)
    try:
        changed_candidate = _receipt(works_and_runs, body_digest="0" * 64)
        async with _factory(restarted)() as unit_of_work:
            replayed = await unit_of_work.webhook_receipts.add_or_get(changed_candidate)
            await unit_of_work.commit()
        assert replayed.inserted is False
        assert replayed.receipt == candidate
        assert replayed.receipt.body_digest == "e" * 64
        assert await _counts(restarted) == (1, 2, 2, 2)
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_api_05_write_intent_serializes_simultaneous_source_event_receipts(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "webhook-race.db")
    await _seed_instance_rows(runtime)
    works_and_runs = await _persist_work_runs(runtime)
    candidate = _receipt(works_and_runs)

    async def insert_once() -> WebhookReceiptInsertResult:
        async with _factory(runtime)() as unit_of_work:
            result = await unit_of_work.webhook_receipts.add_or_get(candidate)
            await unit_of_work.commit()
            return result

    try:
        results = await asyncio.gather(insert_once(), insert_once())
        assert sorted(result.inserted for result in results) == [False, True]
        assert results[0].receipt == results[1].receipt == candidate
        assert await _counts(runtime) == (1, 2, 2, 2)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_receipt_rolls_back_without_an_explicit_outer_commit(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "webhook-rollback.db")
    await _seed_instance_rows(runtime)
    works_and_runs = await _persist_work_runs(runtime)
    try:
        async with _factory(runtime)() as unit_of_work:
            inserted = await unit_of_work.webhook_receipts.add_or_get(_receipt(works_and_runs))
            assert inserted.inserted is True

        assert await _counts(runtime) == (0, 0, 2, 2)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_unrelated_work_or_run_reuse_fails_without_partial_receipt(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "webhook-link-conflict.db")
    await _seed_instance_rows(runtime)
    works_and_runs = await _persist_work_runs(runtime)
    try:
        async with _factory(runtime)() as unit_of_work:
            first = await unit_of_work.webhook_receipts.add_or_get(_receipt(works_and_runs))
            await unit_of_work.commit()
        assert first.inserted is True

        conflicting = _receipt(
            works_and_runs,
            receipt_id="webhook-receipt.api-05.0002",
            source="local.other-events",
            event_id="event.api-05.0002",
        )
        with pytest.raises(WebhookReceiptPersistenceError) as error:
            async with _factory(runtime)() as unit_of_work:
                await unit_of_work.webhook_receipts.add_or_get(conflicting)
                await unit_of_work.commit()
        assert error.value.code == "webhook_receipt_insert_conflict"
        assert await _counts(runtime) == (1, 2, 2, 2)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_database_rejects_contradictory_delivery_links(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "webhook-tamper.db")
    await _seed_instance_rows(runtime)
    works_and_runs = await _persist_work_runs(runtime)
    candidate = _receipt(works_and_runs)
    try:
        async with _factory(runtime)() as unit_of_work:
            await unit_of_work.webhook_receipts.add_or_get(candidate)
            await unit_of_work.commit()

        with pytest.raises(IntegrityError):
            async with runtime.session_factory() as session, session.begin():
                await session.execute(
                    update(WebhookReceiptDeliveryRecord)
                    .where(WebhookReceiptDeliveryRecord.instance_id == INSTANCE_IDS[0])
                    .values(instance_id=TAMPER_INSTANCE_ID)
                )

        async with _factory(runtime)() as unit_of_work:
            assert await unit_of_work.webhook_receipts.get(candidate.id) == candidate
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_database_rejects_cross_linked_run_and_work_pairs(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "webhook-cross-link.db")
    await _seed_instance_rows(runtime)
    works_and_runs = await _persist_work_runs(runtime)
    first, second = works_and_runs
    contradictory = WebhookReceipt(
        id="webhook-receipt.api-05.cross-link",
        source=SOURCE,
        event_id=EVENT_ID,
        trigger_id=TRIGGER_ID,
        body_digest="e" * 64,
        digest_key_version=WEBHOOK_DIGEST_KEY_VERSION_PREFIX + ("f" * 64),
        mapper_version=MAPPER_VERSION,
        received_at=NOW,
        deliveries=(
            WebhookReceiptDelivery(
                instance_id=first[0].instance_id,
                work_item_id=first[0].id,
                run_id=second[1].id,
            ),
            WebhookReceiptDelivery(
                instance_id=second[0].instance_id,
                work_item_id=second[0].id,
                run_id=first[1].id,
            ),
        ),
    )
    try:
        with pytest.raises(WebhookReceiptPersistenceError) as error:
            async with _factory(runtime)() as unit_of_work:
                await unit_of_work.webhook_receipts.add_or_get(contradictory)
                await unit_of_work.commit()
        assert error.value.code == "webhook_receipt_insert_conflict"
        assert await _counts(runtime) == (0, 0, 2, 2)
    finally:
        await runtime.dispose()
