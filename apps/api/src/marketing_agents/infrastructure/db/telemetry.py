"""Emit safe operational observations only after their outer audit transaction commits."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from marketing_agents.domain.audit import AuditEvent, AuditEventDraft
from marketing_agents.domain.enums import RunState
from marketing_agents.infrastructure.db.models.approval import ApprovalRequestRecord
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.infrastructure.db.models.work import WorkItemRecord
from marketing_agents.infrastructure.db.repositories.audit import SQLAlchemyAuditRepository
from marketing_agents.observability import LocalTelemetry
from marketing_agents.observability.logging import (
    OperationEvent,
    OperationOutcome,
    safe_correlation_id,
)
from marketing_agents.observability.telemetry import CommittedObservation

MAX_PENDING_OBSERVATIONS = 1_024
_RUN_STATES = frozenset(item.value for item in RunState)


class TelemetryAuditRepository(SQLAlchemyAuditRepository):
    """Preserves all base validation and transaction semantics.

    The buffer contains only allowlisted projected facts, never event drafts,
    metadata, actions, bodies, error messages, or credentials. Savepoint and
    outer rollbacks discard affected observations. This is diagnostic delivery,
    not an atomic external side effect or a replacement for the audit ledger.
    """

    def __init__(self, session: AsyncSession, telemetry: LocalTelemetry) -> None:
        super().__init__(session)
        self._telemetry = telemetry
        self._pending: list[tuple[SessionTransaction | None, CommittedObservation]] = []
        sqlalchemy_event.listen(session.sync_session, "after_commit", self._committed)
        sqlalchemy_event.listen(session.sync_session, "after_soft_rollback", self._rolled_back)

    def _committed(self, session: Session) -> None:
        if session.in_nested_transaction():
            return
        pending, self._pending = self._pending, []
        for _, observation in pending:
            self._telemetry.record(observation)

    def _rolled_back(self, session: Session, previous_transaction: SessionTransaction) -> None:
        del session
        retained = []
        for transaction, observation in self._pending:
            parent = transaction
            while parent is not None and parent is not previous_transaction:
                parent = parent.parent
            if parent is None:
                retained.append((transaction, observation))
        self._pending = retained

    async def append_many(self, events: tuple[AuditEventDraft, ...]) -> tuple[AuditEvent, ...]:
        try:
            result = await super().append_many(events)
        except Exception:
            self._telemetry.audit_failure()
            raise
        await self._stage(events)
        return result

    async def append_global_many(
        self, events: tuple[AuditEventDraft, ...]
    ) -> tuple[AuditEvent, ...]:
        try:
            result = await super().append_global_many(events)
        except Exception:
            self._telemetry.audit_failure()
            raise
        await self._stage(events)
        return result

    async def _stage(self, events: tuple[AuditEventDraft, ...]) -> None:
        try:
            for event in events:
                observation = await self._project(event)
                if observation is None:
                    continue
                if len(self._pending) >= MAX_PENDING_OBSERVATIONS:
                    self._telemetry.metrics.observe("dropped_series")
                    continue
                session = self._session.sync_session
                transaction = session.get_nested_transaction() or session.get_transaction()
                self._pending.append((transaction, observation))
        except Exception:
            # Diagnostics cannot change the business/audit transaction outcome.
            # The data needed here has already passed base audit validation.
            self._telemetry.diagnostic_failure()

    async def _project(self, event: AuditEventDraft) -> CommittedObservation | None:
        name = event.event_type
        observation: CommittedObservation | None = None
        if name in {"run.received", "run.transitioned"} and event.new_state in _RUN_STATES:
            row = (
                await self._session.execute(
                    select(WorkItemRecord.workflow_id, WorkItemRecord.agent_instance_id)
                    .join(RunRecord, RunRecord.work_item_id == WorkItemRecord.id)
                    .where(RunRecord.id == event.run_id)
                )
            ).one_or_none()
            workflow = self._telemetry.metrics.catalog_label(
                "workflow", None if row is None else row.workflow_id
            )
            template = self._telemetry.template_for(None if row is None else row.agent_instance_id)
            observation = CommittedObservation(
                OperationEvent.RUN,
                OperationOutcome.ACCEPTED,
                "run_transitions",
                (("state", str(event.new_state)), ("workflow", workflow), ("template", template)),
            )
        elif name in {"approval.approved", "approval.rejected", "approval.expired"}:
            requested_at = (
                await self._session.execute(
                    select(ApprovalRequestRecord.requested_at).where(
                        ApprovalRequestRecord.id == event.approval_request_id
                    )
                )
            ).scalar_one_or_none()
            age = (
                None
                if requested_at is None
                else max(0, (event.occurred_at - requested_at).total_seconds())
            )
            observation = CommittedObservation(
                OperationEvent.APPROVAL,
                OperationOutcome.ACCEPTED,
                "approval_decisions",
                (("decision", name.removeprefix("approval.")),),
                approval_age_seconds=age,
            )
        elif name in {"action.succeeded", "action.failed", "action.outcome_unknown"}:
            result = {
                "action.succeeded": "succeeded",
                "action.failed": "failed",
                "action.outcome_unknown": "unknown",
            }[name]
            observation = CommittedObservation(
                OperationEvent.CONNECTOR,
                {
                    "succeeded": OperationOutcome.ACCEPTED,
                    "failed": OperationOutcome.FAILED,
                    "unknown": OperationOutcome.UNKNOWN,
                }[result],
                "connector_results",
                (("result", result),),
            )
        elif name == "action.retry_released" or (
            name == "attempt.reserved"
            and event.safe_metadata.values.get("attempt_kind") == "tool"
            and type(event.safe_metadata.values.get("attempt_number")) is int
            and event.safe_metadata.values["attempt_number"] > 1
        ):
            observation = CommittedObservation(
                OperationEvent.RETRY, OperationOutcome.ACCEPTED, "connector_retries"
            )
        elif (
            name == "attempt.completed" and event.safe_metadata.values.get("attempt_kind") == "tool"
        ):
            result = {
                "succeeded": "succeeded",
                "transient_failure": "retry",
                "permanent_failure": "failed",
                "cancelled": "unknown",
            }.get(str(event.safe_metadata.values.get("attempt_outcome")), "unknown")
            observation = CommittedObservation(
                OperationEvent.CONNECTOR,
                OperationOutcome.ACCEPTED if result == "succeeded" else OperationOutcome.FAILED,
                "connector_results",
                (("result", result),),
            )
        elif name in {
            "work.duplicate_returned",
            "webhook.duplicate_suppressed",
            "work.idempotency_collision",
            "webhook.idempotency_collision",
        }:
            conflict = name.endswith("idempotency_collision")
            observation = CommittedObservation(
                OperationEvent.IDEMPOTENCY,
                OperationOutcome.REJECTED if conflict else OperationOutcome.ACCEPTED,
                "idempotency",
                (("kind", "conflict" if conflict else "duplicate"),),
            )
        elif name in {"schedule.misfire_skipped", "schedule.misfire_run_once"}:
            observation = CommittedObservation(
                OperationEvent.SCHEDULER_MISFIRE,
                OperationOutcome.ACCEPTED,
                "scheduler_misfires",
                (("disposition", name.removeprefix("schedule.misfire_")),),
            )
        if observation is None:
            return None
        return replace(
            observation,
            correlation_id=safe_correlation_id(event.correlation_id),
            run_id=safe_correlation_id(event.run_id),
            step_id=safe_correlation_id(event.step_id),
        )
