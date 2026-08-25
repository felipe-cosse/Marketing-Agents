"""Fail-closed initial schedule persistence and exact create replay."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import (
    ScheduleInsertResult,
    ScheduleRepositoryConflict,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Schedule, ScheduleClaim
from marketing_agents.domain.enums import MisfirePolicy
from marketing_agents.domain.validation import require_id, require_utc
from marketing_agents.infrastructure.db.models.schedule import ScheduleRecord

_SCHEDULE_INTEGRITY_DOMAIN = b"marketing-agents:schedule:persistence:v1\x00"


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
    cron_expression: str,
    timezone_name: str,
    next_run_at_utc: datetime,
    misfire_policy: str,
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
        "cron_expression": cron_expression,
        "timezone_name": timezone_name,
        "next_run_at_utc": _timestamp_material(next_run_at_utc),
        "misfire_policy": misfire_policy,
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
        cron_expression=schedule.cron,
        timezone_name=schedule.timezone,
        next_run_at_utc=schedule.next_run_at_utc,
        misfire_policy=schedule.misfire_policy.value,
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
        cron_expression=record.cron_expression,
        timezone_name=record.timezone_name,
        next_run_at_utc=record.next_run_at_utc,
        misfire_policy=record.misfire_policy,
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
        cron_expression=schedule.cron,
        timezone_name=schedule.timezone,
        next_run_at_utc=schedule.next_run_at_utc,
        misfire_policy=schedule.misfire_policy.value,
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
            cron=record.cron_expression,
            timezone=record.timezone_name,
            next_run_at_utc=record.next_run_at_utc,
            misfire_policy=MisfirePolicy(record.misfire_policy),
            enabled=record.enabled,
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
        schedule.cron,
        schedule.timezone,
        schedule.next_run_at_utc,
        schedule.misfire_policy,
        schedule.enabled,
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
                cron_expression=current.cron_expression,
                timezone_name=current.timezone_name,
                next_run_at_utc=current.next_run_at_utc,
                misfire_policy=current.misfire_policy,
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
