"""SQLAlchemy atomic timeline allocation and append-only audit repository."""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.domain.audit import (
    AuditActorSource,
    AuditEvent,
    AuditEventDraft,
    AuditOutcome,
    _issue_audit_event_draft,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.infrastructure.db.models.audit import AuditEventRecord
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.security.audit_metadata import hydrate_audit_metadata


class AuditPersistenceInvariantError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _record_to_domain_unchecked(record: AuditEventRecord) -> AuditEvent:
    metadata = hydrate_audit_metadata(
        record.event_type,
        record.safe_metadata,
        classification=DataClassification(record.metadata_classification),
        occurred_at=record.occurred_at,
        expires_at=record.metadata_expires_at,
    )
    if metadata.issuance_fingerprint != record.metadata_fingerprint:
        raise AuditPersistenceInvariantError(
            "audit_metadata_corrupt", "persisted audit metadata fingerprint does not match"
        )
    transition_sequence: int | None
    if record.aggregate_type == "run":
        transition_sequence = record.run_transition_sequence
    elif record.aggregate_type == "step":
        transition_sequence = record.step_transition_sequence
    else:
        transition_sequence = None
    draft = _issue_audit_event_draft(
        id=record.id,
        run_id=record.run_id,
        event_type=record.event_type,
        aggregate_type=record.aggregate_type,
        aggregate_id=record.aggregate_id,
        outcome=AuditOutcome(record.outcome),
        actor_id=record.actor_id,
        actor_source=AuditActorSource(record.actor_source),
        auth_method=record.auth_method,
        correlation_id=record.correlation_id,
        safe_metadata=metadata,
        occurred_at=record.occurred_at,
        step_id=record.step_id,
        action_id=record.action_id,
        action_attempt_number=record.action_attempt_number,
        receipt_id=record.receipt_id,
        approval_request_id=record.approval_request_id,
        approval_decision_id=record.approval_decision_id,
        artifact_id=record.artifact_id,
        attempt_id=record.attempt_id,
        attempted_command=record.attempted_command,
        expected_version=record.expected_version,
        observed_version=record.observed_version,
        observed_state=record.observed_state,
        requested_state=record.requested_state,
        mutation_version=record.mutation_version,
        transition_sequence=transition_sequence,
        previous_state=record.previous_state,
        new_state=record.new_state,
        reason_code=record.reason_code,
    )
    if draft.schema_version != record.schema_version:
        raise AuditPersistenceInvariantError(
            "audit_schema_corrupt", "persisted audit schema version does not match"
        )
    if draft.issuance_fingerprint != record.event_fingerprint:
        raise AuditPersistenceInvariantError(
            "audit_event_corrupt", "persisted audit event fingerprint does not match"
        )
    return AuditEvent(
        draft=draft,
        global_sequence=record.global_sequence,
        run_sequence=record.run_sequence,
    )


def _record_to_domain(record: AuditEventRecord) -> AuditEvent:
    try:
        return _record_to_domain_unchecked(record)
    except AuditPersistenceInvariantError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditPersistenceInvariantError(
            "audit_record_corrupt",
            "persisted audit event violates its typed semantic contract",
        ) from exc


def _draft_to_record(draft: AuditEventDraft, run_sequence: int) -> AuditEventRecord:
    return AuditEventRecord(
        id=draft.id,
        schema_version=draft.schema_version,
        run_id=draft.run_id,
        run_sequence=run_sequence,
        event_type=draft.event_type,
        aggregate_type=draft.aggregate_type,
        aggregate_id=draft.aggregate_id,
        outcome=draft.outcome.value,
        actor_id=draft.actor_id,
        actor_source=draft.actor_source.value,
        auth_method=draft.auth_method,
        correlation_id=draft.correlation_id,
        safe_metadata=cast(dict[str, Any], _plain_json(draft.safe_metadata.values)),
        metadata_classification=draft.safe_metadata.classification.value,
        metadata_expires_at=draft.safe_metadata.expires_at,
        metadata_fingerprint=draft.safe_metadata.issuance_fingerprint,
        event_fingerprint=draft.issuance_fingerprint,
        occurred_at=draft.occurred_at,
        step_id=draft.step_id,
        action_id=draft.action_id,
        action_attempt_number=draft.action_attempt_number,
        receipt_id=draft.receipt_id,
        approval_request_id=draft.approval_request_id,
        approval_decision_id=draft.approval_decision_id,
        artifact_id=draft.artifact_id,
        attempt_id=draft.attempt_id,
        attempted_command=draft.attempted_command,
        expected_version=draft.expected_version,
        observed_version=draft.observed_version,
        observed_state=draft.observed_state,
        requested_state=draft.requested_state,
        mutation_version=draft.mutation_version,
        run_transition_sequence=(
            draft.transition_sequence if draft.aggregate_type == "run" else None
        ),
        step_transition_sequence=(
            draft.transition_sequence if draft.aggregate_type == "step" else None
        ),
        previous_state=draft.previous_state,
        new_state=draft.new_state,
        reason_code=draft.reason_code,
    )


def _plain_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


class SQLAlchemyAuditRepository:
    """Allocate run order and insert sealed events in one repository operation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEventDraft) -> AuditEvent:
        return (await self.append_many((event,)))[0]

    async def append_many(self, events: tuple[AuditEventDraft, ...]) -> tuple[AuditEvent, ...]:
        if type(events) is not tuple or not events or len(events) > 128:
            raise ValueError("audit append batch must contain from 1 through 128 events")
        if any(type(event) is not AuditEventDraft for event in events):
            raise ValueError("audit append requires exact sealed event drafts")
        run_id = events[0].run_id
        if any(event.run_id != run_id for event in events):
            raise ValueError("one audit append batch may target only one run timeline")
        if len({event.id for event in events}) != len(events):
            raise ValueError("audit append batch event IDs must be unique")
        for event in events:
            event.verify_integrity()
        allocation = (
            update(RunRecord)
            .where(RunRecord.id == run_id)
            .values(next_timeline_sequence=RunRecord.next_timeline_sequence + len(events))
            .returning(RunRecord.next_timeline_sequence)
            .execution_options(synchronize_session=False)
        )
        try:
            end_sequence = (await self._session.execute(allocation)).scalar_one_or_none()
        except OperationalError as exc:
            sqlite_code = getattr(exc.orig, "sqlite_errorcode", None)
            if self._session.get_bind().dialect.name == "sqlite" and sqlite_code in {
                sqlite3.SQLITE_BUSY,
                getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517),
            }:
                raise AuditPersistenceInvariantError(
                    "audit_sequence_busy",
                    "audit timeline sequence allocation lost a concurrent SQLite snapshot",
                ) from None
            raise
        if end_sequence is None:
            raise AuditPersistenceInvariantError(
                "audit_run_missing", "audit timeline run does not exist"
            )
        first_sequence = end_sequence - len(events) + 1
        preexisting_count = first_sequence - 1
        count_statement = select(
            func.count(AuditEventRecord.global_sequence),
            func.min(AuditEventRecord.run_sequence),
            func.max(AuditEventRecord.run_sequence),
        ).where(AuditEventRecord.run_id == run_id)
        event_count, minimum_sequence, maximum_sequence = (
            await self._session.execute(count_statement)
        ).one()
        if (
            event_count != preexisting_count
            or (
                preexisting_count == 0
                and (minimum_sequence is not None or maximum_sequence is not None)
            )
            or (
                preexisting_count > 0
                and (minimum_sequence != 1 or maximum_sequence != preexisting_count)
            )
            or (
                preexisting_count == 0
                and (events[0].event_type != "run.received" or events[0].mutation_version != 1)
            )
        ):
            raise AuditPersistenceInvariantError(
                "audit_timeline_not_contiguous",
                "audit append cannot extend a missing or corrupt per-run timeline",
            )
        records = tuple(
            _draft_to_record(event, first_sequence + offset) for offset, event in enumerate(events)
        )
        self._session.add_all(records)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise AuditPersistenceInvariantError(
                "audit_append_conflict",
                "audit event identity or mutation witness already exists",
            ) from exc
        return tuple(_record_to_domain(record) for record in records)

    async def get_mutation_event(
        self,
        aggregate_type: str,
        aggregate_id: str,
        mutation_version: int,
    ) -> AuditEvent | None:
        statement = select(AuditEventRecord).where(
            AuditEventRecord.aggregate_type == aggregate_type,
            AuditEventRecord.aggregate_id == aggregate_id,
            AuditEventRecord.mutation_version == mutation_version,
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else _record_to_domain(record)

    async def get(self, event_id: str) -> AuditEvent | None:
        record = (
            await self._session.execute(
                select(AuditEventRecord).where(AuditEventRecord.id == event_id)
            )
        ).scalar_one_or_none()
        return None if record is None else _record_to_domain(record)

    async def get_attempt_event(self, run_id: str, attempt_id: str) -> AuditEvent | None:
        record = (
            await self._session.execute(
                select(AuditEventRecord).where(
                    AuditEventRecord.run_id == run_id,
                    AuditEventRecord.attempt_id == attempt_id,
                )
            )
        ).scalar_one_or_none()
        return None if record is None else _record_to_domain(record)

    async def list_run(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        _validate_cursor(after_sequence, limit)
        count_subquery = (
            select(func.count(AuditEventRecord.global_sequence))
            .where(AuditEventRecord.run_id == run_id)
            .scalar_subquery()
        )
        minimum_subquery = (
            select(func.min(AuditEventRecord.run_sequence))
            .where(AuditEventRecord.run_id == run_id)
            .scalar_subquery()
        )
        maximum_subquery = (
            select(func.max(AuditEventRecord.run_sequence))
            .where(AuditEventRecord.run_id == run_id)
            .scalar_subquery()
        )
        snapshot = (
            await self._session.execute(
                select(
                    RunRecord.next_timeline_sequence,
                    count_subquery,
                    minimum_subquery,
                    maximum_subquery,
                ).where(RunRecord.id == run_id)
            )
        ).one_or_none()
        if snapshot is None:
            raise AuditPersistenceInvariantError(
                "audit_run_missing", "audit timeline run does not exist"
            )
        run_counter, event_count, minimum_sequence, maximum_sequence = snapshot
        if (
            run_counter == 0
            or event_count != run_counter
            or (run_counter > 0 and (minimum_sequence != 1 or maximum_sequence != run_counter))
        ):
            raise AuditPersistenceInvariantError(
                "audit_timeline_not_contiguous",
                "persisted per-run audit timeline disagrees with its sequence counter",
            )
        if after_sequence > run_counter:
            raise AuditPersistenceInvariantError(
                "audit_cursor_beyond_timeline", "audit cursor exceeds the persisted timeline"
            )
        statement = (
            select(AuditEventRecord)
            .where(
                AuditEventRecord.run_id == run_id,
                AuditEventRecord.run_sequence > after_sequence,
                AuditEventRecord.run_sequence <= run_counter,
            )
            .order_by(AuditEventRecord.run_sequence)
            .limit(limit)
        )
        rows = tuple((await self._session.execute(statement)).scalars())
        expected_count = min(limit, run_counter - after_sequence)
        expected_sequences = tuple(range(after_sequence + 1, after_sequence + expected_count + 1))
        if tuple(row.run_sequence for row in rows) != expected_sequences:
            raise AuditPersistenceInvariantError(
                "audit_timeline_not_contiguous",
                "persisted per-run audit timeline contains a sequence gap",
            )
        return tuple(_record_to_domain(row) for row in rows)


def _validate_cursor(after_sequence: int, limit: int) -> None:
    if (
        not isinstance(after_sequence, int)
        or isinstance(after_sequence, bool)
        or after_sequence < 0
    ):
        raise ValueError("audit cursor must be a nonnegative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("audit page limit must be from 1 through 500")
