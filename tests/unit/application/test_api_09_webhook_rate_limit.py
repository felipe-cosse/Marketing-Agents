"""API-09: authenticated webhook admission has a bounded per-source rate."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.ports.webhook_sources import (
    MappedWebhookEnvelope,
    WebhookEnvelopeMappingError,
    WebhookSourceDefinition,
)
from marketing_agents.application.ports.webhooks import WebhookVerifierConfig
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.application.services.webhook_intake import (
    WebhookAdmissionCommand,
    WebhookAdmissionService,
    WebhookAdmissionServiceError,
)
from marketing_agents.application.services.webhook_rate_limit import (
    ProcessLocalWebhookAdmissionRateLimiter,
    WebhookAdmissionRateLimiterUnavailable,
)
from marketing_agents.domain.audit import AuditContext, AuditOutcome
from marketing_agents.infrastructure.webhook_signatures import (
    WEBHOOK_SIGNATURE_DOMAIN,
    EnvironmentWebhookSecretResolver,
    HmacSha256WebhookSignatureVerifier,
)
from marketing_agents.security.digest_key import DigestKey

NOW = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)
SOURCE = "source.api09.rate"
TRIGGER = "trigger.api09.rate"
SECRET_REFERENCE = "env:MARKETING_AGENTS_API09_WEBHOOK_SECRET"
SECRET = "api09-webhook-rate-limit-test-secret"


class _Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


class _Ids:
    def new(self, namespace: str) -> str:
        return f"{namespace}.api09.rate"


class _UnusedUnitOfWorkFactory:
    def __call__(self) -> UnitOfWork:
        raise AssertionError("rate denial must precede persistence")


class _RecordingMapper:
    version = "mapper.api09.rate.v1"

    def __init__(self) -> None:
        self.calls = 0

    def parse(self, _raw_body: bytes) -> MappedWebhookEnvelope:
        self.calls += 1
        raise WebhookEnvelopeMappingError(
            "webhook_envelope_invalid",
            "safe invalid envelope",
        )


class _Registry:
    def __init__(self, definition: WebhookSourceDefinition) -> None:
        self.definition = definition

    def resolve(self, source: str, trigger_id: str) -> WebhookSourceDefinition | None:
        if (source, trigger_id) == (SOURCE, TRIGGER):
            return self.definition
        return None


class _NeverResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_all_in_uow(self, *_args: object, **_kwargs: object) -> tuple[()]:
        self.calls += 1
        raise AssertionError("rate denial must precede admission resolution")


def _command(*, valid_signature: bool) -> WebhookAdmissionCommand:
    raw_body = b'{"eventId":"event.api09.rate","input":{}}'
    timestamp = str(int(NOW.timestamp()))
    signed = WEBHOOK_SIGNATURE_DOMAIN + timestamp.encode("ascii") + b"\x00" + raw_body
    signature = hmac.digest(SECRET.encode(), signed, "sha256").hex()
    if not valid_signature:
        signature = "0" * 64
    return WebhookAdmissionCommand(
        source=SOURCE,
        trigger_id=TRIGGER,
        raw_body=raw_body,
        received_headers=(
            ("X-Webhook-Timestamp", timestamp),
            ("X-Webhook-Signature", f"v1={signature}"),
        ),
        correlation_id="correlation.api09.webhook.rate",
    )


def test_api_09_rate_limiter_uses_exact_windows_and_resists_clock_rollback() -> None:
    limiter = ProcessLocalWebhookAdmissionRateLimiter()

    first = limiter.consume(
        source=SOURCE,
        observed_at=NOW,
        max_calls=2,
        window_seconds=10,
    )
    second = limiter.consume(
        source=SOURCE,
        observed_at=NOW + timedelta(seconds=1),
        max_calls=2,
        window_seconds=10,
    )
    denied = limiter.consume(
        source=SOURCE,
        observed_at=NOW + timedelta(seconds=9, milliseconds=1),
        max_calls=2,
        window_seconds=10,
    )
    rolled_back = limiter.consume(
        source=SOURCE,
        observed_at=NOW - timedelta(minutes=1),
        max_calls=2,
        window_seconds=10,
    )
    boundary = limiter.consume(
        source=SOURCE,
        observed_at=NOW + timedelta(seconds=10),
        max_calls=2,
        window_seconds=10,
    )

    assert first.allowed is True
    assert second.allowed is True
    assert (denied.allowed, denied.retry_after_seconds) == (False, 1)
    assert (rolled_back.allowed, rolled_back.retry_after_seconds) == (False, 1)
    assert boundary.allowed is True


def test_api_09_rate_limiter_bounds_trusted_source_cardinality_and_prunes() -> None:
    limiter = ProcessLocalWebhookAdmissionRateLimiter(max_tracked_sources=2)
    for source in ("source.api09.one", "source.api09.two"):
        assert limiter.consume(
            source=source,
            observed_at=NOW,
            max_calls=1,
            window_seconds=10,
        ).allowed

    with pytest.raises(WebhookAdmissionRateLimiterUnavailable):
        limiter.consume(
            source="source.api09.three",
            observed_at=NOW,
            max_calls=1,
            window_seconds=10,
        )
    assert limiter.tracked_source_count() == 2

    assert limiter.consume(
        source="source.api09.three",
        observed_at=NOW + timedelta(seconds=10),
        max_calls=1,
        window_seconds=10,
    ).allowed
    assert limiter.tracked_source_count() == 1


@pytest.mark.asyncio
async def test_api_09_only_authenticated_sources_consume_and_denial_precedes_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    mapper = _RecordingMapper()
    resolver = _NeverResolver()
    limiter = ProcessLocalWebhookAdmissionRateLimiter()
    verifier = HmacSha256WebhookSignatureVerifier(
        EnvironmentWebhookSecretResolver({"MARKETING_AGENTS_API09_WEBHOOK_SECRET": SECRET})
    )
    definition = WebhookSourceDefinition(
        source=SOURCE,
        trigger_id=TRIGGER,
        mapper_version=mapper.version,
        signature_verifier=verifier,
        verifier_config=WebhookVerifierConfig(secret_reference=SECRET_REFERENCE),
        mapper=mapper,
        admission_rate_max_calls=1,
        admission_rate_window_seconds=60,
    )
    service = WebhookAdmissionService(
        OrchestrationDependencies(
            clock,
            _Ids(),
            cast(Any, _UnusedUnitOfWorkFactory()),
        ),
        DigestKey(bytes(range(32))),
        _Registry(definition),
        resolver,
        current_catalog_hash="catalog-sha256-v1:" + ("a" * 64),
        admission_rate_limiter=limiter,
    )

    async def no_audit(*_args: object, **_kwargs: object) -> None:
        return None

    rate_denials: list[tuple[str, int]] = []

    async def audit_rate_denial(
        _command: object,
        *,
        attempt_id: str,
        retry_after_seconds: int,
        **_kwargs: object,
    ) -> None:
        rate_denials.append((attempt_id, retry_after_seconds))

    monkeypatch.setattr(service, "_audit_signature_rejected", no_audit)
    monkeypatch.setattr(service, "_audit_schema_rejected_or_unavailable", no_audit)
    monkeypatch.setattr(service, "_audit_authenticated_rate_denial", audit_rate_denial)

    with pytest.raises(WebhookAdmissionServiceError) as unsigned:
        await service.submit(_command(valid_signature=False))
    assert unsigned.value.code == "webhook_authentication_failed"
    assert limiter.tracked_source_count() == 0

    with pytest.raises(WebhookAdmissionServiceError) as mapped:
        await service.submit(_command(valid_signature=True))
    assert mapped.value.code == "webhook_envelope_invalid"
    assert mapper.calls == 1
    assert resolver.calls == 0
    assert rate_denials == []

    with pytest.raises(WebhookAdmissionServiceError) as limited:
        await service.submit(_command(valid_signature=True))
    assert limited.value.code == "webhook_rate_limited"
    assert limited.value.retry_after_seconds == 60
    assert mapper.calls == 1
    assert resolver.calls == 0
    assert rate_denials == [("webhook-ingress.api09.rate", 60)]


def test_api_09_rate_limit_audit_is_verified_redacted_and_bounded() -> None:
    factory = AuditEventFactory(
        AuditContext.verified_webhook(
            "service.api09.verified-webhook",
            correlation_id="correlation.api09.webhook.rate",
        )
    )
    event = factory.webhook_rate_limited(
        source=SOURCE,
        trigger_id=TRIGGER,
        webhook_attempt_id="webhook-ingress.api09.audit",
        retry_after_seconds=59,
        occurred_at=NOW,
    )

    assert event.event_type == "ingress.rate_limited"
    assert event.aggregate_type == "webhook_ingress"
    assert event.outcome is AuditOutcome.REJECTED
    assert event.reason_code == "rate_limit_exhausted"
    assert event.mutation_version is None
    assert event.safe_metadata.values == {
        "source": SOURCE,
        "trigger_id": TRIGGER,
        "webhook_attempt_id": "webhook-ingress.api09.audit",
        "retry_after_seconds": 59,
    }
    event.verify_integrity()
    rendered = repr(event)
    for canary in (SECRET, SECRET_REFERENCE, _command(valid_signature=True).raw_body):
        assert repr(canary) not in rendered

    for invalid in (0, 3_601, True):
        with pytest.raises(ValueError, match="bounded positive integer"):
            factory.webhook_rate_limited(
                source=SOURCE,
                trigger_id=TRIGGER,
                webhook_attempt_id=f"webhook-ingress.api09.invalid-{invalid}",
                retry_after_seconds=invalid,
                occurred_at=NOW,
            )
