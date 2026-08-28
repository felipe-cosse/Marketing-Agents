"""API-05: authenticated webhook intake is durable, atomic, and fan-out safe."""

from __future__ import annotations

import asyncio
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.webhook_sources import (
    MappedWebhookEnvelope,
    WebhookSourceDefinition,
)
from marketing_agents.application.ports.webhooks import WebhookVerifierConfig
from marketing_agents.application.services.webhook_intake import (
    WebhookAdmissionCommand,
    WebhookAdmissionDisposition,
    WebhookAdmissionService,
    WebhookAdmissionServiceError,
)
from marketing_agents.application.services.webhook_rate_limit import (
    ProcessLocalWebhookAdmissionRateLimiter,
)
from marketing_agents.domain.audit import AuditEvent, AuditEventDraft
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.domain.instance_configuration import InstanceTriggerBinding
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.instance_configuration_seed import (
    seed_instance_configurations,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.db import (
    AuditEventRecord,
    Base,
    DatabaseRuntime,
    InstanceConfigurationSQLAlchemyUnitOfWorkFactory,
    RunRecord,
    RunStateTransitionRecord,
    SQLAlchemyAuditRepository,
    SQLAlchemyInstanceConfigurationRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyWebhookAdmissionUnitOfWorkFactory,
    SQLAlchemyWebhookReceiptRepository,
    SQLAlchemyWorkRepository,
    WebhookReceiptDeliveryRecord,
    WebhookReceiptRecord,
    WorkItemRecord,
    create_database_runtime,
)
from marketing_agents.infrastructure.scheduling.cron_recurrence import (
    CroniterRecurrenceCalculator,
)
from marketing_agents.infrastructure.webhook_ingress import (
    CompiledCatalogWebhookAdmissionResolver,
)
from marketing_agents.infrastructure.webhook_signatures import (
    WEBHOOK_SIGNATURE_DOMAIN,
    EnvironmentWebhookSecretResolver,
    HmacSha256WebhookSignatureVerifier,
)
from marketing_agents.infrastructure.webhook_sources import (
    StaticWebhookSourceRegistry,
    StrictJsonWebhookEnvelopeMapper,
)
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
NOW = datetime(2026, 8, 26, 21, tzinfo=UTC)
SOURCE = "integration.events"
TRIGGER_ID = "trigger.webhook.integration-events.v1"
SECRET_REFERENCE = "env:API_05_INTEGRATION_SECRET"
SECRET = "api-05-integration-secret-material-at-least-32-bytes"
DIGEST_KEY = DigestKey(bytes(range(32)))
TARGET_INSTANCE_IDS = (
    "inst.community.events.attendee-scheduler.01",
    "inst.community.events.attendee-scheduler.02",
)
EVENT_ID = "event.api-05.intake.0001"
SENSITIVE_CONTENT = "api-05-raw-body-content-canary-never-audit"


class _FixedClock:
    def now(self) -> datetime:
        return NOW


class _IncrementingIds:
    def __init__(self, label: str) -> None:
        self._label = label
        self._next = 0

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.api-05.{self._label}.{self._next:04d}"


class _CountingStrictMapper:
    version = StrictJsonWebhookEnvelopeMapper.version

    def __init__(self) -> None:
        self.calls = 0
        self._mapper = StrictJsonWebhookEnvelopeMapper()

    def parse(self, raw_body: bytes) -> MappedWebhookEnvelope:
        self.calls += 1
        return self._mapper.parse(raw_body)


class _FaultAfterWebhookAuditFlush(SQLAlchemyAuditRepository):
    async def append_global_many(
        self,
        events: tuple[AuditEventDraft, ...],
    ) -> tuple[AuditEvent, ...]:
        await super().append_global_many(events)
        raise RuntimeError("injected fault after webhook audit flush")


def _catalog() -> CompiledCatalog:
    return compile_catalog(CATALOG_ROOT)


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
    seeded = await seed_instance_configurations(
        catalog,
        InstanceConfigurationSQLAlchemyUnitOfWorkFactory(runtime.session_factory),
        CroniterRecurrenceCalculator(),
    )
    assert seeded.total == 43


async def _set_target_bindings(
    runtime: DatabaseRuntime,
    *,
    enabled: bool,
    configuration_revision: int,
) -> None:
    factory = InstanceConfigurationSQLAlchemyUnitOfWorkFactory(runtime.session_factory)
    async with factory() as unit_of_work:
        for instance_id in TARGET_INSTANCE_IDS:
            current = await unit_of_work.configurations.get(instance_id)
            assert current is not None
            replacement = replace(
                current,
                enabled=enabled,
                trigger_bindings=(
                    InstanceTriggerBinding(
                        kind=TriggerKind.WEBHOOK,
                        enabled=enabled,
                        event_source=SOURCE,
                    ),
                )
                if enabled
                else (),
                configuration_revision=configuration_revision,
            )
            assert await unit_of_work.configurations.compare_and_swap(current, replacement)
        await unit_of_work.commit()


def _uow_factory(
    runtime: DatabaseRuntime,
    *,
    fault_after_webhook_audit: bool = False,
) -> SQLAlchemyWebhookAdmissionUnitOfWorkFactory:
    return SQLAlchemyWebhookAdmissionUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=(
                _FaultAfterWebhookAuditFlush
                if fault_after_webhook_audit
                else SQLAlchemyAuditRepository
            ),
            configurations=SQLAlchemyInstanceConfigurationRepository,
            webhook_receipts=SQLAlchemyWebhookReceiptRepository,
        ),
    )


def _service(
    runtime: DatabaseRuntime,
    catalog: CompiledCatalog,
    *,
    ids_label: str,
    mapper: _CountingStrictMapper | None = None,
    ids: _IncrementingIds | None = None,
    fault_after_webhook_audit: bool = False,
    admission_rate_max_calls: int = 60,
    admission_rate_window_seconds: int = 60,
    admission_rate_limiter: ProcessLocalWebhookAdmissionRateLimiter | None = None,
) -> WebhookAdmissionService:
    installed_mapper = mapper or _CountingStrictMapper()
    verifier = HmacSha256WebhookSignatureVerifier(
        EnvironmentWebhookSecretResolver({"API_05_INTEGRATION_SECRET": SECRET})
    )
    definition = WebhookSourceDefinition(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        mapper_version=installed_mapper.version,
        signature_verifier=verifier,
        verifier_config=WebhookVerifierConfig(secret_reference=SECRET_REFERENCE),
        mapper=installed_mapper,
        admission_rate_max_calls=admission_rate_max_calls,
        admission_rate_window_seconds=admission_rate_window_seconds,
    )
    dependencies = OrchestrationDependencies(
        _FixedClock(),
        ids or _IncrementingIds(ids_label),
        _uow_factory(
            runtime,
            fault_after_webhook_audit=fault_after_webhook_audit,
        ),
    )
    return WebhookAdmissionService(
        dependencies,
        DIGEST_KEY,
        StaticWebhookSourceRegistry((definition,)),
        CompiledCatalogWebhookAdmissionResolver(
            catalog,
            mock_connectors_active=True,
        ),
        current_catalog_hash=catalog.content_hash,
        admission_rate_limiter=admission_rate_limiter,
    )


def _body(
    *,
    event_id: str = EVENT_ID,
    request_id: str = "request-api-05-intake-0001",
    source_content: object = SENSITIVE_CONTENT,
) -> bytes:
    return json.dumps(
        {
            "eventId": event_id,
            "input": {
                "request_id": request_id,
                "source_content": source_content,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _signature(raw_body: bytes) -> str:
    timestamp = str(int(NOW.timestamp()))
    payload = WEBHOOK_SIGNATURE_DOMAIN + timestamp.encode("ascii") + b"\x00" + raw_body
    return "v1=" + hmac.digest(SECRET.encode(), payload, "sha256").hex()


def _command(
    raw_body: bytes,
    *,
    correlation_id: str,
    signature: str | None = None,
) -> WebhookAdmissionCommand:
    timestamp = str(int(NOW.timestamp()))
    return WebhookAdmissionCommand(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        raw_body=raw_body,
        received_headers=(
            ("content-type", "application/json"),
            ("x-webhook-timestamp", timestamp),
            ("x-webhook-signature", _signature(raw_body) if signature is None else signature),
        ),
        correlation_id=correlation_id,
    )


async def _counts(runtime: DatabaseRuntime) -> tuple[int, int, int, int, int, int]:
    models = (
        WebhookReceiptRecord,
        WebhookReceiptDeliveryRecord,
        WorkItemRecord,
        RunRecord,
        RunStateTransitionRecord,
        AuditEventRecord,
    )
    async with runtime.session_factory() as session:
        values = [
            int((await session.execute(select(func.count()).select_from(model))).scalar_one())
            for model in models
        ]
    return cast(tuple[int, int, int, int, int, int], tuple(values))


async def _audit_event_types(runtime: DatabaseRuntime) -> tuple[str, ...]:
    async with runtime.session_factory() as session:
        rows = (
            await session.execute(
                select(AuditEventRecord.event_type).order_by(AuditEventRecord.global_sequence)
            )
        ).scalars()
        return tuple(rows)


@pytest.mark.asyncio
async def test_api_05_authenticated_intake_creates_two_target_receipt_without_audit_leaks(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "webhook-intake.db")
    await _seed(runtime, catalog)
    await _set_target_bindings(runtime, enabled=True, configuration_revision=2)
    raw_body = _body()
    signature = _signature(raw_body)
    try:
        result = await _service(runtime, catalog, ids_label="create").submit(
            _command(
                raw_body,
                correlation_id="correlation.api-05.intake.create",
                signature=signature,
            )
        )

        assert result.disposition is WebhookAdmissionDisposition.CREATED
        assert result.receipt.source == SOURCE
        assert result.receipt.event_id == EVENT_ID
        assert result.receipt.trigger_id == TRIGGER_ID
        assert tuple(item.instance_id for item in result.receipt.deliveries) == TARGET_INSTANCE_IDS
        assert len({item.work_item_id for item in result.receipt.deliveries}) == 2
        assert len({item.run_id for item in result.receipt.deliveries}) == 2
        assert await _counts(runtime) == (1, 2, 2, 2, 2, 4)

        async with runtime.session_factory() as session:
            works = tuple((await session.execute(select(WorkItemRecord))).scalars())
            runs = tuple((await session.execute(select(RunRecord))).scalars())
            audits = tuple((await session.execute(select(AuditEventRecord))).scalars())
        assert {item.agent_instance_id for item in works} == set(TARGET_INSTANCE_IDS)
        assert all(item.configuration_revision == 2 for item in works)
        assert all(item.mode == WorkMode.MOCK_EXECUTION.value for item in works)
        assert all(item.configuration_revision == 2 for item in runs)

        audit_projection = repr(
            tuple(
                {key: value for key, value in item.__dict__.items() if not key.startswith("_sa_")}
                for item in audits
            )
        )
        forbidden_keys = {
            "admission_digest",
            "body_digest",
            "digest_key_version",
            "event_id",
            "raw_body",
            "raw_event_id",
            "secret_ref",
            "signature",
        }
        assert all(forbidden_keys.isdisjoint(item.safe_metadata) for item in audits)
        forbidden_values = (
            raw_body.decode(),
            SENSITIVE_CONTENT,
            signature,
            SECRET,
            SECRET_REFERENCE,
            result.receipt.body_digest,
            result.receipt.digest_key_version,
            *(item.input_digest for item in works),
            *(item.admission_digest for item in works),
            *(item.digest_key_version for item in works),
        )
        assert all(value not in audit_projection for value in forbidden_values)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_09_authenticated_rate_denial_is_audited_before_429_and_fails_closed(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "webhook-rate-limited.db")
    await _seed(runtime, catalog)
    await _set_target_bindings(runtime, enabled=True, configuration_revision=2)
    mapper = _CountingStrictMapper()
    service = _service(
        runtime,
        catalog,
        ids_label="rate-limited",
        mapper=mapper,
        admission_rate_max_calls=1,
        admission_rate_window_seconds=60,
    )
    allowed_body = _body(event_id="event.api-09.rate.allowed")
    denied_canary = "api-09-rate-denied-raw-content-canary"
    denied_body = _body(
        event_id="event.api-09.rate.denied",
        request_id="request-api-09-rate-denied",
        source_content=denied_canary,
    )
    try:
        await service.submit(
            _command(
                allowed_body,
                correlation_id="correlation.api-09.rate.allowed",
            )
        )
        before = await _counts(runtime)

        with pytest.raises(WebhookAdmissionServiceError) as denied:
            await service.submit(
                _command(
                    denied_body,
                    correlation_id="correlation.api-09.rate.denied",
                )
            )

        assert denied.value.code == "webhook_rate_limited"
        assert denied.value.retry_after_seconds == 60
        assert mapper.calls == 1
        after = await _counts(runtime)
        assert after[:5] == before[:5]
        assert after[5] == before[5] + 2
        assert (await _audit_event_types(runtime))[-2:] == (
            "webhook.signature_validated",
            "ingress.rate_limited",
        )
        async with runtime.session_factory() as session:
            rate_event = (
                await session.execute(
                    select(AuditEventRecord).where(
                        AuditEventRecord.event_type == "ingress.rate_limited"
                    )
                )
            ).scalar_one()
            audit_rows = tuple((await session.execute(select(AuditEventRecord))).scalars())
        assert rate_event.reason_code == "rate_limit_exhausted"
        assert rate_event.outcome == "rejected"
        assert rate_event.safe_metadata == {
            "source": SOURCE,
            "trigger_id": TRIGGER_ID,
            "webhook_attempt_id": rate_event.safe_metadata["webhook_attempt_id"],
            "retry_after_seconds": 60,
        }
        rendered = repr(audit_rows)
        for forbidden in (
            denied_body.decode(),
            denied_canary,
            _signature(denied_body),
            SECRET,
            SECRET_REFERENCE,
            "event.api-09.rate.denied",
        ):
            assert forbidden not in rendered

        limiter = ProcessLocalWebhookAdmissionRateLimiter()
        assert limiter.consume(
            source=SOURCE,
            observed_at=NOW,
            max_calls=1,
            window_seconds=60,
        ).allowed
        fault_mapper = _CountingStrictMapper()
        faulting = _service(
            runtime,
            catalog,
            ids_label="rate-audit-fault",
            mapper=fault_mapper,
            fault_after_webhook_audit=True,
            admission_rate_max_calls=1,
            admission_rate_window_seconds=60,
            admission_rate_limiter=limiter,
        )
        with pytest.raises(WebhookAdmissionServiceError) as unavailable:
            await faulting.submit(
                _command(
                    denied_body,
                    correlation_id="correlation.api-09.rate.audit-fault",
                )
            )
        assert unavailable.value.code == "webhook_service_unavailable"
        assert fault_mapper.calls == 0
        assert await _counts(runtime) == after
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_restart_replay_survives_configuration_drift_and_changed_body_collides(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    database_path = tmp_path / "webhook-restart.db"
    runtime = await _runtime(database_path)
    await _seed(runtime, catalog)
    await _set_target_bindings(runtime, enabled=True, configuration_revision=2)
    raw_body = _body()
    try:
        created = await _service(runtime, catalog, ids_label="initial").submit(
            _command(raw_body, correlation_id="correlation.api-05.restart.initial")
        )
        assert created.disposition is WebhookAdmissionDisposition.CREATED
        await _set_target_bindings(runtime, enabled=False, configuration_revision=3)
    finally:
        await runtime.dispose()

    restarted = await _runtime(database_path, initialize=False)
    try:
        replayed = await _service(restarted, catalog, ids_label="restarted").submit(
            _command(raw_body, correlation_id="correlation.api-05.restart.replay")
        )
        assert replayed.disposition is WebhookAdmissionDisposition.REPLAYED
        assert replayed.receipt == created.receipt
        assert tuple(item.instance_id for item in replayed.receipt.deliveries) == (
            TARGET_INSTANCE_IDS
        )

        changed_body = _body(
            request_id="request-api-05-intake-changed",
            source_content="api-05-signed-changed-body",
        )
        with pytest.raises(WebhookAdmissionServiceError) as collision:
            await _service(restarted, catalog, ids_label="collision").submit(
                _command(
                    changed_body,
                    correlation_id="correlation.api-05.restart.collision",
                )
            )
        assert collision.value.code == "webhook_idempotency_conflict"
        assert await _counts(restarted) == (1, 2, 2, 2, 2, 8)
        assert (await _audit_event_types(restarted))[-4:] == (
            "webhook.signature_validated",
            "webhook.duplicate_suppressed",
            "webhook.signature_validated",
            "webhook.idempotency_collision",
        )
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_api_05_invalid_signature_precedes_mapper_and_business_persistence(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "webhook-invalid-signature.db")
    await _seed(runtime, catalog)
    await _set_target_bindings(runtime, enabled=True, configuration_revision=2)
    mapper = _CountingStrictMapper()
    try:
        with pytest.raises(WebhookAdmissionServiceError) as rejected:
            await _service(
                runtime,
                catalog,
                ids_label="invalid-signature",
                mapper=mapper,
            ).submit(
                _command(
                    b"not-even-json-api-05-canary",
                    correlation_id="correlation.api-05.invalid-signature",
                    signature="v1=" + "0" * 64,
                )
            )
        assert rejected.value.code == "webhook_authentication_failed"
        assert mapper.calls == 0
        assert await _counts(runtime) == (0, 0, 0, 0, 0, 1)
        assert await _audit_event_types(runtime) == ("webhook.signature_rejected",)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_signed_input_schema_rejection_creates_no_receipt_or_target_work(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "webhook-schema-rejection.db")
    await _seed(runtime, catalog)
    await _set_target_bindings(runtime, enabled=True, configuration_revision=2)
    mapper = _CountingStrictMapper()
    try:
        with pytest.raises(WebhookAdmissionServiceError) as rejected:
            await _service(
                runtime,
                catalog,
                ids_label="schema-rejection",
                mapper=mapper,
            ).submit(
                _command(
                    _body(source_content=42),
                    correlation_id="correlation.api-05.schema-rejection",
                )
            )
        assert rejected.value.code == "input_schema_invalid"
        assert rejected.value.pointer == "/input/source_content"
        assert "42" not in str(rejected.value)
        assert mapper.calls == 1
        assert await _counts(runtime) == (0, 0, 0, 0, 0, 2)
        assert await _audit_event_types(runtime) == (
            "webhook.signature_validated",
            "webhook.schema_rejected",
        )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_registered_source_without_explicit_instance_binding_is_forbidden(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "webhook-no-binding.db")
    await _seed(runtime, catalog)
    try:
        with pytest.raises(WebhookAdmissionServiceError) as rejected:
            await _service(runtime, catalog, ids_label="no-binding").submit(
                _command(
                    _body(),
                    correlation_id="correlation.api-05.no-binding",
                )
            )
        assert rejected.value.code == "webhook_binding_forbidden"
        assert await _counts(runtime) == (0, 0, 0, 0, 0, 1)
        assert await _audit_event_types(runtime) == ("webhook.signature_validated",)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_concurrent_exact_replay_creates_one_complete_fanout(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "webhook-concurrent.db")
    await _seed(runtime, catalog)
    await _set_target_bindings(runtime, enabled=True, configuration_revision=2)
    raw_body = _body(event_id="event.api-05.concurrent.0001")
    shared_ids = _IncrementingIds("concurrent")
    service = _service(
        runtime,
        catalog,
        ids_label="unused",
        ids=shared_ids,
    )
    try:
        results = await asyncio.gather(
            service.submit(_command(raw_body, correlation_id="correlation.api-05.concurrent.a")),
            service.submit(_command(raw_body, correlation_id="correlation.api-05.concurrent.b")),
        )
        assert {item.disposition for item in results} == {
            WebhookAdmissionDisposition.CREATED,
            WebhookAdmissionDisposition.REPLAYED,
        }
        assert results[0].receipt == results[1].receipt
        assert len(results[0].receipt.deliveries) == 2
        assert await _counts(runtime) == (1, 2, 2, 2, 2, 6)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_05_fault_after_webhook_audit_flush_rolls_back_complete_fanout(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "webhook-audit-rollback.db")
    await _seed(runtime, catalog)
    await _set_target_bindings(runtime, enabled=True, configuration_revision=2)
    command = _command(
        _body(event_id="event.api-05.rollback.0001"),
        correlation_id="correlation.api-05.rollback",
    )
    try:
        with pytest.raises(WebhookAdmissionServiceError) as failed:
            await _service(
                runtime,
                catalog,
                ids_label="faulted",
                fault_after_webhook_audit=True,
            ).submit(command)
        assert failed.value.code == "webhook_service_unavailable"
        assert await _counts(runtime) == (0, 0, 0, 0, 0, 0)

        recovered = await _service(runtime, catalog, ids_label="recovered").submit(command)
        assert recovered.disposition is WebhookAdmissionDisposition.CREATED
        assert await _counts(runtime) == (1, 2, 2, 2, 2, 4)
    finally:
        await runtime.dispose()
