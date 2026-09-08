"""Normalized deployment projections and local database/key identity."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class TriggerDefinitionRecord(Base):
    """Transactionally maintained projection of a versioned instance configuration."""

    __tablename__ = "trigger_definitions"
    __table_args__ = (
        UniqueConstraint("instance_id", "kind", name="uq_trigger_definitions_instance_kind"),
        CheckConstraint("kind IN ('manual', 'webhook', 'schedule')", name="ck_trigger_kind"),
        CheckConstraint("version >= 1", name="ck_trigger_version_positive"),
        CheckConstraint(
            "length(configuration_json) BETWEEN 2 AND 65536",
            name="ck_trigger_configuration_bounded",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        String(240), ForeignKey("agent_instance_configs.instance_id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(
        Boolean(create_constraint=True, name="bool_trigger_definitions_enabled")
    )
    configuration_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)


class LocalRuntimeIdentityRecord(Base):
    """Non-secret fingerprint binding one local installation to its digest key."""

    __tablename__ = "local_runtime_identity"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_local_runtime_identity_singleton"),
        CheckConstraint("format_version = 1", name="ck_local_runtime_identity_format"),
        CheckConstraint(
            "length(key_fingerprint) = 90",
            name="ck_local_runtime_identity_fingerprint_length",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    format_version: Mapped[int] = mapped_column(Integer)
    key_fingerprint: Mapped[str] = mapped_column(String(90))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
