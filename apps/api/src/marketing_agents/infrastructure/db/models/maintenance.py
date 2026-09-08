"""Portable retention-job ledger with bounded completion and lease state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class MaintenanceRunRecord(Base):
    __tablename__ = "maintenance_runs"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_maintenance_runs_occurrence_key"),
        CheckConstraint("state IN ('running', 'completed', 'failed')", name="ck_maintenance_state"),
        CheckConstraint("version >= 1", name="ck_maintenance_version_positive"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_claimed_at IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_claimed_at IS NOT NULL AND "
            "lease_expires_at IS NOT NULL AND lease_expires_at > lease_claimed_at)",
            name="ck_maintenance_lease_complete",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_maintenance_completion_order",
        ),
        CheckConstraint("length(counts_json) BETWEEN 2 AND 4096", name="ck_maintenance_counts"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    occurrence_key: Mapped[str] = mapped_column(String(240))
    job_kind: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(240), nullable=True)
    lease_claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    counts_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
