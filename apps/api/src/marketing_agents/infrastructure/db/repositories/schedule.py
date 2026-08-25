"""Fail-closed initial schedule persistence and exact create replay."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import (
    ScheduleInsertResult,
    ScheduleOccurrenceInsertResult,
    ScheduleOccurrenceLinkResult,
    ScheduleRepositoryConflict,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Schedule, ScheduleClaim, ScheduleOccurrence
from marketing_agents.domain.enums import MisfirePolicy, OccurrenceState
from marketing_agents.domain.schedule_occurrence_identity import (
    SCHEDULE_OCCURRENCE_ID_SCHEME,
)
from marketing_agents.domain.validation import require_id, require_utc
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.infrastructure.db.models.schedule import (
    ScheduleOccurrenceRecord,
    ScheduleRecord,
)

_SCHEDULE_INTEGRITY_DOMAIN = b"marketing-agents:schedule:persistence:v1\x00"
_OCCURRENCE_INTEGRITY_DOMAIN = b"marketing-agents:schedule-occurrence:persistence:v1\x00"


class SchedulePersistenceConflict(ScheduleRepositoryConflict):
    """Infrastructure-specific schedule conflict with a stable application code."""


def _timestamp_material(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _optional_timestamp_material(value: datetime | None) -> str | None:
    return None if value is None else _timestamp_material(value)


def _integrity_material(
    *,
    schedule_id: str,
    trigger_id: str,
    instance_id: str,
    workflow_id: str,
    cron_expression: str,
    timezone_name: str,
    recurrence_version: str,
    next_run_at_utc: datetime,
    last_scheduled_at_utc: datetime | None,
    misfire_policy: str,
    misfire_grace_seconds: int,
    enabled: bool,
    version: int,
    lease_owner: str | None,
    lease_claimed_at_utc: datetime | None,
    lease_expires_at_utc: datetime | None,
) -> dict[str, Any]:
    return {
        "id": schedule_id,
        "trigger_id": trigger_id,
        "instance_id": instance_id,
        "workflow_id": workflow_id,
        "cron_expression": cron_expression,
        "timezone_name": timezone_name,
        "recurrence_version": recurrence_version,
        "next_run_at_utc": _timestamp_material(next_run_at_utc),
        "last_scheduled_at_utc": _optional_timestamp_material(last_scheduled_at_utc),
        "misfire_policy": misfire_policy,
        "misfire_grace_seconds": misfire_grace_seconds,
        "enabled": enabled,
        "version": version,
        "lease_owner": lease_owner,
        "lease_claimed_at_utc": _optional_timestamp_material(lease_claimed_at_utc),
        "lease_expires_at_utc": _optional_timestamp_material(lease_expires_at_utc),
    }


def _integrity_digest(material: dict[str, Any]) -> str:
    return hashlib.sha256(_SCHEDULE_INTEGRITY_DOMAIN + canonical_json_bytes(material)).hexdigest()


def _schedule_material(schedule: Schedule) -> dict[str, Any]:
    return _integrity_material(
        schedule_id=schedule.id,
        trigger_id=schedule.trigger_id,
        instance_id=schedule.instance_id,
        workflow_id=schedule.workflow_id,
        cron_expression=schedule.cron,
        timezone_name=schedule.timezone,
        recurrence_version=schedule.recurrence_version,
        next_run_at_utc=schedule.next_run_at_utc,
        last_scheduled_at_utc=schedule.last_scheduled_at_utc,
        misfire_policy=schedule.misfire_policy.value,
        misfire_grace_seconds=schedule.misfire_grace_seconds,
        enabled=schedule.enabled,
        version=schedule.version,
        lease_owner=None,
        lease_claimed_at_utc=None,
        lease_expires_at_utc=None,
    )


def _record_material(record: ScheduleRecord) -> dict[str, Any]:
    return _integrity_material(
        schedule_id=record.id,
        trigger_id=record.trigger_id,
        instance_id=record.instance_id,
        workflow_id=record.workflow_id,
        cron_expression=record.cron_expression,
        timezone_name=record.timezone_name,
        recurrence_version=record.recurrence_version,
        next_run_at_utc=record.next_run_at_utc,
        last_scheduled_at_utc=record.last_scheduled_at_utc,
        misfire_policy=record.misfire_policy,
        misfire_grace_seconds=record.misfire_grace_seconds,
        enabled=record.enabled,
        version=record.version,
        lease_owner=record.lease_owner,
        lease_claimed_at_utc=record.lease_claimed_at_utc,
        lease_expires_at_utc=record.lease_expires_at_utc,
    )


def _to_record(schedule: Schedule) -> ScheduleRecord:
    if (
        type(schedule) is not Schedule
        or not isinstance(schedule.misfire_policy, MisfirePolicy)
        or type(schedule.enabled) is not bool
        or type(schedule.version) is not int
        or schedule.version != 1
        or schedule.last_scheduled_at_utc is not None
    ):
        raise SchedulePersistenceConflict(
            "schedule_invalid",
            "schedule creation requires one exact validated version-one Schedule",
        )
    material = _schedule_material(schedule)
    return ScheduleRecord(
        id=schedule.id,
        trigger_id=schedule.trigger_id,
        instance_id=schedule.instance_id,
        workflow_id=schedule.workflow_id,
        cron_expression=schedule.cron,
        timezone_name=schedule.timezone,
        recurrence_version=schedule.recurrence_version,
        next_run_at_utc=schedule.next_run_at_utc,
        last_scheduled_at_utc=None,
        misfire_policy=schedule.misfire_policy.value,
        misfire_grace_seconds=schedule.misfire_grace_seconds,
        enabled=schedule.enabled,
        version=schedule.version,
        lease_owner=None,
        lease_claimed_at_utc=None,
        lease_expires_at_utc=None,
        integrity_digest=_integrity_digest(material),
    )


def _claim_from_record(record: ScheduleRecord) -> ScheduleClaim | None:
    lease_owner = record.lease_owner
    claimed_at_utc = record.lease_claimed_at_utc
    lease_expires_at_utc = record.lease_expires_at_utc
    values = (lease_owner, claimed_at_utc, lease_expires_at_utc)
    if values == (None, None, None):
        return None
    if lease_owner is None or claimed_at_utc is None or lease_expires_at_utc is None:
        raise SchedulePersistenceConflict(
            "schedule_tampered",
            "persisted schedule lease is incomplete",
        )
    try:
        return ScheduleClaim(
            schedule_id=record.id,
            scheduled_for_utc=record.next_run_at_utc,
            lease_owner=lease_owner,
            claimed_at_utc=claimed_at_utc,
            lease_expires_at_utc=lease_expires_at_utc,
            recurrence_version=record.recurrence_version,
            version=record.version,
        )
    except (TypeError, ValueError) as exc:
        raise SchedulePersistenceConflict(
            "schedule_tampered",
            "persisted schedule lease is invalid",
        ) from exc


def _to_domain(record: ScheduleRecord) -> Schedule:
    try:
        observed_digest = _integrity_digest(_record_material(record))
    except (TypeError, ValueError) as exc:
        raise SchedulePersistenceConflict(
            "schedule_tampered",
            "persisted schedule is not canonical",
        ) from exc
    if not hmac.compare_digest(record.integrity_digest, observed_digest):
        raise SchedulePersistenceConflict(
            "schedule_tampered",
            "persisted schedule integrity digest does not match",
        )
    try:
        schedule = Schedule(
            id=record.id,
            trigger_id=record.trigger_id,
            instance_id=record.instance_id,
            workflow_id=record.workflow_id,
            cron=record.cron_expression,
            timezone=record.timezone_name,
            next_run_at_utc=record.next_run_at_utc,
            last_scheduled_at_utc=record.last_scheduled_at_utc,
            misfire_policy=MisfirePolicy(record.misfire_policy),
            misfire_grace_seconds=record.misfire_grace_seconds,
            enabled=record.enabled,
            recurrence_version=record.recurrence_version,
            version=record.version,
        )
        _claim_from_record(record)
        return schedule
    except (TypeError, ValueError) as exc:
        raise SchedulePersistenceConflict(
            "schedule_tampered",
            "persisted schedule is invalid",
        ) from exc


def _creation_facts(schedule: Schedule) -> tuple[Any, ...]:
    """Facts supplied by initial configuration rather than runtime claiming."""

    return (
        schedule.id,
        schedule.trigger_id,
        schedule.instance_id,
        schedule.workflow_id,
        schedule.cron,
        schedule.timezone,
        schedule.recurrence_version,
        schedule.next_run_at_utc,
        schedule.last_scheduled_at_utc,
        schedule.misfire_policy,
        schedule.misfire_grace_seconds,
        schedule.enabled,
    )


def _occurrence_material(
    *,
    occurrence_id: str,
    identity_scheme: str,
    schedule_id: str,
    scheduled_for_utc: datetime,
    scheduled_local: str,
    timezone_name: str,
    timezone_fold: int,
    recurrence_version: str,
    state: str,
    work_item_id: str | None,
    run_id: str | None,
    misfire_policy_applied: str | None,
    misfire_grace_seconds: int | None,
    misfire_evaluated_at_utc: datetime | None,
    first_missed_at_utc: datetime | None,
    last_missed_at_utc: datetime | None,
    missed_count: int | None,
) -> dict[str, Any]:
    return {
        "id": occurrence_id,
        "identity_scheme": identity_scheme,
        "schedule_id": schedule_id,
        "scheduled_for_utc": _timestamp_material(scheduled_for_utc),
        "scheduled_local": scheduled_local,
        "timezone_name": timezone_name,
        "timezone_fold": timezone_fold,
        "recurrence_version": recurrence_version,
        "state": state,
        "work_item_id": work_item_id,
        "run_id": run_id,
        "misfire_policy_applied": misfire_policy_applied,
        "misfire_grace_seconds": misfire_grace_seconds,
        "misfire_evaluated_at_utc": _optional_timestamp_material(misfire_evaluated_at_utc),
        "first_missed_at_utc": _optional_timestamp_material(first_missed_at_utc),
        "last_missed_at_utc": _optional_timestamp_material(last_missed_at_utc),
        "missed_count": missed_count,
    }


def _occurrence_digest(material: dict[str, Any]) -> str:
    return hashlib.sha256(_OCCURRENCE_INTEGRITY_DOMAIN + canonical_json_bytes(material)).hexdigest()


def _occurrence_domain_material(occurrence: ScheduleOccurrence) -> dict[str, Any]:
    return _occurrence_material(
        occurrence_id=occurrence.id,
        identity_scheme=SCHEDULE_OCCURRENCE_ID_SCHEME,
        schedule_id=occurrence.schedule_id,
        scheduled_for_utc=occurrence.scheduled_for_utc,
        scheduled_local=occurrence.scheduled_local,
        timezone_name=occurrence.timezone,
        timezone_fold=occurrence.timezone_fold,
        recurrence_version=occurrence.recurrence_version,
        state=occurrence.state.value,
        work_item_id=occurrence.work_item_id,
        run_id=occurrence.run_id,
        misfire_policy_applied=(
            None
            if occurrence.misfire_policy_applied is None
            else occurrence.misfire_policy_applied.value
        ),
        misfire_grace_seconds=occurrence.misfire_grace_seconds,
        misfire_evaluated_at_utc=occurrence.misfire_evaluated_at_utc,
        first_missed_at_utc=occurrence.first_missed_at_utc,
        last_missed_at_utc=occurrence.last_missed_at_utc,
        missed_count=occurrence.missed_count,
    )


def _occurrence_record_material(record: ScheduleOccurrenceRecord) -> dict[str, Any]:
    return _occurrence_material(
        occurrence_id=record.id,
        identity_scheme=record.identity_scheme,
        schedule_id=record.schedule_id,
        scheduled_for_utc=record.scheduled_for_utc,
        scheduled_local=record.scheduled_local,
        timezone_name=record.timezone_name,
        timezone_fold=record.timezone_fold,
        recurrence_version=record.recurrence_version,
        state=record.state,
        work_item_id=record.work_item_id,
        run_id=record.run_id,
        misfire_policy_applied=record.misfire_policy_applied,
        misfire_grace_seconds=record.misfire_grace_seconds,
        misfire_evaluated_at_utc=record.misfire_evaluated_at_utc,
        first_missed_at_utc=record.first_missed_at_utc,
        last_missed_at_utc=record.last_missed_at_utc,
        missed_count=record.missed_count,
    )


def _occurrence_to_record(occurrence: ScheduleOccurrence) -> ScheduleOccurrenceRecord:
    if type(occurrence) is not ScheduleOccurrence:
        raise SchedulePersistenceConflict(
            "occurrence_invalid",
            "occurrence persistence requires one exact validated ScheduleOccurrence",
        )
    material = _occurrence_domain_material(occurrence)
    return ScheduleOccurrenceRecord(
        id=occurrence.id,
        identity_scheme=SCHEDULE_OCCURRENCE_ID_SCHEME,
        schedule_id=occurrence.schedule_id,
        scheduled_for_utc=occurrence.scheduled_for_utc,
        scheduled_local=occurrence.scheduled_local,
        timezone_name=occurrence.timezone,
        timezone_fold=occurrence.timezone_fold,
        recurrence_version=occurrence.recurrence_version,
        state=occurrence.state.value,
        misfire_policy_applied=(
            None
            if occurrence.misfire_policy_applied is None
            else occurrence.misfire_policy_applied.value
        ),
        misfire_grace_seconds=occurrence.misfire_grace_seconds,
        misfire_evaluated_at_utc=occurrence.misfire_evaluated_at_utc,
        first_missed_at_utc=occurrence.first_missed_at_utc,
        last_missed_at_utc=occurrence.last_missed_at_utc,
        missed_count=occurrence.missed_count,
        work_item_id=occurrence.work_item_id,
        run_id=occurrence.run_id,
        integrity_digest=_occurrence_digest(material),
    )


def _occurrence_to_domain(record: ScheduleOccurrenceRecord) -> ScheduleOccurrence:
    try:
        observed_digest = _occurrence_digest(_occurrence_record_material(record))
    except (TypeError, ValueError) as exc:
        raise SchedulePersistenceConflict(
            "occurrence_tampered",
            "persisted schedule occurrence is not canonical",
        ) from exc
    if record.identity_scheme != SCHEDULE_OCCURRENCE_ID_SCHEME or not hmac.compare_digest(
        record.integrity_digest, observed_digest
    ):
        raise SchedulePersistenceConflict(
            "occurrence_tampered",
            "persisted schedule occurrence integrity does not match",
        )
    try:
        return ScheduleOccurrence(
            id=record.id,
            schedule_id=record.schedule_id,
            scheduled_for_utc=record.scheduled_for_utc,
            scheduled_local=record.scheduled_local,
            timezone=record.timezone_name,
            timezone_fold=record.timezone_fold,
            recurrence_version=record.recurrence_version,
            state=OccurrenceState(record.state),
            work_item_id=record.work_item_id,
            run_id=record.run_id,
            misfire_policy_applied=(
                None
                if record.misfire_policy_applied is None
                else MisfirePolicy(record.misfire_policy_applied)
            ),
            misfire_grace_seconds=record.misfire_grace_seconds,
            misfire_evaluated_at_utc=record.misfire_evaluated_at_utc,
            first_missed_at_utc=record.first_missed_at_utc,
            last_missed_at_utc=record.last_missed_at_utc,
            missed_count=record.missed_count,
        )
    except (TypeError, ValueError) as exc:
        raise SchedulePersistenceConflict(
            "occurrence_tampered",
            "persisted schedule occurrence is invalid",
        ) from exc


def _occurrence_identity_facts(occurrence: ScheduleOccurrence) -> tuple[Any, ...]:
    return (
        occurrence.id,
        occurrence.schedule_id,
        occurrence.scheduled_for_utc,
        occurrence.scheduled_local,
        occurrence.timezone,
        occurrence.timezone_fold,
        occurrence.recurrence_version,
        occurrence.misfire_policy_applied,
        occurrence.misfire_grace_seconds,
        occurrence.misfire_evaluated_at_utc,
        occurrence.first_missed_at_utc,
        occurrence.last_missed_at_utc,
        occurrence.missed_count,
    )


def _is_sqlite_busy(session: AsyncSession, exc: OperationalError) -> bool:
    return session.get_bind().dialect.name == "sqlite" and getattr(
        exc.orig, "sqlite_errorcode", None
    ) in {sqlite3.SQLITE_BUSY, getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517)}


class SQLAlchemyScheduleRepository:
    """Schedule configuration, due scanning, and optimistic lease persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, schedule_id: str) -> Schedule | None:
        record = await self._session.get(ScheduleRecord, schedule_id)
        return None if record is None else _to_domain(record)

    async def get_claim(self, schedule_id: str) -> ScheduleClaim | None:
        record = await self._session.get(ScheduleRecord, schedule_id)
        if record is None:
            return None
        _to_domain(record)
        return _claim_from_record(record)

    async def fence_claim(
        self,
        claim: ScheduleClaim,
        *,
        now: datetime,
    ) -> bool:
        """Acquire a transaction-scoped write fence over one exact active lease."""

        if type(claim) is not ScheduleClaim:
            raise ValueError("schedule processing requires an exact ScheduleClaim")
        require_utc(now, "schedule processing boundary")
        if now < claim.claimed_at_utc or claim.lease_expires_at_utc < now:
            return False

        current_statement = (
            select(ScheduleRecord)
            .where(ScheduleRecord.id == claim.schedule_id)
            .execution_options(populate_existing=True)
        )
        current = (await self._session.execute(current_statement)).scalar_one_or_none()
        if current is None:
            return False
        _to_domain(current)
        if _claim_from_record(current) != claim:
            return False

        statement = (
            update(ScheduleRecord)
            .where(
                ScheduleRecord.id == claim.schedule_id,
                ScheduleRecord.version == claim.version,
                ScheduleRecord.integrity_digest == current.integrity_digest,
                ScheduleRecord.enabled.is_(True),
                ScheduleRecord.next_run_at_utc == claim.scheduled_for_utc,
                ScheduleRecord.recurrence_version == claim.recurrence_version,
                ScheduleRecord.lease_owner == claim.lease_owner,
                ScheduleRecord.lease_claimed_at_utc == claim.claimed_at_utc,
                ScheduleRecord.lease_expires_at_utc == claim.lease_expires_at_utc,
                ScheduleRecord.lease_claimed_at_utc <= now,
                ScheduleRecord.lease_expires_at_utc >= now,
            )
            .values(integrity_digest=current.integrity_digest)
            .returning(ScheduleRecord.id)
            .execution_options(synchronize_session=False)
        )
        try:
            fenced_id = (await self._session.execute(statement)).scalar_one_or_none()
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                return False
            raise
        return fenced_id == claim.schedule_id

    async def get_occurrence(
        self,
        occurrence_id: str,
    ) -> ScheduleOccurrence | None:
        require_id(occurrence_id, "occurrence ID")
        statement = (
            select(ScheduleOccurrenceRecord)
            .where(ScheduleOccurrenceRecord.id == occurrence_id)
            .execution_options(populate_existing=True)
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else _occurrence_to_domain(record)

    async def get_occurrence_by_schedule_due(
        self,
        schedule_id: str,
        scheduled_for_utc: datetime,
    ) -> ScheduleOccurrence | None:
        require_id(schedule_id, "occurrence schedule ID")
        require_utc(scheduled_for_utc, "scheduled occurrence time")
        statement = (
            select(ScheduleOccurrenceRecord)
            .where(
                ScheduleOccurrenceRecord.schedule_id == schedule_id,
                ScheduleOccurrenceRecord.scheduled_for_utc == scheduled_for_utc,
            )
            .execution_options(populate_existing=True)
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else _occurrence_to_domain(record)

    async def list_claimable_due(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Schedule, ...]:
        require_utc(now, "schedule claim boundary")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("schedule claim limit must be from 1 through 100")
        statement = (
            select(ScheduleRecord)
            .where(
                ScheduleRecord.enabled.is_(True),
                ScheduleRecord.next_run_at_utc <= now,
                or_(
                    and_(
                        ScheduleRecord.lease_owner.is_(None),
                        ScheduleRecord.lease_claimed_at_utc.is_(None),
                        ScheduleRecord.lease_expires_at_utc.is_(None),
                    ),
                    and_(
                        ScheduleRecord.lease_owner.is_not(None),
                        ScheduleRecord.lease_claimed_at_utc.is_not(None),
                        ScheduleRecord.lease_expires_at_utc < now,
                    ),
                ),
            )
            .order_by(ScheduleRecord.next_run_at_utc, ScheduleRecord.id)
            .limit(limit)
        )
        records = (await self._session.execute(statement)).scalars()
        return tuple(_to_domain(record) for record in records)

    async def try_claim(
        self,
        *,
        schedule_id: str,
        expected_version: int,
        expected_due_at_utc: datetime,
        lease_owner: str,
        claimed_at_utc: datetime,
        lease_expires_at_utc: datetime,
    ) -> ScheduleClaim | None:
        require_id(schedule_id, "claim schedule ID")
        require_id(lease_owner, "claim lease owner")
        require_utc(expected_due_at_utc, "expected schedule due time")
        require_utc(claimed_at_utc, "schedule claim time")
        require_utc(lease_expires_at_utc, "schedule claim expiry")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected schedule version must be positive")
        if lease_expires_at_utc <= claimed_at_utc:
            raise ValueError("schedule claim expiry must follow claim time")

        current_statement = (
            select(ScheduleRecord)
            .where(ScheduleRecord.id == schedule_id)
            .execution_options(populate_existing=True)
        )
        current = (await self._session.execute(current_statement)).scalar_one_or_none()
        if current is None:
            return None
        _to_domain(current)
        current_claim = _claim_from_record(current)
        if (
            current.version != expected_version
            or current.next_run_at_utc != expected_due_at_utc
            or not current.enabled
            or current.next_run_at_utc > claimed_at_utc
            or (
                current_claim is not None
                and not current_claim.lease_expires_at_utc < claimed_at_utc
            )
        ):
            return None

        expected_digest = current.integrity_digest
        new_version = expected_version + 1
        new_digest = _integrity_digest(
            _integrity_material(
                schedule_id=current.id,
                trigger_id=current.trigger_id,
                instance_id=current.instance_id,
                workflow_id=current.workflow_id,
                cron_expression=current.cron_expression,
                timezone_name=current.timezone_name,
                recurrence_version=current.recurrence_version,
                next_run_at_utc=current.next_run_at_utc,
                last_scheduled_at_utc=current.last_scheduled_at_utc,
                misfire_policy=current.misfire_policy,
                misfire_grace_seconds=current.misfire_grace_seconds,
                enabled=current.enabled,
                version=new_version,
                lease_owner=lease_owner,
                lease_claimed_at_utc=claimed_at_utc,
                lease_expires_at_utc=lease_expires_at_utc,
            )
        )
        claimable_lease = or_(
            and_(
                ScheduleRecord.lease_owner.is_(None),
                ScheduleRecord.lease_claimed_at_utc.is_(None),
                ScheduleRecord.lease_expires_at_utc.is_(None),
            ),
            and_(
                ScheduleRecord.lease_owner.is_not(None),
                ScheduleRecord.lease_claimed_at_utc.is_not(None),
                ScheduleRecord.lease_expires_at_utc < claimed_at_utc,
            ),
        )
        statement = (
            update(ScheduleRecord)
            .where(
                ScheduleRecord.id == schedule_id,
                ScheduleRecord.version == expected_version,
                ScheduleRecord.integrity_digest == expected_digest,
                ScheduleRecord.enabled.is_(True),
                ScheduleRecord.next_run_at_utc == expected_due_at_utc,
                ScheduleRecord.next_run_at_utc <= claimed_at_utc,
                claimable_lease,
            )
            .values(
                lease_owner=lease_owner,
                lease_claimed_at_utc=claimed_at_utc,
                lease_expires_at_utc=lease_expires_at_utc,
                version=new_version,
                integrity_digest=new_digest,
            )
            .returning(ScheduleRecord.id)
            .execution_options(synchronize_session=False)
        )
        try:
            claimed_id = (await self._session.execute(statement)).scalar_one_or_none()
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                return None
            raise
        if claimed_id is None:
            return None
        fresh_statement = (
            select(ScheduleRecord)
            .where(ScheduleRecord.id == claimed_id)
            .execution_options(populate_existing=True)
        )
        fresh = (await self._session.execute(fresh_statement)).scalar_one()
        _to_domain(fresh)
        claim = _claim_from_record(fresh)
        if claim is None:
            raise SchedulePersistenceConflict(
                "schedule_tampered",
                "claimed schedule did not retain its lease",
            )
        return claim

    async def advance_and_release_claim(
        self,
        claim: ScheduleClaim,
        *,
        next_run_at_utc: datetime,
        completed_at_utc: datetime,
    ) -> Schedule | None:
        """Atomically persist the next occurrence and release one exact live claim."""

        if type(claim) is not ScheduleClaim:
            raise ValueError("schedule advancement requires an exact ScheduleClaim")
        require_utc(next_run_at_utc, "next scheduled UTC time")
        require_utc(completed_at_utc, "schedule processing completion time")
        if next_run_at_utc <= claim.scheduled_for_utc:
            raise ValueError("next scheduled UTC time must follow the completed occurrence")
        if completed_at_utc < claim.claimed_at_utc or claim.lease_expires_at_utc < completed_at_utc:
            return None

        current_statement = (
            select(ScheduleRecord)
            .where(ScheduleRecord.id == claim.schedule_id)
            .execution_options(populate_existing=True)
        )
        current = (await self._session.execute(current_statement)).scalar_one_or_none()
        if current is None:
            return None
        _to_domain(current)
        if _claim_from_record(current) != claim:
            return None

        expected_digest = current.integrity_digest
        new_version = claim.version + 1
        new_digest = _integrity_digest(
            _integrity_material(
                schedule_id=current.id,
                trigger_id=current.trigger_id,
                instance_id=current.instance_id,
                workflow_id=current.workflow_id,
                cron_expression=current.cron_expression,
                timezone_name=current.timezone_name,
                recurrence_version=current.recurrence_version,
                next_run_at_utc=next_run_at_utc,
                last_scheduled_at_utc=claim.scheduled_for_utc,
                misfire_policy=current.misfire_policy,
                misfire_grace_seconds=current.misfire_grace_seconds,
                enabled=current.enabled,
                version=new_version,
                lease_owner=None,
                lease_claimed_at_utc=None,
                lease_expires_at_utc=None,
            )
        )
        statement = (
            update(ScheduleRecord)
            .where(
                ScheduleRecord.id == claim.schedule_id,
                ScheduleRecord.version == claim.version,
                ScheduleRecord.integrity_digest == expected_digest,
                ScheduleRecord.enabled.is_(True),
                ScheduleRecord.next_run_at_utc == claim.scheduled_for_utc,
                ScheduleRecord.recurrence_version == claim.recurrence_version,
                ScheduleRecord.lease_owner == claim.lease_owner,
                ScheduleRecord.lease_claimed_at_utc == claim.claimed_at_utc,
                ScheduleRecord.lease_expires_at_utc == claim.lease_expires_at_utc,
                ScheduleRecord.lease_claimed_at_utc <= completed_at_utc,
                ScheduleRecord.lease_expires_at_utc >= completed_at_utc,
            )
            .values(
                last_scheduled_at_utc=claim.scheduled_for_utc,
                next_run_at_utc=next_run_at_utc,
                lease_owner=None,
                lease_claimed_at_utc=None,
                lease_expires_at_utc=None,
                version=new_version,
                integrity_digest=new_digest,
            )
            .returning(ScheduleRecord.id)
            .execution_options(synchronize_session=False)
        )
        try:
            advanced_id = (await self._session.execute(statement)).scalar_one_or_none()
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                return None
            raise
        if advanced_id is None:
            return None

        fresh_statement = (
            select(ScheduleRecord)
            .where(ScheduleRecord.id == advanced_id)
            .execution_options(populate_existing=True)
        )
        fresh = (await self._session.execute(fresh_statement)).scalar_one()
        advanced = _to_domain(fresh)
        if _claim_from_record(fresh) is not None:
            raise SchedulePersistenceConflict(
                "schedule_tampered",
                "advanced schedule retained lease state",
            )
        return advanced

    async def add_or_get(self, schedule: Schedule) -> ScheduleInsertResult:
        record = _to_record(schedule)
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError as exc:
            existing = await self.get(schedule.id)
            if existing is None:
                raise SchedulePersistenceConflict(
                    "schedule_insert_conflict",
                    "schedule could not be inserted with its exact configuration",
                ) from exc
            if _creation_facts(existing) != _creation_facts(schedule):
                raise SchedulePersistenceConflict(
                    "schedule_id_conflict",
                    "schedule ID already identifies a different initial configuration",
                ) from exc
            return ScheduleInsertResult(existing, inserted=False)
        return ScheduleInsertResult(schedule, inserted=True)

    async def add_occurrence_or_get(
        self,
        occurrence: ScheduleOccurrence,
    ) -> ScheduleOccurrenceInsertResult:
        if (
            type(occurrence) is not ScheduleOccurrence
            or occurrence.state not in (OccurrenceState.CLAIMED, OccurrenceState.SKIPPED)
            or occurrence.work_item_id is not None
            or occurrence.run_id is not None
        ):
            raise SchedulePersistenceConflict(
                "occurrence_invalid",
                "new schedule occurrence must be unlinked and claimed or skipped",
            )
        record = _occurrence_to_record(occurrence)
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError as exc:
            existing = await self.get_occurrence(occurrence.id)
            if existing is not None:
                if _occurrence_identity_facts(existing) != _occurrence_identity_facts(occurrence):
                    raise SchedulePersistenceConflict(
                        "occurrence_id_conflict",
                        "occurrence ID already identifies different immutable facts",
                    ) from exc
                return ScheduleOccurrenceInsertResult(existing, inserted=False)
            due_existing = await self.get_occurrence_by_schedule_due(
                occurrence.schedule_id,
                occurrence.scheduled_for_utc,
            )
            if due_existing is not None:
                raise SchedulePersistenceConflict(
                    "occurrence_identity_conflict",
                    "schedule and due instant already identify another occurrence",
                ) from exc
            raise SchedulePersistenceConflict(
                "occurrence_insert_conflict",
                "occurrence could not be inserted with its exact identity",
            ) from exc
        return ScheduleOccurrenceInsertResult(occurrence, inserted=True)

    async def mark_occurrence_enqueued(
        self,
        *,
        occurrence_id: str,
        work_item_id: str,
        run_id: str,
    ) -> ScheduleOccurrenceLinkResult:
        require_id(occurrence_id, "occurrence ID")
        require_id(work_item_id, "occurrence WorkItem ID")
        require_id(run_id, "occurrence Run ID")
        run_work_item_id = await self._session.scalar(
            select(RunRecord.work_item_id).where(RunRecord.id == run_id)
        )
        if run_work_item_id != work_item_id:
            raise SchedulePersistenceConflict(
                "occurrence_receipt_conflict",
                "occurrence Run does not belong to the supplied WorkItem",
            )
        current = await self.get_occurrence(occurrence_id)
        if current is None:
            raise SchedulePersistenceConflict(
                "occurrence_missing",
                "occurrence must exist before its receipt can be linked",
            )
        if current.state is OccurrenceState.ENQUEUED:
            if current.work_item_id != work_item_id or current.run_id != run_id:
                raise SchedulePersistenceConflict(
                    "occurrence_receipt_conflict",
                    "occurrence already links a different WorkItem or Run",
                )
            return ScheduleOccurrenceLinkResult(current, linked=False)
        if (
            current.state is not OccurrenceState.CLAIMED
            or current.work_item_id is not None
            or current.run_id is not None
        ):
            raise SchedulePersistenceConflict(
                "occurrence_state_conflict",
                "only one unlinked claimed occurrence can become enqueued",
            )

        linked = replace(
            current,
            state=OccurrenceState.ENQUEUED,
            work_item_id=work_item_id,
            run_id=run_id,
        )
        current_record = await self._session.get(ScheduleOccurrenceRecord, occurrence_id)
        if current_record is None:
            raise SchedulePersistenceConflict(
                "occurrence_missing",
                "occurrence disappeared before its receipt could be linked",
            )
        _occurrence_to_domain(current_record)
        expected_digest = current_record.integrity_digest
        new_digest = _occurrence_digest(_occurrence_domain_material(linked))
        statement = (
            update(ScheduleOccurrenceRecord)
            .where(
                ScheduleOccurrenceRecord.id == occurrence_id,
                ScheduleOccurrenceRecord.integrity_digest == expected_digest,
                ScheduleOccurrenceRecord.state == OccurrenceState.CLAIMED.value,
                ScheduleOccurrenceRecord.work_item_id.is_(None),
                ScheduleOccurrenceRecord.run_id.is_(None),
            )
            .values(
                state=OccurrenceState.ENQUEUED.value,
                work_item_id=work_item_id,
                run_id=run_id,
                integrity_digest=new_digest,
            )
            .returning(ScheduleOccurrenceRecord.id)
            .execution_options(synchronize_session=False)
        )
        try:
            async with self._session.begin_nested():
                linked_id = (await self._session.execute(statement)).scalar_one_or_none()
        except IntegrityError as exc:
            raise SchedulePersistenceConflict(
                "occurrence_receipt_conflict",
                "WorkItem or Run already belongs to another occurrence",
            ) from exc
        if linked_id is None:
            fresh = await self.get_occurrence(occurrence_id)
            if (
                fresh is not None
                and fresh.state is OccurrenceState.ENQUEUED
                and fresh.work_item_id == work_item_id
                and fresh.run_id == run_id
            ):
                return ScheduleOccurrenceLinkResult(fresh, linked=False)
            raise SchedulePersistenceConflict(
                "occurrence_receipt_conflict",
                "occurrence changed before its receipt could be linked",
            )
        return ScheduleOccurrenceLinkResult(linked, linked=True)
