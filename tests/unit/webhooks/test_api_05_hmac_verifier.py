from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from marketing_agents.application.ports.webhooks import (
    WEBHOOK_SERVICE_ROLE,
    WEBHOOK_SUBMIT_SCOPE,
    VerifiedWebhookIdentity,
    WebhookSecretResolutionError,
    WebhookSignatureVerificationError,
    WebhookVerifierConfig,
    webhook_source_scope,
    webhook_trigger_scope,
)
from marketing_agents.config import Settings
from marketing_agents.domain.identity import AuthenticationMethod, PrincipalKind
from marketing_agents.infrastructure.webhook_signatures import (
    WEBHOOK_SIGNATURE_DOMAIN,
    EnvironmentWebhookSecretResolver,
    HmacSha256WebhookSignatureVerifier,
)

SOURCE = "source.api05"
TRIGGER_ID = "trigger.webhook.api05"
SECRET_REFERENCE = "env:MARKETING_AGENTS_WEBHOOK_API05_SECRET"
SECRET = "api05-unit-test-secret-material-32-bytes-minimum"
RECEIVED_AT = datetime(2026, 8, 26, 19, 30, tzinfo=UTC)
RAW_BODY = b'{"eventId":"evt-1","input":{"text":"exact bytes"}}'
ROOT = Path(__file__).resolve().parents[3]


def _config(**overrides: object) -> WebhookVerifierConfig:
    values: dict[str, object] = {"secret_reference": SECRET_REFERENCE}
    values.update(overrides)
    return WebhookVerifierConfig(**values)  # type: ignore[arg-type]


def _verifier(
    environment: dict[str, str] | None = None,
) -> HmacSha256WebhookSignatureVerifier:
    return HmacSha256WebhookSignatureVerifier(
        EnvironmentWebhookSecretResolver(
            {"MARKETING_AGENTS_WEBHOOK_API05_SECRET": SECRET}
            if environment is None
            else environment
        )
    )


def _signature(timestamp: str, raw_body: bytes = RAW_BODY, secret: str = SECRET) -> str:
    signed_payload = WEBHOOK_SIGNATURE_DOMAIN + timestamp.encode("ascii") + b"\x00" + raw_body
    digest = hmac.digest(secret.encode(), signed_payload, "sha256").hex()
    return f"v1={digest}"


def _headers(
    *,
    timestamp: str | None = None,
    signature: str | None = None,
) -> tuple[tuple[str, str], ...]:
    timestamp = str(int(RECEIVED_AT.timestamp())) if timestamp is None else timestamp
    signature = _signature(timestamp) if signature is None else signature
    return (
        ("content-type", "application/json"),
        ("X-Webhook-Timestamp", timestamp),
        ("x-WEBHOOK-signature", signature),
    )


def _verify(
    *,
    raw_body: bytes = RAW_BODY,
    headers: tuple[tuple[str, str], ...] | None = None,
    received_at: datetime = RECEIVED_AT,
    verifier: HmacSha256WebhookSignatureVerifier | None = None,
    config: WebhookVerifierConfig | None = None,
) -> VerifiedWebhookIdentity:
    return (verifier or _verifier()).verify(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        raw_body=raw_body,
        received_headers=_headers() if headers is None else headers,
        received_at=received_at,
        verifier_config=config or _config(),
    )


def _denial_code(**overrides: object) -> str:
    with pytest.raises(WebhookSignatureVerificationError) as caught:
        _verify(**overrides)  # type: ignore[arg-type]
    assert str(caught.value) == "webhook signature verification failed"
    return caught.value.code


def test_api_05_settings_and_example_never_expose_or_ship_a_webhook_secret() -> None:
    settings = Settings(_env_file=None, webhook_hmac_secret=SECRET)
    snapshot = settings.safe_snapshot()

    assert snapshot["webhook_hmac_secret"] == "[REDACTED]"
    assert SECRET not in repr(settings)
    assert SECRET not in json.dumps(snapshot, sort_keys=True)
    assert "WEBHOOK_HMAC_SECRET=" in (ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"WEBHOOK_HMAC_SECRET={SECRET}" not in (ROOT / ".env.example").read_text(
        encoding="utf-8"
    )


def test_api_05_verifies_exact_raw_bytes_and_issues_exact_service_authority() -> None:
    identity = _verify()

    assert identity.source == SOURCE
    assert identity.trigger_id == TRIGGER_ID
    assert identity.signed_at == RECEIVED_AT
    assert identity.verified_at == RECEIVED_AT
    assert identity.principal.kind is PrincipalKind.SERVICE
    assert identity.principal.authentication_method is AuthenticationMethod.VERIFIED_WEBHOOK
    assert identity.principal.roles == frozenset({WEBHOOK_SERVICE_ROLE})
    assert identity.principal.scopes == frozenset(
        {
            WEBHOOK_SUBMIT_SCOPE,
            webhook_source_scope(SOURCE),
            webhook_trigger_scope(TRIGGER_ID),
        }
    )
    identity.verify_integrity()

    assert _denial_code(raw_body=RAW_BODY + b" ") == "webhook_signature_invalid"


def test_api_05_rejects_direct_or_mutated_verified_identity() -> None:
    identity = _verify()
    with pytest.raises(ValueError, match="must be issued"):
        VerifiedWebhookIdentity(
            source=identity.source,
            trigger_id=identity.trigger_id,
            signed_at=identity.signed_at,
            verified_at=identity.verified_at,
            principal=identity.principal,
            _seal=object(),
        )

    object.__setattr__(identity, "trigger_id", "trigger.webhook.other")
    with pytest.raises(ValueError, match=r"does not match|changed after"):
        identity.verify_integrity()


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    (
        (
            (("x-webhook-timestamp", str(int(RECEIVED_AT.timestamp()))),),
            "webhook_signature_missing",
        ),
        (
            (("x-webhook-signature", _signature(str(int(RECEIVED_AT.timestamp())))),),
            "webhook_timestamp_missing",
        ),
        (
            (
                *_headers(),
                ("X-WEBHOOK-SIGNATURE", _signature(str(int(RECEIVED_AT.timestamp())))),
            ),
            "webhook_signature_duplicate",
        ),
        (
            (*_headers(), ("X-WEBHOOK-TIMESTAMP", str(int(RECEIVED_AT.timestamp())))),
            "webhook_timestamp_duplicate",
        ),
    ),
)
def test_api_05_rejects_missing_and_case_insensitive_duplicate_auth_headers(
    headers: tuple[tuple[str, str], ...],
    expected_code: str,
) -> None:
    assert _denial_code(headers=headers) == expected_code


@pytest.mark.parametrize(
    ("timestamp", "signature", "expected_code"),
    (
        ("01700000000", "v1=" + "0" * 64, "webhook_timestamp_malformed"),
        ("-1", "v1=" + "0" * 64, "webhook_timestamp_malformed"),
        ("1.0", "v1=" + "0" * 64, "webhook_timestamp_malformed"),
        (" 1700000000", "v1=" + "0" * 64, "webhook_timestamp_malformed"),
        (str(int(RECEIVED_AT.timestamp())), "0" * 64, "webhook_signature_malformed"),
        (str(int(RECEIVED_AT.timestamp())), "v2=" + "0" * 64, "webhook_signature_malformed"),
        (str(int(RECEIVED_AT.timestamp())), "v1=" + "A" * 64, "webhook_signature_malformed"),
        (str(int(RECEIVED_AT.timestamp())), "v1=" + "0" * 63, "webhook_signature_malformed"),
        (
            str(int(RECEIVED_AT.timestamp())),
            "v1=" + "0" * 64 + ",v1=x",
            "webhook_signature_malformed",
        ),
    ),
)
def test_api_05_rejects_noncanonical_timestamp_and_signature_headers(
    timestamp: str,
    signature: str,
    expected_code: str,
) -> None:
    assert _denial_code(headers=_headers(timestamp=timestamp, signature=signature)) == expected_code


def test_api_05_freshness_and_future_skew_boundaries_are_inclusive() -> None:
    for delta in (-300, 30):
        timestamp = str(int((RECEIVED_AT + timedelta(seconds=delta)).timestamp()))
        identity = _verify(headers=_headers(timestamp=timestamp, signature=_signature(timestamp)))
        assert identity.signed_at == RECEIVED_AT + timedelta(seconds=delta)

    stale = str(int((RECEIVED_AT - timedelta(seconds=301)).timestamp()))
    future = str(int((RECEIVED_AT + timedelta(seconds=31)).timestamp()))
    assert (
        _denial_code(headers=_headers(timestamp=stale, signature=_signature(stale)))
        == "webhook_signature_stale"
    )
    assert (
        _denial_code(headers=_headers(timestamp=future, signature=_signature(future)))
        == "webhook_signature_future"
    )


def test_api_05_uses_constant_time_digest_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    compared: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def compare(left: str, right: str) -> bool:
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(
        "marketing_agents.infrastructure.webhook_signatures.hmac.compare_digest",
        compare,
    )

    _verify()

    assert len(compared) == 1
    assert all(len(value) == 64 for value in compared[0])


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    (
        (
            (("x-webhook-signature", "v1=" + "0" * 64), ("bad header", "value")),
            "webhook_headers_invalid",
        ),
        (
            (("x-webhook-signature", "v1=" + "0" * 64), ("x-other", "bad\rvalue")),
            "webhook_headers_invalid",
        ),
        (
            (("x-webhook-signature", "v1=" + "0" * 64), ("x-other", "café")),
            "webhook_headers_invalid",
        ),
    ),
)
def test_api_05_rejects_malformed_received_header_collections(
    headers: tuple[tuple[str, str], ...],
    expected_code: str,
) -> None:
    assert _denial_code(headers=headers) == expected_code


def test_api_05_rejects_non_bytes_and_defense_in_depth_body_overflow() -> None:
    with pytest.raises(WebhookSignatureVerificationError) as wrong_type:
        _verifier().verify(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            raw_body=bytearray(RAW_BODY),  # type: ignore[arg-type]
            received_headers=_headers(),
            received_at=RECEIVED_AT,
            verifier_config=_config(),
        )
    assert wrong_type.value.code == "webhook_body_invalid"

    assert (
        _denial_code(
            raw_body=b"12345",
            config=_config(max_body_bytes=4),
        )
        == "webhook_body_too_large"
    )


@pytest.mark.parametrize(
    ("environment", "reference", "expected_code"),
    (
        ({}, SECRET_REFERENCE, "webhook_secret_unavailable"),
        (
            {"MARKETING_AGENTS_WEBHOOK_API05_SECRET": "too-short"},
            SECRET_REFERENCE,
            "webhook_secret_invalid",
        ),
        (
            {"MARKETING_AGENTS_WEBHOOK_API05_SECRET": SECRET + "\n"},
            SECRET_REFERENCE,
            "webhook_secret_invalid",
        ),
        (
            {"MARKETING_AGENTS_WEBHOOK_API05_SECRET": SECRET},
            "literal-secret-material-must-not-be-accepted",
            "webhook_secret_reference_invalid",
        ),
    ),
)
def test_api_05_secret_resolution_is_reference_only_and_fails_closed(
    environment: dict[str, str],
    reference: str,
    expected_code: str,
) -> None:
    resolver = EnvironmentWebhookSecretResolver(environment)
    with pytest.raises(WebhookSecretResolutionError) as caught:
        resolver.resolve(reference)
    assert caught.value.code == expected_code
    assert SECRET not in repr(resolver)
    assert SECRET not in repr(caught.value)
    assert SECRET not in str(caught.value)


def test_api_05_config_and_proof_representations_exclude_secret_reference() -> None:
    config = _config()
    identity = _verify(config=config)

    assert SECRET_REFERENCE not in repr(config)
    assert SECRET_REFERENCE not in str(config.safe_snapshot())
    assert SECRET not in repr(_verifier())
    assert "principal=[SEALED]" in repr(identity)
