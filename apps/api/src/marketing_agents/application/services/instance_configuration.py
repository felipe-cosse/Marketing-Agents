"""Authorized reads and atomic optimistic updates for instance configuration."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from marketing_agents.application.policies.catalog_authorization import (
    CatalogAuthorizationError,
    authorize_catalog_reader,
)
from marketing_agents.application.policies.instance_configuration_authorization import (
    InstanceConfigurationAuthorizationError,
    authorize_instance_configuration_admin,
)
from marketing_agents.application.ports.clock import Clock
from marketing_agents.application.ports.instance_configuration import (
    InstanceConfigurationConstraintProvider,
    InstanceConfigurationConstraints,
    InstanceConfigurationUnitOfWorkFactory,
    RegisteredBindingProvider,
)
from marketing_agents.application.ports.recurrence import (
    RecurrenceCalculationError,
    RecurrenceCalculator,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.instance_configuration import (
    MAX_INSTANCE_CONNECTOR_BINDING_ID_LENGTH,
    MAX_INSTANCE_CONNECTOR_BINDINGS,
    MAX_INSTANCE_TRIGGER_BINDINGS,
    MAX_INSTANCE_VARIANT_LABEL_LENGTH,
    InstanceConfiguration,
    InstanceConfigurationPatch,
    configuration_to_plain_mapping,
)
from marketing_agents.domain.validation import (
    frozen_json_mapping,
    require_digest,
    require_id,
    require_utc,
)
from marketing_agents.security.digest_key import DigestKey

_SNAPSHOT_VERSION_DOMAIN = b"marketing-agents:instance-configuration-snapshot:v1\x00"


class InstanceConfigurationServiceError(ValueError):
    """Stable, non-sensitive configuration failure for API problem mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class UpdateInstanceConfigurationCommand:
    """Server-built optimistic patch command; actor and time are deliberately absent."""

    instance_id: str
    expected_revision: int
    patch: InstanceConfigurationPatch
    correlation_id: str

    def __post_init__(self) -> None:
        require_id(self.instance_id, "configuration command instance ID")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("configuration expected revision must be a positive integer")
        if type(self.patch) is not InstanceConfigurationPatch:
            raise ValueError("configuration command requires an exact patch")
        require_id(self.correlation_id, "configuration command correlation ID")


@dataclass(frozen=True, slots=True)
class InstanceConfigurationUpdateResult:
    configuration: InstanceConfiguration
    changed: bool

    def __post_init__(self) -> None:
        if type(self.configuration) is not InstanceConfiguration or type(self.changed) is not bool:
            raise ValueError("configuration update result is invalid")


@dataclass(frozen=True, slots=True)
class InstanceConfigurationSnapshot:
    """Complete ordered configuration snapshot and deterministic overlay version."""

    configurations: tuple[InstanceConfiguration, ...]
    version: str

    def __post_init__(self) -> None:
        if type(self.configurations) is not tuple or any(
            type(item) is not InstanceConfiguration for item in self.configurations
        ):
            raise ValueError("configuration snapshot must contain exact immutable projections")
        instance_ids = tuple(item.instance_id for item in self.configurations)
        if instance_ids != tuple(sorted(instance_ids)) or len(instance_ids) != len(
            set(instance_ids)
        ):
            raise ValueError("configuration snapshot instances must be unique and ordered")
        require_digest(self.version, "configuration snapshot version")


@dataclass(frozen=True, slots=True)
class InstanceConfigurationSchema:
    """Catalog-constrained JSON Schema containing deployment fields only."""

    instance_id: str
    template_id: str
    configuration_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_id(self.instance_id, "configuration schema instance ID")
        require_id(self.template_id, "configuration schema template ID")
        object.__setattr__(
            self,
            "configuration_schema",
            frozen_json_mapping(self.configuration_schema, "instance configuration schema"),
        )


class _CompareAndSwapLost(RuntimeError):
    pass


class InstanceConfigurationService:
    """Validate static authority and persist one audited +1 configuration revision."""

    def __init__(
        self,
        *,
        unit_of_work_factory: InstanceConfigurationUnitOfWorkFactory,
        constraints: InstanceConfigurationConstraintProvider,
        registered_bindings: RegisteredBindingProvider,
        recurrence: RecurrenceCalculator,
        clock: Clock,
        audit_pseudonym_key: DigestKey,
    ) -> None:
        if type(audit_pseudonym_key) is not DigestKey:
            raise ValueError("instance configuration audit requires the exact pseudonym key")
        self._unit_of_work_factory = unit_of_work_factory
        self._constraints = constraints
        self._registered_bindings = registered_bindings
        self._recurrence = recurrence
        self._clock = clock
        self._audit_pseudonym_key = audit_pseudonym_key

    async def read(
        self,
        instance_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfiguration:
        self._authorize_reader(principal)
        self._require_instance_id(instance_id)
        constraints = await self._constraints_for(instance_id)
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                configuration = await unit_of_work.configurations.get(instance_id)
        except InstanceConfigurationServiceError:
            raise
        except Exception:
            raise self._unavailable() from None
        if configuration is None:
            raise self._unavailable()
        self._require_configuration(configuration, constraints, now=self._utc_now())
        return configuration

    async def read_all(
        self,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationSnapshot:
        self._authorize_reader(principal)
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                configurations = await unit_of_work.configurations.list_all()
        except Exception:
            raise self._unavailable() from None
        if type(configurations) is not tuple or any(
            type(item) is not InstanceConfiguration for item in configurations
        ):
            raise self._unavailable()
        ordered = tuple(sorted(configurations, key=lambda item: item.instance_id))
        if len({item.instance_id for item in ordered}) != len(ordered):
            raise self._unavailable()
        now = self._utc_now()
        for configuration in ordered:
            constraints = await self._constraints_for(configuration.instance_id)
            self._require_configuration(configuration, constraints, now=now)
        material = [
            {
                "instance_id": item.instance_id,
                "configuration_revision": item.configuration_revision,
                "configuration": configuration_to_plain_mapping(item),
            }
            for item in ordered
        ]
        version = hashlib.sha256(
            _SNAPSHOT_VERSION_DOMAIN + canonical_json_bytes(material)
        ).hexdigest()
        return InstanceConfigurationSnapshot(configurations=ordered, version=version)

    async def schema(
        self,
        instance_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationSchema:
        self._authorize_reader(principal)
        self._require_instance_id(instance_id)
        constraints = await self._constraints_for(instance_id)
        registered = self._registered_bindings_for(constraints)
        return InstanceConfigurationSchema(
            instance_id=constraints.instance_id,
            template_id=constraints.template_id,
            configuration_schema=_configuration_schema(constraints, registered),
        )

    async def update(
        self,
        command: UpdateInstanceConfigurationCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationUpdateResult:
        self._authorize_admin(principal)
        if type(command) is not UpdateInstanceConfigurationCommand:
            raise InstanceConfigurationServiceError(
                "configuration_command_invalid",
                "instance configuration command is invalid",
            )
        if command.patch.is_empty:
            raise InstanceConfigurationServiceError(
                "configuration_patch_empty",
                "instance configuration patch must contain at least one field",
            )
        constraints = await self._constraints_for(command.instance_id)
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                current = await unit_of_work.configurations.get(command.instance_id)
                if current is None:
                    raise self._unavailable()
                now = self._utc_now()
                self._require_configuration(current, constraints, now=now)
                if current.configuration_revision != command.expected_revision:
                    raise self._revision_conflict(current.configuration_revision)
                try:
                    candidate = command.patch.apply(current)
                except (AttributeError, TypeError, ValueError):
                    raise InstanceConfigurationServiceError(
                        "configuration_invalid",
                        "instance configuration is invalid",
                    ) from None
                self._require_configuration(candidate, constraints, now=now, client_value=True)
                if candidate == current:
                    return InstanceConfigurationUpdateResult(
                        configuration=current,
                        changed=False,
                    )
                replacement = candidate.with_revision(current.configuration_revision + 1)
                replaced = await unit_of_work.configurations.compare_and_swap(
                    current,
                    replacement,
                )
                if type(replaced) is not bool:
                    raise self._unavailable()
                if not replaced:
                    raise _CompareAndSwapLost
                audit_context = AuditContext.authenticated_user(
                    principal.actor_id,
                    authentication_method=principal.authentication_method.value,
                    correlation_id=command.correlation_id,
                )
                audit = AuditEventFactory(
                    audit_context,
                    configuration_pseudonym_key=self._audit_pseudonym_key,
                ).instance_configuration_changed(
                    instance_id=current.instance_id,
                    previous_configuration=configuration_to_plain_mapping(current),
                    new_configuration=configuration_to_plain_mapping(replacement),
                    previous_revision=current.configuration_revision,
                    new_revision=replacement.configuration_revision,
                    occurred_at=now,
                )
                await unit_of_work.audits.append_global(audit)
                await unit_of_work.commit()
                return InstanceConfigurationUpdateResult(
                    configuration=replacement,
                    changed=True,
                )
        except _CompareAndSwapLost:
            current_revision = await self._current_revision(command.instance_id)
            raise self._revision_conflict(current_revision) from None
        except InstanceConfigurationServiceError:
            raise
        except Exception:
            raise self._unavailable() from None

    async def _constraints_for(self, instance_id: str) -> InstanceConfigurationConstraints:
        try:
            constraints = await self._constraints.get(instance_id)
        except Exception:
            raise self._unavailable() from None
        if constraints is None:
            raise InstanceConfigurationServiceError(
                "instance_not_found",
                "agent instance does not exist",
            )
        if (
            type(constraints) is not InstanceConfigurationConstraints
            or constraints.instance_id != instance_id
        ):
            raise self._unavailable()
        return constraints

    def _registered_bindings_for(
        self,
        constraints: InstanceConfigurationConstraints,
    ) -> dict[str, frozenset[str]]:
        registered: dict[str, frozenset[str]] = {}
        try:
            for family in sorted(constraints.allowed_connector_families):
                binding_ids = self._registered_bindings.registered_binding_ids(family)
                if type(binding_ids) is not frozenset or any(
                    type(binding_id) is not str for binding_id in binding_ids
                ):
                    raise TypeError
                for binding_id in binding_ids:
                    require_id(binding_id, "registered connector binding ID")
                    if len(binding_id) > MAX_INSTANCE_CONNECTOR_BINDING_ID_LENGTH:
                        raise ValueError
                registered[family] = binding_ids
        except Exception:
            raise self._unavailable() from None
        return registered

    def _require_configuration(
        self,
        configuration: InstanceConfiguration,
        constraints: InstanceConfigurationConstraints,
        *,
        now: datetime,
        client_value: bool = False,
    ) -> None:
        error = (
            InstanceConfigurationServiceError(
                "configuration_invalid",
                "instance configuration is invalid",
            )
            if client_value
            else self._unavailable()
        )
        if (
            type(configuration) is not InstanceConfiguration
            or configuration.instance_id != constraints.instance_id
        ):
            raise error
        configured_kinds = {binding.kind for binding in configuration.trigger_bindings}
        if not configured_kinds.issubset(constraints.supported_trigger_kinds):
            raise error
        if not set(configuration.connector_bindings).issubset(
            constraints.allowed_connector_families
        ):
            raise error
        registered = self._registered_bindings_for(constraints)
        if any(
            binding.binding_id not in registered.get(family, frozenset())
            for family, binding in configuration.connector_bindings.items()
        ):
            raise error
        if configuration.schedule is None:
            return
        try:
            next_run = self._recurrence.next_after(
                cron=configuration.schedule.cron,
                timezone=configuration.schedule.timezone,
                after_utc=now,
            )
        except RecurrenceCalculationError:
            if client_value:
                raise InstanceConfigurationServiceError(
                    "configuration_schedule_invalid",
                    "instance schedule expression is invalid",
                ) from None
            raise self._unavailable() from None
        except Exception:
            raise self._unavailable() from None
        try:
            require_utc(next_run, "next configured schedule time")
        except (AttributeError, ValueError):
            raise self._unavailable() from None
        if next_run <= now:
            raise self._unavailable()

    async def _current_revision(self, instance_id: str) -> int:
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                current = await unit_of_work.configurations.get(instance_id)
        except Exception:
            raise self._unavailable() from None
        if current is None or type(current) is not InstanceConfiguration:
            raise self._unavailable()
        return current.configuration_revision

    def _utc_now(self) -> datetime:
        try:
            now = self._clock.now()
            require_utc(now, "instance configuration clock")
        except (AttributeError, TypeError, ValueError):
            raise self._unavailable() from None
        return now

    @staticmethod
    def _authorize_reader(principal: AuthenticatedPrincipal) -> None:
        try:
            authorize_catalog_reader(principal)
        except CatalogAuthorizationError:
            raise InstanceConfigurationServiceError(
                "configuration_read_forbidden",
                "instance configuration read is forbidden",
            ) from None

    @staticmethod
    def _authorize_admin(principal: AuthenticatedPrincipal) -> None:
        try:
            authorize_instance_configuration_admin(principal)
        except InstanceConfigurationAuthorizationError as exc:
            raise InstanceConfigurationServiceError(exc.code, str(exc)) from None

    @staticmethod
    def _require_instance_id(instance_id: str) -> None:
        try:
            require_id(instance_id, "configured instance ID")
        except (TypeError, ValueError):
            raise InstanceConfigurationServiceError(
                "instance_not_found",
                "agent instance does not exist",
            ) from None

    @staticmethod
    def _revision_conflict(current_revision: int) -> InstanceConfigurationServiceError:
        return InstanceConfigurationServiceError(
            "configuration_revision_conflict",
            "instance configuration revision changed",
            current_revision=current_revision,
        )

    @staticmethod
    def _unavailable() -> InstanceConfigurationServiceError:
        return InstanceConfigurationServiceError(
            "configuration_unavailable",
            "instance configuration is unavailable",
        )


def _configuration_schema(
    constraints: InstanceConfigurationConstraints,
    registered: Mapping[str, frozenset[str]],
) -> dict[str, Any]:
    trigger_variants: list[dict[str, Any]] = []
    if TriggerKind.MANUAL in constraints.supported_trigger_kinds:
        trigger_variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {"const": "manual"},
                    "enabled": {"type": "boolean", "default": True},
                    "eventSource": {"type": "null"},
                    "cron": {"type": "null"},
                    "timezone": {"type": "null"},
                    "misfirePolicy": {"type": "null"},
                    "misfireGraceSeconds": {"type": "null"},
                },
            }
        )
    if TriggerKind.WEBHOOK in constraints.supported_trigger_kinds:
        trigger_variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "eventSource"],
                "properties": {
                    "type": {"const": "webhook"},
                    "enabled": {"type": "boolean", "default": True},
                    "eventSource": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$",
                    },
                    "cron": {"type": "null"},
                    "timezone": {"type": "null"},
                    "misfirePolicy": {"type": "null"},
                    "misfireGraceSeconds": {"type": "null"},
                },
            }
        )
    if TriggerKind.SCHEDULE in constraints.supported_trigger_kinds:
        trigger_variants.extend(
            (
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "enabled"],
                    "properties": {
                        "type": {"const": "schedule"},
                        "enabled": {"const": False},
                        "eventSource": {"type": "null"},
                        "cron": {"type": "null"},
                        "timezone": {"type": "null"},
                        "misfirePolicy": {"type": "null"},
                        "misfireGraceSeconds": {"type": "null"},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "type",
                        "cron",
                        "timezone",
                        "misfirePolicy",
                        "misfireGraceSeconds",
                    ],
                    "properties": {
                        "type": {"const": "schedule"},
                        "enabled": {"const": True, "default": True},
                        "eventSource": {"type": "null"},
                        "cron": {"type": "string", "minLength": 1, "maxLength": 100},
                        "timezone": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                        },
                        "misfirePolicy": {"enum": ["skip", "run_once"]},
                        "misfireGraceSeconds": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 86_400,
                        },
                    },
                },
            )
        )
    connector_properties: dict[str, Any] = {}
    for family in sorted(constraints.allowed_connector_families):
        if not registered[family]:
            continue
        connector_properties[family] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["connectorFamily", "bindingId"],
            "properties": {
                "connectorFamily": {"const": family},
                "bindingId": {"enum": sorted(registered[family])},
                "enabled": {"type": "boolean", "default": True},
            },
        }
    schedule_schema: dict[str, Any]
    if TriggerKind.SCHEDULE in constraints.supported_trigger_kinds:
        schedule_schema = {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "cron",
                        "timezone",
                        "misfirePolicy",
                        "misfireGraceSeconds",
                    ],
                    "properties": {
                        "cron": {"type": "string", "minLength": 1, "maxLength": 100},
                        "timezone": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                        },
                        "misfirePolicy": {"enum": ["skip", "run_once"]},
                        "misfireGraceSeconds": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 86_400,
                        },
                    },
                },
            ]
        }
    else:
        schedule_schema = {"type": "null"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:marketing-agents:instance-configuration:{constraints.instance_id}:v1",
        "type": "object",
        "description": (
            "Structural deployment PATCH schema. The API additionally enforces registered "
            "bindings, recurrence validity, and exact trigger/schedule value consistency."
        ),
        "additionalProperties": False,
        "minProperties": 1,
        "properties": {
            "enabled": {"type": "boolean"},
            "variantLabel": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": MAX_INSTANCE_VARIANT_LABEL_LENGTH,
            },
            "triggerBindings": {
                "type": "array",
                "maxItems": MAX_INSTANCE_TRIGGER_BINDINGS,
                "items": ({"oneOf": trigger_variants} if trigger_variants else False),
            },
            "connectorBindings": {
                "type": "object",
                "maxProperties": MAX_INSTANCE_CONNECTOR_BINDINGS,
                "properties": connector_properties,
                "additionalProperties": False,
            },
            "schedule": schedule_schema,
        },
    }
