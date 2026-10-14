"""Read-only access to the scheduler's existing durable, integrity-checked facts."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from marketing_agents.domain.entities import Schedule, ScheduleOccurrence


class ScheduleControlReader(Protocol):
    async def get(self, schedule_id: str) -> Schedule | None: ...

    async def list_schedules(self, *, after_id: str | None, limit: int) -> tuple[Schedule, ...]: ...

    async def list_occurrences(
        self,
        schedule_id: str,
        *,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[ScheduleOccurrence, ...]: ...
