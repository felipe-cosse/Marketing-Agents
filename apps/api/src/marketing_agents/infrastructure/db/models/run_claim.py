"""Fenced process leases for queued Run advancement."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.types import UTCDateTime


class RunWorkerClaimRecord(Base):
    __tablename__ = "run_worker_claims"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_run_worker_claim_version"),
        CheckConstraint("expires_at > claimed_at", name="ck_run_worker_claim_times"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), primary_key=True
    )
    owner: Mapped[str] = mapped_column(String(240))
    token: Mapped[str] = mapped_column(String(64))
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    available_at: Mapped[datetime] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(Integer)
