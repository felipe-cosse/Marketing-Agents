"""Canonical approval request, decision, and single-use persistence records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime

APPROVAL_STATUSES_SQL = "'pending','approved','rejected','expired','consumed'"


class ApprovalRequestRecord(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("action_id", "generation", name="uq_approval_request_generation"),
        UniqueConstraint(
            "id", "action_id", "run_id", "step_id", name="uq_approval_request_binding"
        ),
        UniqueConstraint(
            "id",
            "action_id",
            "action_hash",
            "authorization_set_id",
            "run_id",
            "plan_hash",
            "proposal_revision",
            "step_id",
            "step_key",
            name="uq_approval_request_exact_binding",
        ),
        UniqueConstraint("replacement_request_id", name="uq_approval_request_replacement_target"),
        UniqueConstraint(
            "run_id",
            "plan_hash",
            "proposal_revision",
            "step_key",
            "generation",
            name="uq_approval_request_plan_step_generation",
        ),
        ForeignKeyConstraint(
            [
                "action_id",
                "action_hash",
                "authorization_set_id",
                "run_id",
                "plan_hash",
                "proposal_revision",
                "step_id",
                "step_key",
            ],
            [
                "external_actions.id",
                "external_actions.action_hash",
                "external_actions.authorization_set_id",
                "external_actions.run_id",
                "external_actions.plan_hash",
                "external_actions.proposal_revision",
                "external_actions.step_id",
                "external_actions.step_key",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["step_id", "run_id", "step_key"],
            ["run_steps.id", "run_steps.run_id", "run_steps.key"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "replacement_request_id",
                "action_id",
                "action_hash",
                "authorization_set_id",
                "run_id",
                "plan_hash",
                "proposal_revision",
                "step_id",
                "step_key",
            ],
            [
                "approval_requests.id",
                "approval_requests.action_id",
                "approval_requests.action_hash",
                "approval_requests.authorization_set_id",
                "approval_requests.run_id",
                "approval_requests.plan_hash",
                "approval_requests.proposal_revision",
                "approval_requests.step_id",
                "approval_requests.step_key",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({APPROVAL_STATUSES_SQL})", name="ck_approval_request_status"),
        CheckConstraint(
            "generation >= 1 AND proposal_revision >= 1 AND version >= 1",
            name="ck_approval_request_positive_versions",
        ),
        CheckConstraint(
            "length(action_hash) = 64 AND length(plan_hash) = 64 AND "
            "length(semantic_action_hash) = 64 AND length(integrity_digest) = 64",
            name="ck_approval_request_hash_lengths",
        ),
        CheckConstraint(
            "expires_after_seconds BETWEEN 60 AND 86400 AND expires_at > requested_at "
            "AND updated_at >= requested_at",
            name="ck_approval_request_times",
        ),
        CheckConstraint(
            "(replacement_request_id IS NULL AND renewed_at IS NULL) OR "
            "(replacement_request_id IS NOT NULL AND renewed_at IS NOT NULL)",
            name="ck_approval_request_renewal_complete",
        ),
        CheckConstraint(
            "(status = 'pending' AND version = 1 AND updated_at = requested_at AND "
            "expired_at IS NULL AND replacement_request_id IS NULL) OR "
            "(status IN ('approved','rejected') AND version = 2 AND expired_at IS NULL "
            "AND replacement_request_id IS NULL) OR "
            "(status = 'expired' AND expired_at IS NOT NULL AND expired_at >= expires_at "
            ") OR "
            "(status = 'consumed' AND version >= 3 AND expired_at IS NULL AND "
            "replacement_request_id IS NULL)",
            name="ck_approval_request_lifecycle_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(240), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_set_id: Mapped[str] = mapped_column(String(240), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[str] = mapped_column(String(240), nullable=False)
    step_key: Mapped[str] = mapped_column(String(240), nullable=False)
    template_id: Mapped[str] = mapped_column(String(240), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(240), nullable=False)
    action_type: Mapped[str] = mapped_column(String(240), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(240), nullable=False)
    connector_family: Mapped[str] = mapped_column(String(120), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(240), nullable=False)
    semantic_action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    redacted_destination: Mapped[str] = mapped_column(String(300), nullable=False)
    redacted_projection: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    policy_id: Mapped[str] = mapped_column(String(240), nullable=False)
    required_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    required_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    expires_after_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    allow_self_approval: Mapped[bool] = mapped_column(
        Boolean(create_constraint=True, name="bool_approval_request_self_approval"),
        nullable=False,
    )
    requested_by: Mapped[str] = mapped_column(String(240), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    replacement_request_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    renewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class ApprovalDecisionRecord(Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_approval_decision_request"),
        UniqueConstraint(
            "id",
            "request_id",
            "action_id",
            "action_hash",
            "authorization_set_id",
            "run_id",
            "plan_hash",
            "proposal_revision",
            "step_id",
            "step_key",
            name="uq_approval_decision_binding",
        ),
        ForeignKeyConstraint(
            [
                "request_id",
                "action_id",
                "action_hash",
                "authorization_set_id",
                "run_id",
                "plan_hash",
                "proposal_revision",
                "step_id",
                "step_key",
            ],
            [
                "approval_requests.id",
                "approval_requests.action_id",
                "approval_requests.action_hash",
                "approval_requests.authorization_set_id",
                "approval_requests.run_id",
                "approval_requests.plan_hash",
                "approval_requests.proposal_revision",
                "approval_requests.step_id",
                "approval_requests.step_key",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("decision IN ('approve','reject')", name="ck_approval_decision_kind"),
        CheckConstraint(
            "(decision = 'approve' AND reason_code = 'approval_granted') OR "
            "(decision = 'reject' AND reason_code = 'approval_rejected')",
            name="ck_approval_decision_reason",
        ),
        CheckConstraint(
            "proposal_revision >= 1 AND length(action_hash) = 64 AND length(plan_hash) = 64 "
            "AND length(integrity_digest) = 64",
            name="ck_approval_decision_binding_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(240), nullable=False)
    action_id: Mapped[str] = mapped_column(String(240), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_set_id: Mapped[str] = mapped_column(String(240), nullable=False)
    run_id: Mapped[str] = mapped_column(String(240), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[str] = mapped_column(String(240), nullable=False)
    step_key: Mapped[str] = mapped_column(String(240), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(240), nullable=False)
    authentication_method: Mapped[str] = mapped_column(String(240), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(240), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    authority_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    authority_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class ApprovalUseRecord(Base):
    __tablename__ = "approval_uses"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_approval_use_request"),
        UniqueConstraint("decision_id", name="uq_approval_use_decision"),
        UniqueConstraint("reservation_id", name="uq_approval_use_reservation"),
        ForeignKeyConstraint(
            [
                "decision_id",
                "request_id",
                "action_id",
                "action_hash",
                "authorization_set_id",
                "run_id",
                "plan_hash",
                "proposal_revision",
                "step_id",
                "step_key",
            ],
            [
                "approval_decisions.id",
                "approval_decisions.request_id",
                "approval_decisions.action_id",
                "approval_decisions.action_hash",
                "approval_decisions.authorization_set_id",
                "approval_decisions.run_id",
                "approval_decisions.plan_hash",
                "approval_decisions.proposal_revision",
                "approval_decisions.step_id",
                "approval_decisions.step_key",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "action_id",
                "reservation_id",
                "request_id",
                "decision_id",
                "action_hash",
                "authorization_set_id",
                "run_id",
                "step_id",
                "used_at",
            ],
            [
                "external_actions.id",
                "external_actions.reservation_id",
                "external_actions.approval_request_id",
                "external_actions.approval_decision_id",
                "external_actions.action_hash",
                "external_actions.authorization_set_id",
                "external_actions.run_id",
                "external_actions.step_id",
                "external_actions.reserved_at",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "proposal_revision >= 1 AND length(action_hash) = 64 AND length(plan_hash) = 64 "
            "AND length(integrity_digest) = 64",
            name="ck_approval_use_binding_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(240), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(240), nullable=False)
    action_id: Mapped[str] = mapped_column(String(240), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_set_id: Mapped[str] = mapped_column(String(240), nullable=False)
    run_id: Mapped[str] = mapped_column(String(240), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[str] = mapped_column(String(240), nullable=False)
    step_key: Mapped[str] = mapped_column(String(240), nullable=False)
    reservation_id: Mapped[str] = mapped_column(String(240), nullable=False)
    used_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
