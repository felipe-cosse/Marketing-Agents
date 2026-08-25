"""Portable durable budgets, attempt reservations, and rate windows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
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


class RunExecutionControlRecord(Base):
    __tablename__ = "run_execution_controls"
    __table_args__ = (
        UniqueConstraint("run_id", "policy_hash", name="uq_execution_controls_run_policy"),
        ForeignKeyConstraint(
            ["run_id", "policy_hash"],
            ["run_plans.run_id", "run_plans.plan_hash"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(policy_hash) = 64", name="ck_execution_controls_policy_hash"),
        CheckConstraint(
            "run_timeout_seconds BETWEEN 1 AND 3600",
            name="ck_execution_controls_run_timeout",
        ),
        CheckConstraint(
            "max_model_calls BETWEEN 0 AND 100 AND model_calls >= 0 "
            "AND model_calls <= max_model_calls",
            name="ck_execution_controls_model_budget",
        ),
        CheckConstraint(
            "max_tool_calls BETWEEN 0 AND 1000 AND tool_calls >= 0 "
            "AND tool_calls <= max_tool_calls",
            name="ck_execution_controls_tool_budget",
        ),
        CheckConstraint("version >= 1", name="ck_execution_controls_version"),
        CheckConstraint(
            "(started_at IS NULL AND deadline_at IS NULL) OR "
            "(started_at IS NOT NULL AND deadline_at IS NOT NULL AND deadline_at > started_at)",
            name="ck_execution_controls_start_deadline",
        ),
        CheckConstraint(
            "(cancel_requested_at IS NULL AND cancel_actor_digest IS NULL) OR "
            "(cancel_requested_at IS NOT NULL AND cancel_actor_digest IS NOT NULL "
            "AND length(cancel_actor_digest) = 64)",
            name="ck_execution_controls_cancel_fence",
        ),
        CheckConstraint(
            "created_at <= updated_at",
            name="ck_execution_controls_update_time",
        ),
        CheckConstraint(
            "length(integrity_digest) = 64",
            name="ck_execution_controls_integrity_digest",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), primary_key=True
    )
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancel_actor_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionOperationPolicyRecord(Base):
    __tablename__ = "execution_operation_policies"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "step_id",
            "operation_key",
            "policy_hash",
            "kind",
            name="uq_execution_operations_exact_binding",
        ),
        ForeignKeyConstraint(
            ["run_id", "policy_hash"],
            ["run_execution_controls.run_id", "run_execution_controls.policy_hash"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["step_id", "run_id"],
            ["run_steps.id", "run_steps.run_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind IN ('model','tool')", name="ck_execution_operations_kind"),
        CheckConstraint(
            "retry_backoff IN ('none','bounded_exponential')",
            name="ck_execution_operations_backoff",
        ),
        CheckConstraint("max_attempts BETWEEN 1 AND 3", name="ck_execution_operations_attempts"),
        CheckConstraint(
            "step_timeout_seconds BETWEEN 1 AND 120",
            name="ck_execution_operations_timeout",
        ),
        CheckConstraint(
            "max_input_bytes BETWEEN 1 AND 1048576 AND "
            "max_input_field_bytes BETWEEN 1 AND 262144 AND "
            "max_input_field_bytes <= max_input_bytes AND "
            "max_output_bytes BETWEEN 1 AND 4194304 AND "
            "max_model_output_tokens BETWEEN 1 AND 32768",
            name="ck_execution_operations_payload_budgets",
        ),
        CheckConstraint(
            "configuration_revision >= 1 AND "
            "(binding_configuration_revision IS NULL OR "
            "binding_configuration_revision >= 1)",
            name="ck_execution_operations_revisions",
        ),
        CheckConstraint(
            "connector_timeout_seconds IS NULL OR connector_timeout_seconds BETWEEN 1 AND 120",
            name="ck_execution_operations_connector_timeout",
        ),
        CheckConstraint(
            "data_classification IN ('public','internal','personal','sensitive','secret')",
            name="ck_execution_operations_classification",
        ),
        CheckConstraint(
            "(connector_family = 'model' AND binding_id IS NULL AND "
            "binding_configuration_revision IS NULL AND request_schema_id IS NOT NULL AND "
            "result_schema_id IS NOT NULL AND connector_timeout_seconds IS NULL AND "
            "data_classification = 'internal') OR "
            "(connector_family <> 'model' AND binding_id IS NOT NULL AND "
            "binding_configuration_revision = configuration_revision AND "
            "request_schema_id IS NOT NULL AND result_schema_id IS NOT NULL AND "
            "connector_timeout_seconds IS NOT NULL)",
            name="ck_execution_operations_connector_contract",
        ),
        CheckConstraint("rate_limit_scope = 'template'", name="ck_execution_operations_rate_scope"),
        CheckConstraint(
            "rate_window_max_calls BETWEEN 1 AND 100",
            name="ck_execution_operations_rate_capacity",
        ),
        CheckConstraint(
            "rate_window_seconds BETWEEN 1 AND 3600",
            name="ck_execution_operations_rate_seconds",
        ),
        CheckConstraint(
            "length(policy_hash) = 64 AND length(integrity_digest) = 64 AND "
            "length(result_schema_hash) = 81 AND "
            "substr(result_schema_hash, 1, 17) = 'schema-sha256-v1:'",
            name="ck_execution_operations_digests",
        ),
    )

    step_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(240), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(240), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(240), nullable=False)
    selected_instance_id: Mapped[str] = mapped_column(String(240), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_family: Mapped[str] = mapped_column(String(120), nullable=False)
    binding_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    binding_configuration_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_schema_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    result_schema_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    result_schema_hash: Mapped[str] = mapped_column(String(81), nullable=False)
    request_redaction_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    result_redaction_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    data_classification: Mapped[str] = mapped_column(String(16), nullable=False)
    connector_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_backoff: Mapped[str] = mapped_column(String(32), nullable=False)
    step_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_input_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_input_field_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_model_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_limit_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    rate_limit_key: Mapped[str] = mapped_column(String(240), nullable=False)
    rate_window_max_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class RateLimitWindowRecord(Base):
    __tablename__ = "rate_limit_windows"
    __table_args__ = (
        CheckConstraint("scope = 'template'", name="ck_rate_windows_scope"),
        CheckConstraint("capacity BETWEEN 1 AND 100", name="ck_rate_windows_capacity"),
        CheckConstraint(
            "used >= 1 AND used <= capacity",
            name="ck_rate_windows_usage",
        ),
        CheckConstraint("version >= 1", name="ck_rate_windows_version"),
        CheckConstraint(
            "started_at < ends_at AND started_at <= updated_at AND updated_at < ends_at",
            name="ck_rate_windows_times",
        ),
        CheckConstraint(
            "length(integrity_digest) = 64",
            name="ck_rate_windows_integrity_digest",
        ),
    )

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(240), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionAttemptRecord(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        UniqueConstraint(
            "step_id",
            "operation_key",
            "attempt_number",
            name="uq_execution_attempts_operation_number",
        ),
        UniqueConstraint(
            "id",
            "run_id",
            "step_id",
            name="uq_execution_attempts_id_run_step",
        ),
        ForeignKeyConstraint(
            ["run_id", "step_id", "operation_key", "policy_hash", "kind"],
            [
                "execution_operation_policies.run_id",
                "execution_operation_policies.step_id",
                "execution_operation_policies.operation_key",
                "execution_operation_policies.policy_hash",
                "execution_operation_policies.kind",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["rate_limit_scope", "rate_limit_key", "rate_window_started_at"],
            ["rate_limit_windows.scope", "rate_limit_windows.key", "rate_limit_windows.started_at"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["output_artifact_id", "run_id", "step_id"],
            ["artifacts.id", "artifacts.run_id", "artifacts.step_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind IN ('model','tool')", name="ck_execution_attempts_kind"),
        CheckConstraint("attempt_number >= 1", name="ck_execution_attempts_number"),
        CheckConstraint(
            "source_control_version >= 1 AND source_step_version >= 1",
            name="ck_execution_attempts_source_versions",
        ),
        CheckConstraint("version IN (1,2)", name="ck_execution_attempts_version"),
        CheckConstraint(
            "eligible_at <= reserved_at AND reserved_at < call_deadline_at",
            name="ck_execution_attempts_call_window",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('succeeded','transient_failure','permanent_failure','cancelled')",
            name="ck_execution_attempts_outcome",
        ),
        CheckConstraint(
            "(outcome IS NULL AND completed_at IS NULL AND retry_not_before IS NULL "
            "AND terminal_reason_code IS NULL AND safe_error_code IS NULL "
            "AND output_artifact_id IS NULL AND version = 1) OR "
            "(outcome = 'succeeded' AND completed_at IS NOT NULL AND retry_not_before IS NULL "
            "AND terminal_reason_code IS NULL AND safe_error_code IS NULL "
            "AND output_artifact_id IS NOT NULL AND version = 2) OR "
            "(outcome = 'transient_failure' AND completed_at IS NOT NULL AND version = 2 "
            "AND output_artifact_id IS NULL "
            "AND ((retry_not_before IS NOT NULL AND terminal_reason_code IS NULL) OR "
            "(retry_not_before IS NULL AND terminal_reason_code IN "
            "('attempts_exhausted','retry_deadline_exceeded')))) OR "
            "(outcome = 'permanent_failure' AND completed_at IS NOT NULL "
            "AND retry_not_before IS NULL AND terminal_reason_code = 'permanent_failure' "
            "AND output_artifact_id IS NULL AND version = 2) OR "
            "(outcome = 'cancelled' AND completed_at IS NOT NULL AND retry_not_before IS NULL "
            "AND terminal_reason_code IN ('cancelled','run_cancelled') "
            "AND output_artifact_id IS NULL AND version = 2)",
            name="ck_execution_attempts_completion",
        ),
        CheckConstraint(
            "input_classification IN ('public','internal','personal','sensitive','secret')",
            name="ck_execution_attempts_input_classification",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= reserved_at",
            name="ck_execution_attempts_completion_time",
        ),
        CheckConstraint(
            "retry_not_before IS NULL OR retry_not_before >= completed_at",
            name="ck_execution_attempts_retry_time",
        ),
        CheckConstraint(
            "length(policy_hash) = 64 AND length(integrity_digest) = 64",
            name="ck_execution_attempts_digests",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(240), nullable=False)
    step_id: Mapped[str] = mapped_column(String(240), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(240), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_control_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_step_version: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    call_deadline_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    input_schema_id: Mapped[str] = mapped_column(String(240), nullable=False)
    redacted_input: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    input_classification: Mapped[str] = mapped_column(String(16), nullable=False)
    rate_limit_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    rate_limit_key: Mapped[str] = mapped_column(String(240), nullable=False)
    rate_window_started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    retry_not_before: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    terminal_reason_code: Mapped[str | None] = mapped_column(String(240), nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(String(240), nullable=True)
    output_artifact_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
