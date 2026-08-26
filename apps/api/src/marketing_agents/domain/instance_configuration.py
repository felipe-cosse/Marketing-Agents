"""Immutable deployment-only configuration for one catalog agent instance."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Self

from marketing_agents.domain.enums import MisfirePolicy, TriggerKind
from marketing_agents.domain.validation import require_iana_timezone, require_id, require_text

MAX_INSTANCE_TRIGGER_BINDINGS = 16
MAX_INSTANCE_CONNECTOR_BINDINGS = 16
MAX_INSTANCE_VARIANT_LABEL_LENGTH = 100
MAX_INSTANCE_BINDING_TEXT_LENGTH = 100
MAX_INSTANCE_CONNECTOR_BINDING_ID_LENGTH = 120
MAX_INSTANCE_MISFIRE_GRACE_SECONDS = 86_400

_CONNECTOR_FAMILY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CONNECTOR_BINDING_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_EVENT_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


def _require_exact_bool(value: bool, field_name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be an exact boolean")


def _require_connector_family(value: str, field_name: str) -> None:
    if (
        type(value) is not str
        or len(value) > MAX_INSTANCE_BINDING_TEXT_LENGTH
        or _CONNECTOR_FAMILY_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a bounded connector-family slug")


def _require_connector_binding_id(value: str, field_name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_INSTANCE_CONNECTOR_BINDING_ID_LENGTH
        or _CONNECTOR_BINDING_ID_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a bounded registered binding identifier")


@dataclass(frozen=True, slots=True)
class InstanceSchedule:
    """Validated schedule parameters retained independently from scheduler state."""

    cron: str
    timezone: str
    misfire_policy: MisfirePolicy
    misfire_grace_seconds: int

    def __post_init__(self) -> None:
        require_text(self.cron, "instance schedule cron", maximum=MAX_INSTANCE_BINDING_TEXT_LENGTH)
        require_iana_timezone(self.timezone, "instance schedule timezone")
        if type(self.misfire_policy) is not MisfirePolicy:
            raise ValueError("instance schedule misfire policy must use the exact enum")
        if (
            type(self.misfire_grace_seconds) is not int
            or not 0 <= self.misfire_grace_seconds <= MAX_INSTANCE_MISFIRE_GRACE_SECONDS
        ):
            raise ValueError("instance schedule misfire grace must be an integer from 0 to 86400")


@dataclass(frozen=True, slots=True)
class InstanceTriggerBinding:
    """One kind-specific, non-secret deployment trigger binding."""

    kind: TriggerKind
    enabled: bool = True
    event_source: str | None = None
    cron: str | None = None
    timezone: str | None = None
    misfire_policy: MisfirePolicy | None = None
    misfire_grace_seconds: int | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not TriggerKind:
            raise ValueError("instance trigger kind must use the exact enum")
        _require_exact_bool(self.enabled, "instance trigger enabled state")
        if self.event_source is not None and (
            type(self.event_source) is not str
            or _EVENT_SOURCE_PATTERN.fullmatch(self.event_source) is None
        ):
            raise ValueError("instance trigger event source must be a bounded safe identifier")
        if self.cron is not None:
            require_text(
                self.cron,
                "instance trigger cron",
                maximum=MAX_INSTANCE_BINDING_TEXT_LENGTH,
            )
        if self.timezone is not None:
            require_iana_timezone(self.timezone, "instance trigger timezone")
        if self.misfire_policy is not None and type(self.misfire_policy) is not MisfirePolicy:
            raise ValueError("instance trigger misfire policy must use the exact enum")
        if self.misfire_grace_seconds is not None and (
            type(self.misfire_grace_seconds) is not int
            or not 0 <= self.misfire_grace_seconds <= MAX_INSTANCE_MISFIRE_GRACE_SECONDS
        ):
            raise ValueError("instance trigger misfire grace must be an integer from 0 to 86400")

        schedule_values = (
            self.cron,
            self.timezone,
            self.misfire_policy,
            self.misfire_grace_seconds,
        )
        if self.kind is TriggerKind.MANUAL:
            if self.event_source is not None or any(value is not None for value in schedule_values):
                raise ValueError("manual triggers cannot retain source or schedule parameters")
            return
        if self.kind is TriggerKind.WEBHOOK:
            if self.event_source is None or any(value is not None for value in schedule_values):
                raise ValueError("webhook triggers require only one non-secret event source")
            return
        if self.event_source is not None:
            raise ValueError("schedule triggers cannot retain a webhook event source")
        supplied_schedule_values = sum(value is not None for value in schedule_values)
        if self.enabled and supplied_schedule_values != len(schedule_values):
            raise ValueError("enabled schedule triggers require complete schedule parameters")
        if not self.enabled and supplied_schedule_values != 0:
            raise ValueError("disabled schedule triggers cannot retain schedule parameters")

    @property
    def schedule_parameters(self) -> InstanceSchedule | None:
        """Return an exact schedule snapshot when the trigger duplicates those fields."""

        if self.cron is None:
            return None
        assert self.timezone is not None
        assert self.misfire_policy is not None
        assert self.misfire_grace_seconds is not None
        return InstanceSchedule(
            cron=self.cron,
            timezone=self.timezone,
            misfire_policy=self.misfire_policy,
            misfire_grace_seconds=self.misfire_grace_seconds,
        )


@dataclass(frozen=True, slots=True)
class InstanceConnectorBinding:
    """One registered connector binding selected for an authorized family."""

    connector_family: str
    binding_id: str
    enabled: bool = True

    def __post_init__(self) -> None:
        _require_connector_family(self.connector_family, "instance connector family")
        _require_connector_binding_id(self.binding_id, "instance connector binding ID")
        _require_exact_bool(self.enabled, "instance connector enabled state")


@dataclass(frozen=True, slots=True)
class InstanceConfiguration:
    """The complete mutable deployment projection; template fields are absent by design."""

    instance_id: str
    enabled: bool
    variant_label: str | None
    trigger_bindings: tuple[InstanceTriggerBinding, ...]
    connector_bindings: Mapping[str, InstanceConnectorBinding]
    schedule: InstanceSchedule | None
    configuration_revision: int

    def __post_init__(self) -> None:
        require_id(self.instance_id, "configured instance ID")
        _require_exact_bool(self.enabled, "instance enabled state")
        if self.variant_label is not None:
            normalized_variant_label = unicodedata.normalize("NFC", self.variant_label)
            require_text(
                normalized_variant_label,
                "instance variant label",
                maximum=MAX_INSTANCE_VARIANT_LABEL_LENGTH,
            )
            object.__setattr__(self, "variant_label", normalized_variant_label)
        if (
            type(self.trigger_bindings) is not tuple
            or any(type(item) is not InstanceTriggerBinding for item in self.trigger_bindings)
            or len(self.trigger_bindings) > MAX_INSTANCE_TRIGGER_BINDINGS
        ):
            raise ValueError("instance trigger bindings must be one bounded immutable tuple")
        trigger_kinds = tuple(item.kind for item in self.trigger_bindings)
        if len(trigger_kinds) != len(set(trigger_kinds)):
            raise ValueError("instance trigger kinds must be unique")
        if not isinstance(self.connector_bindings, Mapping) or (
            len(self.connector_bindings) > MAX_INSTANCE_CONNECTOR_BINDINGS
        ):
            raise ValueError("instance connector bindings must be one bounded mapping")
        normalized_bindings: dict[str, InstanceConnectorBinding] = {}
        for family, binding in self.connector_bindings.items():
            _require_connector_family(family, "instance connector binding key")
            if type(binding) is not InstanceConnectorBinding:
                raise ValueError("instance connector bindings must use exact immutable values")
            if family != binding.connector_family:
                raise ValueError("connector binding keys must exactly match connector families")
            normalized_bindings[family] = binding
        object.__setattr__(
            self,
            "connector_bindings",
            MappingProxyType(dict(sorted(normalized_bindings.items()))),
        )
        if self.schedule is not None and type(self.schedule) is not InstanceSchedule:
            raise ValueError("instance schedule must use the exact immutable value")
        if type(self.configuration_revision) is not int or self.configuration_revision < 1:
            raise ValueError("instance configuration revision must be a positive integer")

        schedule_triggers = tuple(
            item for item in self.trigger_bindings if item.kind is TriggerKind.SCHEDULE
        )
        enabled_schedule = len(schedule_triggers) == 1 and schedule_triggers[0].enabled
        if (self.schedule is not None) is not enabled_schedule:
            raise ValueError(
                "schedule configuration and one enabled schedule trigger must appear together"
            )
        if enabled_schedule:
            trigger_schedule = schedule_triggers[0].schedule_parameters
            if trigger_schedule != self.schedule:
                raise ValueError("schedule trigger parameters must exactly match the schedule")

    def with_revision(self, configuration_revision: int) -> Self:
        """Copy this complete projection with one caller-selected revision."""

        return type(self)(
            instance_id=self.instance_id,
            enabled=self.enabled,
            variant_label=self.variant_label,
            trigger_bindings=self.trigger_bindings,
            connector_bindings=self.connector_bindings,
            schedule=self.schedule,
            configuration_revision=configuration_revision,
        )


@dataclass(frozen=True, slots=True)
class PatchValue[ValueT]:
    """Explicitly distinguish an omitted PATCH field from a supplied null value."""

    provided: bool
    value: ValueT | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.provided) is not bool:
            raise ValueError("patch presence must be an exact boolean")
        if not self.provided and self.value is not None:
            raise ValueError("an omitted patch field cannot retain a value")

    @classmethod
    def omitted(cls) -> PatchValue[ValueT]:
        return cls(provided=False)

    @classmethod
    def of(cls, value: ValueT) -> PatchValue[ValueT]:
        return cls(provided=True, value=value)


@dataclass(frozen=True, slots=True)
class InstanceConfigurationPatch:
    """Typed partial replacement with explicit omitted-versus-null semantics."""

    enabled: PatchValue[bool] = field(default_factory=PatchValue.omitted)
    variant_label: PatchValue[str | None] = field(default_factory=PatchValue.omitted)
    trigger_bindings: PatchValue[tuple[InstanceTriggerBinding, ...]] = field(
        default_factory=PatchValue.omitted
    )
    connector_bindings: PatchValue[Mapping[str, InstanceConnectorBinding]] = field(
        default_factory=PatchValue.omitted
    )
    schedule: PatchValue[InstanceSchedule | None] = field(default_factory=PatchValue.omitted)

    def __post_init__(self) -> None:
        fields = (
            self.enabled,
            self.variant_label,
            self.trigger_bindings,
            self.connector_bindings,
            self.schedule,
        )
        if any(type(item) is not PatchValue for item in fields):
            raise ValueError("configuration patch fields must use exact patch values")
        if self.enabled.provided and type(self.enabled.value) is not bool:
            raise ValueError("a supplied enabled state cannot be null")
        if self.variant_label.provided and self.variant_label.value is not None:
            require_text(
                self.variant_label.value,
                "instance variant label",
                maximum=MAX_INSTANCE_VARIANT_LABEL_LENGTH,
            )
        if self.trigger_bindings.provided:
            triggers = self.trigger_bindings.value
            if type(triggers) is not tuple or any(
                type(item) is not InstanceTriggerBinding for item in triggers
            ):
                raise ValueError("supplied trigger bindings cannot be null or mutable")
        if self.connector_bindings.provided:
            bindings = self.connector_bindings.value
            if not isinstance(bindings, Mapping):
                raise ValueError("supplied connector bindings cannot be null")
            normalized: dict[str, InstanceConnectorBinding] = {}
            for family, binding in bindings.items():
                if type(family) is not str or type(binding) is not InstanceConnectorBinding:
                    raise ValueError("supplied connector bindings must use exact values")
                normalized[family] = binding
            object.__setattr__(
                self,
                "connector_bindings",
                PatchValue.of(MappingProxyType(dict(sorted(normalized.items())))),
            )
        if (
            self.schedule.provided
            and self.schedule.value is not None
            and type(self.schedule.value) is not InstanceSchedule
        ):
            raise ValueError("supplied schedule must use the exact immutable value")

    @property
    def is_empty(self) -> bool:
        return not any(
            field_value.provided
            for field_value in (
                self.enabled,
                self.variant_label,
                self.trigger_bindings,
                self.connector_bindings,
                self.schedule,
            )
        )

    def apply(self, current: InstanceConfiguration) -> InstanceConfiguration:
        """Apply replacements without changing the current optimistic revision."""

        if type(current) is not InstanceConfiguration:
            raise ValueError("configuration patch requires an exact current projection")
        enabled = self.enabled.value if self.enabled.provided else current.enabled
        triggers = (
            self.trigger_bindings.value
            if self.trigger_bindings.provided
            else current.trigger_bindings
        )
        connectors = (
            self.connector_bindings.value
            if self.connector_bindings.provided
            else current.connector_bindings
        )
        if type(enabled) is not bool or triggers is None or connectors is None:
            raise ValueError("required configuration patch fields cannot be null")
        return InstanceConfiguration(
            instance_id=current.instance_id,
            enabled=enabled,
            variant_label=(
                self.variant_label.value if self.variant_label.provided else current.variant_label
            ),
            trigger_bindings=triggers,
            connector_bindings=connectors,
            schedule=self.schedule.value if self.schedule.provided else current.schedule,
            configuration_revision=current.configuration_revision,
        )


def configuration_to_plain_mapping(configuration: InstanceConfiguration) -> dict[str, Any]:
    """Return the exact canonical-JSON-safe five-field audit/persistence snapshot."""

    if type(configuration) is not InstanceConfiguration:
        raise ValueError("configuration serialization requires the exact domain projection")
    return {
        "enabled": configuration.enabled,
        "variant_label": configuration.variant_label,
        "trigger_bindings": [
            {
                "type": binding.kind.value,
                "enabled": binding.enabled,
                "event_source": binding.event_source,
                "cron": binding.cron,
                "timezone": binding.timezone,
                "misfire_policy": (
                    binding.misfire_policy.value if binding.misfire_policy is not None else None
                ),
                "misfire_grace_seconds": binding.misfire_grace_seconds,
            }
            for binding in configuration.trigger_bindings
        ],
        "connector_bindings": {
            family: {
                "connector_family": binding.connector_family,
                "binding_id": binding.binding_id,
                "enabled": binding.enabled,
            }
            for family, binding in configuration.connector_bindings.items()
        },
        "schedule": (
            None
            if configuration.schedule is None
            else {
                "cron": configuration.schedule.cron,
                "timezone": configuration.schedule.timezone,
                "misfire_policy": configuration.schedule.misfire_policy.value,
                "misfire_grace_seconds": configuration.schedule.misfire_grace_seconds,
            }
        ),
    }
