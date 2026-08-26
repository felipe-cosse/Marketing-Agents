"""API-05: authenticated webhook admission is atomic and receipt-idempotent."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.repositories import WebhookReceiptInsertResult
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.ports.webhook_admission import (
    WebhookAdmissionBinding,
    WebhookAdmissionResolutionError,
)
from marketing_agents.application.ports.webhook_sources import (
    MappedWebhookEnvelope,
    WebhookEnvelopeMappingError,
    WebhookSourceDefinition,
)
from marketing_agents.application.ports.webhooks import (
    VerifiedWebhookIdentity,
    WebhookReceivedHeaders,
    WebhookSignatureVerificationError,
    WebhookVerifierConfig,
)
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
    WorkRunReceiptResult,
)
from marketing_agents.application.services.incoming_work_validation import (
    ValidatedIncomingWork,
)
from marketing_agents.application.services.webhook_intake import (
    WebhookAdmissionCommand,
    WebhookAdmissionDisposition,
    WebhookAdmissionResult,
    WebhookAdmissionService,
    WebhookAdmissionServiceError,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditEventDraft
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.webhook import (
    MAX_WEBHOOK_RECEIPT_DELIVERIES,
    WebhookReceipt,
    WebhookReceiptDelivery,
)
from marketing_agents.infrastructure.webhook_signatures import (
    WEBHOOK_SIGNATURE_DOMAIN,
    EnvironmentWebhookSecretResolver,
    HmacSha256WebhookSignatureVerifier,
)
from marketing_agents.infrastructure.webhook_sources import StrictJsonWebhookEnvelopeMapper
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.webhook_digest import derive_webhook_body_digest

NOW = datetime(2026, 8, 26, 20, 30, tzinfo=UTC)
SOURCE = "source.api05.service"
TRIGGER_ID = "trigger.api05.webhook"
EVENT_ID = "event.api05.webhook.0001"
CATALOG_HASH = "catalog-sha256-v1:" + ("a" * 64)
SECRET_REFERENCE = "env:MARKETING_AGENTS_WEBHOOK_SERVICE_TEST_SECRET"
SECRET = "api05-service-test-secret-material-32-bytes"
KEY = DigestKey(bytes(range(32)))
RAW_BODY = b'{"eventId":"event.api05.webhook.0001","input":{"text":"safe"}}'
CANARY = "raw-audit-storage-failure-canary"


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Ids:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def new(self, namespace: str) -> str:
        self.calls.append(namespace)
        return f"{namespace}.api05.{len(self.calls):04d}"


class _Verifier:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls: list[dict[str, object]] = []
        self.error: WebhookSignatureVerificationError | None = None
        self.output_override: object | None = None
        self._delegate = HmacSha256WebhookSignatureVerifier(
            EnvironmentWebhookSecretResolver(
                {"MARKETING_AGENTS_WEBHOOK_SERVICE_TEST_SECRET": SECRET}
            )
        )

    def verify(
        self,
        *,
        source: str,
        trigger_id: str,
        raw_body: bytes,
        received_headers: WebhookReceivedHeaders,
        received_at: datetime,
        verifier_config: WebhookVerifierConfig,
    ) -> VerifiedWebhookIdentity:
        self.order.append("signature")
        self.calls.append(
            {
                "source": source,
                "trigger_id": trigger_id,
                "raw_body": raw_body,
                "received_headers": received_headers,
                "received_at": received_at,
                "verifier_config": verifier_config,
            }
        )
        if self.error is not None:
            raise self.error
        if self.output_override is not None:
            return cast(VerifiedWebhookIdentity, self.output_override)
        return self._delegate.verify(
            source=source,
            trigger_id=trigger_id,
            raw_body=raw_body,
            received_headers=received_headers,
            received_at=received_at,
            verifier_config=verifier_config,
        )


class _Mapper:
    version = StrictJsonWebhookEnvelopeMapper.version

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls: list[bytes] = []
        self.error: Exception | None = None
        self._delegate = StrictJsonWebhookEnvelopeMapper()

    def parse(self, raw_body: bytes) -> MappedWebhookEnvelope:
        self.order.append("json")
        self.calls.append(raw_body)
        if self.error is not None:
            raise self.error
        return self._delegate.parse(raw_body)


class _Registry:
    def __init__(self, definition: WebhookSourceDefinition) -> None:
        self.definition = definition
        self.calls: list[tuple[str, str]] = []

    def resolve(self, source: str, trigger_id: str) -> WebhookSourceDefinition | None:
        self.calls.append((source, trigger_id))
        if (source, trigger_id) != (self.definition.source, self.definition.trigger_id):
            return None
        return self.definition


@dataclass(frozen=True, slots=True)
class _Validated:
    envelope: AdmissionEnvelope


class _Validator:
    def __init__(self) -> None:
        self.calls: list[AdmissionEnvelope] = []

    def validate(self, envelope: AdmissionEnvelope) -> ValidatedIncomingWork:
        self.calls.append(envelope)
        return cast(ValidatedIncomingWork, _Validated(envelope))


class _Resolver:
    def __init__(self, bindings: tuple[WebhookAdmissionBinding, ...]) -> None:
        self.bindings = bindings
        self.error: WebhookAdmissionResolutionError | None = None
        self.calls: list[tuple[UnitOfWork, str, str]] = []
        self.after_resolve: Callable[[], None] | None = None

    async def resolve_all_in_uow(
        self,
        unit_of_work: UnitOfWork,
        *,
        source: str,
        trigger_id: str,
    ) -> tuple[WebhookAdmissionBinding, ...]:
        self.calls.append((unit_of_work, source, trigger_id))
        if self.error is not None:
            raise self.error
        if self.after_resolve is not None:
            self.after_resolve()
        return self.bindings


@dataclass(frozen=True, slots=True)
class _WorkOutcome:
    id: str
    instance_id: str


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    id: str


class _ReceiptService:
    def __init__(self) -> None:
        self.calls: list[tuple[UnitOfWork, ValidatedIncomingWork, object]] = []
        self.fail_on_call: int | None = None

    async def receive_in_uow(
        self,
        unit_of_work: UnitOfWork,
        incoming: ValidatedIncomingWork,
        *,
        audit_context: object,
    ) -> WorkRunReceiptResult:
        self.calls.append((unit_of_work, incoming, audit_context))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("receipt fan-out failed")
        envelope = cast(_Validated, incoming).envelope
        suffix = envelope.instance_id.rsplit(".", 1)[-1]
        return WorkRunReceiptResult(
            work_item=cast(
                WorkItem,
                _WorkOutcome(
                    id=f"work.api05.{suffix}",
                    instance_id=envelope.instance_id,
                ),
            ),
            run=cast(Run, _RunOutcome(id=f"run.api05.{suffix}")),
            disposition=WorkRunReceiptDisposition.CREATED,
            initial_transition=None,
        )


class _WebhookReceiptRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], WebhookReceipt] = {}
        self.lookups: list[tuple[str, str]] = []
        self.adds: list[WebhookReceipt] = []

    async def get(self, receipt_id: str) -> WebhookReceipt | None:
        return next((item for item in self.records.values() if item.id == receipt_id), None)

    async def get_by_source_event(
        self,
        source: str,
        event_id: str,
    ) -> WebhookReceipt | None:
        self.lookups.append((source, event_id))
        return self.records.get((source, event_id))

    async def add_or_get(self, receipt: WebhookReceipt) -> WebhookReceiptInsertResult:
        self.adds.append(receipt)
        key = (receipt.source, receipt.event_id)
        existing = self.records.get(key)
        if existing is not None:
            return WebhookReceiptInsertResult(receipt=existing, inserted=False)
        self.records[key] = receipt
        return WebhookReceiptInsertResult(receipt=receipt, inserted=True)


class _WorkRepository:
    def __init__(self) -> None:
        self.existing_by_key: dict[tuple[str, str, str], object] = {}
        self.lookups: list[tuple[str, str, str]] = []

    async def get_by_source_key(
        self,
        source: str,
        event_id: str,
        instance_id: str,
    ) -> WorkItem | None:
        key = (source, event_id, instance_id)
        self.lookups.append(key)
        return cast(WorkItem | None, self.existing_by_key.get(key))


class _AuditRepository:
    def __init__(self) -> None:
        self.batches: list[tuple[AuditEventDraft, ...]] = []
        self.fail = False

    async def append_global(self, event: AuditEventDraft) -> object:
        return (await self.append_global_many((event,)))[0]

    async def append_global_many(
        self,
        events: tuple[AuditEventDraft, ...],
    ) -> tuple[object, ...]:
        if self.fail:
            raise RuntimeError(CANARY)
        self.batches.append(events)
        return tuple(object() for _ in events)


class _State:
    def __init__(self) -> None:
        self.receipts = _WebhookReceiptRepository()
        self.works = _WorkRepository()
        self.audits = _AuditRepository()


class _UnitOfWork:
    def __init__(self, state: _State) -> None:
        self.webhook_receipts = state.receipts
        self.works = state.works
        self.audits = state.audits
        self.enters = 0
        self.exits = 0
        self.commits = 0
        self.exit_error_types: list[type[BaseException] | None] = []

    async def __aenter__(self) -> _UnitOfWork:
        self.enters += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self.exits += 1
        self.exit_error_types.append(exc_type)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _UnitOfWorkFactory:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.units: list[_UnitOfWork] = []

    def __call__(self) -> UnitOfWork:
        unit = _UnitOfWork(self.state)
        self.units.append(unit)
        return cast(UnitOfWork, unit)


@dataclass(slots=True)
class _Harness:
    service: WebhookAdmissionService
    state: _State
    units: _UnitOfWorkFactory
    ids: _Ids
    verifier: _Verifier
    mapper: _Mapper
    resolver: _Resolver
    receipt_service: _ReceiptService
    order: list[str]


def _binding(instance_suffix: str, *, revision: int = 1) -> WebhookAdmissionBinding:
    return WebhookAdmissionBinding(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        instance_id=f"instance.api05.{instance_suffix}",
        workflow_id=f"workflow.api05.{instance_suffix}",
        configuration_revision=revision,
        mode=WorkMode.MOCK_EXECUTION,
        validator=_Validator(),
    )


def _command(raw_body: bytes = RAW_BODY) -> WebhookAdmissionCommand:
    timestamp = str(int(NOW.timestamp()))
    signed = WEBHOOK_SIGNATURE_DOMAIN + timestamp.encode("ascii") + b"\x00" + raw_body
    signature = hmac.digest(SECRET.encode(), signed, "sha256").hex()
    return WebhookAdmissionCommand(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        raw_body=raw_body,
        received_headers=(
            ("X-Webhook-Timestamp", timestamp),
            ("X-Webhook-Signature", f"v1={signature}"),
        ),
        correlation_id="correlation.api05.webhook.0001",
    )


def _stored_receipt(
    *,
    raw_body: bytes = RAW_BODY,
    receipt_id: str = "webhook-receipt.api05.original",
    deliveries: tuple[WebhookReceiptDelivery, ...] | None = None,
) -> WebhookReceipt:
    digest = derive_webhook_body_digest(raw_body, KEY)
    return WebhookReceipt(
        id=receipt_id,
        source=SOURCE,
        event_id=EVENT_ID,
        trigger_id=TRIGGER_ID,
        body_digest=digest.value,
        digest_key_version=digest.digest_key_version,
        mapper_version=StrictJsonWebhookEnvelopeMapper.version,
        received_at=NOW,
        deliveries=deliveries
        or (
            WebhookReceiptDelivery(
                instance_id="instance.api05.original",
                work_item_id="work.api05.original",
                run_id="run.api05.original",
            ),
        ),
    )


def _service(
    bindings: tuple[WebhookAdmissionBinding, ...] | None = None,
) -> _Harness:
    order: list[str] = []
    verifier = _Verifier(order)
    mapper = _Mapper(order)
    definition = WebhookSourceDefinition(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        mapper_version=StrictJsonWebhookEnvelopeMapper.version,
        signature_verifier=verifier,
        verifier_config=WebhookVerifierConfig(secret_reference=SECRET_REFERENCE),
        mapper=mapper,
    )
    state = _State()
    units = _UnitOfWorkFactory(state)
    ids = _Ids()
    resolver = _Resolver(bindings or (_binding("one"),))
    service = WebhookAdmissionService(
        OrchestrationDependencies(_Clock(), ids, units),
        KEY,
        _Registry(definition),
        resolver,
        current_catalog_hash=CATALOG_HASH,
    )
    receipt_service = _ReceiptService()
    service._receipt_service = cast(Any, receipt_service)
    return _Harness(
        service=service,
        state=state,
        units=units,
        ids=ids,
        verifier=verifier,
        mapper=mapper,
        resolver=resolver,
        receipt_service=receipt_service,
        order=order,
    )


def _event_types(harness: _Harness) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(event.event_type for event in batch) for batch in harness.state.audits.batches
    )


def test_api_05_receipt_bound_matches_the_fixed_43_instance_catalog() -> None:
    assert MAX_WEBHOOK_RECEIPT_DELIVERIES == 43
    deliveries = tuple(
        WebhookReceiptDelivery(
            instance_id=f"instance.api05.{index:02d}",
            work_item_id=f"work.api05.{index:02d}",
            run_id=f"run.api05.{index:02d}",
        )
        for index in range(MAX_WEBHOOK_RECEIPT_DELIVERIES + 1)
    )

    with pytest.raises(ValueError, match="bounded immutable tuple"):
        _stored_receipt(deliveries=deliveries)


@pytest.mark.asyncio
async def test_api_05_exact_replay_returns_original_links_without_resolving_work() -> None:
    harness = _service()
    original = _stored_receipt()
    harness.state.receipts.records[(SOURCE, EVENT_ID)] = original

    result = await harness.service.submit(_command())

    assert result == WebhookAdmissionResult(
        receipt=original,
        disposition=WebhookAdmissionDisposition.REPLAYED,
    )
    assert result.receipt is original
    assert harness.resolver.calls == []
    assert harness.receipt_service.calls == []
    assert harness.state.works.lookups == []
    assert harness.units.units[0].commits == 1
    assert _event_types(harness) == (
        ("webhook.signature_validated", "webhook.duplicate_suppressed"),
    )


@pytest.mark.asyncio
async def test_api_05_authenticated_same_event_different_body_commits_collision_only() -> None:
    harness = _service()
    original = _stored_receipt()
    harness.state.receipts.records[(SOURCE, EVENT_ID)] = original
    changed = b'{"eventId":"event.api05.webhook.0001","input":{"text":"changed"}}'

    with pytest.raises(WebhookAdmissionServiceError) as caught:
        await harness.service.submit(_command(changed))

    assert caught.value.code == "webhook_idempotency_conflict"
    assert harness.resolver.calls == []
    assert harness.receipt_service.calls == []
    assert harness.state.receipts.records[(SOURCE, EVENT_ID)] is original
    assert harness.units.units[0].commits == 1
    assert _event_types(harness) == (
        ("webhook.signature_validated", "webhook.idempotency_collision"),
    )


@pytest.mark.asyncio
async def test_api_05_rechecks_receipt_after_serialization_lock_wait() -> None:
    harness = _service((_binding("would-not-run"),))
    winner = _stored_receipt()
    harness.resolver.after_resolve = lambda: harness.state.receipts.records.__setitem__(
        (SOURCE, EVENT_ID),
        winner,
    )

    result = await harness.service.submit(_command())

    assert result.receipt is winner
    assert result.disposition is WebhookAdmissionDisposition.REPLAYED
    assert harness.state.receipts.lookups == [(SOURCE, EVENT_ID), (SOURCE, EVENT_ID)]
    assert harness.receipt_service.calls == []
    assert harness.state.works.lookups == []
    assert harness.units.units[0].commits == 1
    assert _event_types(harness) == (
        ("webhook.signature_validated", "webhook.duplicate_suppressed"),
    )


@pytest.mark.asyncio
async def test_api_05_exact_replay_ignores_later_binding_configuration_drift() -> None:
    harness = _service((_binding("original", revision=7),))
    command = _command()

    created = await harness.service.submit(command)
    harness.resolver.bindings = (_binding("replacement", revision=99),)
    harness.resolver.error = WebhookAdmissionResolutionError(
        "webhook_binding_unavailable",
        "later configuration is unavailable",
    )
    replayed = await harness.service.submit(command)

    assert created.disposition is WebhookAdmissionDisposition.CREATED
    assert replayed.disposition is WebhookAdmissionDisposition.REPLAYED
    assert replayed.receipt is created.receipt
    assert replayed.receipt.deliveries == created.receipt.deliveries
    assert len(harness.resolver.calls) == 1
    assert len(harness.receipt_service.calls) == 1
    assert tuple(unit.commits for unit in harness.units.units) == (1, 1)


@pytest.mark.asyncio
async def test_api_05_fan_out_uses_one_uow_and_one_receipt_for_every_binding() -> None:
    harness = _service((_binding("one"), _binding("two")))

    result = await harness.service.submit(_command())

    assert result.disposition is WebhookAdmissionDisposition.CREATED
    assert tuple(item.instance_id for item in result.receipt.deliveries) == (
        "instance.api05.one",
        "instance.api05.two",
    )
    assert tuple(item.work_item_id for item in result.receipt.deliveries) == (
        "work.api05.one",
        "work.api05.two",
    )
    unit = harness.units.units[0]
    assert len(harness.receipt_service.calls) == 2
    assert all(cast(object, call[0]) is unit for call in harness.receipt_service.calls)
    assert harness.state.receipts.adds == [result.receipt]
    assert unit.commits == 1
    assert _event_types(harness) == (("webhook.signature_validated", "webhook.received"),)


@pytest.mark.asyncio
async def test_api_05_partial_fan_out_failure_never_commits_receipt_or_audit() -> None:
    harness = _service((_binding("one"), _binding("two")))
    harness.receipt_service.fail_on_call = 2

    with pytest.raises(WebhookAdmissionServiceError) as caught:
        await harness.service.submit(_command())

    assert caught.value.code == "webhook_service_unavailable"
    assert len(harness.receipt_service.calls) == 2
    assert harness.state.receipts.adds == []
    assert harness.state.audits.batches == []
    unit = harness.units.units[0]
    assert unit.commits == 0
    assert unit.exit_error_types == [RuntimeError]


@pytest.mark.asyncio
async def test_api_05_preexisting_work_without_receipt_fails_closed_before_admission() -> None:
    binding = _binding("orphan")
    harness = _service((binding,))
    harness.state.works.existing_by_key[(SOURCE, EVENT_ID, binding.instance_id)] = object()

    with pytest.raises(WebhookAdmissionServiceError) as caught:
        await harness.service.submit(_command())

    assert caught.value.code == "webhook_receipt_missing"
    assert harness.receipt_service.calls == []
    assert harness.state.receipts.adds == []
    assert harness.state.audits.batches == []
    assert harness.units.units[0].commits == 0


@pytest.mark.asyncio
async def test_api_05_signature_rejection_precedes_json_mapping_and_work_resolution() -> None:
    harness = _service()
    harness.verifier.error = WebhookSignatureVerificationError("webhook_signature_invalid")

    with pytest.raises(WebhookAdmissionServiceError) as caught:
        await harness.service.submit(_command(b"not-json"))

    assert caught.value.code == "webhook_authentication_failed"
    assert harness.order == ["signature"]
    assert harness.mapper.calls == []
    assert harness.resolver.calls == []
    assert harness.state.receipts.lookups == []
    assert harness.units.units[0].commits == 1
    assert _event_types(harness) == (("webhook.signature_rejected",),)


@pytest.mark.asyncio
async def test_api_05_invalid_verifier_output_is_adapter_unavailable_not_auth_denial() -> None:
    harness = _service()
    harness.verifier.output_override = object()

    with pytest.raises(WebhookAdmissionServiceError) as caught:
        await harness.service.submit(_command())

    assert caught.value.code == "webhook_service_unavailable"
    assert str(caught.value) == "webhook admission is temporarily unavailable"
    assert harness.mapper.calls == []
    assert harness.resolver.calls == []
    assert harness.units.units == []


@pytest.mark.asyncio
async def test_api_05_signature_rejection_audit_failure_is_safe_and_fail_closed() -> None:
    harness = _service()
    harness.verifier.error = WebhookSignatureVerificationError("webhook_signature_invalid")
    harness.state.audits.fail = True

    with pytest.raises(WebhookAdmissionServiceError) as caught:
        await harness.service.submit(_command(b"not-json"))

    assert caught.value.code == "webhook_service_unavailable"
    assert str(caught.value) == "webhook admission is temporarily unavailable"
    assert CANARY not in str(caught.value)
    assert harness.order == ["signature"]
    assert harness.units.units[0].commits == 0


@pytest.mark.asyncio
async def test_api_05_authenticated_receipt_audit_failure_never_commits_admission() -> None:
    harness = _service((_binding("one"), _binding("two")))
    harness.state.audits.fail = True

    with pytest.raises(WebhookAdmissionServiceError) as caught:
        await harness.service.submit(_command())

    assert caught.value.code == "webhook_service_unavailable"
    assert str(caught.value) == "webhook admission is temporarily unavailable"
    assert CANARY not in str(caught.value)
    assert len(harness.receipt_service.calls) == 2
    unit = harness.units.units[0]
    assert unit.commits == 0
    assert unit.exit_error_types == [RuntimeError]


@pytest.mark.asyncio
async def test_api_05_schema_rejection_audit_failure_is_safe_and_fail_closed() -> None:
    harness = _service()
    harness.mapper.error = WebhookEnvelopeMappingError(
        "webhook_envelope_invalid",
        "untrusted mapper detail",
        pointer="/input/text",
    )
    harness.state.audits.fail = True

    with pytest.raises(WebhookAdmissionServiceError) as caught:
        await harness.service.submit(_command())

    assert caught.value.code == "webhook_service_unavailable"
    assert str(caught.value) == "webhook admission is temporarily unavailable"
    assert CANARY not in str(caught.value)
    assert harness.order == ["signature", "json"]
    assert harness.resolver.calls == []
    assert harness.units.units[0].commits == 0
