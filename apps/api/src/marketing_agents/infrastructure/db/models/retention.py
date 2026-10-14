"""Immutable per-detail expiry and authenticated content-free purge witnesses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class RetentionDetailRecord(Base):
    __tablename__ = "retention_details"
    __table_args__ = (
        CheckConstraint(
            "category IN ('admitted_payload','external_action_payload','approval_detail',"
            "'artifact_detail','connector_receipt_detail','audit_metadata')",
            name="ck_retention_details_category",
        ),
        CheckConstraint("expires_at > created_at", name="ck_retention_details_expiry"),
        CheckConstraint(
            "(purged_at IS NULL AND integrity_digest IS NULL) OR "
            "(purged_at >= expires_at AND length(integrity_digest) = 64)",
            name="ck_retention_details_purge",
        ),
        Index("ix_retention_details_due", "purged_at", "expires_at", "record_kind", "record_id"),
    )

    record_kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    skeleton: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    integrity_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
