"""SQLAlchemy persistence for complete immutable webhook receipt aggregates."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import (
    WebhookReceiptInsertResult,
    WebhookReceiptRepositoryConflict,
)
from marketing_agents.domain.webhook import WebhookReceipt, WebhookReceiptDelivery
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.infrastructure.db.models.webhook import (
    WebhookReceiptDeliveryRecord,
    WebhookReceiptRecord,
)
from marketing_agents.infrastructure.db.models.work import WorkItemRecord


class WebhookReceiptPersistenceError(WebhookReceiptRepositoryConflict):
    """A stored receipt is partial, contradictory, or cannot be inserted exactly."""


def _to_record(receipt: WebhookReceipt) -> WebhookReceiptRecord:
    return WebhookReceiptRecord(
        id=receipt.id,
        source=receipt.source,
        event_id=receipt.event_id,
        trigger_id=receipt.trigger_id,
        body_digest=receipt.body_digest,
        digest_key_version=receipt.digest_key_version,
        mapper_version=receipt.mapper_version,
        received_at=receipt.received_at,
    )


def _delivery_to_record(
    receipt_id: str,
    delivery: WebhookReceiptDelivery,
) -> WebhookReceiptDeliveryRecord:
    return WebhookReceiptDeliveryRecord(
        receipt_id=receipt_id,
        instance_id=delivery.instance_id,
        work_item_id=delivery.work_item_id,
        run_id=delivery.run_id,
    )


class SQLAlchemyWebhookReceiptRepository:
    """Atomically insert or retrieve one source-event receipt and all target links."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, receipt_id: str) -> WebhookReceipt | None:
        record = await self._session.get(WebhookReceiptRecord, receipt_id)
        return None if record is None else await self._to_domain(record)

    async def get_by_source_event(
        self,
        source: str,
        event_id: str,
    ) -> WebhookReceipt | None:
        statement = (
            select(WebhookReceiptRecord)
            .where(
                WebhookReceiptRecord.source == source,
                WebhookReceiptRecord.event_id == event_id,
            )
            .execution_options(populate_existing=True)
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else await self._to_domain(record)

    async def add_or_get(
        self,
        receipt: WebhookReceipt,
    ) -> WebhookReceiptInsertResult:
        if type(receipt) is not WebhookReceipt:
            raise WebhookReceiptPersistenceError(
                "webhook_receipt_invalid",
                "webhook receipt persistence requires one exact validated aggregate",
            )
        try:
            receipt.__post_init__()
        except (TypeError, ValueError) as exc:
            raise WebhookReceiptPersistenceError(
                "webhook_receipt_invalid",
                "webhook receipt persistence requires one exact validated aggregate",
            ) from exc

        try:
            async with self._session.begin_nested():
                self._session.add(_to_record(receipt))
                await self._session.flush()
                self._session.add_all(
                    _delivery_to_record(receipt.id, delivery) for delivery in receipt.deliveries
                )
                await self._session.flush()
                stored = await self.get(receipt.id)
                if stored is None:
                    raise WebhookReceiptPersistenceError(
                        "webhook_receipt_partial",
                        "inserted webhook receipt could not be read back completely",
                    )
        except IntegrityError as exc:
            existing = await self.get_by_source_event(receipt.source, receipt.event_id)
            if existing is None:
                raise WebhookReceiptPersistenceError(
                    "webhook_receipt_insert_conflict",
                    "webhook receipt conflicted outside its source-event identity",
                ) from exc
            return WebhookReceiptInsertResult(existing, inserted=False)
        return WebhookReceiptInsertResult(stored, inserted=True)

    async def _to_domain(self, record: WebhookReceiptRecord) -> WebhookReceipt:
        statement = (
            select(WebhookReceiptDeliveryRecord, WorkItemRecord, RunRecord)
            .join(
                WorkItemRecord,
                WorkItemRecord.id == WebhookReceiptDeliveryRecord.work_item_id,
            )
            .join(
                RunRecord,
                RunRecord.id == WebhookReceiptDeliveryRecord.run_id,
            )
            .where(WebhookReceiptDeliveryRecord.receipt_id == record.id)
            .order_by(WebhookReceiptDeliveryRecord.instance_id)
            .execution_options(populate_existing=True)
        )
        rows = (await self._session.execute(statement)).all()
        if not rows:
            raise WebhookReceiptPersistenceError(
                "webhook_receipt_partial",
                "persisted webhook receipt has no complete delivery outcome",
            )
        deliveries: list[WebhookReceiptDelivery] = []
        try:
            for delivery, work_item, run in rows:
                if (
                    delivery.receipt_id != record.id
                    or delivery.instance_id != work_item.agent_instance_id
                    or delivery.work_item_id != work_item.id
                    or delivery.run_id != run.id
                    or work_item.source != record.source
                    or work_item.event_id != record.event_id
                    or work_item.trigger_id != record.trigger_id
                    or run.work_item_id != work_item.id
                    or run.configuration_revision != work_item.configuration_revision
                ):
                    raise ValueError("webhook receipt delivery linkage is contradictory")
                deliveries.append(
                    WebhookReceiptDelivery(
                        instance_id=delivery.instance_id,
                        work_item_id=delivery.work_item_id,
                        run_id=delivery.run_id,
                    )
                )
            return WebhookReceipt(
                id=record.id,
                source=record.source,
                event_id=record.event_id,
                trigger_id=record.trigger_id,
                body_digest=record.body_digest,
                digest_key_version=record.digest_key_version,
                mapper_version=record.mapper_version,
                received_at=record.received_at,
                deliveries=tuple(deliveries),
            )
        except (TypeError, ValueError) as exc:
            raise WebhookReceiptPersistenceError(
                "webhook_receipt_tampered",
                "persisted webhook receipt is invalid or contradictory",
            ) from exc


__all__ = ["SQLAlchemyWebhookReceiptRepository", "WebhookReceiptPersistenceError"]
