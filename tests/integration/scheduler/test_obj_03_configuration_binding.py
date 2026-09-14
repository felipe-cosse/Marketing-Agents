"""OBJ-03: explicit scheduled input and configuration/schedule atomicity."""

from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.services.incoming_work_validation import (
    ConfiguredIncomingTrigger,
    IncomingWorkValidator,
)
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationService,
    InstanceConfigurationServiceError,
    UpdateInstanceConfigurationCommand,
)
from marketing_agents.application.services.schedule_claiming import ScheduleClaimService
from marketing_agents.application.services.schedule_configuration import (
    ScheduleConfigurationError,
    configured_schedule_id,
    verify_scheduled_input,
)
from marketing_agents.application.services.schedule_occurrence_ingress import (
    ScheduleOccurrenceCommand,
)
from marketing_agents.application.services.schedule_processing import (
    ScheduleClaimProcessingError,
    ScheduleClaimProcessingService,
)
from marketing_agents.domain.entities import Schedule
from marketing_agents.domain.enums import MisfirePolicy, TriggerKind, WorkMode
from marketing_agents.domain.instance_configuration import (
    InstanceConfigurationPatch,
    InstanceSchedule,
    InstanceTriggerBinding,
    PatchValue,
    ScheduledInput,
)
from marketing_agents.domain.schedule_occurrence_identity import SCHEDULE_RECURRENCE_VERSION
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.instance_configuration_seed import (
    catalog_instance_configuration_defaults,
    seed_instance_configurations,
)
from marketing_agents.infrastructure.db import (
    AuditEventRecord,
    ScheduleOccurrenceRecord,
    ScheduleRecord,
    SQLAlchemyAuditRepository,
    SQLAlchemyInstanceConfigurationRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyScheduleRepository,
    SQLAlchemyUnitOfWorkFactory,
    SQLAlchemyWorkRepository,
    WorkItemRecord,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.migrations import upgrade_database
from marketing_agents.infrastructure.db.repositories.instance_configuration import (
    _to_record as config_record,
)
from marketing_agents.infrastructure.db.repositories.schedule import _to_record as schedule_record
from marketing_agents.infrastructure.executable_workflows import build_executable_workflow_registry
from marketing_agents.infrastructure.instance_configuration_constraints import (
    CompiledCatalogInstanceConfigurationConstraintProvider,
    LocalMockRegisteredBindingProvider,
)
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import MetaData, Table, func, select

from tests.integration.api.test_api_03_instance_configuration import _app, _request
from tests.integration.db.test_api_03_instance_configuration_persistence import (
    CATALOG_ROOT,
    NOW,
    _admin,
    _factory,
    _runtime,
)
from tests.integration.scheduler.test_sched_05_occurrence_transaction import (
    IncrementingIds,
    MutableClock,
    _guard,
)
from tests.support.catalog_persistence import seed_catalog_parents
from tests.support.identity import human_principal, service_principal

TARGET = "inst.community.education.course-progress-reminders.01"
KEY = DigestKey(bytes(range(32)))
PAYLOAD = {
    "request_id": "request.obj03.schedule",
    "source_content": "Operator private-canary notes.",
}


@pytest.fixture
async def installation(tmp_path: Path):
    catalog = compile_catalog(CATALOG_ROOT)
    database = await _runtime(tmp_path / "scheduled.db")
    factory = _factory(database)
    recurrence = CroniterRecurrenceCalculator()
    await seed_instance_configurations(catalog, factory, recurrence)
    registry = build_executable_workflow_registry(catalog)
    clock = MutableClock(NOW)
    service = InstanceConfigurationService(
        unit_of_work_factory=factory,
        constraints=CompiledCatalogInstanceConfigurationConstraintProvider(catalog),
        registered_bindings=LocalMockRegisteredBindingProvider(),
        recurrence=recurrence,
        clock=clock,
        audit_pseudonym_key=KEY,
        workflows=registry,
    )
    dependencies = OrchestrationDependencies(
        clock,
        IncrementingIds(),
        SQLAlchemyUnitOfWorkFactory(
            database.session_factory,
            SQLAlchemyRepositoryFactories(
                works=SQLAlchemyWorkRepository,
                runs=SQLAlchemyRunRepository,
                configurations=SQLAlchemyInstanceConfigurationRepository,
                schedules=SQLAlchemyScheduleRepository,
                audits=SQLAlchemyAuditRepository,
            ),
        ),
    )
    try:
        yield database, service, registry, catalog, clock, dependencies
    finally:
        await database.dispose()


def patch(payload=PAYLOAD, mode=WorkMode.DRY_RUN):
    parameters = InstanceSchedule("* * * * *", "UTC", MisfirePolicy.RUN_ONCE, 60)
    return InstanceConfigurationPatch(
        schedule=PatchValue.of(parameters),
        trigger_bindings=PatchValue.of(
            (
                InstanceTriggerBinding(
                    kind=TriggerKind.SCHEDULE,
                    cron=parameters.cron,
                    timezone=parameters.timezone,
                    misfire_policy=parameters.misfire_policy,
                    misfire_grace_seconds=60,
                ),
            )
        ),
        scheduled_input=PatchValue.of(ScheduledInput(payload, mode)),
    )


async def save(service, revision=1, changes=None):
    return await service.update(
        UpdateInstanceConfigurationCommand(
            instance_id=TARGET,
            expected_revision=revision,
            patch=changes or patch(),
            correlation_id="correlation.obj03.configuration",
        ),
        principal=_admin(),
    )


async def schedule_of(database):
    async with _factory(database)() as uow:
        return await uow.schedules.get(configured_schedule_id(TARGET))


@pytest.mark.asyncio
async def test_obj_03_configuration_save_binds_schedule_and_audits_without_input_leak(installation):
    database, service, _, _, _, _ = installation
    result = await save(service)
    config = result.configuration
    snapshot = config.scheduled_input
    assert snapshot is not None and snapshot.authority is not None
    verify_scheduled_input(TARGET, snapshot, KEY)
    schedule = await schedule_of(database)
    assert schedule is not None and schedule.enabled
    assert schedule.configuration_revision == config.configuration_revision == 2
    assert schedule.workflow_id == snapshot.authority.workflow_id
    assert schedule.next_run_at_utc > NOW
    assert dict(snapshot.admitted_payload) == PAYLOAD
    async with database.session_factory() as session:
        audits = list(await session.scalars(select(AuditEventRecord)))
    assert len(audits) == 1
    assert "private-canary" not in str(audits[0].safe_metadata)
    assert (
        audits[0]
        .safe_metadata["new_configuration"]["scheduled_input"]
        .startswith("audit-value-hmac-sha256-v1:")
    )
    assert "private-canary" not in repr(snapshot)
    assert not (await save(service, 2)).changed
    assert await schedule_of(database) == schedule


@pytest.mark.asyncio
async def test_obj_03_irrelevant_revision_preserves_due_but_invalidates_claim(installation):
    database, service, _, _, clock, dependencies = installation
    await save(service)
    before = await schedule_of(database)
    clock.current = before.next_run_at_utc
    claim = await ScheduleClaimService(dependencies).claim_due_once(lease_owner="worker.obj03")
    assert claim is not None
    await save(service, 2, InstanceConfigurationPatch(variant_label=PatchValue.of("New label")))
    after = await schedule_of(database)
    assert after.next_run_at_utc == before.next_run_at_utc
    assert after.configuration_revision == 3 and after.version == claim.version + 1
    async with dependencies.unit_of_work() as uow:
        assert await uow.schedules.get_claim(after.id) is None
        assert not await uow.schedules.fence_claim(claim, now=clock.current)


@pytest.mark.asyncio
async def test_obj_03_disable_keeps_history_projection_and_stops_claims(installation):
    database, service, _, _, clock, dependencies = installation
    await save(service)
    before = await schedule_of(database)
    await save(service, 2, InstanceConfigurationPatch(enabled=PatchValue.of(False)))
    after = await schedule_of(database)
    assert after.id == before.id and not after.enabled
    assert after.next_run_at_utc == before.next_run_at_utc
    clock.current = before.next_run_at_utc
    assert (
        await ScheduleClaimService(dependencies).claim_due_once(lease_owner="worker.obj03") is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,mode",
    [
        ({"request_id": "request.obj03"}, WorkMode.DRY_RUN),
        ({**PAYLOAD, "unexpected": "not allowed"}, WorkMode.DRY_RUN),
        ({**PAYLOAD, "source_content": "x" * 33_000}, WorkMode.DRY_RUN),
    ],
)
async def test_obj_03_invalid_explicit_input_cannot_create_schedule(installation, payload, mode):
    database, service, _, _, _, _ = installation
    with pytest.raises((ValueError, InstanceConfigurationServiceError)):
        await save(service, changes=patch(payload, mode))
    assert await schedule_of(database) is None
    assert (await service.read(TARGET, principal=_admin())).configuration_revision == 1


@pytest.mark.asyncio
async def test_obj_03_binding_rejects_wrong_key_instance_and_modified_payload(installation):
    _, service, _, _, _, _ = installation
    snapshot = (await save(service)).configuration.scheduled_input
    for instance, value, key in (
        (TARGET, snapshot, DigestKey(b"x" * 32)),
        ("inst.other.01", snapshot, KEY),
        (TARGET, replace(snapshot, admitted_payload={**PAYLOAD, "source_content": "Changed"}), KEY),
    ):
        with pytest.raises(ScheduleConfigurationError):
            verify_scheduled_input(instance, value, key)


@pytest.mark.asyncio
async def test_obj_03_audit_failure_rolls_back_schedule_and_configuration(
    installation, monkeypatch
):
    database, service, _, _, _, _ = installation
    original = SQLAlchemyAuditRepository.append_global

    async def fail(self, event):
        await original(self, event)
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(SQLAlchemyAuditRepository, "append_global", fail)
    with pytest.raises(InstanceConfigurationServiceError):
        await save(service)
    assert await schedule_of(database) is None
    assert (await service.read(TARGET, principal=_admin())).configuration_revision == 1
    async with database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditEventRecord)) == 0


@pytest.mark.asyncio
async def test_obj_03_schedule_cas_failure_rolls_back_configuration(installation, monkeypatch):
    database, service, _, _, _, _ = installation
    await save(service)
    before = await schedule_of(database)

    async def conflict(self, previous, replacement):
        return False

    monkeypatch.setattr(SQLAlchemyScheduleRepository, "compare_and_swap_configuration", conflict)
    with pytest.raises(InstanceConfigurationServiceError):
        await save(service, 2, InstanceConfigurationPatch(enabled=PatchValue.of(False)))
    assert await schedule_of(database) == before
    assert (await service.read(TARGET, principal=_admin())).configuration_revision == 2


@pytest.mark.asyncio
async def test_obj_03_api_roundtrip_and_server_authority_rejection(installation):
    database, service, _, _, _, _ = installation
    app = _app(service, principal=_admin())
    parameters = {
        "cron": "* * * * *",
        "timezone": "UTC",
        "misfirePolicy": "run_once",
        "misfireGraceSeconds": 60,
    }
    body = {
        "schedule": parameters,
        "triggerBindings": [{"type": "schedule", "enabled": True, **parameters}],
        "scheduledInput": {"input": PAYLOAD, "executionMode": "dry_run"},
    }
    path = f"/api/v1/agent-instances/{TARGET}/configuration"
    for unsupported_mode in ("mock_execute", "mock_execution", "live", None, 1, {}):
        rejected = await _request(
            app,
            "PATCH",
            path,
            json={
                **body,
                "scheduledInput": {"input": PAYLOAD, "executionMode": unsupported_mode},
            },
            headers={"If-Match": '"instance-configuration-v1-1"'},
        )
        assert rejected.status_code == 422
        assert "private-canary" not in rejected.text
        unchanged = await service.read(TARGET, principal=_admin())
        assert unchanged.configuration_revision == 1
        assert unchanged.scheduled_input is None
        assert await schedule_of(database) is None
    for forbidden in ("authority", "workflowId", "inputSchemaHash", "bindingDigest"):
        rejected = await _request(
            app,
            "PATCH",
            path,
            json={
                **body,
                "scheduledInput": {**body["scheduledInput"], forbidden: "forged"},
            },
            headers={"If-Match": '"instance-configuration-v1-1"'},
        )
        assert rejected.status_code == 422
        assert "forged" not in rejected.text
    response = await _request(
        app, "PATCH", path, json=body, headers={"If-Match": '"instance-configuration-v1-1"'}
    )
    assert response.status_code == 200, response.text
    assert response.json()["configuration"]["scheduledInput"] == body["scheduledInput"]
    assert "bindingDigest" not in response.text and "authority" not in response.text
    assert (await schedule_of(database)).configuration_revision == 2
    saved = await _request(app, "GET", path)
    assert saved.status_code == 200
    assert saved.json() == response.json()
    assert saved.headers["ETag"] == '"instance-configuration-v1-2"'
    assert saved.headers["Cache-Control"] == "no-store"
    assert saved.headers["Vary"] == "Authorization"
    for principal in (
        human_principal(roles=frozenset({"viewer"})),
        human_principal(roles=frozenset({"operator"})),
        service_principal(),
    ):
        denied = await _request(_app(service, principal=principal), "GET", path)
        assert denied.status_code == 403
        assert "private-canary" not in denied.text
        assert "scheduledInput" not in denied.text
    stale = await _request(
        app, "PATCH", path, json=body, headers={"If-Match": '"instance-configuration-v1-1"'}
    )
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_obj_03_missing_input_is_readable_but_never_creates_executable_schedule(installation):
    database, service, _, _, _, _ = installation
    changes = replace(patch(), scheduled_input=PatchValue.omitted())
    config = (await save(service, changes=changes)).configuration
    assert config.schedule is not None and config.scheduled_input is None
    assert await schedule_of(database) is None
    config = (
        await save(
            service,
            2,
            InstanceConfigurationPatch(
                scheduled_input=PatchValue.of(ScheduledInput(PAYLOAD)),
            ),
        )
    ).configuration
    assert (await schedule_of(database)).enabled
    await save(
        service,
        config.configuration_revision,
        InstanceConfigurationPatch(
            scheduled_input=PatchValue.of(None),
        ),
    )
    assert not (await schedule_of(database)).enabled


@pytest.mark.asyncio
async def test_obj_03_recurrence_edit_advances_without_deleting_schedule(installation):
    database, service, _, _, _, _ = installation
    await save(service)
    previous = await schedule_of(database)
    changes = patch()
    new_schedule = replace(changes.schedule.value, cron="*/5 * * * *")
    changes = replace(
        changes,
        schedule=PatchValue.of(new_schedule),
        trigger_bindings=PatchValue.of(
            (replace(changes.trigger_bindings.value[0], cron=new_schedule.cron),)
        ),
    )
    await save(service, 2, changes)
    updated = await schedule_of(database)
    assert updated.id == previous.id and updated.next_run_at_utc > previous.next_run_at_utc
    assert updated.configuration_revision == 3
    assert updated.last_scheduled_at_utc == previous.last_scheduled_at_utc


@pytest.mark.asyncio
async def test_obj_03_bound_due_filter_prevents_legacy_schedule_starvation(installation):
    database, service, _, _, clock, dependencies = installation
    await save(service)
    bound = await schedule_of(database)
    legacy = replace(bound, id="schedule.legacy", configuration_revision=None, next_run_at_utc=NOW)
    async with dependencies.unit_of_work() as uow:
        assert (await uow.schedules.add_or_get(legacy)).inserted
        await uow.commit()
    clock.current = bound.next_run_at_utc
    async with dependencies.unit_of_work() as uow:
        default = await uow.schedules.list_claimable_due(now=clock.current, limit=1)
        filtered = await uow.schedules.list_claimable_due(
            now=clock.current,
            limit=1,
            configuration_bound_only=True,
        )
    assert default[0].id == legacy.id
    assert filtered == (bound,)


@pytest.mark.asyncio
async def test_obj_03_migration_preserves_legacy_configuration_and_schedule_integrity(tmp_path):
    catalog = compile_catalog(CATALOG_ROOT)
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        assert await upgrade_database(database, "0006") == "0006"
        await seed_catalog_parents(database, catalog)
        legacy_configuration = next(
            item
            for item in catalog_instance_configuration_defaults(
                catalog, CroniterRecurrenceCalculator()
            )
            if item.instance_id == TARGET
        )
        legacy_schedule = Schedule(
            id="schedule.legacy",
            trigger_id="trigger.legacy",
            instance_id=TARGET,
            workflow_id="workflow.legacy",
            cron="* * * * *",
            timezone="UTC",
            next_run_at_utc=NOW,
            misfire_policy=MisfirePolicy.RUN_ONCE,
            misfire_grace_seconds=60,
            enabled=True,
            recurrence_version=SCHEDULE_RECURRENCE_VERSION,
        )
        old_records = (
            ("agent_instance_configs", config_record(legacy_configuration)),
            ("schedules", schedule_record(legacy_schedule)),
        )
        async with database.engine.begin() as connection:
            for name, record in old_records:

                def insert_legacy(sync, table_name=name, value=record):
                    table = Table(table_name, MetaData(), autoload_with=sync)
                    sync.execute(
                        table.insert().values(
                            {column.name: getattr(value, column.name) for column in table.columns}
                        )
                    )

                await connection.run_sync(insert_legacy)
        assert await upgrade_database(database, "0007") == "0007"
        async with _factory(database)() as uow:
            assert await uow.configurations.get(TARGET) == legacy_configuration
            assert await uow.schedules.get(legacy_schedule.id) == legacy_schedule
            assert (
                await uow.schedules.list_claimable_due(
                    now=NOW,
                    limit=1,
                    configuration_bound_only=True,
                )
                == ()
            )
    finally:
        await database.dispose()


@dataclass(frozen=True)
class ConfiguredInstance:
    id: str
    template_id: str
    enabled: bool
    configuration_revision: int


@pytest.mark.asyncio
async def test_obj_03_bound_occurrence_uses_explicit_input_and_stale_command_is_denied(
    installation,
):
    database, service, registry, catalog, clock, dependencies = installation
    config = (await save(service)).configuration
    schedule = await schedule_of(database)
    template = next(
        t for t in catalog.templates if t.id == "tpl.community.education.course-progress-reminders"
    )
    definition = registry.get(schedule.workflow_id)
    validator = IncomingWorkValidator(
        catalog_hash=catalog.content_hash,
        templates=(template,),
        instances=(ConfiguredInstance(TARGET, template.id, True, 2),),
        input_schemas_by_template={template.id: definition.input_schema},
        triggers=(
            ConfiguredIncomingTrigger(
                id=schedule.trigger_id,
                instance_id=TARGET,
                kind=TriggerKind.SCHEDULE,
                source="schedule",
                workflow_ids=(definition.id,),
            ),
        ),
        workflows=(definition.admission_definition(),),
        campaign_brief_revisions=(),
        guard=_guard(),
    )
    clock.current = schedule.next_run_at_utc
    claim = await ScheduleClaimService(dependencies).claim_due_once(lease_owner="worker.obj03")
    processor = ScheduleClaimProcessingService(
        dependencies,
        KEY,
        validator,
        CroniterRecurrenceCalculator(),
        current_catalog_hash=catalog.content_hash,
    )
    command = ScheduleOccurrenceCommand(
        claim=claim,
        mode=config.scheduled_input.mode,
        configuration_revision=2,
        admitted_payload=config.scheduled_input.admitted_payload,
    )
    with pytest.raises(ScheduleClaimProcessingError, match="binding changed"):
        await processor.process_claimed_once(
            replace(command, admitted_payload={**PAYLOAD, "source_content": "Forged"})
        )
    result = await processor.process_claimed_once(command)
    assert dict(result.work_item.admitted_payload) == PAYLOAD
    assert result.run.configuration_revision == 2
    replay = await processor.process_claimed_once(command)
    assert replay.run.id == result.run.id
    await save(service, 2, InstanceConfigurationPatch(enabled=PatchValue.of(False)))
    with pytest.raises(ScheduleClaimProcessingError):
        await processor.process_claimed_once(command)
    async with database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(WorkItemRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(ScheduleOccurrenceRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(ScheduleRecord)) == 1
