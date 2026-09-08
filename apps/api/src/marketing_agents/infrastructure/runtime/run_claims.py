"""Portable durable Run queue selection and fenced process leases."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError

from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.validation import require_id
from marketing_agents.infrastructure.db import DatabaseRuntime
from marketing_agents.infrastructure.db.models import RunRecord, RunWorkerClaimRecord
from marketing_agents.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork


class RuntimeStorage(Protocol):
    @property
    def database(self) -> DatabaseRuntime: ...
    @property
    def dependencies(self) -> OrchestrationDependencies: ...


ACTIVE_STATES = ("received", "validated", "planned", "executing", "awaiting_approval")


@dataclass(frozen=True, slots=True)
class RunClaim:
    run_id: str
    owner: str
    token: str
    expires_at: datetime


class RunClaims:
    def __init__(self, runtime: RuntimeStorage, *, lease_seconds: float = 90) -> None:
        if not 5 <= lease_seconds <= 600:
            raise ValueError("Run lease must be between 5 and 600 seconds")
        self.runtime = runtime
        self.duration = timedelta(seconds=lease_seconds)

    async def claim_once(self, owner: str) -> RunClaim | None:
        require_id(owner, "run worker ID")
        now = self.runtime.dependencies.utc_now()
        async with self.runtime.database.session_factory() as session:
            candidates = (
                (
                    await session.execute(
                        select(RunRecord.id)
                        .outerjoin(RunWorkerClaimRecord)
                        .where(
                            RunRecord.state.in_(ACTIVE_STATES),
                            or_(
                                RunWorkerClaimRecord.run_id.is_(None),
                                and_(
                                    RunWorkerClaimRecord.expires_at <= now,
                                    RunWorkerClaimRecord.available_at <= now,
                                ),
                            ),
                        )
                        .order_by(
                            case((RunRecord.state == "awaiting_approval", 1), else_=0),
                            RunWorkerClaimRecord.available_at.asc().nulls_first(),
                            RunRecord.updated_at,
                            RunRecord.id,
                        )
                        .limit(16)
                    )
                )
                .scalars()
                .all()
            )
        for run_id in candidates:
            token = uuid4().hex
            expires = now + self.duration
            async with self.runtime.database.session_factory() as session, session.begin():
                claim = await session.get(RunWorkerClaimRecord, run_id)
                if claim is None:
                    try:
                        async with session.begin_nested():
                            session.add(
                                RunWorkerClaimRecord(
                                    run_id=run_id,
                                    owner=owner,
                                    token=token,
                                    claimed_at=now,
                                    expires_at=expires,
                                    available_at=now,
                                    version=1,
                                )
                            )
                            await session.flush()
                    except IntegrityError:
                        continue
                else:
                    changed = await session.execute(
                        update(RunWorkerClaimRecord)
                        .where(
                            RunWorkerClaimRecord.run_id == run_id,
                            RunWorkerClaimRecord.version == claim.version,
                            RunWorkerClaimRecord.expires_at <= now,
                            RunWorkerClaimRecord.available_at <= now,
                        )
                        .returning(RunWorkerClaimRecord.run_id)
                        .values(
                            owner=owner,
                            token=token,
                            claimed_at=now,
                            expires_at=expires,
                            available_at=now,
                            version=claim.version + 1,
                        )
                    )
                    if changed.scalar_one_or_none() is None:
                        continue
            return RunClaim(run_id, owner, token, expires)
        return None

    async def renew(self, claim: RunClaim) -> bool:
        now = self.runtime.dependencies.utc_now()
        async with self.runtime.database.session_factory() as session, session.begin():
            result = await session.execute(
                update(RunWorkerClaimRecord)
                .where(
                    RunWorkerClaimRecord.run_id == claim.run_id,
                    RunWorkerClaimRecord.owner == claim.owner,
                    RunWorkerClaimRecord.token == claim.token,
                    RunWorkerClaimRecord.expires_at > now,
                )
                .returning(RunWorkerClaimRecord.run_id)
                .values(expires_at=now + self.duration, version=RunWorkerClaimRecord.version + 1)
            )
            return result.scalar_one_or_none() is not None

    @asynccontextmanager
    async def failure_unit_of_work(self, claim: RunClaim) -> AsyncIterator[UnitOfWork | None]:
        """Fence failure and its audit in the same transaction as claim ownership.

        A periodic heartbeat cannot protect a process resumed after its lease
        was replaced. This conditional UPDATE locks the claim until the caller
        commits or rolls back the application mutation, on SQLite and PostgreSQL.
        """
        async with self.runtime.dependencies.unit_of_work() as unit_of_work:
            if not isinstance(unit_of_work, SQLAlchemyUnitOfWork):
                raise TypeError("run failure fencing requires the SQL-backed unit of work")
            session = unit_of_work._require_session()
            result = await session.execute(
                update(RunWorkerClaimRecord)
                .where(
                    RunWorkerClaimRecord.run_id == claim.run_id,
                    RunWorkerClaimRecord.owner == claim.owner,
                    RunWorkerClaimRecord.token == claim.token,
                    RunWorkerClaimRecord.expires_at > self.runtime.dependencies.utc_now(),
                )
                .returning(RunWorkerClaimRecord.run_id)
                .values(version=RunWorkerClaimRecord.version + 1)
            )
            yield unit_of_work if result.scalar_one_or_none() is not None else None

    async def release(self, claim: RunClaim) -> bool:
        now = self.runtime.dependencies.utc_now()
        async with self.runtime.database.session_factory() as session, session.begin():
            result = await session.execute(
                update(RunWorkerClaimRecord)
                .where(
                    RunWorkerClaimRecord.run_id == claim.run_id,
                    RunWorkerClaimRecord.owner == claim.owner,
                    RunWorkerClaimRecord.token == claim.token,
                )
                .returning(RunWorkerClaimRecord.run_id)
                .values(
                    # Retain a valid lease interval while making the row reclaimable.
                    claimed_at=now - timedelta(microseconds=1),
                    expires_at=now,
                    available_at=now + timedelta(seconds=1),
                    version=RunWorkerClaimRecord.version + 1,
                )
            )
            return result.scalar_one_or_none() is not None
