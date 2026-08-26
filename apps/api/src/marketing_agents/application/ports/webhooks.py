"""Application-facing contracts for authenticated webhook signatures."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    PrincipalKind,
)
from marketing_agents.domain.validation import require_id, require_utc
from marketing_agents.security.redaction import SecretValue

DEFAULT_WEBHOOK_SIGNATURE_HEADER = "x-webhook-signature"
DEFAULT_WEBHOOK_TIMESTAMP_HEADER = "x-webhook-timestamp"
DEFAULT_WEBHOOK_MAX_AGE_SECONDS = 300
DEFAULT_WEBHOOK_MAX_FUTURE_SKEW_SECONDS = 30
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 1_048_576
WEBHOOK_SERVICE_ROLE = "webhook_service"
WEBHOOK_SUBMIT_SCOPE = "webhook:submit"
WEBHOOK_SOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,99}$"
WEBHOOK_TRIGGER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,199}$"

_MAX_SECRET_REFERENCE_LENGTH = 240
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9a-z-]{1,128}$")
_SECRET_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9._:/-]{0,239}$")
_SOURCE_ID = re.compile(WEBHOOK_SOURCE_ID_PATTERN)
_TRIGGER_ID = re.compile(WEBHOOK_TRIGGER_ID_PATTERN)
_VERIFIED_WEBHOOK_IDENTITY_SEAL = object()
_VERIFIED_WEBHOOK_IDENTITY_DOMAIN = b"marketing-agents:verified-webhook-identity:v1\x00"

type WebhookReceivedHeaders = tuple[tuple[str, str], ...]


def webhook_source_scope(source: str) -> str:
    """Return the one exact source grant represented by a verified identity."""

    require_webhook_source_id(source, "webhook source")
    return f"webhook:source:{source}"


def webhook_trigger_scope(trigger_id: str) -> str:
    """Return the one exact trigger grant represented by a verified identity."""

    require_webhook_trigger_id(trigger_id, "webhook trigger ID")
    return f"webhook:trigger:{trigger_id}"


def require_webhook_source_id(value: str, field_name: str) -> None:
    if type(value) is not str or _SOURCE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be one bounded path-segment identifier")


def require_webhook_trigger_id(value: str, field_name: str) -> None:
    if type(value) is not str or _TRIGGER_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be one bounded path-segment identifier")


class WebhookSignatureVerificationError(PermissionError):
    """Safe authentication denial that never retains signature material."""

    def __init__(self, code: str) -> None:
        require_id(code, "webhook signature error code")
        super().__init__("webhook signature verification failed")
        self.code = code


class WebhookSecretResolutionError(RuntimeError):
    """Safe fail-closed result for an unavailable configured secret reference."""

    def __init__(self, code: str) -> None:
        require_id(code, "webhook secret resolution error code")
        super().__init__("webhook signature secret is unavailable")
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookVerifierConfig:
    """Non-secret verifier policy plus an opaque runtime secret reference."""

    secret_reference: str = field(repr=False)
    signature_header: str = DEFAULT_WEBHOOK_SIGNATURE_HEADER
    timestamp_header: str = DEFAULT_WEBHOOK_TIMESTAMP_HEADER
    max_age_seconds: int = DEFAULT_WEBHOOK_MAX_AGE_SECONDS
    max_future_skew_seconds: int = DEFAULT_WEBHOOK_MAX_FUTURE_SKEW_SECONDS
    max_body_bytes: int = DEFAULT_WEBHOOK_MAX_BODY_BYTES

    def __post_init__(self) -> None:
        if (
            type(self.secret_reference) is not str
            or len(self.secret_reference) > _MAX_SECRET_REFERENCE_LENGTH
            or _SECRET_REFERENCE.fullmatch(self.secret_reference) is None
        ):
            raise ValueError("webhook secret reference must be one bounded opaque reference")
        for header, field_name in (
            (self.signature_header, "webhook signature header"),
            (self.timestamp_header, "webhook timestamp header"),
        ):
            if type(header) is not str or _HEADER_NAME.fullmatch(header) is None:
                raise ValueError(f"{field_name} must be one normalized HTTP field name")
        if self.signature_header == self.timestamp_header:
            raise ValueError("webhook signature and timestamp headers must be distinct")
        if type(self.max_age_seconds) is not int or not 1 <= self.max_age_seconds <= 86_400:
            raise ValueError("webhook signature maximum age must be from 1 to 86400 seconds")
        if (
            type(self.max_future_skew_seconds) is not int
            or not 0 <= self.max_future_skew_seconds <= 300
        ):
            raise ValueError("webhook signature future skew must be from 0 to 300 seconds")
        if (
            type(self.max_body_bytes) is not int
            or not 1 <= self.max_body_bytes <= DEFAULT_WEBHOOK_MAX_BODY_BYTES
        ):
            raise ValueError("webhook verifier body bound must be from 1 byte to 1 MiB")

    def safe_snapshot(self) -> dict[str, str | int]:
        """Return operational policy without disclosing even the secret reference."""

        return {
            "signature_header": self.signature_header,
            "timestamp_header": self.timestamp_header,
            "max_age_seconds": self.max_age_seconds,
            "max_future_skew_seconds": self.max_future_skew_seconds,
            "max_body_bytes": self.max_body_bytes,
        }


@dataclass(frozen=True, slots=True, init=False, repr=False)
class VerifiedWebhookIdentity:
    """Sealed proof that exact raw bytes authenticated one source and trigger."""

    source: str
    trigger_id: str
    signed_at: datetime
    verified_at: datetime
    principal: AuthenticatedPrincipal = field(repr=False)
    issuance_fingerprint: str = field(repr=False)

    def __init__(
        self,
        *,
        source: str,
        trigger_id: str,
        signed_at: datetime,
        verified_at: datetime,
        principal: AuthenticatedPrincipal,
        _seal: object,
    ) -> None:
        if _seal is not _VERIFIED_WEBHOOK_IDENTITY_SEAL:
            raise ValueError("verified webhook identities must be issued by a signature adapter")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "trigger_id", trigger_id)
        object.__setattr__(self, "signed_at", signed_at)
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "principal", principal)
        self._validate()
        object.__setattr__(self, "issuance_fingerprint", self._fingerprint())

    def _validate(self) -> None:
        require_webhook_source_id(self.source, "verified webhook source")
        require_webhook_trigger_id(self.trigger_id, "verified webhook trigger ID")
        if type(self.signed_at) is not datetime or type(self.verified_at) is not datetime:
            raise ValueError("verified webhook times must use exact datetime values")
        require_utc(self.signed_at, "verified webhook signed time")
        require_utc(self.verified_at, "verified webhook verification time")
        if type(self.principal) is not AuthenticatedPrincipal:
            raise ValueError("verified webhook identity requires an exact principal")
        self.principal.verify_integrity()
        expected_scopes = frozenset(
            {
                WEBHOOK_SUBMIT_SCOPE,
                webhook_source_scope(self.source),
                webhook_trigger_scope(self.trigger_id),
            }
        )
        if (
            self.principal.kind is not PrincipalKind.SERVICE
            or self.principal.authentication_method is not AuthenticationMethod.VERIFIED_WEBHOOK
            or self.principal.roles != frozenset({WEBHOOK_SERVICE_ROLE})
            or self.principal.scopes != expected_scopes
        ):
            raise ValueError("verified webhook principal authority does not match its binding")

    def _fingerprint(self) -> str:
        material = {
            "principal_fingerprint": self.principal.issuance_fingerprint,
            "signed_at": self.signed_at.isoformat(),
            "source": self.source,
            "trigger_id": self.trigger_id,
            "verified_at": self.verified_at.isoformat(),
        }
        return hashlib.sha256(
            _VERIFIED_WEBHOOK_IDENTITY_DOMAIN + canonical_json_bytes(material)
        ).hexdigest()

    def verify_integrity(self) -> None:
        """Fail closed if either the proof or its sealed principal was replaced."""

        self._validate()
        if not hmac.compare_digest(self.issuance_fingerprint, self._fingerprint()):
            raise ValueError("verified webhook identity changed after adapter issuance")

    def __repr__(self) -> str:
        return (
            "VerifiedWebhookIdentity("
            f"source={self.source!r}, trigger_id={self.trigger_id!r}, "
            f"signed_at={self.signed_at!r}, verified_at={self.verified_at!r}, "
            "principal=[SEALED])"
        )


def _issue_verified_webhook_identity(
    *,
    source: str,
    trigger_id: str,
    signed_at: datetime,
    verified_at: datetime,
    principal: AuthenticatedPrincipal,
) -> VerifiedWebhookIdentity:
    """Private issuance seam used only after infrastructure verifies the MAC."""

    return VerifiedWebhookIdentity(
        source=source,
        trigger_id=trigger_id,
        signed_at=signed_at,
        verified_at=verified_at,
        principal=principal,
        _seal=_VERIFIED_WEBHOOK_IDENTITY_SEAL,
    )


class WebhookSecretResolver(Protocol):
    """Resolve an opaque reference without making the secret configuration data."""

    def resolve(self, secret_reference: str) -> SecretValue: ...


class WebhookSignatureVerifier(Protocol):
    """Authenticate exact raw request bytes before any JSON processing."""

    def verify(
        self,
        *,
        source: str,
        trigger_id: str,
        raw_body: bytes,
        received_headers: WebhookReceivedHeaders,
        received_at: datetime,
        verifier_config: WebhookVerifierConfig,
    ) -> VerifiedWebhookIdentity: ...


__all__ = [
    "DEFAULT_WEBHOOK_MAX_AGE_SECONDS",
    "DEFAULT_WEBHOOK_MAX_BODY_BYTES",
    "DEFAULT_WEBHOOK_MAX_FUTURE_SKEW_SECONDS",
    "DEFAULT_WEBHOOK_SIGNATURE_HEADER",
    "DEFAULT_WEBHOOK_TIMESTAMP_HEADER",
    "WEBHOOK_SOURCE_ID_PATTERN",
    "WEBHOOK_TRIGGER_ID_PATTERN",
    "VerifiedWebhookIdentity",
    "WebhookReceivedHeaders",
    "WebhookSecretResolutionError",
    "WebhookSecretResolver",
    "WebhookSignatureVerificationError",
    "WebhookSignatureVerifier",
    "WebhookVerifierConfig",
    "require_webhook_source_id",
    "require_webhook_trigger_id",
    "webhook_source_scope",
    "webhook_trigger_scope",
]
