"""Short read transactions over the authoritative schedule repository, never a copy."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from marketing_agents.domain.entities import Schedule, ScheduleOccurrence
from marketing_agents.infrastructure.db.repositories.schedule import SQLAlchemyScheduleRepository


class SQLAlchemyScheduleControlReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, schedule_id: str) -> Schedule | None:
        async with self._sessions() as session:
            return await SQLAlchemyScheduleRepository(session).get(schedule_id)

    async def list_schedules(self, *, after_id: str | None, limit: int) -> tuple[Schedule, ...]:
        async with self._sessions() as session:
            return await SQLAlchemyScheduleRepository(session).list_configuration_owned(
                after_id=after_id, limit=limit
            )

    async def list_occurrences(
        self,
        schedule_id: str,
        *,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[ScheduleOccurrence, ...]:
        async with self._sessions() as session:
            return await SQLAlchemyScheduleRepository(session).list_occurrences(
                schedule_id, after=after, limit=limit
            )
