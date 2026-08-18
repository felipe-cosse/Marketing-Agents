"""Narrow domain-typed repository ports with no persistence-framework types."""

from __future__ import annotations

from typing import Protocol

from marketing_agents.domain.entities import AuditEvent, Run, WorkItem


class WorkRepository(Protocol):
    async def get(self, work_item_id: str) -> WorkItem | None: ...

    async def get_by_source_key(
        self, source: str, event_id: str, instance_id: str
    ) -> WorkItem | None: ...

    async def add(self, work_item: WorkItem) -> None: ...


class RunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...

    async def add(self, run: Run) -> None: ...

    async def replace(self, expected_version: int, run: Run) -> bool: ...


class AuditRepository(Protocol):
    async def append(self, event: AuditEvent) -> None: ...

    async def next_sequence(self, aggregate_type: str, aggregate_id: str) -> int: ...
