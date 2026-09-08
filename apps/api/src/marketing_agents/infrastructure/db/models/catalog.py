"""Relational operational catalog projection and immutable imported release snapshots."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class CatalogReleaseRecord(Base):
    __tablename__ = "catalog_releases"
    __table_args__ = (
        CheckConstraint("length(content_hash) = 82", name="ck_catalog_release_hash"),
        CheckConstraint("length(content_version) BETWEEN 1 AND 80", name="ck_catalog_version"),
        CheckConstraint("length(snapshot_json) > 2", name="ck_catalog_release_snapshot"),
    )

    content_hash: Mapped[str] = mapped_column(String(96), primary_key=True)
    content_version: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CatalogCurrentReleaseRecord(Base):
    __tablename__ = "catalog_current_release"
    __table_args__ = (CheckConstraint("singleton_id = 1", name="ck_catalog_current_singleton"),)

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_hash: Mapped[str] = mapped_column(
        String(96), ForeignKey("catalog_releases.content_hash", ondelete="RESTRICT"), nullable=False
    )


class DepartmentRecord(Base):
    __tablename__ = "departments"
    __table_args__ = (CheckConstraint("display_order >= 1", name="ck_departments_display_order"),)

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    release_hash: Mapped[str] = mapped_column(
        String(96), ForeignKey("catalog_releases.content_hash", ondelete="RESTRICT"), nullable=False
    )


class FunctionTeamRecord(Base):
    __tablename__ = "function_teams"
    __table_args__ = (
        UniqueConstraint("department_id", "display_order", name="uq_functions_sibling_order"),
        UniqueConstraint("id", "department_id", name="uq_function_department_identity"),
        CheckConstraint("display_order >= 1", name="ck_functions_display_order"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    department_id: Mapped[str] = mapped_column(
        String(240), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    release_hash: Mapped[str] = mapped_column(
        String(96), ForeignKey("catalog_releases.content_hash", ondelete="RESTRICT"), nullable=False
    )


class ToolCapabilityRecord(Base):
    __tablename__ = "tool_capabilities"
    __table_args__ = (
        CheckConstraint("effect IN ('read', 'write')", name="ck_capability_effect"),
        CheckConstraint("default_timeout_seconds BETWEEN 1 AND 120", name="ck_capability_timeout"),
        CheckConstraint(
            "idempotency_support IN ('not_applicable', 'required', 'supported', 'unavailable')",
            name="ck_capability_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    connector_family: Mapped[str] = mapped_column(String(100), nullable=False)
    effect: Mapped[str] = mapped_column(String(10), nullable=False)
    idempotency_support: Mapped[str] = mapped_column(String(20), nullable=False)
    default_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    release_hash: Mapped[str] = mapped_column(
        String(96), ForeignKey("catalog_releases.content_hash", ondelete="RESTRICT"), nullable=False
    )


class ApprovalPolicyRecord(Base):
    __tablename__ = "approval_policies"
    __table_args__ = (
        CheckConstraint("kind IN ('none', 'human_external_write')", name="ck_catalog_policy_kind"),
        CheckConstraint("expiry_seconds BETWEEN 60 AND 86400", name="ck_catalog_policy_expiry"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    expiry_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    release_hash: Mapped[str] = mapped_column(
        String(96), ForeignKey("catalog_releases.content_hash", ondelete="RESTRICT"), nullable=False
    )


class AgentTemplateRecord(Base):
    __tablename__ = "agent_templates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["function_id", "department_id"],
            ["function_teams.id", "function_teams.department_id"],
            ondelete="RESTRICT",
            name="fk_template_function_department",
        ),
        UniqueConstraint("function_id", "display_order", name="uq_templates_sibling_order"),
        CheckConstraint("display_order >= 1", name="ck_templates_display_order"),
        CheckConstraint("length(instruction_hash) = 64", name="ck_template_instruction_hash"),
        CheckConstraint("length(input_schema_hash) = 64", name="ck_template_input_hash"),
        CheckConstraint("length(output_schema_hash) = 64", name="ck_template_output_hash"),
        CheckConstraint("length(policy_hash) = 64", name="ck_template_policy_hash"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    department_id: Mapped[str] = mapped_column(
        String(240), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    function_id: Mapped[str] = mapped_column(String(240), nullable=False)
    approval_policy_id: Mapped[str] = mapped_column(
        String(240), ForeignKey("approval_policies.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_json: Mapped[str] = mapped_column(Text, nullable=False)
    instruction_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    release_hash: Mapped[str] = mapped_column(
        String(96), ForeignKey("catalog_releases.content_hash", ondelete="RESTRICT"), nullable=False
    )


class AgentTemplateCapabilityRecord(Base):
    __tablename__ = "agent_template_capabilities"

    template_id: Mapped[str] = mapped_column(
        String(240), ForeignKey("agent_templates.id", ondelete="RESTRICT"), primary_key=True
    )
    capability_id: Mapped[str] = mapped_column(
        String(240), ForeignKey("tool_capabilities.id", ondelete="RESTRICT"), primary_key=True
    )


class AgentTemplateTriggerKindRecord(Base):
    __tablename__ = "agent_template_trigger_kinds"
    __table_args__ = (
        CheckConstraint(
            "trigger_kind IN ('manual', 'webhook', 'schedule')", name="ck_catalog_kind"
        ),
    )

    template_id: Mapped[str] = mapped_column(
        String(240), ForeignKey("agent_templates.id", ondelete="RESTRICT"), primary_key=True
    )
    trigger_kind: Mapped[str] = mapped_column(String(12), primary_key=True)


class AgentInstanceRecord(Base):
    __tablename__ = "agent_instances"
    __table_args__ = (
        UniqueConstraint("template_id", "display_order", name="uq_instances_sibling_order"),
        UniqueConstraint("template_id", "source_ordinal", name="uq_instances_source_ordinal"),
        CheckConstraint("display_order >= 1", name="ck_instances_display_order"),
        CheckConstraint("source_ordinal BETWEEN 1 AND 99", name="ck_instances_ordinal"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(240), ForeignKey("agent_templates.id", ondelete="RESTRICT"), nullable=False
    )
    source_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    release_hash: Mapped[str] = mapped_column(
        String(96), ForeignKey("catalog_releases.content_hash", ondelete="RESTRICT"), nullable=False
    )
