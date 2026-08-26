"""Portable mutable deployment configuration for one catalog instance."""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base


class AgentInstanceConfigurationRecord(Base):
    """One integrity-sealed mutable configuration row per catalog instance."""

    __tablename__ = "agent_instance_configs"
    __table_args__ = (
        CheckConstraint(
            "length(instance_id) BETWEEN 1 AND 240 AND instance_id = trim(instance_id)",
            name="ck_agent_instance_configs_instance_id_bounded",
        ),
        CheckConstraint(
            "variant_label IS NULL OR "
            "(length(variant_label) BETWEEN 1 AND 100 AND variant_label = trim(variant_label))",
            name="ck_agent_instance_configs_variant_label_bounded",
        ),
        CheckConstraint(
            "length(trigger_bindings_json) BETWEEN 2 AND 65536 "
            "AND trigger_bindings_json = trim(trigger_bindings_json)",
            name="ck_agent_instance_configs_triggers_bounded",
        ),
        CheckConstraint(
            "length(connector_bindings_json) BETWEEN 2 AND 65536 "
            "AND connector_bindings_json = trim(connector_bindings_json)",
            name="ck_agent_instance_configs_connectors_bounded",
        ),
        CheckConstraint(
            "length(schedule_json) BETWEEN 2 AND 65536 AND schedule_json = trim(schedule_json)",
            name="ck_agent_instance_configs_schedule_bounded",
        ),
        CheckConstraint("version >= 1", name="ck_agent_instance_configs_version_positive"),
        CheckConstraint(
            "length(integrity_digest) = 64",
            name="ck_agent_instance_configs_integrity_digest_length",
        ),
    )

    instance_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean(create_constraint=True, name="bool_agent_instance_configs_enabled"),
        nullable=False,
    )
    variant_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trigger_bindings_json: Mapped[str] = mapped_column(Text, nullable=False)
    connector_bindings_json: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
