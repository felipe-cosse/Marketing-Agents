"""Immutable revision snapshots for reusable campaign briefs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class CampaignBriefRecord(Base):
    """Corrections use a new revision; existing work retains its admitted snapshot."""

    __tablename__ = "campaign_briefs"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_campaign_brief_revision_positive"),
        CheckConstraint("length(title) BETWEEN 1 AND 200", name="ck_campaign_brief_title"),
        CheckConstraint("length(objective) BETWEEN 1 AND 2000", name="ck_campaign_brief_objective"),
        CheckConstraint(
            "length(snapshot_json) BETWEEN 2 AND 1048576", name="ck_campaign_brief_snapshot"
        ),
        CheckConstraint("length(snapshot_hash) = 64", name="ck_campaign_brief_snapshot_hash"),
    )

    id: Mapped[str] = mapped_column(String(240), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(Text)
    snapshot_json: Mapped[str] = mapped_column(Text)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
