"""API-04: durable manual admission resolves locked configuration atomically."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunService,
    ManualDryRunServiceError,
)
from marketing_agents.config import Settings
from marketing_agents.domain.audit import AuditEvent, AuditEventDraft
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.instance_configuration import (
    InstanceConnectorBinding,
    InstanceTriggerBinding,
)
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.instance_configuration_seed import (
    seed_instance_configurations,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.db import (
    AgentInstanceConfigurationRecord,
    AuditEventRecord,
    Base,
    DatabaseRuntime,
    InstanceConfigurationSQLAlchemyUnitOfWorkFactory,
    RunRecord,
    RunStateTransitionRecord,
    SQLAlchemyAuditRepository,
    SQLAlchemyInstanceConfigurationRepository,
    SQLAlchemyManualAdmissionUnitOfWorkFactory,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyWorkRepository,
    WorkItemRecord,
    create_database_runtime,
)
from marketing_agents.infrastructure.manual_work import (
    CompiledCatalogManualAdmissionResolver,
)
from marketing_agents.infrastructure.scheduling.cron_recurrence import (
    CroniterRecurrenceCalculator,
)
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.redaction import SecretValue
from sqlalchemy import func, select

from tests.support.identity import StaticIdentityProvider, human_principal

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
TARGET_INSTANCE_ID = "inst.social-media.new-content.linkedin-post-drafter.01"
TARGET_TEMPLATE_ID = "tpl.social-media.new-content.linkedin-post-drafter"
KEY = DigestKey(bytes(range(32)))


class _FixedClock:
    def now(self) -> datetime:
        return NOW


class _IncrementingIds:
    def __init__(self, start: int = 0) -> None:
        self._next = start

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.api-04.persistence.{self._next:04d}"


class _FaultAfterAuditAppend(SQLAlchemyAuditRepository):
    async def append(self, event: AuditEventDraft) -> AuditEvent:
        await super().append(event)
        raise RuntimeError("injected fault after durable audit flush")


def _catalog() -> CompiledCatalog:
    return compile_catalog(CATALOG_ROOT)


def _operator() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.test.api-04-operator",
        roles=frozenset({"operator"}),
        scopes=frozenset({"manual-work:create"}),
    )


def _payload(request_id: str = "request-api-04-0001") -> dict[str, object]:
    return {
        "request_id": request_id,
        "source_content": "Draft a bounded LinkedIn post from this supplied content.",
    }


def _command(
    *,
    key: str | None = "manual-api-04-idempotency-0001",
    payload: dict[str, object] | None = None,
    mode: WorkMode = WorkMode.DRY_RUN,
    campaign_brief_id: str | None = None,
    demo_scenario_id: str | None = None,
    correlation_id: str = "correlation.api-04.persistence.0001",
) -> ManualDryRunCommand:
    return ManualDryRunCommand(
        instance_id=TARGET_INSTANCE_ID,
        input_payload=payload or _payload(),
        correlation_id=correlation_id,
        mode=mode,
        idempotency_key=None if key is None else SecretValue(key),
        campaign_brief_id=campaign_brief_id,
        demo_scenario_id=demo_scenario_id,
    )


def _uow_factory(
    runtime: DatabaseRuntime,
    *,
    fault_after_audit: bool = False,
) -> SQLAlchemyManualAdmissionUnitOfWorkFactory:
    return SQLAlchemyManualAdmissionUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=(_FaultAfterAuditAppend if fault_after_audit else SQLAlchemyAuditRepository),
            configurations=SQLAlchemyInstanceConfigurationRepository,
        ),
    )


def _service(
    runtime: DatabaseRuntime,
    catalog: CompiledCatalog,
    *,
    mock_connectors_active: bool,
    ids: _IncrementingIds | None = None,
    fault_after_audit: bool = False,
) -> ManualDryRunService:
    dependencies = OrchestrationDependencies(
        _FixedClock(),
        ids or _IncrementingIds(),
        _uow_factory(runtime, fault_after_audit=fault_after_audit),
    )
    return ManualDryRunService(
        dependencies,
        KEY,
        CompiledCatalogManualAdmissionResolver(
            catalog,
            mock_connectors_active=mock_connectors_active,
        ),
        current_catalog_hash=catalog.content_hash,
    )


async def _runtime(
    path: Path,
    *,
    initialize: bool = True,
    sqlite_busy_timeout_ms: int = 5_000,
) -> DatabaseRuntime:
    runtime = create_database_runtime(
        f"sqlite+aiosqlite:///{path}",
        sqlite_busy_timeout_ms=sqlite_busy_timeout_ms,
    )
    if initialize:
        async with runtime.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    return runtime


async def _seed(runtime: DatabaseRuntime, catalog: CompiledCatalog) -> None:
    result = await seed_instance_configurations(
        catalog,
        InstanceConfigurationSQLAlchemyUnitOfWorkFactory(runtime.session_factory),
        CroniterRecurrenceCalculator(),
    )
    assert result.total == 43


async def _counts(runtime: DatabaseRuntime) -> tuple[int, int, int, int]:
    models = (WorkItemRecord, RunRecord, RunStateTransitionRecord, AuditEventRecord)
    async with runtime.session_factory() as session:
        values: list[int] = []
        for model in models:
            count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
            values.append(int(count))
    return cast(tuple[int, int, int, int], tuple(values))


async def _replace_configuration(
    runtime: DatabaseRuntime,
    *,
    configuration_revision: int,
    enabled: bool | None = None,
    variant_label: str | None = None,
    trigger_bindings: tuple[InstanceTriggerBinding, ...] | None = None,
    connector_bindings: Mapping[str, InstanceConnectorBinding] | None = None,
) -> None:
    factory = InstanceConfigurationSQLAlchemyUnitOfWorkFactory(runtime.session_factory)
    async with factory() as unit_of_work:
        current = await unit_of_work.configurations.get(TARGET_INSTANCE_ID)
        assert current is not None
        replacement = replace(
            current,
            enabled=current.enabled if enabled is None else enabled,
            variant_label=(current.variant_label if variant_label is None else variant_label),
            trigger_bindings=(
                current.trigger_bindings if trigger_bindings is None else trigger_bindings
            ),
            connector_bindings=(
                current.connector_bindings if connector_bindings is None else connector_bindings
            ),
            configuration_revision=configuration_revision,
        )
        assert await unit_of_work.configurations.compare_and_swap(current, replacement)
        await unit_of_work.commit()


@pytest.mark.asyncio
async def test_api_04_keyed_receipt_replays_after_restart_and_conflicts_on_drift(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    database_path = tmp_path / "manual-replay.db"
    first_runtime = await _runtime(database_path)
    await _seed(first_runtime, catalog)
    command = _command()
    try:
        created = await _service(
            first_runtime,
            catalog,
            mock_connectors_active=True,
        ).submit(command, _operator())
        assert created.disposition is WorkRunReceiptDisposition.CREATED
        assert created.work_item.configuration_revision == 1
        assert created.work_item.trigger_id == (
            "trigger.manual.social-media.new-content.linkedin-post-drafter.01.v1"
        )
        assert created.work_item.workflow_id == (
            "workflow.manual.social-media.new-content.linkedin-post-drafter.v1"
        )
        assert created.work_item.input_schema_id == (
            "urn:marketing-agents:catalog:v1:"
            "tpl.social-media.new-content.linkedin-post-drafter:input"
        )
        assert await _counts(first_runtime) == (1, 1, 1, 3)
    finally:
        await first_runtime.dispose()

    restarted_runtime = await _runtime(database_path, initialize=False)
    try:
        replayed = await _service(
            restarted_runtime,
            catalog,
            mock_connectors_active=True,
            ids=_IncrementingIds(100),
        ).submit(
            _command(correlation_id="correlation.api-04.persistence.replay"),
            _operator(),
        )
        assert replayed.disposition is WorkRunReceiptDisposition.REPLAYED
        assert replayed.work_item.id == created.work_item.id
        assert replayed.run.id == created.run.id
        assert replayed.event_id == created.event_id

        with pytest.raises(ManualDryRunServiceError) as payload_conflict:
            await _service(
                restarted_runtime,
                catalog,
                mock_connectors_active=True,
                ids=_IncrementingIds(200),
            ).submit(
                _command(payload=_payload("request-api-04-changed")),
                _operator(),
            )
        assert payload_conflict.value.code == "idempotency_conflict"

        with pytest.raises(ManualDryRunServiceError) as mode_conflict:
            await _service(
                restarted_runtime,
                catalog,
                mock_connectors_active=True,
                ids=_IncrementingIds(300),
            ).submit(
                _command(mode=WorkMode.MOCK_EXECUTION),
                _operator(),
            )
        assert mode_conflict.value.code == "idempotency_conflict"

        fresh_service = _service(
            restarted_runtime,
            catalog,
            mock_connectors_active=True,
            ids=_IncrementingIds(400),
        )
        fresh_a = await fresh_service.submit(
            _command(key=None, correlation_id="correlation.api-04.fresh.0001"),
            _operator(),
        )
        fresh_b = await fresh_service.submit(
            _command(key=None, correlation_id="correlation.api-04.fresh.0002"),
            _operator(),
        )
        assert fresh_a.disposition is WorkRunReceiptDisposition.CREATED
        assert fresh_b.disposition is WorkRunReceiptDisposition.CREATED
        assert fresh_a.event_id != fresh_b.event_id
        assert fresh_a.work_item.id != fresh_b.work_item.id
        assert fresh_a.run.id != fresh_b.run.id
        assert await _counts(restarted_runtime) == (3, 3, 3, 15)
    finally:
        await restarted_runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_effective_revision_virtual_manual_and_disablement_are_enforced(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "effective-configuration.db")
    await _seed(runtime, catalog)
    service = _service(runtime, catalog, mock_connectors_active=False)
    try:
        virtual_manual = await service.submit(
            _command(key=None, correlation_id="correlation.api-04.virtual-manual"),
            _operator(),
        )
        assert virtual_manual.work_item.configuration_revision == 1

        await _replace_configuration(
            runtime,
            variant_label="Operator revision two",
            configuration_revision=2,
        )
        revision_two = await service.submit(
            _command(
                key=None,
                payload=_payload("request-api-04-revision-two"),
                correlation_id="correlation.api-04.revision-two",
            ),
            _operator(),
        )
        assert revision_two.work_item.configuration_revision == 2
        assert revision_two.run.configuration_revision == 2

        await _replace_configuration(
            runtime,
            trigger_bindings=(InstanceTriggerBinding(kind=TriggerKind.MANUAL, enabled=False),),
            configuration_revision=3,
        )
        with pytest.raises(ManualDryRunServiceError) as disabled_manual:
            await service.submit(
                _command(key=None, correlation_id="correlation.api-04.manual-disabled"),
                _operator(),
            )
        assert disabled_manual.value.code == "manual_trigger_unavailable"

        await _replace_configuration(
            runtime,
            enabled=False,
            trigger_bindings=(),
            configuration_revision=4,
        )
        with pytest.raises(ManualDryRunServiceError) as disabled_instance:
            await service.submit(
                _command(key=None, correlation_id="correlation.api-04.instance-disabled"),
                _operator(),
            )
        assert disabled_instance.value.code == "instance_disabled"
        assert await _counts(runtime) == (2, 2, 2, 6)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_same_key_conflicts_after_effective_configuration_revision_changes(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "configuration-revision-conflict.db")
    await _seed(runtime, catalog)
    service = _service(runtime, catalog, mock_connectors_active=True)
    command = _command(key="manual-api-04-configuration-revision-key")
    try:
        original = await service.submit(command, _operator())
        assert original.disposition is WorkRunReceiptDisposition.CREATED
        assert original.work_item.configuration_revision == 1
        assert original.run.configuration_revision == 1

        await _replace_configuration(
            runtime,
            variant_label="Configuration revision changed",
            configuration_revision=2,
        )
        with pytest.raises(ManualDryRunServiceError) as collision:
            await service.submit(command, _operator())
        assert collision.value.code == "idempotency_conflict"
        assert await _counts(runtime) == (1, 1, 1, 5)

        async with runtime.session_factory() as session:
            retained_work = await session.get(WorkItemRecord, original.work_item.id)
            retained_run = await session.get(RunRecord, original.run.id)
            configured = await session.get(
                AgentInstanceConfigurationRecord,
                TARGET_INSTANCE_ID,
            )
        assert retained_work is not None
        assert retained_run is not None
        assert configured is not None
        assert retained_work.configuration_revision == 1
        assert retained_run.configuration_revision == 1
        assert retained_run.work_item_id == retained_work.id
        assert configured.version == 2
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_compiled_input_schema_rejects_before_receipt(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "compiled-input-schema.db")
    await _seed(runtime, catalog)
    try:
        with pytest.raises(ManualDryRunServiceError) as invalid:
            await _service(
                runtime,
                catalog,
                mock_connectors_active=False,
            ).submit(
                _command(
                    key="manual-api-04-invalid-input",
                    payload={
                        "request_id": "request-api-04-invalid",
                        "source_content": 42,
                    },
                ),
                _operator(),
            )
        assert invalid.value.code == "input_schema_invalid"
        assert invalid.value.pointer == "/input/source_content"
        assert "42" not in str(invalid.value)
        assert await _counts(runtime) == (0, 0, 0, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_concurrent_same_key_serializes_replay_and_changed_conflict(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(
        tmp_path / "manual-concurrent-idempotency.db",
        sqlite_busy_timeout_ms=1,
    )
    await _seed(runtime, catalog)
    operator = _operator()
    try:
        identical = await asyncio.gather(
            _service(
                runtime,
                catalog,
                mock_connectors_active=True,
                ids=_IncrementingIds(),
            ).submit(
                _command(correlation_id="correlation.api-04.concurrent.identical-a"),
                operator,
            ),
            _service(
                runtime,
                catalog,
                mock_connectors_active=True,
                ids=_IncrementingIds(100),
            ).submit(
                _command(correlation_id="correlation.api-04.concurrent.identical-b"),
                operator,
            ),
        )
        assert {item.disposition for item in identical} == {
            WorkRunReceiptDisposition.CREATED,
            WorkRunReceiptDisposition.REPLAYED,
        }
        assert len({item.work_item.id for item in identical}) == 1
        assert len({item.run.id for item in identical}) == 1
        assert len({item.event_id for item in identical}) == 1
        assert await _counts(runtime) == (1, 1, 1, 5)

        changed = await asyncio.gather(
            _service(
                runtime,
                catalog,
                mock_connectors_active=True,
                ids=_IncrementingIds(200),
            ).submit(
                _command(
                    key="manual-api-04-concurrent-changed",
                    payload=_payload("request-api-04-concurrent-a"),
                    correlation_id="correlation.api-04.concurrent.changed-a",
                ),
                operator,
            ),
            _service(
                runtime,
                catalog,
                mock_connectors_active=True,
                ids=_IncrementingIds(300),
            ).submit(
                _command(
                    key="manual-api-04-concurrent-changed",
                    payload=_payload("request-api-04-concurrent-b"),
                    correlation_id="correlation.api-04.concurrent.changed-b",
                ),
                operator,
            ),
            return_exceptions=True,
        )
        successful = [item for item in changed if not isinstance(item, BaseException)]
        rejected = [item for item in changed if isinstance(item, BaseException)]
        assert len(successful) == 1
        assert successful[0].disposition is WorkRunReceiptDisposition.CREATED
        assert len(rejected) == 1
        assert isinstance(rejected[0], ManualDryRunServiceError)
        assert rejected[0].code == "idempotency_conflict"
        assert await _counts(runtime) == (2, 2, 2, 10)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_http_route_composes_real_service_resolver_and_durable_uow(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    database_path = tmp_path / "manual-http-composition.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    runtime = await _runtime(database_path)
    await _seed(runtime, catalog)
    service = _service(runtime, catalog, mock_connectors_active=True)
    application = create_app(
        settings=Settings(
            app_env="test",
            database_url=database_url,
            catalog_root=CATALOG_ROOT,
            marketing_agents_digest_key_path=tmp_path / "unused-digest.key",
        ),
        identity_provider=StaticIdentityProvider(_operator()),
        manual_dry_run_service=service,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/api/v1/agent-instances/{TARGET_INSTANCE_ID}/dry-runs",
                headers={
                    "Authorization": "Bearer local-api-04-operator-token",
                    "Content-Type": "application/json",
                    "Idempotency-Key": "manual-api-04-http-composition",
                },
                json={"input": _payload("request-api-04-http-composition")},
            )

        assert response.status_code == 202
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["status"] == "accepted"
        assert body["disposition"] == "created"
        assert body["executionMode"] == "dry_run"
        assert body["eventId"].startswith("manual-event-hmac-sha256-v1:")
        assert body["workId"].startswith("work.")
        assert body["runId"].startswith("run.")
        assert body["instanceUrl"] == (f"/api/v1/agent-instances/{TARGET_INSTANCE_ID}")
        assert body["runUrl"] == f"/api/v1/runs/{body['runId']}"
        assert await _counts(runtime) == (1, 1, 1, 3)

        async with runtime.session_factory() as session:
            work = await session.get(WorkItemRecord, body["workId"])
            run = await session.get(RunRecord, body["runId"])
        assert work is not None
        assert run is not None
        assert work.agent_instance_id == TARGET_INSTANCE_ID
        assert work.event_id == body["eventId"]
        assert run.work_item_id == work.id
        assert run.configuration_revision == work.configuration_revision
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_mock_mode_and_unregistered_resource_policies_fail_closed(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "manual-mode-policy.db")
    await _seed(runtime, catalog)
    operator = _operator()
    try:
        dry_only = _service(runtime, catalog, mock_connectors_active=False)
        with pytest.raises(ManualDryRunServiceError) as mock_disabled:
            await dry_only.submit(
                _command(
                    mode=WorkMode.MOCK_EXECUTION,
                    key="manual-api-04-mode-disabled",
                ),
                operator,
            )
        assert mock_disabled.value.code == "work_mode_not_allowed"

        with pytest.raises(ManualDryRunServiceError) as unknown_brief:
            await dry_only.submit(
                _command(
                    campaign_brief_id="brief.api-04.unknown",
                    key="manual-api-04-brief-unknown",
                ),
                operator,
            )
        assert unknown_brief.value.code == "campaign_brief_unknown"

        with pytest.raises(ManualDryRunServiceError) as unknown_demo:
            await dry_only.submit(
                _command(
                    demo_scenario_id="demo.api-04.unknown",
                    key="manual-api-04-demo-unknown",
                ),
                operator,
            )
        assert unknown_demo.value.code == "demo_scenario_unknown"
        assert await _counts(runtime) == (0, 0, 0, 0)

        mock_enabled = await _service(
            runtime,
            catalog,
            mock_connectors_active=True,
        ).submit(
            _command(
                mode=WorkMode.MOCK_EXECUTION,
                key="manual-api-04-mode-enabled",
            ),
            operator,
        )
        assert mock_enabled.mode is WorkMode.MOCK_EXECUTION
        assert mock_enabled.work_item.mode is WorkMode.MOCK_EXECUTION
        assert await _counts(runtime) == (1, 1, 1, 3)

        await _replace_configuration(
            runtime,
            connector_bindings={
                "crm": InstanceConnectorBinding(
                    connector_family="crm",
                    binding_id="mock.crm.unregistered",
                )
            },
            configuration_revision=2,
        )
        with pytest.raises(ManualDryRunServiceError) as invalid_connector:
            await _service(
                runtime,
                catalog,
                mock_connectors_active=True,
            ).submit(
                _command(
                    key="manual-api-04-invalid-connector-binding",
                    payload=_payload("request-api-04-invalid-connector"),
                ),
                operator,
            )
        assert invalid_connector.value.code == "manual_binding_unavailable"
        assert await _counts(runtime) == (1, 1, 1, 3)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_fault_after_audit_flush_rolls_back_the_complete_receipt(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "manual-rollback.db")
    await _seed(runtime, catalog)
    command = _command(key="manual-api-04-rollback-key")
    operator = _operator()
    try:
        with pytest.raises(ManualDryRunServiceError) as failed:
            await _service(
                runtime,
                catalog,
                mock_connectors_active=False,
                fault_after_audit=True,
            ).submit(command, operator)
        assert failed.value.code == "manual_work_unavailable"
        assert await _counts(runtime) == (0, 0, 0, 0)

        recovered = await _service(
            runtime,
            catalog,
            mock_connectors_active=False,
        ).submit(command, operator)
        assert recovered.disposition is WorkRunReceiptDisposition.CREATED
        assert await _counts(runtime) == (1, 1, 1, 3)
    finally:
        await runtime.dispose()


def test_api_04_resolver_is_bound_to_the_expected_catalog_template() -> None:
    catalog = _catalog()
    instance = next(item for item in catalog.instances if item.id == TARGET_INSTANCE_ID)
    assert instance.template_id == TARGET_TEMPLATE_ID
