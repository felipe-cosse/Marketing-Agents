"""Dedicated schedule resources backed only by the existing configuration authority.

Disabling clears the instance's schedule binding, not the instance itself. The
existing durable schedule retains recurrence/history for a later explicit enable.
No route owns worker leases, occurrence identity, or a second scheduling model.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from marketing_agents.application.policies.catalog_authorization import (
    CatalogAuthorizationError,
    authorize_catalog_reader,
)
from marketing_agents.application.policies.instance_configuration_authorization import (
    InstanceConfigurationAuthorizationError,
    authorize_instance_configuration_admin,
)
from marketing_agents.application.ports.recurrence import RecurrenceCalculator
from marketing_agents.application.ports.schedule_control import ScheduleControlReader
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationService,
    UpdateInstanceConfigurationCommand,
)
from marketing_agents.application.services.schedule_configuration import configured_schedule_id
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Schedule, ScheduleOccurrence
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.instance_configuration import (
    InstanceConfiguration,
    InstanceConfigurationPatch,
    InstanceSchedule,
    InstanceTriggerBinding,
    PatchValue,
    ScheduledInput,
)
from marketing_agents.domain.recurrence_resolution import RecurrenceResult
from marketing_agents.domain.validation import require_id, require_utc

_SCHEDULE_ID = re.compile(r"^schedule\.configuration\.[0-9a-f]{64}$")


class ScheduleControlError(ValueError):
    def __init__(self, code: str, *, current_revision: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class SchedulePageQuery:
    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ScheduleControlError("schedule_query_invalid")
        if self.cursor is not None and (
            type(self.cursor) is not str
            or not 1 <= len(self.cursor) <= 2048
            or not self.cursor.isascii()
        ):
            raise ScheduleControlError("schedule_cursor_invalid")


@dataclass(frozen=True, slots=True)
class ScheduleMutation:
    operation: Literal["create", "patch", "enable", "disable"]
    target_id: str
    expected_revision: int
    correlation_id: str
    parameters: InstanceSchedule | None = None
    scheduled_input: ScheduledInput | None = None

    def __post_init__(self) -> None:
        require_id(self.target_id, "schedule command target")
        require_id(self.correlation_id, "schedule command correlation")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ScheduleControlError("schedule_input_invalid")
        if self.parameters is not None and type(self.parameters) is not InstanceSchedule:
            raise ScheduleControlError("schedule_input_invalid")
        if self.scheduled_input is not None and (
            type(self.scheduled_input) is not ScheduledInput
            or self.scheduled_input.authority is not None
            or self.scheduled_input.mode is not WorkMode.DRY_RUN
        ):
            raise ScheduleControlError("schedule_input_invalid")
        if self.operation == "create":
            valid = self.parameters is not None and self.scheduled_input is not None
        elif self.operation == "patch":
            valid = self.parameters is not None or self.scheduled_input is not None
        else:
            valid = (
                self.operation in ("enable", "disable")
                and self.parameters is None
                and self.scheduled_input is None
            )
        if not valid:
            raise ScheduleControlError("schedule_input_invalid")


@dataclass(frozen=True, slots=True)
class ScheduleResource:
    schedule: Schedule
    configuration_revision: int
    preview_utc: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class SchedulePage:
    items: tuple[ScheduleResource, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ScheduleOccurrencePage:
    items: tuple[ScheduleOccurrence, ...]
    next_cursor: str | None


def _encode_cursor(scope: str, key: list[str]) -> str:
    raw = canonical_json_bytes({"scope": scope, "key": key, "version": 1})
    return "schedule-v1." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(query: SchedulePageQuery, scope: str) -> list[str] | None:
    if type(query) is not SchedulePageQuery:
        raise ScheduleControlError("schedule_query_invalid")
    query.__post_init__()
    if query.cursor is None:
        return None
    try:
        prefix, encoded = query.cursor.split(".", 1)
        if prefix != "schedule-v1":
            raise ValueError
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (
            type(value) is not dict
            or set(value) != {"scope", "key", "version"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["scope"] != scope
            or type(value["key"]) is not list
            or len(value["key"]) != (1 if scope == "schedules" else 2)
            or any(type(item) is not str for item in value["key"])
            or _encode_cursor(scope, value["key"]) != query.cursor
        ):
            raise ValueError
        require_id(value["key"][-1], "schedule cursor identity")
        if scope == "schedules" and _SCHEDULE_ID.fullmatch(value["key"][0]) is None:
            raise ValueError
        return value["key"]
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, RecursionError):
        raise ScheduleControlError("schedule_cursor_invalid") from None


def _authorize(principal: AuthenticatedPrincipal, *, mutation: bool = False) -> None:
    try:
        if mutation:
            authorize_instance_configuration_admin(principal)
        else:
            authorize_catalog_reader(principal)
    except (CatalogAuthorizationError, InstanceConfigurationAuthorizationError):
        raise ScheduleControlError("schedule_forbidden") from None


class ScheduleControlService:
    def __init__(
        self,
        *,
        reader: ScheduleControlReader,
        configurations: InstanceConfigurationService,
        recurrence: RecurrenceCalculator,
    ) -> None:
        self._reader = reader
        self._configurations = configurations
        self._recurrence = recurrence

    async def read(
        self, schedule_id: str, *, principal: AuthenticatedPrincipal
    ) -> ScheduleResource:
        _authorize(principal)
        schedule = await self._owned(schedule_id)
        configuration = await self._configurations.read(schedule.instance_id, principal=principal)
        return self._resource(schedule, configuration)

    async def list_schedules(
        self, query: SchedulePageQuery, *, principal: AuthenticatedPrincipal
    ) -> SchedulePage:
        _authorize(principal)
        boundary = _decode_cursor(query, "schedules")
        after_id = None if boundary is None else boundary[0]
        rows = await self._reader.list_schedules(after_id=after_id, limit=query.limit + 1)
        if (
            type(rows) is not tuple
            or len(rows) > query.limit + 1
            or any(type(row) is not Schedule for row in rows)
            or tuple(row.id for row in rows) != tuple(sorted({row.id for row in rows}))
            or any(after_id is not None and row.id <= after_id for row in rows)
        ):
            raise ScheduleControlError("schedule_unavailable")
        resources = []
        for row in rows[: query.limit]:
            configuration = await self._configurations.read(row.instance_id, principal=principal)
            resources.append(self._resource(row, configuration))
        return SchedulePage(
            tuple(resources),
            _encode_cursor("schedules", [rows[query.limit - 1].id])
            if len(rows) > query.limit
            else None,
        )

    async def occurrences(
        self,
        schedule_id: str,
        query: SchedulePageQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ScheduleOccurrencePage:
        _authorize(principal)
        await self._owned(schedule_id)
        scope = "occurrences:" + schedule_id
        boundary = _decode_cursor(query, scope)
        after = None
        if boundary is not None:
            try:
                due = datetime.fromisoformat(boundary[0])
                require_utc(due, "occurrence cursor instant")
                after = (due, boundary[1])
            except ValueError:
                raise ScheduleControlError("schedule_cursor_invalid") from None
        rows = await self._reader.list_occurrences(schedule_id, after=after, limit=query.limit + 1)
        if (
            type(rows) is not tuple
            or len(rows) > query.limit + 1
            or any(
                type(row) is not ScheduleOccurrence or row.schedule_id != schedule_id
                for row in rows
            )
        ):
            raise ScheduleControlError("schedule_unavailable")
        keys = tuple((row.scheduled_for_utc, row.id) for row in rows)
        if keys != tuple(sorted(set(keys))) or any(
            after is not None and key <= after for key in keys
        ):
            raise ScheduleControlError("schedule_unavailable")
        page = rows[: query.limit]
        return ScheduleOccurrencePage(
            page,
            _encode_cursor(scope, [page[-1].scheduled_for_utc.isoformat(), page[-1].id])
            if len(rows) > query.limit
            else None,
        )

    async def mutate(
        self, command: ScheduleMutation, *, principal: AuthenticatedPrincipal
    ) -> ScheduleResource:
        _authorize(principal, mutation=True)
        if type(command) is not ScheduleMutation:
            raise ScheduleControlError("schedule_input_invalid")
        # Revalidate even a dataclass forged by an in-process caller.
        command.__post_init__()
        previous = None
        if command.operation == "create":
            instance_id = command.target_id
            identifier = configured_schedule_id(instance_id)
            if await self._reader.get(identifier) is not None:
                raise ScheduleControlError("schedule_exists")
        else:
            previous = await self._owned(command.target_id)
            instance_id = previous.instance_id
            identifier = previous.id
        current = await self._configurations.read(instance_id, principal=principal)
        if current.configuration_revision != command.expected_revision:
            raise ScheduleControlError(
                "schedule_revision_conflict", current_revision=current.configuration_revision
            )
        if previous is not None:
            self._resource(previous, current)
        if command.operation != "disable" and not current.enabled:
            raise ScheduleControlError("schedule_instance_disabled")
        if command.operation == "enable" and current.scheduled_input is None:
            raise ScheduleControlError("schedule_input_required")
        if command.operation == "patch" and (previous is None or not previous.enabled):
            raise ScheduleControlError("schedule_disabled")
        parameters = command.parameters
        if parameters is None and command.operation != "disable":
            assert previous is not None
            parameters = InstanceSchedule(
                previous.cron,
                previous.timezone,
                previous.misfire_policy,
                previous.misfire_grace_seconds,
            )
        binding = InstanceTriggerBinding(kind=TriggerKind.SCHEDULE, enabled=False)
        if parameters is not None:
            binding = InstanceTriggerBinding(
                kind=TriggerKind.SCHEDULE,
                enabled=True,
                cron=parameters.cron,
                timezone=parameters.timezone,
                misfire_policy=parameters.misfire_policy,
                misfire_grace_seconds=parameters.misfire_grace_seconds,
            )
        triggers = (
            *(item for item in current.trigger_bindings if item.kind is not TriggerKind.SCHEDULE),
            binding,
        )
        result = await self._configurations.update(
            UpdateInstanceConfigurationCommand(
                instance_id=instance_id,
                expected_revision=command.expected_revision,
                correlation_id=command.correlation_id,
                patch=InstanceConfigurationPatch(
                    trigger_bindings=PatchValue.of(triggers),
                    schedule=PatchValue.of(parameters),
                    scheduled_input=PatchValue.omitted()
                    if command.scheduled_input is None
                    else PatchValue.of(command.scheduled_input),
                ),
            ),
            principal=principal,
        )
        # A concurrent edit after the commit makes its response unconfirmed, never
        # a fabricated claim that the caller's revision is still current.
        schedule = await self._owned(identifier)
        return self._resource(schedule, result.configuration)

    async def _owned(self, schedule_id: str) -> Schedule:
        require_id(schedule_id, "schedule resource ID")
        schedule = await self._reader.get(schedule_id)
        if schedule is None:
            raise ScheduleControlError("schedule_not_found")
        if (
            type(schedule) is not Schedule
            or schedule.id != schedule_id
            or schedule.configuration_revision is None
            or configured_schedule_id(schedule.instance_id) != schedule.id
        ):
            raise ScheduleControlError("schedule_unavailable")
        return schedule

    def _resource(
        self, schedule: Schedule, configuration: InstanceConfiguration
    ) -> ScheduleResource:
        snapshot = configuration.scheduled_input
        if (
            type(schedule) is not Schedule
            or type(configuration) is not InstanceConfiguration
            or configured_schedule_id(configuration.instance_id) != schedule.id
            or schedule.instance_id != configuration.instance_id
            or schedule.configuration_revision != configuration.configuration_revision
            or schedule.enabled
            != (
                configuration.enabled
                and configuration.schedule is not None
                and snapshot is not None
            )
            or (
                snapshot is not None
                and (
                    snapshot.authority is None
                    or snapshot.authority.workflow_id != schedule.workflow_id
                )
            )
        ):
            raise ScheduleControlError("schedule_unavailable")
        if configuration.schedule is not None and configuration.schedule != InstanceSchedule(
            schedule.cron,
            schedule.timezone,
            schedule.misfire_policy,
            schedule.misfire_grace_seconds,
        ):
            raise ScheduleControlError("schedule_unavailable")
        previews = [schedule.next_run_at_utc]
        for _ in range(2):
            value = self._recurrence.next_occurrence_after(
                cron=schedule.cron, timezone=schedule.timezone, after_utc=previews[-1]
            )
            if type(value) is not RecurrenceResult or value.scheduled_for_utc <= previews[-1]:
                raise ScheduleControlError("schedule_unavailable")
            require_utc(value.scheduled_for_utc, "schedule preview time")
            previews.append(value.scheduled_for_utc)
        return ScheduleResource(schedule, configuration.configuration_revision, tuple(previews))
