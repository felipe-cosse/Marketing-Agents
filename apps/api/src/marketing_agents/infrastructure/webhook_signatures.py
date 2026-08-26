"""Fail-closed raw-body HMAC-SHA256 webhook authentication."""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime

from marketing_agents.application.ports.webhooks import (
    VerifiedWebhookIdentity,
    WebhookReceivedHeaders,
    WebhookSecretResolutionError,
    WebhookSecretResolver,
    WebhookSignatureVerificationError,
    WebhookVerifierConfig,
    _issue_verified_webhook_identity,
    require_webhook_source_id,
    require_webhook_trigger_id,
)
from marketing_agents.domain.validation import require_utc
from marketing_agents.infrastructure.adapters.identity import (
    issue_verified_webhook_principal,
)
from marketing_agents.security.redaction import SecretValue

WEBHOOK_SIGNATURE_DOMAIN = b"marketing-agents:webhook-signature:hmac-sha256:v1\x00"
WEBHOOK_SIGNATURE_VERSION = "v1"
MIN_WEBHOOK_SECRET_BYTES = 32
MAX_WEBHOOK_SECRET_BYTES = 1_024
MAX_WEBHOOK_RECEIVED_HEADERS = 128
MAX_WEBHOOK_RECEIVED_HEADER_BYTES = 32_768

_ENV_SECRET_REFERENCE = re.compile(r"^env:([A-Z][A-Z0-9_]{0,127})$")
_RECEIVED_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_SIGNATURE = re.compile(r"^v1=([0-9a-f]{64})$")
_TIMESTAMP = re.compile(r"^(?:0|[1-9][0-9]{0,11})$")


def _secret_error(code: str) -> WebhookSecretResolutionError:
    return WebhookSecretResolutionError(code)


def _signature_error(code: str) -> WebhookSignatureVerificationError:
    return WebhookSignatureVerificationError(code)


class EnvironmentWebhookSecretResolver:
    """Resolve only explicit ``env:VARIABLE`` references at the verifier boundary."""

    __slots__ = ("_environment",)

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        if environment is not None and not isinstance(environment, Mapping):
            raise ValueError("webhook secret environment must be a string mapping")
        self._environment = os.environ if environment is None else environment

    def resolve(self, secret_reference: str) -> SecretValue:
        if type(secret_reference) is not str:
            raise _secret_error("webhook_secret_reference_invalid")
        matched = _ENV_SECRET_REFERENCE.fullmatch(secret_reference)
        if matched is None:
            raise _secret_error("webhook_secret_reference_invalid")
        secret = self._environment.get(matched.group(1))
        if type(secret) is not str:
            raise _secret_error("webhook_secret_unavailable")
        try:
            encoded = secret.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _secret_error("webhook_secret_invalid") from None
        if (
            not MIN_WEBHOOK_SECRET_BYTES <= len(encoded) <= MAX_WEBHOOK_SECRET_BYTES
            or secret != secret.strip()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in secret)
        ):
            raise _secret_error("webhook_secret_invalid")
        return SecretValue(secret)


class HmacSha256WebhookSignatureVerifier:
    """Verify canonical timestamp headers and exact raw bytes with one HMAC key."""

    __slots__ = ("_secret_resolver",)

    def __init__(self, secret_resolver: WebhookSecretResolver) -> None:
        if not callable(getattr(secret_resolver, "resolve", None)):
            raise ValueError("webhook verifier requires a secret resolver")
        self._secret_resolver = secret_resolver

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
        if type(verifier_config) is not WebhookVerifierConfig:
            raise ValueError("webhook verifier requires exact validated configuration")
        try:
            require_webhook_source_id(source, "webhook source")
            require_webhook_trigger_id(trigger_id, "webhook trigger ID")
        except ValueError:
            raise _signature_error("webhook_authority_invalid") from None
        if type(raw_body) is not bytes:
            raise _signature_error("webhook_body_invalid")
        if len(raw_body) > verifier_config.max_body_bytes:
            raise _signature_error("webhook_body_too_large")
        if type(received_at) is not datetime:
            raise ValueError("webhook received time must be timezone-aware UTC")
        try:
            require_utc(received_at, "webhook received time")
        except (AttributeError, TypeError, ValueError):
            raise ValueError("webhook received time must be timezone-aware UTC") from None

        signature_value, timestamp_value = self._authentication_headers(
            received_headers,
            verifier_config,
        )
        signature_match = _SIGNATURE.fullmatch(signature_value)
        if signature_match is None:
            raise _signature_error("webhook_signature_malformed")
        if _TIMESTAMP.fullmatch(timestamp_value) is None:
            raise _signature_error("webhook_timestamp_malformed")
        timestamp_seconds = int(timestamp_value)
        try:
            signed_at = datetime.fromtimestamp(timestamp_seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise _signature_error("webhook_timestamp_malformed") from None

        age_seconds = (received_at - signed_at).total_seconds()
        if age_seconds > verifier_config.max_age_seconds:
            raise _signature_error("webhook_signature_stale")
        if age_seconds < -verifier_config.max_future_skew_seconds:
            raise _signature_error("webhook_signature_future")

        secret = self._secret_resolver.resolve(verifier_config.secret_reference)
        if type(secret) is not SecretValue:
            raise _secret_error("webhook_secret_unavailable")
        secret_bytes = secret.reveal().encode("utf-8", errors="strict")
        if not MIN_WEBHOOK_SECRET_BYTES <= len(secret_bytes) <= MAX_WEBHOOK_SECRET_BYTES:
            raise _secret_error("webhook_secret_invalid")
        signed_payload = (
            WEBHOOK_SIGNATURE_DOMAIN + timestamp_value.encode("ascii") + b"\x00" + raw_body
        )
        expected = hmac.digest(secret_bytes, signed_payload, "sha256").hex()
        if not hmac.compare_digest(expected, signature_match.group(1)):
            raise _signature_error("webhook_signature_invalid")

        try:
            principal = issue_verified_webhook_principal(source=source, trigger_id=trigger_id)
            return _issue_verified_webhook_identity(
                source=source,
                trigger_id=trigger_id,
                signed_at=signed_at,
                verified_at=received_at,
                principal=principal,
            )
        except (TypeError, ValueError):
            raise _signature_error("webhook_identity_invalid") from None

    @staticmethod
    def _authentication_headers(
        received_headers: WebhookReceivedHeaders,
        verifier_config: WebhookVerifierConfig,
    ) -> tuple[str, str]:
        if (
            type(received_headers) is not tuple
            or len(received_headers) > MAX_WEBHOOK_RECEIVED_HEADERS
        ):
            raise _signature_error("webhook_headers_invalid")
        signature_values: list[str] = []
        timestamp_values: list[str] = []
        total_bytes = 0
        for item in received_headers:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise _signature_error("webhook_headers_invalid")
            name, value = item
            try:
                name_bytes = name.encode("ascii", errors="strict")
                value_bytes = value.encode("ascii", errors="strict")
            except UnicodeEncodeError:
                raise _signature_error("webhook_headers_invalid") from None
            total_bytes += len(name_bytes) + len(value_bytes)
            if (
                total_bytes > MAX_WEBHOOK_RECEIVED_HEADER_BYTES
                or _RECEIVED_HEADER_NAME.fullmatch(name) is None
                or any(byte < 0x20 or byte == 0x7F for byte in value_bytes)
            ):
                raise _signature_error("webhook_headers_invalid")
            normalized_name = name.lower()
            if normalized_name == verifier_config.signature_header:
                signature_values.append(value)
            elif normalized_name == verifier_config.timestamp_header:
                timestamp_values.append(value)

        if not signature_values:
            raise _signature_error("webhook_signature_missing")
        if len(signature_values) != 1:
            raise _signature_error("webhook_signature_duplicate")
        if not timestamp_values:
            raise _signature_error("webhook_timestamp_missing")
        if len(timestamp_values) != 1:
            raise _signature_error("webhook_timestamp_duplicate")
        return signature_values[0], timestamp_values[0]


__all__ = [
    "MAX_WEBHOOK_RECEIVED_HEADERS",
    "MAX_WEBHOOK_RECEIVED_HEADER_BYTES",
    "MAX_WEBHOOK_SECRET_BYTES",
    "MIN_WEBHOOK_SECRET_BYTES",
    "WEBHOOK_SIGNATURE_DOMAIN",
    "WEBHOOK_SIGNATURE_VERSION",
    "EnvironmentWebhookSecretResolver",
    "HmacSha256WebhookSignatureVerifier",
]
