"""API-03: durable, reseed-safe, optimistic, audited instance configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationService,
    InstanceConfigurationServiceError,
    UpdateInstanceConfigurationCommand,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.instance_configuration import (
    InstanceConfigurationPatch,
    InstanceConnectorBinding,
    PatchValue,
)
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.instance_configuration_seed import (
    InstanceConfigurationSeedError,
    seed_instance_configurations,
)
from marketing_agents.infrastructure.catalog.models import ScheduleBinding, TriggerBinding
from marketing_agents.infrastructure.db import (
    AgentInstanceConfigurationRecord,
    AuditEventRecord,
    Base,
    DatabaseRuntime,
    InstanceConfigurationSQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.instance_configuration_constraints import (
    CompiledCatalogInstanceConfigurationConstraintProvider,
    LocalMockRegisteredBindingProvider,
)
from marketing_agents.infrastructure.scheduling.cron_recurrence import (
    CroniterRecurrenceCalculator,
)
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import func, select, update

from tests.support.identity import human_principal, service_principal

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
TARGET_INSTANCE_ID = "inst.social-media.new-content.linkedin-comment-replier.01"


@dataclass(frozen=True, slots=True)
class FixedClock:
    current: datetime = NOW

    def now(self) -> datetime:
        return self.current


def _factory(runtime: DatabaseRuntime) -> InstanceConfigurationSQLAlchemyUnitOfWorkFactory:
    return InstanceConfigurationSQLAlchemyUnitOfWorkFactory(runtime.session_factory)


def _service(
    runtime: DatabaseRuntime,
    catalog: Any,
    *,
    unit_of_work_factory: Any | None = None,
) -> InstanceConfigurationService:
    return InstanceConfigurationService(
        unit_of_work_factory=unit_of_work_factory or _factory(runtime),
        constraints=CompiledCatalogInstanceConfigurationConstraintProvider(catalog),
        registered_bindings=LocalMockRegisteredBindingProvider(),
        recurrence=CroniterRecurrenceCalculator(),
        clock=FixedClock(),
        audit_pseudonym_key=DigestKey(bytes(range(32))),
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


def _admin():
    return human_principal(
        actor_id="principal.test.local-admin",
        roles=frozenset({"local_admin"}),
        scopes=frozenset({"configuration:write"}),
    )


def _variant_patch(value: str | None) -> InstanceConfigurationPatch:
    return InstanceConfigurationPatch(variant_label=PatchValue.of(value))


class _FaultAfterAuditAppend:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def append_global(self, event: Any) -> Any:
        await self._delegate.append_global(event)
        raise RuntimeError("injected audit persistence failure")


class _FaultingUnitOfWork:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    @property
    def configurations(self) -> Any:
        return self._delegate.configurations

    @property
    def audits(self) -> _FaultAfterAuditAppend:
        return _FaultAfterAuditAppend(self._delegate.audits)

    async def __aenter__(self) -> _FaultingUnitOfWork:
        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._delegate.__aexit__(*args)

    async def commit(self) -> None:
        await self._delegate.commit()

    async def rollback(self) -> None:
        await self._delegate.rollback()


class _FaultingUnitOfWorkFactory:
    def __init__(self, delegate: InstanceConfigurationSQLAlchemyUnitOfWorkFactory) -> None:
        self._delegate = delegate

    def __call__(self) -> _FaultingUnitOfWork:
        return _FaultingUnitOfWork(self._delegate())


class _UnexpectedUnitOfWorkFactory:
    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> None:
        self.called = True
        raise AssertionError("invalid catalog must fail before opening a transaction")


@pytest.mark.asyncio
async def test_api_03_seed_rejects_invalid_recurrence_before_transaction() -> None:
    catalog = compile_catalog(CATALOG_ROOT)
    templates = {item.id: item for item in catalog.templates}
    scheduled = next(
        item
        for item in catalog.instances
        if "schedule" in templates[item.template_id].supported_trigger_types
    )
    invalid_cron = "not-a-cron"
    invalid_instance = scheduled.model_copy(
        update={
            "schedule": ScheduleBinding(
                cron=invalid_cron,
                timezone="UTC",
                misfire_policy="skip",
                misfire_grace_seconds=0,
            ),
            "trigger_bindings": (
                *scheduled.trigger_bindings,
                TriggerBinding(
                    type="schedule",
                    enabled=True,
                    cron=invalid_cron,
                    timezone="UTC",
                    misfire_policy="skip",
                    misfire_grace_seconds=0,
                ),
            ),
        }
    )
    invalid_catalog = replace(
        catalog,
        instances=tuple(
            invalid_instance if item.id == scheduled.id else item for item in catalog.instances
        ),
    )
    factory = _UnexpectedUnitOfWorkFactory()

    with pytest.raises(InstanceConfigurationSeedError) as captured:
        await seed_instance_configurations(
            invalid_catalog,
            factory,
            CroniterRecurrenceCalculator(),
        )
    assert captured.value.code == "catalog_instance_schedule_invalid"
    assert factory.called is False


@pytest.mark.asyncio
async def test_api_03_seed_restart_and_reseed_preserve_operator_override(
    tmp_path: Path,
) -> None:
    catalog = compile_catalog(CATALOG_ROOT)
    database_path = tmp_path / "instance-config-restart.db"
    first_runtime = await _runtime(database_path)
    admin = _admin()
    try:
        first_seed = await seed_instance_configurations(
            catalog,
            _factory(first_runtime),
            CroniterRecurrenceCalculator(),
        )
        assert first_seed.inserted == 43
        assert first_seed.preserved == 0
        assert first_seed.total == 43
        assert first_seed.catalog_content_hash == catalog.content_hash

        service = _service(first_runtime, catalog)
        initial = await service.read(TARGET_INSTANCE_ID, principal=admin)
        assert initial.configuration_revision == 1
        changed = await service.update(
            UpdateInstanceConfigurationCommand(
                instance_id=TARGET_INSTANCE_ID,
                expected_revision=1,
                patch=_variant_patch("Operator override"),
                correlation_id="correlation.api-03.restart-update",
            ),
            principal=admin,
        )
        assert changed.changed is True
        assert changed.configuration.variant_label == "Operator override"
        assert changed.configuration.configuration_revision == 2
    finally:
        await first_runtime.dispose()

    restarted_runtime = create_database_runtime(f"sqlite+aiosqlite:///{database_path}")
    try:
        restarted_service = _service(restarted_runtime, catalog)
        restored = await restarted_service.read(TARGET_INSTANCE_ID, principal=admin)
        assert restored.variant_label == "Operator override"
        assert restored.configuration_revision == 2

        second_seed = await seed_instance_configurations(
            catalog,
            _factory(restarted_runtime),
            CroniterRecurrenceCalculator(),
        )
        assert second_seed.inserted == 0
        assert second_seed.preserved == 43
        preserved = await restarted_service.read(TARGET_INSTANCE_ID, principal=admin)
        assert preserved == restored

        snapshot = await restarted_service.read_all(principal=admin)
        assert len(snapshot.configurations) == 43
        assert tuple(item.instance_id for item in snapshot.configurations) == tuple(
            sorted(item.id for item in catalog.instances)
        )
        assert len(snapshot.version) == 64
    finally:
        await restarted_runtime.dispose()


@pytest.mark.asyncio
async def test_api_03_update_is_exact_plus_one_atomic_and_stale_safe(
    tmp_path: Path,
) -> None:
    catalog = compile_catalog(CATALOG_ROOT)
    runtime = await _runtime(tmp_path / "instance-config-audit.db")
    admin = _admin()
    try:
        await seed_instance_configurations(
            catalog,
            _factory(runtime),
            CroniterRecurrenceCalculator(),
        )
        service = _service(runtime, catalog)
        updated = await service.update(
            UpdateInstanceConfigurationCommand(
                instance_id=TARGET_INSTANCE_ID,
                expected_revision=1,
                patch=_variant_patch("ops@example.com"),
                correlation_id="correlation.api-03.audited-update",
            ),
            principal=admin,
        )
        assert updated.configuration.configuration_revision == 2
        assert updated.configuration.variant_label == "ops@example.com"

        with pytest.raises(InstanceConfigurationServiceError) as stale:
            await service.update(
                UpdateInstanceConfigurationCommand(
                    instance_id=TARGET_INSTANCE_ID,
                    expected_revision=1,
                    patch=_variant_patch("Lost update"),
                    correlation_id="correlation.api-03.stale-update",
                ),
                principal=admin,
            )
        assert stale.value.code == "configuration_revision_conflict"
        assert stale.value.current_revision == 2

        unchanged = await service.update(
            UpdateInstanceConfigurationCommand(
                instance_id=TARGET_INSTANCE_ID,
                expected_revision=2,
                patch=_variant_patch("ops@example.com"),
                correlation_id="correlation.api-03.no-op",
            ),
            principal=admin,
        )
        assert unchanged.changed is False
        assert unchanged.configuration.configuration_revision == 2

        async with runtime.session_factory() as session:
            audits = tuple(
                (
                    await session.execute(
                        select(AuditEventRecord).where(
                            AuditEventRecord.aggregate_type == "agent_instance_configuration"
                        )
                    )
                ).scalars()
            )
        assert len(audits) == 1
        audit = audits[0]
        assert audit.event_type == "instance.configuration_changed"
        assert audit.aggregate_id == TARGET_INSTANCE_ID
        assert audit.expected_version == 1
        assert audit.observed_version == 1
        assert audit.mutation_version == 2
        assert audit.actor_source == "user"
        assert audit.actor_id.startswith("audit-actor-v1:")
        assert audit.actor_id != admin.actor_id
        assert audit.run_id is None
        assert audit.safe_metadata["previous_configuration"]["variant_label"] is None
        audited_label = audit.safe_metadata["new_configuration"]["variant_label"]
        assert audited_label.startswith("audit-value-hmac-sha256-v1:")
        assert audited_label != "ops@example.com"
        assert "ops@example.com" not in str(audit.safe_metadata)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_03_audit_failure_rolls_back_configuration_and_audit(
    tmp_path: Path,
) -> None:
    catalog = compile_catalog(CATALOG_ROOT)
    runtime = await _runtime(tmp_path / "instance-config-rollback.db")
    admin = _admin()
    try:
        factory = _factory(runtime)
        await seed_instance_configurations(
            catalog,
            factory,
            CroniterRecurrenceCalculator(),
        )
        failing_service = _service(
            runtime,
            catalog,
            unit_of_work_factory=_FaultingUnitOfWorkFactory(factory),
        )
        with pytest.raises(InstanceConfigurationServiceError) as failure:
            await failing_service.update(
                UpdateInstanceConfigurationCommand(
                    instance_id=TARGET_INSTANCE_ID,
                    expected_revision=1,
                    patch=_variant_patch("Must roll back"),
                    correlation_id="correlation.api-03.audit-failure",
                ),
                principal=admin,
            )
        assert failure.value.code == "configuration_unavailable"

        current = await _service(runtime, catalog).read(
            TARGET_INSTANCE_ID,
            principal=admin,
        )
        assert current.configuration_revision == 1
        assert current.variant_label is None
        async with runtime.session_factory() as session:
            audit_count = (
                await session.execute(
                    select(func.count())
                    .select_from(AuditEventRecord)
                    .where(AuditEventRecord.aggregate_type == "agent_instance_configuration")
                )
            ).scalar_one()
        assert audit_count == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_03_rejects_unregistered_binding_and_tampered_storage(
    tmp_path: Path,
) -> None:
    catalog = compile_catalog(CATALOG_ROOT)
    runtime = await _runtime(tmp_path / "instance-config-integrity.db")
    admin = _admin()
    try:
        await seed_instance_configurations(
            catalog,
            _factory(runtime),
            CroniterRecurrenceCalculator(),
        )
        service = _service(runtime, catalog)
        invalid_binding = InstanceConnectorBinding(
            connector_family="social",
            binding_id="mock.social.unregistered",
        )
        with pytest.raises(InstanceConfigurationServiceError) as invalid:
            await service.update(
                UpdateInstanceConfigurationCommand(
                    instance_id=TARGET_INSTANCE_ID,
                    expected_revision=1,
                    patch=InstanceConfigurationPatch(
                        connector_bindings=PatchValue.of({"social": invalid_binding})
                    ),
                    correlation_id="correlation.api-03.invalid-binding",
                ),
                principal=admin,
            )
        assert invalid.value.code == "configuration_invalid"

        async with runtime.engine.begin() as connection:
            await connection.execute(
                update(AgentInstanceConfigurationRecord)
                .where(AgentInstanceConfigurationRecord.instance_id == TARGET_INSTANCE_ID)
                .values(variant_label="database tamper")
            )
        with pytest.raises(InstanceConfigurationServiceError) as tampered:
            await service.read(TARGET_INSTANCE_ID, principal=admin)
        assert tampered.value.code == "configuration_unavailable"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_03_service_defense_allows_viewers_and_requires_human_local_admin(
    tmp_path: Path,
) -> None:
    catalog = compile_catalog(CATALOG_ROOT)
    runtime = await _runtime(tmp_path / "instance-config-authorization.db")
    viewer = human_principal(
        actor_id="principal.test.viewer",
        roles=frozenset({"viewer"}),
        scopes=frozenset({"catalog:read"}),
    )
    machine = service_principal(
        roles=frozenset({"local_admin"}),
        scopes=frozenset({"configuration:write"}),
    )
    try:
        await seed_instance_configurations(
            catalog,
            _factory(runtime),
            CroniterRecurrenceCalculator(),
        )
        service = _service(runtime, catalog)
        assert (await service.read(TARGET_INSTANCE_ID, principal=viewer)).instance_id == (
            TARGET_INSTANCE_ID
        )
        assert (await service.schema(TARGET_INSTANCE_ID, principal=viewer)).instance_id == (
            TARGET_INSTANCE_ID
        )
        assert len((await service.read_all(principal=viewer)).configurations) == 43

        denied_command = UpdateInstanceConfigurationCommand(
            instance_id="inst.unknown.not-enumerated.value.01",
            expected_revision=1,
            patch=InstanceConfigurationPatch(enabled=PatchValue.of(False)),
            correlation_id="correlation.api-03.denied-update",
        )
        with pytest.raises(InstanceConfigurationServiceError) as denied_viewer:
            await service.update(denied_command, principal=viewer)
        assert denied_viewer.value.code == "configuration_admin_role_missing"

        with pytest.raises(InstanceConfigurationServiceError) as denied_machine:
            await service.update(denied_command, principal=machine)
        assert denied_machine.value.code == "configuration_human_required"
        with pytest.raises(InstanceConfigurationServiceError) as denied_machine_read:
            await service.read(TARGET_INSTANCE_ID, principal=machine)
        assert denied_machine_read.value.code == "configuration_read_forbidden"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_03_configuration_schema_matches_patch_defaults_and_template_support(
    tmp_path: Path,
) -> None:
    catalog = compile_catalog(CATALOG_ROOT)
    runtime = await _runtime(tmp_path / "instance-config-schema.db")
    viewer = human_principal(
        actor_id="principal.test.viewer",
        roles=frozenset({"viewer"}),
        scopes=frozenset({"catalog:read"}),
    )
    templates = {item.id: item for item in catalog.templates}
    without_schedule = next(
        item
        for item in catalog.instances
        if "schedule" not in templates[item.template_id].supported_trigger_types
    )
    with_schedule = next(
        item
        for item in catalog.instances
        if "schedule" in templates[item.template_id].supported_trigger_types
    )
    try:
        service = _service(runtime, catalog)
        unsupported = json.loads(
            canonical_json_bytes(
                (await service.schema(without_schedule.id, principal=viewer)).configuration_schema
            )
        )
        supported = json.loads(
            canonical_json_bytes(
                (await service.schema(with_schedule.id, principal=viewer)).configuration_schema
            )
        )
        Draft202012Validator.check_schema(unsupported)
        Draft202012Validator.check_schema(supported)
        assert supported["description"].startswith("Structural deployment PATCH schema")

        unsupported_validator = Draft202012Validator(unsupported)
        assert unsupported_validator.is_valid({"triggerBindings": [{"type": "manual"}]})
        assert unsupported_validator.is_valid({"schedule": None})
        assert not unsupported_validator.is_valid(
            {
                "schedule": {
                    "cron": "0 9 * * 1-5",
                    "timezone": "UTC",
                    "misfirePolicy": "skip",
                    "misfireGraceSeconds": 0,
                }
            }
        )
        assert "oneOf" in supported["properties"]["schedule"]

        connector_schema: dict[str, Any] | None = None
        for instance in catalog.instances:
            schema = json.loads(
                canonical_json_bytes(
                    (await service.schema(instance.id, principal=viewer)).configuration_schema
                )
            )
            connector_properties = schema["properties"]["connectorBindings"]["properties"]
            if connector_properties:
                connector_schema = next(iter(connector_properties.values()))
                break
        assert connector_schema is not None
        assert "enabled" not in connector_schema["required"]
        assert connector_schema["properties"]["enabled"]["default"] is True
    finally:
        await runtime.dispose()
