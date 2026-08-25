"""Portable persistent schedule configuration and next-run projection."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
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
            "length(recurrence_version) BETWEEN 1 AND 64 "
            "AND recurrence_version = trim(recurrence_version)",
            name="ck_schedules_recurrence_version_bounded",
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
    workflow_id: Mapped[str] = mapped_column(String(240), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(100), nullable=False)
    recurrence_version: Mapped[str] = mapped_column(String(64), nullable=False)
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


class ScheduleOccurrenceRecord(Base):
    """One immutable due identity with an atomic WorkItem and primary-Run link."""

    __tablename__ = "schedule_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "scheduled_for_utc",
            name="uq_schedule_occurrences_schedule_due",
        ),
        UniqueConstraint(
            "work_item_id",
            name="uq_schedule_occurrences_work_item",
        ),
        UniqueConstraint(
            "run_id",
            name="uq_schedule_occurrences_run",
        ),
        CheckConstraint(
            "length(identity_scheme) BETWEEN 1 AND 64 AND identity_scheme = trim(identity_scheme)",
            name="ck_schedule_occurrences_identity_scheme_bounded",
        ),
        CheckConstraint(
            "length(recurrence_version) BETWEEN 1 AND 64 "
            "AND recurrence_version = trim(recurrence_version)",
            name="ck_schedule_occurrences_recurrence_version_bounded",
        ),
        CheckConstraint(
            "length(scheduled_local) = 26",
            name="ck_schedule_occurrences_local_canonical_length",
        ),
        CheckConstraint(
            "length(timezone_name) BETWEEN 1 AND 100 AND timezone_name = trim(timezone_name)",
            name="ck_schedule_occurrences_timezone_bounded",
        ),
        CheckConstraint(
            "timezone_fold IN (0,1)",
            name="ck_schedule_occurrences_fold_supported",
        ),
        CheckConstraint(
            "state IN ('due','claimed','enqueued','skipped','completed')",
            name="ck_schedule_occurrences_state_supported",
        ),
        CheckConstraint(
            "(work_item_id IS NULL AND run_id IS NULL) OR "
            "(work_item_id IS NOT NULL AND run_id IS NOT NULL)",
            name="ck_schedule_occurrences_receipt_complete",
        ),
        CheckConstraint(
            "(state IN ('enqueued','completed') AND work_item_id IS NOT NULL) OR "
            "(state IN ('due','claimed','skipped') AND work_item_id IS NULL)",
            name="ck_schedule_occurrences_state_receipt",
        ),
        CheckConstraint(
            "length(integrity_digest) = 64",
            name="ck_schedule_occurrences_integrity_digest_length",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    identity_scheme: Mapped[str] = mapped_column(String(64), nullable=False)
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("schedules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scheduled_for_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    scheduled_local: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone_fold: Mapped[int] = mapped_column(Integer, nullable=False)
    recurrence_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    work_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("work_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
