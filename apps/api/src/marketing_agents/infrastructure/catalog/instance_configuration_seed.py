"""Insert-only seeding of mutable instance configuration from a compiled catalog."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from marketing_agents.application.ports.instance_configuration import (
    InstanceConfigurationConstraints,
    InstanceConfigurationUnitOfWorkFactory,
)
from marketing_agents.application.ports.recurrence import (
    RecurrenceCalculationError,
    RecurrenceCalculator,
)
from marketing_agents.domain.enums import MisfirePolicy, TriggerKind
from marketing_agents.domain.instance_configuration import (
    InstanceConfiguration,
    InstanceConnectorBinding,
    InstanceSchedule,
    InstanceTriggerBinding,
)
from marketing_agents.domain.validation import require_utc
from marketing_agents.infrastructure.catalog.models import (
    MARKETING_AGENTS_V1_CONTRACT,
    AgentInstanceRecord,
    CompiledCatalog,
    TriggerBinding,
)
from marketing_agents.infrastructure.instance_configuration_constraints import (
    CompiledCatalogInstanceConfigurationConstraintProvider,
    InstanceConfigurationConstraintError,
    validate_mock_connector_bindings,
)

_CATALOG_HASH_PATTERN = re.compile(r"^catalog-sha256-v1:[a-f0-9]{64}$")
_RECURRENCE_VALIDATION_BOUNDARY = datetime(2000, 1, 1, tzinfo=UTC)


class InstanceConfigurationSeedError(RuntimeError):
    """The compiled catalog cannot be inserted without overwriting local configuration."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InstanceConfigurationSeedResult:
    inserted: int
    preserved: int
    total: int
    catalog_content_hash: str

    def __post_init__(self) -> None:
        if (
            type(self.inserted) is not int
            or type(self.preserved) is not int
            or type(self.total) is not int
            or self.inserted < 0
            or self.preserved < 0
            or self.total < 1
            or self.inserted + self.preserved != self.total
        ):
            raise ValueError("instance configuration seed counts are inconsistent")
        if (
            type(self.catalog_content_hash) is not str
            or _CATALOG_HASH_PATTERN.fullmatch(self.catalog_content_hash) is None
        ):
            raise ValueError(
                "instance configuration seed hash must be one versioned SHA-256 digest"
            )


def _trigger_from_catalog(binding: TriggerBinding) -> InstanceTriggerBinding:
    try:
        trigger_type = binding.type
        misfire_policy = binding.misfire_policy
        return InstanceTriggerBinding(
            kind=TriggerKind(trigger_type),
            enabled=binding.enabled,
            event_source=binding.event_source,
            cron=binding.cron,
            timezone=binding.timezone,
            misfire_policy=(None if misfire_policy is None else MisfirePolicy(misfire_policy)),
            misfire_grace_seconds=binding.misfire_grace_seconds,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise InstanceConfigurationSeedError(
            "catalog_instance_configuration_invalid",
            "compiled catalog trigger configuration is invalid",
        ) from exc


def _configuration_from_catalog(instance: AgentInstanceRecord) -> InstanceConfiguration:
    if type(instance) is not AgentInstanceRecord:
        raise InstanceConfigurationSeedError(
            "catalog_instance_configuration_invalid",
            "configuration seed requires exact compiled catalog instance records",
        )
    try:
        connectors = {
            family: InstanceConnectorBinding(
                connector_family=binding.connector_family,
                binding_id=binding.binding_id,
                enabled=binding.enabled,
            )
            for family, binding in sorted(instance.connector_bindings.items())
        }
        schedule = (
            None
            if instance.schedule is None
            else InstanceSchedule(
                cron=instance.schedule.cron,
                timezone=instance.schedule.timezone,
                misfire_policy=MisfirePolicy(instance.schedule.misfire_policy),
                misfire_grace_seconds=instance.schedule.misfire_grace_seconds,
            )
        )
        return InstanceConfiguration(
            instance_id=instance.id,
            enabled=instance.enabled,
            variant_label=None if instance.variant is None else instance.variant.variant_label,
            trigger_bindings=tuple(
                _trigger_from_catalog(binding) for binding in instance.trigger_bindings
            ),
            connector_bindings=connectors,
            schedule=schedule,
            configuration_revision=instance.configuration_revision,
        )
    except InstanceConfigurationSeedError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise InstanceConfigurationSeedError(
            "catalog_instance_configuration_invalid",
            "compiled catalog instance configuration is invalid",
        ) from exc


def _validate_against_constraints(
    catalog: CompiledCatalog,
    configuration: InstanceConfiguration,
    constraints: InstanceConfigurationConstraints,
    recurrence: RecurrenceCalculator,
) -> None:
    if configuration.instance_id != constraints.instance_id:
        raise InstanceConfigurationSeedError(
            "seed_instance_identity_mismatch",
            "persisted configuration identity does not match the compiled catalog",
        )
    configured_kinds = {binding.kind for binding in configuration.trigger_bindings}
    if not configured_kinds.issubset(constraints.supported_trigger_kinds):
        raise InstanceConfigurationSeedError(
            "seed_trigger_unsupported",
            "persisted configuration uses a trigger unsupported by its template",
        )
    try:
        validate_mock_connector_bindings(
            catalog,
            configuration.instance_id,
            configuration.connector_bindings,
        )
    except InstanceConfigurationConstraintError as exc:
        raise InstanceConfigurationSeedError(exc.code, str(exc)) from exc
    _validate_recurrence(configuration, recurrence)


def _validate_recurrence(
    configuration: InstanceConfiguration,
    recurrence: RecurrenceCalculator,
) -> None:
    if configuration.schedule is None:
        return
    try:
        next_run = recurrence.next_after(
            cron=configuration.schedule.cron,
            timezone=configuration.schedule.timezone,
            after_utc=_RECURRENCE_VALIDATION_BOUNDARY,
        )
        require_utc(next_run, "seed recurrence result")
        if next_run <= _RECURRENCE_VALIDATION_BOUNDARY:
            raise ValueError("seed recurrence must advance its validation boundary")
    except RecurrenceCalculationError as exc:
        raise InstanceConfigurationSeedError(
            "catalog_instance_schedule_invalid",
            "compiled catalog schedule expression is invalid",
        ) from exc
    except Exception as exc:
        raise InstanceConfigurationSeedError(
            "catalog_instance_schedule_invalid",
            "compiled catalog recurrence calculator contract failed",
        ) from exc


def catalog_instance_configuration_defaults(
    catalog: CompiledCatalog,
    recurrence: RecurrenceCalculator,
) -> tuple[InstanceConfiguration, ...]:
    """Build all defaults before any transaction is opened."""

    expected = MARKETING_AGENTS_V1_CONTRACT.instances
    if type(catalog) is not CompiledCatalog or expected != 43 or len(catalog.instances) != expected:
        raise InstanceConfigurationSeedError(
            "catalog_instance_count_invalid",
            "configuration seed requires the exact 43-instance compiled catalog",
        )
    if (
        type(catalog.content_hash) is not str
        or _CATALOG_HASH_PATTERN.fullmatch(catalog.content_hash) is None
    ):
        raise InstanceConfigurationSeedError(
            "catalog_content_hash_invalid",
            "configuration seed requires one versioned compiled catalog hash",
        )
    instance_ids = tuple(instance.id for instance in catalog.instances)
    if len(instance_ids) != len(set(instance_ids)):
        raise InstanceConfigurationSeedError(
            "catalog_instance_identity_invalid",
            "compiled catalog instance identities must be unique",
        )
    try:
        defaults = tuple(_configuration_from_catalog(instance) for instance in catalog.instances)
        for configuration in defaults:
            validate_mock_connector_bindings(
                catalog,
                configuration.instance_id,
                configuration.connector_bindings,
            )
            _validate_recurrence(configuration, recurrence)
    except InstanceConfigurationConstraintError as exc:
        raise InstanceConfigurationSeedError(exc.code, str(exc)) from exc
    return tuple(sorted(defaults, key=lambda item: item.instance_id))


async def seed_instance_configurations(
    catalog: CompiledCatalog,
    unit_of_work_factory: InstanceConfigurationUnitOfWorkFactory,
    recurrence: RecurrenceCalculator,
) -> InstanceConfigurationSeedResult:
    """Insert missing defaults atomically while preserving every existing local override."""

    defaults = catalog_instance_configuration_defaults(catalog, recurrence)
    expected_by_id = {configuration.instance_id: configuration for configuration in defaults}
    constraint_provider = CompiledCatalogInstanceConfigurationConstraintProvider(catalog)
    try:
        async with unit_of_work_factory() as unit_of_work:
            existing = await unit_of_work.configurations.list_all()
            if type(existing) is not tuple or any(
                type(configuration) is not InstanceConfiguration for configuration in existing
            ):
                raise InstanceConfigurationSeedError(
                    "seed_repository_invalid",
                    "configuration repository returned an invalid snapshot",
                )
            existing_by_id = {
                configuration.instance_id: configuration for configuration in existing
            }
            if len(existing_by_id) != len(existing) or not set(existing_by_id).issubset(
                expected_by_id
            ):
                raise InstanceConfigurationSeedError(
                    "seed_instance_identity_mismatch",
                    "persisted configuration contains an identity outside the compiled catalog",
                )
            for configuration in existing:
                constraints = await constraint_provider.get(configuration.instance_id)
                if constraints is None:
                    raise InstanceConfigurationSeedError(
                        "seed_instance_identity_mismatch",
                        "persisted configuration identity is absent from the compiled catalog",
                    )
                _validate_against_constraints(
                    catalog,
                    configuration,
                    constraints,
                    recurrence,
                )

            inserted = 0
            for configuration in defaults:
                if configuration.instance_id in existing_by_id:
                    continue
                was_inserted = await unit_of_work.configurations.insert_missing(configuration)
                if type(was_inserted) is not bool:
                    raise InstanceConfigurationSeedError(
                        "seed_repository_invalid",
                        "configuration repository returned an invalid insert result",
                    )
                inserted += int(was_inserted)

            verified = await unit_of_work.configurations.list_all()
            if type(verified) is not tuple or any(
                type(configuration) is not InstanceConfiguration for configuration in verified
            ):
                raise InstanceConfigurationSeedError(
                    "seed_repository_invalid",
                    "configuration repository returned an invalid verification snapshot",
                )
            verified_ids = tuple(configuration.instance_id for configuration in verified)
            if verified_ids != tuple(sorted(expected_by_id)):
                raise InstanceConfigurationSeedError(
                    "seed_projection_incomplete",
                    "configuration seed did not produce the exact compiled instance projection",
                )
            for configuration in verified:
                constraints = await constraint_provider.get(configuration.instance_id)
                if constraints is None:
                    raise InstanceConfigurationSeedError(
                        "seed_instance_identity_mismatch",
                        "verified configuration identity is absent from the compiled catalog",
                    )
                _validate_against_constraints(
                    catalog,
                    configuration,
                    constraints,
                    recurrence,
                )
            await unit_of_work.commit()
    except InstanceConfigurationSeedError:
        raise
    except Exception as exc:
        raise InstanceConfigurationSeedError(
            "seed_persistence_failed",
            "instance configuration seed transaction failed",
        ) from exc

    return InstanceConfigurationSeedResult(
        inserted=inserted,
        preserved=len(defaults) - inserted,
        total=len(defaults),
        catalog_content_hash=catalog.content_hash,
    )
