"""Immutable schema-bound artifact and parent-lineage records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class ArtifactRecord(Base):
    """One immutable structured output and its complete provenance snapshot."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("id", "run_id", "step_id", name="uq_artifacts_id_run_step"),
        ForeignKeyConstraint(
            ["step_id", "run_id"],
            ["run_steps.id", "run_steps.run_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "configuration_revision >= 1",
            name="ck_artifacts_configuration_revision_positive",
        ),
        CheckConstraint(
            "classification IN ('public','internal','personal','sensitive','secret')",
            name="ck_artifacts_classification_supported",
        ),
        CheckConstraint(
            "length(payload_hash) = 64 AND length(envelope_fingerprint) = 64",
            name="ck_artifacts_digest_lengths",
        ),
        CheckConstraint(
            "length(output_schema_hash) = 81 AND "
            "substr(output_schema_hash, 1, 17) = 'schema-sha256-v1:'",
            name="ck_artifacts_output_schema_hash",
        ),
        Index("ix_artifacts_run_created_id", "run_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    step_id: Mapped[str] = mapped_column(String(240), nullable=False)
    output_schema_id: Mapped[str] = mapped_column(String(240), nullable=False)
    output_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    output_schema_hash: Mapped[str] = mapped_column(String(81), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    envelope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ArtifactParentRecord(Base):
    """Ordered same-Run lineage edge between two immutable artifacts."""

    __tablename__ = "artifact_parent_edges"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "ordinal",
            name="uq_artifact_parent_edges_artifact_ordinal",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "run_id", "artifact_step_id"],
            ["artifacts.id", "artifacts.run_id", "artifacts.step_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["parent_artifact_id", "run_id", "parent_step_id"],
            ["artifacts.id", "artifacts.run_id", "artifacts.step_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 1", name="ck_artifact_parent_edges_ordinal_positive"),
        CheckConstraint(
            "artifact_id <> parent_artifact_id",
            name="ck_artifact_parent_edges_not_self",
        ),
    )

    artifact_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    parent_artifact_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(240), nullable=False)
    artifact_step_id: Mapped[str] = mapped_column(String(240), nullable=False)
    parent_step_id: Mapped[str] = mapped_column(String(240), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
