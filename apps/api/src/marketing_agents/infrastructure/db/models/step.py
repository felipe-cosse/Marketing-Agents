"""Persisted plan steps, explicit dependencies, and append-only state history."""

from __future__ import annotations

from datetime import datetime

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

STEP_STATES_SQL = (
    "'pending','ready','awaiting_approval','executing','succeeded','failed',"
    "'rejected','cancelled','skipped'"
)
STEP_COMMANDS_SQL = (
    "'initialize','mark_ready','wait_for_approval','start','succeed','fail',"
    "'reject','cancel','skip'"
)


class RunStepRecord(Base):
    __tablename__ = "run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "key", name="uq_run_steps_run_key"),
        UniqueConstraint("run_id", "ordinal", name="uq_run_steps_run_ordinal"),
        UniqueConstraint("id", "run_id", name="uq_run_steps_id_run"),
        UniqueConstraint("id", "run_id", "key", name="uq_run_steps_id_run_key"),
        ForeignKeyConstraint(
            ["run_id", "plan_hash"],
            ["run_plans.run_id", "run_plans.plan_hash"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "run_id",
                "plan_hash",
                "selected_instance_id",
                "template_id",
                "configuration_revision",
            ],
            [
                "run_plan_selected_instances.run_id",
                "run_plan_selected_instances.plan_hash",
                "run_plan_selected_instances.instance_id",
                "run_plan_selected_instances.template_id",
                "run_plan_selected_instances.configuration_revision",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "ordinal >= 1 AND source_order >= 1 AND version >= 1",
            name="ck_run_steps_positive_numbers",
        ),
        CheckConstraint("configuration_revision >= 1", name="ck_run_steps_config_positive"),
        CheckConstraint(
            "binding_configuration_revision IS NULL OR binding_configuration_revision >= 1",
            name="ck_run_steps_binding_config_positive",
        ),
        CheckConstraint(f"state IN ({STEP_STATES_SQL})", name="ck_run_steps_state_supported"),
        CheckConstraint("effect IN ('read','write')", name="ck_run_steps_effect_supported"),
        CheckConstraint(
            "idempotency_support IN ('not_applicable','required','supported','unavailable')",
            name="ck_run_steps_idempotency_supported",
        ),
        CheckConstraint(
            "timeout_seconds IS NULL OR timeout_seconds BETWEEN 1 AND 120",
            name="ck_run_steps_timeout_bounded",
        ),
        CheckConstraint(
            "approval_expires_after_seconds IS NULL OR approval_expires_after_seconds >= 1",
            name="ck_run_steps_approval_expiry_positive",
        ),
        CheckConstraint(
            "(connector_family IN ('model','artifact') AND binding_id IS NULL AND "
            "binding_configuration_revision IS NULL AND timeout_seconds IS NULL) OR "
            "(connector_family NOT IN ('model','artifact') AND binding_id IS NOT NULL AND "
            "binding_configuration_revision = configuration_revision AND "
            "timeout_seconds IS NOT NULL)",
            name="ck_run_steps_binding_complete",
        ),
        CheckConstraint(
            "(effect = 'read' AND idempotency_support = 'not_applicable' AND "
            "approval_expires_after_seconds IS NULL AND "
            "approval_allow_self_approval IS NULL) OR "
            "(effect = 'write' AND connector_family NOT IN ('model','artifact') AND "
            "idempotency_support = 'required' AND "
            "approval_expires_after_seconds IS NOT NULL AND "
            "approval_allow_self_approval IS NOT NULL AND request_schema_id IS NOT NULL)",
            name="ck_run_steps_effect_policy_snapshot",
        ),
        CheckConstraint(
            "(state IN ('succeeded','failed','rejected','cancelled','skipped') AND "
            "terminal_reason_code IS NOT NULL) OR "
            "(state NOT IN ('succeeded','failed','rejected','cancelled','skipped') AND "
            "terminal_reason_code IS NULL)",
            name="ck_run_steps_terminal_reason",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    key: Mapped[str] = mapped_column(String(240), nullable=False)
    kind: Mapped[str] = mapped_column(String(120), nullable=False)
    selected_instance_id: Mapped[str] = mapped_column(String(240), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(240), nullable=False)
    effect: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    template_id: Mapped[str] = mapped_column(String(240), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_family: Mapped[str] = mapped_column(String(120), nullable=False)
    routing_slot_key: Mapped[str | None] = mapped_column(String(240), nullable=True)
    binding_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    binding_configuration_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_schema_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    request_redaction_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    idempotency_support: Mapped[str] = mapped_column(String(32), nullable=False)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approval_policy_id: Mapped[str] = mapped_column(String(240), nullable=False)
    approval_required_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    approval_required_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    approval_expires_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approval_allow_self_approval: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    terminal_result: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_reason_code: Mapped[str | None] = mapped_column(String(240), nullable=True)


class RunStepDependencyRecord(Base):
    __tablename__ = "run_step_dependencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["step_id", "run_id", "step_key"],
            ["run_steps.id", "run_steps.run_id", "run_steps.key"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "dependency_key"],
            ["run_steps.run_id", "run_steps.key"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("step_key <> dependency_key", name="ck_step_dependencies_not_self"),
    )

    step_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    dependency_key: Mapped[str] = mapped_column(String(240), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(240), nullable=False)
    step_key: Mapped[str] = mapped_column(String(240), nullable=False)


class RunStepStateTransitionRecord(Base):
    __tablename__ = "run_step_state_transitions"
    __table_args__ = (
        UniqueConstraint(
            "step_id",
            "run_id",
            "sequence",
            name="uq_step_transitions_step_run_sequence",
        ),
        ForeignKeyConstraint(
            ["step_id", "run_id"],
            ["run_steps.id", "run_steps.run_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("sequence >= 1", name="ck_step_transitions_sequence_positive"),
        CheckConstraint(
            "sequence = resulting_version AND resulting_version = expected_version + 1",
            name="ck_step_transitions_versions_contiguous",
        ),
        CheckConstraint(
            f"new_state IN ({STEP_STATES_SQL}) AND "
            f"(previous_state IS NULL OR previous_state IN ({STEP_STATES_SQL}))",
            name="ck_step_transitions_states_supported",
        ),
        CheckConstraint(
            f"command IN ({STEP_COMMANDS_SQL})",
            name="ck_step_transitions_command_supported",
        ),
        CheckConstraint(
            "(sequence = 1 AND command = 'initialize' AND previous_state IS NULL AND "
            "new_state = 'pending' AND expected_version = 0 AND resulting_version = 1) OR "
            "(sequence > 1 AND command <> 'initialize' AND previous_state IS NOT NULL AND "
            "previous_state <> new_state)",
            name="ck_step_transitions_initial_or_change",
        ),
    )

    step_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    command: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(240), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_version: Mapped[int] = mapped_column(Integer, nullable=False)


class RunPlanRecord(Base):
    __tablename__ = "run_plans"
    __table_args__ = (
        UniqueConstraint("run_id", "plan_hash", name="uq_run_plans_run_hash"),
        CheckConstraint("workflow_version >= 1", name="ck_run_plans_workflow_version"),
        CheckConstraint("step_count >= 1", name="ck_run_plans_step_count_positive"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), primary_key=True
    )
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(240), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workflow_definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_content_hash: Mapped[str] = mapped_column(String(96), nullable=False)
    graph_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    routing_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunPlanSelectedInstanceRecord(Base):
    __tablename__ = "run_plan_selected_instances"
    __table_args__ = (
        UniqueConstraint("run_id", "selection_order", name="uq_plan_instances_selection_order"),
        UniqueConstraint(
            "run_id", "plan_hash", "instance_id", name="uq_plan_instances_run_hash_id"
        ),
        UniqueConstraint(
            "run_id",
            "plan_hash",
            "instance_id",
            "template_id",
            name="uq_plan_instances_template",
        ),
        UniqueConstraint(
            "run_id",
            "plan_hash",
            "instance_id",
            "template_id",
            "configuration_revision",
            name="uq_plan_instances_exact_snapshot",
        ),
        ForeignKeyConstraint(
            ["run_id", "plan_hash"],
            ["run_plans.run_id", "run_plans.plan_hash"],
            ondelete="RESTRICT",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_id: Mapped[str] = mapped_column(String(240), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selection_order: Mapped[int] = mapped_column(Integer, nullable=False)
    target: Mapped[bool] = mapped_column(Boolean, nullable=False)


class RunPlanRoutingAssignmentRecord(Base):
    __tablename__ = "run_plan_routing_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "plan_hash"],
            ["run_plans.run_id", "run_plans.plan_hash"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "plan_hash", "instance_id", "template_id"],
            [
                "run_plan_selected_instances.run_id",
                "run_plan_selected_instances.plan_hash",
                "run_plan_selected_instances.instance_id",
                "run_plan_selected_instances.template_id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "assignment_order", name="uq_plan_assignments_order"),
    )

    run_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    slot_key: Mapped[str] = mapped_column(String(240), primary_key=True)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(240), nullable=False)
    template_id: Mapped[str] = mapped_column(String(240), nullable=False)
    required_capability_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    assignment_order: Mapped[int] = mapped_column(Integer, nullable=False)
