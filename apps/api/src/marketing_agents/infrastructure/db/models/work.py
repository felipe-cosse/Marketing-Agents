"""Portable ORM record for admitted work."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class WorkItemRecord(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "event_id",
            "agent_instance_id",
            name="uq_work_items_source_event_instance",
        ),
        CheckConstraint(
            "configuration_revision >= 1",
            name="ck_work_items_configuration_revision_positive",
        ),
        CheckConstraint(
            "campaign_brief_revision IS NULL OR campaign_brief_revision >= 1",
            name="ck_work_items_brief_revision_positive",
        ),
        CheckConstraint(
            "(campaign_brief_id IS NULL AND campaign_brief_revision IS NULL) OR "
            "(campaign_brief_id IS NOT NULL AND campaign_brief_revision IS NOT NULL)",
            name="ck_work_items_brief_reference_complete",
        ),
        CheckConstraint(
            "mode IN ('dry_run', 'mock_execution')",
            name="ck_work_items_mode_supported",
        ),
        CheckConstraint(
            "length(input_digest) = 64 AND length(admission_digest) = 64",
            name="ck_work_items_digest_lengths",
        ),
        CheckConstraint(
            "length(digest_key_version) = 89",
            name="ck_work_items_digest_key_version_length",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    source: Mapped[str] = mapped_column(String(240), nullable=False)
    event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    agent_instance_id: Mapped[str] = mapped_column(String(240), nullable=False)
    trigger_id: Mapped[str] = mapped_column(String(240), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(240), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    campaign_brief_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    campaign_brief_revision: Mapped[int | None] = mapped_column(nullable=True)
    configuration_revision: Mapped[int] = mapped_column(nullable=False)
    admitted_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    admission_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    digest_key_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
