"""Durable external-action, dispatch-attempt, and mock-receipt records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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

ACTION_STATES_SQL = (
    "'proposed','awaiting_approval','approved','dispatch_reserved','dispatching',"
    "'succeeded','failed','rejected','cancelled','superseded','outcome_unknown'"
)
IDEMPOTENCY_SUPPORT_SQL = "'required','supported','unavailable'"


class ExternalActionRecord(Base):
    __tablename__ = "external_actions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_external_actions_idempotency_key"),
        UniqueConstraint(
            "run_id",
            "plan_hash",
            "proposal_revision",
            "step_key",
            name="uq_external_actions_plan_revision_step",
        ),
        CheckConstraint(f"state IN ({ACTION_STATES_SQL})", name="ck_actions_state"),
        CheckConstraint(
            f"idempotency_support IN ({IDEMPOTENCY_SUPPORT_SQL})",
            name="ck_actions_idempotency_support",
        ),
        CheckConstraint(
            "length(plan_hash) = 64 AND length(semantic_action_hash) = 64 "
            "AND length(action_hash) = 64",
            name="ck_actions_hash_lengths",
        ),
        CheckConstraint(
            "proposal_revision >= 1 AND version >= 1 AND "
            "delivery_attempt_limit BETWEEN 1 AND 10 AND delivery_attempt_count >= 0 AND "
            "delivery_attempt_count <= delivery_attempt_limit",
            name="ck_actions_positive_revisions_attempts",
        ),
        CheckConstraint(
            "binding_configuration_revision >= 1",
            name="ck_actions_binding_revision_positive",
        ),
        CheckConstraint(
            "timeout_seconds >= 1 AND timeout_seconds <= 120",
            name="ck_actions_timeout_bounded",
        ),
        CheckConstraint(
            "(reservation_id IS NULL AND reservation_authorization_set_id IS NULL AND "
            "approval_request_id IS NULL AND approval_decision_id IS NULL AND "
            "reservation_action_hash IS NULL AND reservation_capability_id IS NULL AND "
            "reservation_binding_id IS NULL AND reservation_idempotency_key IS NULL AND "
            "reserved_at IS NULL) OR "
            "(reservation_id IS NOT NULL AND reservation_authorization_set_id IS NOT NULL "
            "AND approval_request_id IS NOT NULL AND approval_decision_id IS NOT NULL "
            "AND reservation_action_hash IS NOT NULL AND reservation_capability_id IS NOT NULL "
            "AND reservation_binding_id IS NOT NULL AND "
            "reservation_idempotency_key IS NOT NULL "
            "AND reserved_at IS NOT NULL)",
            name="ck_actions_reservation_complete",
        ),
        CheckConstraint(
            "reservation_id IS NULL OR (reservation_action_hash = action_hash AND "
            "reservation_capability_id = capability_id AND "
            "reservation_binding_id = connector_binding_id AND "
            "reservation_idempotency_key = idempotency_key)",
            name="ck_actions_reservation_exact_binding",
        ),
        CheckConstraint(
            "(state IN ('dispatch_reserved','dispatching','succeeded','failed',"
            "'outcome_unknown') AND reservation_id IS NOT NULL) OR "
            "(state IN ('proposed','awaiting_approval','approved') AND "
            "reservation_id IS NULL) OR state IN ('rejected','cancelled','superseded')",
            name="ck_actions_reservation_state",
        ),
        CheckConstraint(
            "(state = 'dispatching' AND dispatch_lease_owner IS NOT NULL AND "
            "dispatch_attempt_number IS NOT NULL AND dispatch_claimed_at IS NOT NULL AND "
            "dispatch_lease_expires_at IS NOT NULL AND "
            "dispatch_attempt_number = delivery_attempt_count) OR "
            "(state <> 'dispatching' AND dispatch_lease_owner IS NULL AND "
            "dispatch_attempt_number IS NULL AND dispatch_claimed_at IS NULL AND "
            "dispatch_lease_expires_at IS NULL AND connector_call_started_at IS NULL)",
            name="ck_actions_dispatch_lease_state",
        ),
        CheckConstraint(
            "(state = 'succeeded' AND connector_receipt_id IS NOT NULL AND "
            "connector_result_status IS NOT NULL AND connector_safe_metadata IS NOT NULL "
            "AND completed_at IS NOT NULL AND "
            "terminal_reason_code IS NULL) OR "
            "(state <> 'succeeded' AND connector_receipt_id IS NULL AND "
            "connector_result_status IS NULL AND connector_safe_metadata IS NULL AND "
            "completed_at IS NULL)",
            name="ck_actions_success_result_state",
        ),
        CheckConstraint(
            "(state IN ('failed','rejected','cancelled','superseded','outcome_unknown') "
            "AND terminal_reason_code IS NOT NULL) OR "
            "(state NOT IN ('failed','rejected','cancelled','superseded','outcome_unknown') "
            "AND terminal_reason_code IS NULL)",
            name="ck_actions_terminal_reason_state",
        ),
        CheckConstraint(
            "state NOT IN ('dispatching','succeeded','failed','outcome_unknown') OR "
            "delivery_attempt_count >= 1",
            name="ck_actions_attempted_state_count",
        ),
        CheckConstraint(
            "(state = 'superseded' AND superseded_by_action_id IS NOT NULL AND "
            "superseded_at IS NOT NULL) OR "
            "(state <> 'superseded' AND superseded_by_action_id IS NULL AND "
            "superseded_at IS NULL)",
            name="ck_actions_superseded_state",
        ),
        Index("ix_external_actions_run_plan", "run_id", "plan_hash"),
        Index("ix_external_actions_stale", "state", "dispatch_lease_expires_at"),
        Index("ix_external_actions_authorization_set", "authorization_set_id", "step_key"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    step_id: Mapped[str] = mapped_column(String(240), nullable=False)
    authorization_set_id: Mapped[str] = mapped_column(String(240), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(String(240), nullable=False)
    action_type: Mapped[str] = mapped_column(String(120), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(240), nullable=False)
    connector_family: Mapped[str] = mapped_column(String(120), nullable=False)
    connector_binding_id: Mapped[str] = mapped_column(String(240), nullable=False)
    semantic_action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_envelope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    redacted_projection: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approval_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    binding_configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    request_schema_id: Mapped[str] = mapped_column(String(240), nullable=False)
    idempotency_support: Mapped[str] = mapped_column(String(32), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_attempt_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    reservation_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    reservation_authorization_set_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    approval_request_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    approval_decision_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    reservation_action_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reservation_capability_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    reservation_binding_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    reservation_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reserved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    dispatch_lease_owner: Mapped[str | None] = mapped_column(String(240), nullable=True)
    dispatch_attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    dispatch_lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    connector_call_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    connector_receipt_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    connector_result_status: Mapped[str | None] = mapped_column(String(120), nullable=True)
    connector_safe_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    terminal_reason_code: Mapped[str | None] = mapped_column(String(240), nullable=True)
    superseded_by_action_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ExternalActionDispatchAttemptRecord(Base):
    __tablename__ = "external_action_dispatch_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="ck_action_attempt_number_positive"),
        CheckConstraint(
            f"idempotency_support IN ({IDEMPOTENCY_SUPPORT_SQL})",
            name="ck_action_attempt_idempotency_support",
        ),
        CheckConstraint(
            "conclusion IS NULL OR conclusion IN "
            "('succeeded','failed','outcome_unknown','pre_call_expired','provider_retry')",
            name="ck_action_attempt_conclusion",
        ),
        CheckConstraint(
            "(completed_at IS NULL AND conclusion IS NULL) OR "
            "(completed_at IS NOT NULL AND conclusion IS NOT NULL)",
            name="ck_action_attempt_completion",
        ),
    )

    external_action_id: Mapped[str] = mapped_column(
        ForeignKey("external_actions.id", ondelete="RESTRICT"), primary_key=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_support: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_owner: Mapped[str] = mapped_column(String(240), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    call_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    conclusion: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(240), nullable=True)
    connector_receipt_id: Mapped[str | None] = mapped_column(String(240), nullable=True)


class ConnectorActionReceiptRecord(Base):
    __tablename__ = "connector_action_receipts"
    __table_args__ = (
        UniqueConstraint("receipt_id", name="uq_connector_action_receipts_receipt_id"),
        UniqueConstraint(
            "external_action_id",
            name="uq_connector_action_receipts_external_action_id",
        ),
        CheckConstraint("length(action_hash) = 64", name="ck_connector_receipt_hash_length"),
    )

    connector_binding_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    external_action_id: Mapped[str] = mapped_column(
        ForeignKey("external_actions.id", ondelete="RESTRICT"), nullable=False
    )
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(240), nullable=False)
    receipt_id: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(120), nullable=False)
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
