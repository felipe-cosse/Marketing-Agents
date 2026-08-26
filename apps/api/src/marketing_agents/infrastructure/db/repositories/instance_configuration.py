"""Integrity-sealed instance configuration persistence and optimistic updates."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from marketing_agents.application.ports.instance_configuration import (
    InstanceConfigurationRepositoryError,
)
from marketing_agents.domain.canonical_json import CanonicalJsonError, canonical_json_bytes
from marketing_agents.domain.enums import MisfirePolicy, TriggerKind
from marketing_agents.domain.instance_configuration import (
    InstanceConfiguration,
    InstanceConnectorBinding,
    InstanceSchedule,
    InstanceTriggerBinding,
)
from marketing_agents.domain.validation import require_id
from marketing_agents.infrastructure.db.models.instance_configuration import (
    AgentInstanceConfigurationRecord,
)
from marketing_agents.infrastructure.db.repositories.audit import SQLAlchemyAuditRepository

_INTEGRITY_DOMAIN = b"marketing-agents:instance-configuration:persistence:v1\x00"
_MAX_SNAPSHOT_BYTES = 65_536
_TRIGGER_KEYS = frozenset(
    {
        "type",
        "enabled",
        "event_source",
        "cron",
        "timezone",
        "misfire_policy",
        "misfire_grace_seconds",
    }
)
_CONNECTOR_KEYS = frozenset({"connector_family", "binding_id", "enabled"})
_SCHEDULE_KEYS = frozenset({"cron", "timezone", "misfire_policy", "misfire_grace_seconds"})


class InstanceConfigurationPersistenceError(InstanceConfigurationRepositoryError):
    """A persisted configuration is invalid, tampered, or cannot be inserted exactly."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def _trigger_material(binding: InstanceTriggerBinding) -> dict[str, Any]:
    return {
        "type": binding.kind.value,
        "enabled": binding.enabled,
        "event_source": binding.event_source,
        "cron": binding.cron,
        "timezone": binding.timezone,
        "misfire_policy": (
            None if binding.misfire_policy is None else binding.misfire_policy.value
        ),
        "misfire_grace_seconds": binding.misfire_grace_seconds,
    }


def _connector_material(binding: InstanceConnectorBinding) -> dict[str, Any]:
    return {
        "connector_family": binding.connector_family,
        "binding_id": binding.binding_id,
        "enabled": binding.enabled,
    }


def _schedule_material(schedule: InstanceSchedule | None) -> dict[str, Any] | None:
    if schedule is None:
        return None
    return {
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "misfire_policy": schedule.misfire_policy.value,
        "misfire_grace_seconds": schedule.misfire_grace_seconds,
    }


def _configuration_material(configuration: InstanceConfiguration) -> dict[str, Any]:
    if type(configuration) is not InstanceConfiguration:
        raise InstanceConfigurationPersistenceError(
            "instance_configuration_invalid",
            "configuration persistence requires one exact validated instance configuration",
        )
    return {
        "instance_id": configuration.instance_id,
        "enabled": configuration.enabled,
        "variant_label": configuration.variant_label,
        "trigger_bindings": [
            _trigger_material(binding) for binding in configuration.trigger_bindings
        ],
        "connector_bindings": {
            family: _connector_material(configuration.connector_bindings[family])
            for family in sorted(configuration.connector_bindings)
        },
        "schedule": _schedule_material(configuration.schedule),
        "configuration_revision": configuration.configuration_revision,
    }


def _integrity_digest(material: dict[str, Any]) -> str:
    return hashlib.sha256(_INTEGRITY_DOMAIN + canonical_json_bytes(material)).hexdigest()


def _canonical_text(value: Any, label: str) -> str:
    try:
        payload = canonical_json_bytes(value)
    except CanonicalJsonError as exc:
        raise InstanceConfigurationPersistenceError(
            "instance_configuration_invalid",
            f"{label} is not canonical JSON",
        ) from exc
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise InstanceConfigurationPersistenceError(
            "instance_configuration_invalid",
            f"{label} exceeds the persistence byte limit",
        )
    return payload.decode("utf-8")


def _parse_canonical_text(raw: str, label: str) -> Any:
    if type(raw) is not str:
        raise InstanceConfigurationPersistenceError(
            "instance_configuration_tampered",
            f"persisted {label} is not text",
        )
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InstanceConfigurationPersistenceError(
            "instance_configuration_tampered",
            f"persisted {label} is not valid canonical JSON",
        ) from exc
    try:
        canonical = _canonical_text(value, label)
    except InstanceConfigurationPersistenceError as exc:
        raise InstanceConfigurationPersistenceError(
            "instance_configuration_tampered",
            f"persisted {label} is not valid canonical JSON",
        ) from exc
    if canonical != raw:
        raise InstanceConfigurationPersistenceError(
            "instance_configuration_tampered",
            f"persisted {label} is not canonical JSON",
        )
    return value


def _to_record(configuration: InstanceConfiguration) -> AgentInstanceConfigurationRecord:
    material = _configuration_material(configuration)
    return AgentInstanceConfigurationRecord(
        instance_id=configuration.instance_id,
        enabled=configuration.enabled,
        variant_label=configuration.variant_label,
        trigger_bindings_json=_canonical_text(material["trigger_bindings"], "trigger bindings"),
        connector_bindings_json=_canonical_text(
            material["connector_bindings"], "connector bindings"
        ),
        schedule_json=_canonical_text(material["schedule"], "schedule"),
        version=configuration.configuration_revision,
        integrity_digest=_integrity_digest(material),
    )


def _record_material(record: AgentInstanceConfigurationRecord) -> dict[str, Any]:
    return {
        "instance_id": record.instance_id,
        "enabled": record.enabled,
        "variant_label": record.variant_label,
        "trigger_bindings": _parse_canonical_text(record.trigger_bindings_json, "trigger bindings"),
        "connector_bindings": _parse_canonical_text(
            record.connector_bindings_json, "connector bindings"
        ),
        "schedule": _parse_canonical_text(record.schedule_json, "schedule"),
        "configuration_revision": record.version,
    }


def _to_domain(record: AgentInstanceConfigurationRecord) -> InstanceConfiguration:
    try:
        material = _record_material(record)
        observed_digest = _integrity_digest(material)
        if type(record.integrity_digest) is not str or not hmac.compare_digest(
            record.integrity_digest, observed_digest
        ):
            raise InstanceConfigurationPersistenceError(
                "instance_configuration_tampered",
                "persisted instance configuration integrity digest does not match",
            )

        trigger_values = material["trigger_bindings"]
        connector_values = material["connector_bindings"]
        schedule_value = material["schedule"]
        if not isinstance(trigger_values, list) or not isinstance(connector_values, dict):
            raise ValueError("persisted configuration collections have invalid shapes")
        if schedule_value is not None and not isinstance(schedule_value, dict):
            raise ValueError("persisted schedule has an invalid shape")
        if any(not isinstance(item, dict) or set(item) != _TRIGGER_KEYS for item in trigger_values):
            raise ValueError("persisted trigger binding has an invalid shape")
        if any(
            not isinstance(family, str)
            or not isinstance(item, dict)
            or set(item) != _CONNECTOR_KEYS
            for family, item in connector_values.items()
        ):
            raise ValueError("persisted connector binding has an invalid shape")
        if schedule_value is not None and set(schedule_value) != _SCHEDULE_KEYS:
            raise ValueError("persisted schedule has an invalid shape")
        triggers = tuple(
            InstanceTriggerBinding(
                kind=TriggerKind(item["type"]),
                enabled=item["enabled"],
                event_source=item["event_source"],
                cron=item["cron"],
                timezone=item["timezone"],
                misfire_policy=(
                    None
                    if item["misfire_policy"] is None
                    else MisfirePolicy(item["misfire_policy"])
                ),
                misfire_grace_seconds=item["misfire_grace_seconds"],
            )
            for item in trigger_values
        )
        connectors = {
            family: InstanceConnectorBinding(**item) for family, item in connector_values.items()
        }
        schedule = (
            None
            if schedule_value is None
            else InstanceSchedule(
                cron=schedule_value["cron"],
                timezone=schedule_value["timezone"],
                misfire_policy=MisfirePolicy(schedule_value["misfire_policy"]),
                misfire_grace_seconds=schedule_value["misfire_grace_seconds"],
            )
        )
        return InstanceConfiguration(
            instance_id=material["instance_id"],
            enabled=material["enabled"],
            variant_label=material["variant_label"],
            trigger_bindings=triggers,
            connector_bindings=connectors,
            schedule=schedule,
            configuration_revision=material["configuration_revision"],
        )
    except InstanceConfigurationPersistenceError:
        raise
    except (CanonicalJsonError, KeyError, TypeError, ValueError) as exc:
        raise InstanceConfigurationPersistenceError(
            "instance_configuration_tampered",
            "persisted instance configuration is invalid",
        ) from exc


def _is_sqlite_busy(session: AsyncSession, exc: OperationalError) -> bool:
    return session.get_bind().dialect.name == "sqlite" and getattr(
        exc.orig, "sqlite_errorcode", None
    ) in {sqlite3.SQLITE_BUSY, getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517)}


class SQLAlchemyInstanceConfigurationRepository:
    """Load, insert, and atomically replace integrity-sealed configurations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, instance_id: str) -> InstanceConfiguration | None:
        require_id(instance_id, "instance configuration ID")
        statement = (
            select(AgentInstanceConfigurationRecord)
            .where(AgentInstanceConfigurationRecord.instance_id == instance_id)
            .execution_options(populate_existing=True)
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else _to_domain(record)

    async def list_all(self) -> tuple[InstanceConfiguration, ...]:
        statement = (
            select(AgentInstanceConfigurationRecord)
            .order_by(AgentInstanceConfigurationRecord.instance_id)
            .execution_options(populate_existing=True)
        )
        records = (await self._session.execute(statement)).scalars()
        return tuple(_to_domain(record) for record in records)

    async def insert_missing(self, configuration: InstanceConfiguration) -> bool:
        record = _to_record(configuration)
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError as exc:
            existing = await self.get(configuration.instance_id)
            if existing is None:
                raise InstanceConfigurationPersistenceError(
                    "instance_configuration_insert_conflict",
                    "instance configuration could not be inserted exactly",
                ) from exc
            return False
        return True

    async def compare_and_swap(
        self,
        previous: InstanceConfiguration,
        replacement: InstanceConfiguration,
    ) -> bool:
        previous_material = _configuration_material(previous)
        replacement_record = _to_record(replacement)
        if (
            previous.instance_id != replacement.instance_id
            or replacement.configuration_revision != previous.configuration_revision + 1
        ):
            raise InstanceConfigurationPersistenceError(
                "instance_configuration_revision_invalid",
                "replacement must advance the same instance configuration by exactly one revision",
            )
        statement = (
            update(AgentInstanceConfigurationRecord)
            .where(
                AgentInstanceConfigurationRecord.instance_id == previous.instance_id,
                AgentInstanceConfigurationRecord.version == previous.configuration_revision,
                AgentInstanceConfigurationRecord.integrity_digest
                == _integrity_digest(previous_material),
            )
            .values(
                enabled=replacement_record.enabled,
                variant_label=replacement_record.variant_label,
                trigger_bindings_json=replacement_record.trigger_bindings_json,
                connector_bindings_json=replacement_record.connector_bindings_json,
                schedule_json=replacement_record.schedule_json,
                version=replacement_record.version,
                integrity_digest=replacement_record.integrity_digest,
            )
            .returning(AgentInstanceConfigurationRecord.instance_id)
            .execution_options(synchronize_session=False)
        )
        try:
            updated_id = (await self._session.execute(statement)).scalar_one_or_none()
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                return False
            raise
        return updated_id == previous.instance_id


class InstanceConfigurationSQLAlchemyUnitOfWork:
    """Dedicated transaction boundary for configuration and its audit event."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._configurations: SQLAlchemyInstanceConfigurationRepository | None = None
        self._audits: SQLAlchemyAuditRepository | None = None
        self._finished = False

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise InstanceConfigurationPersistenceError(
                "instance_configuration_uow_inactive",
                "instance configuration unit of work has not been entered",
            )
        return self._session

    @property
    def configurations(self) -> SQLAlchemyInstanceConfigurationRepository:
        if self._configurations is None:
            raise InstanceConfigurationPersistenceError(
                "instance_configuration_uow_inactive",
                "instance configuration unit of work has not been entered",
            )
        return self._configurations

    @property
    def audits(self) -> SQLAlchemyAuditRepository:
        if self._audits is None:
            raise InstanceConfigurationPersistenceError(
                "instance_configuration_uow_inactive",
                "instance configuration unit of work has not been entered",
            )
        return self._audits

    async def __aenter__(self) -> InstanceConfigurationSQLAlchemyUnitOfWork:
        if self._session is not None:
            raise InstanceConfigurationPersistenceError(
                "instance_configuration_uow_reentered",
                "instance configuration unit of work cannot be entered more than once",
            )
        self._session = self._session_factory()
        await self._session.begin()
        if self._session.get_bind().dialect.name == "sqlite":
            await self._session.execute(text("BEGIN DEFERRED"))
        self._configurations = SQLAlchemyInstanceConfigurationRepository(self._session)
        self._audits = SQLAlchemyAuditRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        session = self._require_session()
        try:
            if not self._finished:
                await session.rollback()
        finally:
            await session.close()

    async def commit(self) -> None:
        session = self._require_session()
        if self._finished:
            raise InstanceConfigurationPersistenceError(
                "instance_configuration_uow_finished",
                "instance configuration transaction is already finished",
            )
        await session.commit()
        self._finished = True

    async def rollback(self) -> None:
        session = self._require_session()
        if self._finished:
            raise InstanceConfigurationPersistenceError(
                "instance_configuration_uow_finished",
                "instance configuration transaction is already finished",
            )
        await session.rollback()
        self._finished = True


@dataclass(frozen=True, slots=True)
class InstanceConfigurationSQLAlchemyUnitOfWorkFactory:
    session_factory: async_sessionmaker[AsyncSession]

    def __call__(self) -> InstanceConfigurationSQLAlchemyUnitOfWork:
        return InstanceConfigurationSQLAlchemyUnitOfWork(self.session_factory)
