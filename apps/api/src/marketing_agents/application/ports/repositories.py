"""Narrow domain-typed repository ports with no persistence-framework types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalDecision,
    ApprovalRenewal,
    StoredActionApprovalRequest,
)
from marketing_agents.domain.audit import AuditEvent, AuditEventDraft
from marketing_agents.domain.entities import (
    ConnectorActionReceipt,
    ExternalAction,
    ExternalActionResultSnapshot,
    Run,
    RunPlanRoutingAssignment,
    RunPlanSelectedInstance,
    RunPlanSnapshot,
    RunStep,
    WorkItem,
)
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.run_lifecycle import RunStateTransition, RunTransitionResult
from marketing_agents.domain.step_lifecycle import StepStateTransition, StepTransitionResult


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

    async def fence(
        self,
        *,
        run_id: str,
        expected_version: int,
        expected_state: RunState,
    ) -> bool: ...

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


class ExternalActionRepositoryConflict(RuntimeError):
    """Stable action hydration/mutation conflict exposed through the application port."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


@dataclass(frozen=True, slots=True)
class ApprovalRequestSetInsertResult:
    requests: tuple[StoredActionApprovalRequest, ...]
    inserted: bool


@dataclass(frozen=True, slots=True)
class ApprovalDecisionInsertResult:
    request: StoredActionApprovalRequest
    inserted: bool


class ApprovalRepositoryConflict(RuntimeError):
    """Stable approval mutation conflict exposed through the application port."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalRepository(Protocol):
    async def get(self, request_id: str) -> StoredActionApprovalRequest | None: ...

    async def list_current_set(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> tuple[StoredActionApprovalRequest, ...]: ...

    async def add_initial_set_or_get(
        self,
        requests: tuple[ActionApprovalRequest, ...],
    ) -> ApprovalRequestSetInsertResult: ...

    async def record_decision(
        self,
        *,
        expected_version: int,
        expected_action_version: int,
        decision: ApprovalDecision,
    ) -> ApprovalDecisionInsertResult: ...

    async def mark_expired(
        self,
        *,
        request_id: str,
        expected_version: int,
        expected_action_version: int,
        expired_at: datetime,
    ) -> StoredActionApprovalRequest: ...

    async def renew_expired(
        self,
        *,
        expected_version: int,
        expected_action_version: int,
        renewal: ApprovalRenewal,
    ) -> StoredActionApprovalRequest: ...


@dataclass(frozen=True, slots=True)
class RunStepPlanInsertResult:
    plan: RunPlanSnapshot
    steps: tuple[RunStep, ...]
    inserted: bool


class RunStepRepository(Protocol):
    async def get(self, step_id: str) -> RunStep | None: ...

    async def get_plan(self, run_id: str) -> RunPlanSnapshot | None: ...

    async def add_plan(
        self,
        plan: RunPlanSnapshot,
        selected_instances: tuple[RunPlanSelectedInstance, ...],
        assignments: tuple[RunPlanRoutingAssignment, ...],
        steps: tuple[RunStep, ...],
        initial_transitions: tuple[StepStateTransition, ...],
    ) -> RunStepPlanInsertResult: ...

    async def list_for_run(self, run_id: str) -> tuple[RunStep, ...]: ...

    async def validate_plan_for_execution(self, run_id: str) -> tuple[RunStep, ...]: ...

    async def apply_transition(
        self,
        *,
        expected_run_version: int,
        expected_run_state: RunState,
        expected_version: int,
        expected_state: StepState,
        result: StepTransitionResult,
    ) -> bool: ...

    async def list_transitions(self, step_id: str) -> tuple[StepStateTransition, ...]: ...


class AuditRepository(Protocol):
    async def append(self, event: AuditEventDraft) -> AuditEvent: ...

    async def append_many(self, events: tuple[AuditEventDraft, ...]) -> tuple[AuditEvent, ...]: ...

    async def get(self, event_id: str) -> AuditEvent | None: ...

    async def get_attempt_event(self, run_id: str, attempt_id: str) -> AuditEvent | None: ...

    async def get_mutation_event(
        self,
        aggregate_type: str,
        aggregate_id: str,
        mutation_version: int,
    ) -> AuditEvent | None: ...

    async def list_run(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]: ...
