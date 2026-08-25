"""Portable persistent schedule configuration and next-run projection."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class ScheduleRecord(Base):
    """One v1 schedule configuration with its currently persisted next UTC occurrence."""

    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint(
            "length(cron_expression) BETWEEN 1 AND 100 AND cron_expression = trim(cron_expression)",
            name="ck_schedules_cron_bounded",
        ),
        CheckConstraint(
            "length(timezone_name) BETWEEN 1 AND 100 AND timezone_name = trim(timezone_name)",
            name="ck_schedules_timezone_bounded",
        ),
        CheckConstraint(
            "misfire_policy IN ('skip','run_once')",
            name="ck_schedules_misfire_policy_supported",
        ),
        CheckConstraint("version >= 1", name="ck_schedules_version_positive"),
        CheckConstraint(
            "lease_owner IS NULL OR "
            "(length(lease_owner) BETWEEN 1 AND 240 AND lease_owner = trim(lease_owner))",
            name="ck_schedules_lease_owner_bounded",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_claimed_at_utc IS NULL "
            "AND lease_expires_at_utc IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_claimed_at_utc IS NOT NULL "
            "AND lease_expires_at_utc IS NOT NULL)",
            name="ck_schedules_lease_complete",
        ),
        CheckConstraint(
            "lease_expires_at_utc IS NULL OR lease_expires_at_utc > lease_claimed_at_utc",
            name="ck_schedules_lease_expiry_after_claim",
        ),
        CheckConstraint(
            "lease_claimed_at_utc IS NULL OR next_run_at_utc <= lease_claimed_at_utc",
            name="ck_schedules_lease_due_at_claim",
        ),
        CheckConstraint(
            "lease_owner IS NULL OR version >= 2",
            name="ck_schedules_lease_version_advanced",
        ),
        CheckConstraint(
            "length(integrity_digest) = 64",
            name="ck_schedules_integrity_digest_length",
        ),
        Index(
            "ix_schedules_enabled_next_run_id",
            "enabled",
            "next_run_at_utc",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    trigger_id: Mapped[str] = mapped_column(String(240), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(240), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(100), nullable=False)
    next_run_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    misfire_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean(create_constraint=True, name="bool_schedules_enabled"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(240), nullable=True)
    lease_claimed_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_expires_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
