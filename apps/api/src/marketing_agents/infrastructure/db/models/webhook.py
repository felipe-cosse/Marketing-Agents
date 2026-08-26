"""Portable durable receipts for authenticated webhook fan-out."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.domain.webhook import WEBHOOK_DIGEST_KEY_VERSION_PREFIX
from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime

_DIGEST_KEY_VERSION_LENGTH = len(WEBHOOK_DIGEST_KEY_VERSION_PREFIX) + 64


class WebhookReceiptRecord(Base):
    """One authenticated source event, independent of its delivery fan-out size."""

    __tablename__ = "webhook_receipts"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "event_id",
            name="uq_webhook_receipts_source_event",
        ),
        CheckConstraint(
            "length(id) BETWEEN 1 AND 240 AND id = trim(id)",
            name="ck_webhook_receipts_id_bounded",
        ),
        CheckConstraint(
            "length(source) BETWEEN 1 AND 240 AND source = trim(source)",
            name="ck_webhook_receipts_source_bounded",
        ),
        CheckConstraint(
            "length(event_id) BETWEEN 1 AND 240 AND event_id = trim(event_id)",
            name="ck_webhook_receipts_event_id_bounded",
        ),
        CheckConstraint(
            "length(trigger_id) BETWEEN 1 AND 240 AND trigger_id = trim(trigger_id)",
            name="ck_webhook_receipts_trigger_id_bounded",
        ),
        CheckConstraint(
            "length(body_digest) = 64 AND body_digest = lower(body_digest)",
            name="ck_webhook_receipts_body_digest",
        ),
        CheckConstraint(
            f"length(digest_key_version) = {_DIGEST_KEY_VERSION_LENGTH} AND "
            f"substr(digest_key_version, 1, {len(WEBHOOK_DIGEST_KEY_VERSION_PREFIX)}) = "
            f"'{WEBHOOK_DIGEST_KEY_VERSION_PREFIX}'",
            name="ck_webhook_receipts_digest_key_version",
        ),
        CheckConstraint(
            "length(mapper_version) BETWEEN 1 AND 240 AND mapper_version = trim(mapper_version)",
            name="ck_webhook_receipts_mapper_version_bounded",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    source: Mapped[str] = mapped_column(String(240), nullable=False)
    event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    trigger_id: Mapped[str] = mapped_column(String(240), nullable=False)
    body_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    digest_key_version: Mapped[str] = mapped_column(String(128), nullable=False)
    mapper_version: Mapped[str] = mapped_column(String(240), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class WebhookReceiptDeliveryRecord(Base):
    """One immutable target outcome belonging to a source-level receipt."""

    __tablename__ = "webhook_receipt_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "work_item_id",
            name="uq_webhook_receipt_deliveries_work_item",
        ),
        UniqueConstraint(
            "run_id",
            name="uq_webhook_receipt_deliveries_run",
        ),
        ForeignKeyConstraint(
            ("work_item_id", "instance_id"),
            ("work_items.id", "work_items.agent_instance_id"),
            ondelete="RESTRICT",
            name="fk_webhook_receipt_deliveries_work_instance",
        ),
        ForeignKeyConstraint(
            ("run_id", "work_item_id"),
            ("runs.id", "runs.work_item_id"),
            ondelete="RESTRICT",
            name="fk_webhook_receipt_deliveries_run_work",
        ),
        CheckConstraint(
            "length(receipt_id) BETWEEN 1 AND 240 AND receipt_id = trim(receipt_id)",
            name="ck_webhook_receipt_deliveries_receipt_id_bounded",
        ),
        CheckConstraint(
            "length(instance_id) BETWEEN 1 AND 240 AND instance_id = trim(instance_id)",
            name="ck_webhook_receipt_deliveries_instance_id_bounded",
        ),
        CheckConstraint(
            "length(work_item_id) BETWEEN 1 AND 240 AND work_item_id = trim(work_item_id)",
            name="ck_webhook_receipt_deliveries_work_item_id_bounded",
        ),
        CheckConstraint(
            "length(run_id) BETWEEN 1 AND 240 AND run_id = trim(run_id)",
            name="ck_webhook_receipt_deliveries_run_id_bounded",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(
        String(240),
        ForeignKey("webhook_receipts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    instance_id: Mapped[str] = mapped_column(
        String(240),
        ForeignKey("agent_instance_configs.instance_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    work_item_id: Mapped[str] = mapped_column(
        String(240),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(
        String(240),
        nullable=False,
    )


__all__ = ["WebhookReceiptDeliveryRecord", "WebhookReceiptRecord"]
