"""Fail-closed initial schedule persistence and exact create replay."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import (
    ScheduleInsertResult,
    ScheduleRepositoryConflict,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Schedule
from marketing_agents.domain.enums import MisfirePolicy
from marketing_agents.infrastructure.db.models.schedule import ScheduleRecord

_SCHEDULE_INTEGRITY_DOMAIN = b"marketing-agents:schedule:persistence:v1\x00"


class SchedulePersistenceConflict(ScheduleRepositoryConflict):
    """Infrastructure-specific schedule conflict with a stable application code."""


def _timestamp_material(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


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
    )


def _to_record(schedule: Schedule) -> ScheduleRecord:
    if (
        type(schedule) is not Schedule
        or not isinstance(schedule.misfire_policy, MisfirePolicy)
        or type(schedule.enabled) is not bool
        or type(schedule.version) is not int
    ):
        raise SchedulePersistenceConflict(
            "schedule_invalid",
            "schedule persistence requires one exact validated Schedule",
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
        integrity_digest=_integrity_digest(material),
    )


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
        return Schedule(
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
    except (TypeError, ValueError) as exc:
        raise SchedulePersistenceConflict(
            "schedule_tampered",
            "persisted schedule is invalid",
        ) from exc


class SQLAlchemyScheduleRepository:
    """Create-time schedule repository with exact-ID replay semantics."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, schedule_id: str) -> Schedule | None:
        record = await self._session.get(ScheduleRecord, schedule_id)
        return None if record is None else _to_domain(record)

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
            if existing != schedule:
                raise SchedulePersistenceConflict(
                    "schedule_id_conflict",
                    "schedule ID already identifies a different initial configuration",
                ) from exc
            return ScheduleInsertResult(existing, inserted=False)
        return ScheduleInsertResult(schedule, inserted=True)
