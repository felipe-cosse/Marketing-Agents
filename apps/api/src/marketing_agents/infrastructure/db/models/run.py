"""Portable primary-Run and append-only lifecycle transition records."""

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

RUN_STATES_SQL = (
    "'received','validated','planned','awaiting_approval','executing',"
    "'completed','failed','rejected','cancelled'"
)
RUN_COMMANDS_SQL = (
    "'receive','mark_validated','record_plan','activate_plan','release_approved_plan',"
    "'reject_approval','complete','fail','cancel'"
)


class RunRecord(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "work_item_id",
            name="uq_runs_id_work_item",
        ),
        CheckConstraint("version >= 1", name="ck_runs_version_positive"),
        CheckConstraint(
            "next_timeline_sequence >= 0",
            name="ck_runs_timeline_sequence_nonnegative",
        ),
        CheckConstraint(
            "configuration_revision >= 1",
            name="ck_runs_configuration_revision_positive",
        ),
        CheckConstraint(f"state IN ({RUN_STATES_SQL})", name="ck_runs_state_supported"),
        CheckConstraint(
            "(state IN ('completed','failed','rejected','cancelled') AND "
            "terminal_reason_code IS NOT NULL) OR "
            "(state NOT IN ('completed','failed','rejected','cancelled') AND "
            "terminal_reason_code IS NULL)",
            name="ck_runs_terminal_reason_matches_state",
        ),
        CheckConstraint(
            "(state IN ('received','validated') AND approval_required IS NULL) OR "
            "(state IN ('planned','executing','completed') AND approval_required IS NOT NULL) OR "
            "(state IN ('awaiting_approval','rejected') AND approval_required IS TRUE) OR "
            "state IN ('failed','cancelled')",
            name="ck_runs_approval_disposition_matches_state",
        ),
        Index("ix_runs_state_created_at", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    catalog_hash: Mapped[str] = mapped_column(String(96), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    terminal_reason_code: Mapped[str | None] = mapped_column(String(240), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    next_timeline_sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )


class RunStateTransitionRecord(Base):
    __tablename__ = "run_state_transitions"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_run_state_transitions_sequence_positive"),
        CheckConstraint(
            "sequence = resulting_version AND resulting_version = expected_version + 1",
            name="ck_run_state_transitions_versions_contiguous",
        ),
        CheckConstraint(
            "completed_effect_count >= 0 AND outcome_unknown_effect_count >= 0",
            name="ck_run_state_transitions_effect_counts_nonnegative",
        ),
        CheckConstraint(
            f"new_state IN ({RUN_STATES_SQL}) AND "
            f"(previous_state IS NULL OR previous_state IN ({RUN_STATES_SQL}))",
            name="ck_run_state_transitions_states_supported",
        ),
        CheckConstraint(
            f"command IN ({RUN_COMMANDS_SQL})",
            name="ck_run_state_transitions_command_supported",
        ),
        CheckConstraint(
            "(sequence = 1 AND command = 'receive' AND previous_state IS NULL AND "
            "new_state = 'received' AND expected_version = 0 AND resulting_version = 1) OR "
            "(sequence > 1 AND command <> 'receive' AND previous_state IS NOT NULL AND "
            "previous_state <> new_state)",
            name="ck_run_state_transitions_initial_or_change",
        ),
        CheckConstraint(
            "command = 'cancel' OR "
            "(completed_effect_count = 0 AND outcome_unknown_effect_count = 0)",
            name="ck_run_state_transitions_effect_counts_cancel_only",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    command: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(240), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_version: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_effect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome_unknown_effect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
