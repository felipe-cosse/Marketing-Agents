"""Narrow domain-typed repository ports with no persistence-framework types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from marketing_agents.domain.entities import AuditEvent, Run, WorkItem
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.run_lifecycle import RunStateTransition, RunTransitionResult


@dataclass(frozen=True, slots=True)
class WorkInsertResult:
    """Outcome of one atomic source-key insert-or-read operation."""

    work_item: WorkItem
    inserted: bool


class WorkRepository(Protocol):
    async def get(self, work_item_id: str) -> WorkItem | None: ...

    async def get_by_source_key(
        self, source: str, event_id: str, instance_id: str
    ) -> WorkItem | None: ...

    async def add(self, work_item: WorkItem) -> None: ...

    async def add_or_get(self, work_item: WorkItem) -> WorkInsertResult: ...


@dataclass(frozen=True, slots=True)
class RunInsertResult:
    """Outcome of atomic primary-Run receipt for one admitted WorkItem."""

    run: Run
    inserted: bool


class RunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...

    async def get_by_work_item_id(self, work_item_id: str) -> Run | None: ...

    async def add_received_or_get(
        self,
        run: Run,
        initial_transition: RunStateTransition,
    ) -> RunInsertResult: ...

    async def apply_transition(
        self,
        *,
        expected_version: int,
        expected_state: RunState,
        result: RunTransitionResult,
    ) -> bool: ...

    async def list_transitions(self, run_id: str) -> tuple[RunStateTransition, ...]: ...


class AuditRepository(Protocol):
    async def append(self, event: AuditEvent) -> None: ...

    async def next_sequence(self, aggregate_type: str, aggregate_id: str) -> int: ...
