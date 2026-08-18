"""Keyed, domain-separated canonical digests for admitted work."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities._validation import require_digest
from marketing_agents.security.digest_key import DigestKey, digest_key_fingerprint

PAYLOAD_DIGEST_DOMAIN = b"marketing-agents:admitted-payload:hmac-sha256:v1\x00"
ADMISSION_DIGEST_DOMAIN = b"marketing-agents:work-admission:hmac-sha256:v1\x00"
DIGEST_KEY_VERSION_PREFIX = "admission-hmac-sha256-v1:"
DIGEST_KEY_VERSION_PATTERN = re.compile(rf"^{re.escape(DIGEST_KEY_VERSION_PREFIX)}[0-9a-f]{{64}}$")


@dataclass(frozen=True, slots=True, repr=False)
class AdmissionDigests:
    input_digest: str
    admission_digest: str
    digest_key_version: str

    def __post_init__(self) -> None:
        require_digest(self.input_digest, "input digest")
        require_digest(self.admission_digest, "admission digest")
        if DIGEST_KEY_VERSION_PATTERN.fullmatch(self.digest_key_version) is None:
            raise ValueError("digest key version is invalid")

    def __repr__(self) -> str:
        return "AdmissionDigests([REDACTED])"


def admission_digest_key_version(key: DigestKey) -> str:
    fingerprint = digest_key_fingerprint(key)
    prefix = "digest-key-fingerprint-v1:"
    if not fingerprint.startswith(prefix):
        raise ValueError("digest key fingerprint version is unsupported")
    return DIGEST_KEY_VERSION_PREFIX + fingerprint.removeprefix(prefix)


def derive_admission_digests(
    envelope: AdmissionEnvelope,
    key: DigestKey,
) -> AdmissionDigests:
    """Bind canonical payload and complete routing context to the installed key."""

    key_bytes = key.bytes_for_digest()
    payload_bytes = canonical_json_bytes(envelope.admitted_payload)
    input_digest = hmac.new(
        key_bytes,
        PAYLOAD_DIGEST_DOMAIN + payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    digest_key_version = admission_digest_key_version(key)
    projection = {
        "canonicalization_version": 1,
        "digest_scheme": "hmac-sha256-v1",
        "digest_key_version": digest_key_version,
        "source": envelope.source,
        "event_id": envelope.event_id,
        "instance_id": envelope.instance_id,
        "trigger_id": envelope.trigger_id,
        "workflow_id": envelope.workflow_id,
        "mode": envelope.mode.value,
        "campaign_brief": (
            None
            if envelope.brief_id is None
            else {"id": envelope.brief_id, "revision": envelope.brief_revision}
        ),
        "configuration_revision": envelope.configuration_revision,
        "admitted_payload": envelope.admitted_payload,
    }
    admission_digest = hmac.new(
        key_bytes,
        ADMISSION_DIGEST_DOMAIN + canonical_json_bytes(projection),
        hashlib.sha256,
    ).hexdigest()
    return AdmissionDigests(
        input_digest=input_digest,
        admission_digest=admission_digest,
        digest_key_version=digest_key_version,
    )
