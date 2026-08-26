"""Keyed, domain-separated digests for authenticated webhook bodies."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from marketing_agents.domain.webhook import (
    WEBHOOK_DIGEST_KEY_VERSION_PATTERN,
    WEBHOOK_DIGEST_KEY_VERSION_PREFIX,
)
from marketing_agents.security.digest_key import DigestKey, digest_key_fingerprint

WEBHOOK_BODY_DIGEST_DOMAIN = b"marketing-agents:webhook-body:hmac-sha256:v1\x00"
WEBHOOK_BODY_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True, repr=False)
class WebhookBodyDigest:
    """Restricted comparison material; never expose it through transport or audit."""

    value: str
    digest_key_version: str

    def __post_init__(self) -> None:
        if WEBHOOK_BODY_DIGEST_PATTERN.fullmatch(self.value) is None:
            raise ValueError("webhook body digest is invalid")
        if WEBHOOK_DIGEST_KEY_VERSION_PATTERN.fullmatch(self.digest_key_version) is None:
            raise ValueError("webhook body digest key version is invalid")

    def matches(self, other: WebhookBodyDigest) -> bool:
        if type(other) is not WebhookBodyDigest:
            return False
        return self.digest_key_version == other.digest_key_version and hmac.compare_digest(
            self.value,
            other.value,
        )

    def __repr__(self) -> str:
        return "WebhookBodyDigest([REDACTED])"


def derive_webhook_body_digest(raw_body: bytes, key: DigestKey) -> WebhookBodyDigest:
    """Digest exact authenticated bytes without retaining or canonicalizing them."""

    if type(raw_body) is not bytes:
        raise TypeError("webhook body digest requires exact bytes")
    if type(key) is not DigestKey:
        raise TypeError("webhook body digest requires the installed digest key")
    return WebhookBodyDigest(
        value=hmac.new(
            key.bytes_for_digest(),
            WEBHOOK_BODY_DIGEST_DOMAIN + raw_body,
            hashlib.sha256,
        ).hexdigest(),
        digest_key_version=(
            WEBHOOK_DIGEST_KEY_VERSION_PREFIX
            + digest_key_fingerprint(key).removeprefix("digest-key-fingerprint-v1:")
        ),
    )


__all__ = ["WebhookBodyDigest", "derive_webhook_body_digest"]
