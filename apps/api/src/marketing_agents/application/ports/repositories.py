"""Narrow domain-typed repository ports with no persistence-framework types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from marketing_agents.domain.entities import (
    AuditEvent,
    ConnectorActionReceipt,
    ExternalAction,
    ExternalActionResultSnapshot,
    Run,
    WorkItem,
)
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


@dataclass(frozen=True, slots=True)
class ExternalActionSetInsertResult:
    """Atomic all-created or authoritative all-replayed action set."""

    actions: tuple[ExternalAction, ...]
    inserted: bool


class ExternalActionRepository(Protocol):
    async def get(self, action_id: str) -> ExternalAction | None: ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> ExternalAction | None: ...

    async def list_plan_set(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> tuple[ExternalAction, ...]: ...

    async def add_proposed_set_or_get(
        self,
        actions: tuple[ExternalAction, ...],
    ) -> ExternalActionSetInsertResult: ...

    async def claim_reserved(
        self,
        *,
        action_id: str,
        expected_version: int,
        expected_run_version: int,
        lease_owner: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> ExternalAction | None: ...

    async def mark_call_started(
        self,
        *,
        action_id: str,
        expected_version: int,
        expected_run_version: int,
        lease_owner: str,
        attempt_number: int,
        started_at: datetime,
    ) -> ExternalAction | None: ...

    async def complete_succeeded(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        result: ExternalActionResultSnapshot,
    ) -> ExternalAction | None: ...

    async def complete_failed(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        reason_code: str,
        occurred_at: datetime,
    ) -> ExternalAction | None: ...

    async def mark_outcome_unknown(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        reason_code: str,
        occurred_at: datetime,
    ) -> ExternalAction | None: ...

    async def release_stale_for_retry(
        self,
        *,
        action_id: str,
        expected_version: int,
        attempt_number: int,
        occurred_at: datetime,
        conclusion: str,
    ) -> ExternalAction | None: ...

    async def fail_exhausted_stale_pre_call(
        self,
        *,
        action_id: str,
        expected_version: int,
        attempt_number: int,
        occurred_at: datetime,
        reason_code: str,
    ) -> ExternalAction | None: ...

    async def list_stale(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ExternalAction, ...]: ...


@dataclass(frozen=True, slots=True)
class ConnectorReceiptInsertResult:
    receipt: ConnectorActionReceipt
    inserted: bool


class ConnectorReceiptRepository(Protocol):
    async def get(
        self,
        connector_binding_id: str,
        idempotency_key: str,
    ) -> ConnectorActionReceipt | None: ...

    async def add_or_get(
        self,
        receipt: ConnectorActionReceipt,
    ) -> ConnectorReceiptInsertResult: ...


class AuditRepository(Protocol):
    async def append(self, event: AuditEvent) -> None: ...

    async def next_sequence(self, aggregate_type: str, aggregate_id: str) -> int: ...
