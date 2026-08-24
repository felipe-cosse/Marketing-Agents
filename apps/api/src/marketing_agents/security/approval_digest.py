"""Keyed, domain-separated corruption-detection digests for approval records."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.security.digest_key import DigestKey

_REQUEST_DOMAIN = b"marketing-agents:approval-request-record:hmac-sha256:v1\x00"
_DECISION_DOMAIN = b"marketing-agents:approval-decision-record:hmac-sha256:v1\x00"
_USE_DOMAIN = b"marketing-agents:approval-use-record:hmac-sha256:v1\x00"
_SET_DOMAIN = b"marketing-agents:authorization-set-record:hmac-sha256:v1\x00"
_SET_HEAD_DOMAIN = b"marketing-agents:authorization-set-head-record:hmac-sha256:v1\x00"
_SET_MEMBER_DOMAIN = b"marketing-agents:authorization-set-member-record:hmac-sha256:v1\x00"


def _record_digest(
    domain: bytes,
    material: Mapping[str, Any],
    key: DigestKey,
) -> str:
    return hmac.new(
        key.bytes_for_digest(),
        domain + canonical_json_bytes(material),
        hashlib.sha256,
    ).hexdigest()


def approval_request_record_digest(
    material: Mapping[str, Any],
    key: DigestKey,
) -> str:
    """Bind every persisted request leaf and lifecycle scalar to the installed key."""

    return _record_digest(_REQUEST_DOMAIN, material, key)


def approval_decision_record_digest(
    material: Mapping[str, Any],
    key: DigestKey,
) -> str:
    """Bind one append-only decision fact to the installed key."""

    return _record_digest(_DECISION_DOMAIN, material, key)


def approval_use_record_digest(
    material: Mapping[str, Any],
    key: DigestKey,
) -> str:
    """Bind one reservation/use fact to the installed key."""

    return _record_digest(_USE_DOMAIN, material, key)


def authorization_set_record_digest(
    material: Mapping[str, Any],
    key: DigestKey,
) -> str:
    """Bind one authorization-set lifecycle and release snapshot."""

    return _record_digest(_SET_DOMAIN, material, key)


def authorization_set_head_record_digest(
    material: Mapping[str, Any],
    key: DigestKey,
) -> str:
    """Bind the run-owned pointer selecting one current authorization epoch."""

    return _record_digest(_SET_HEAD_DOMAIN, material, key)


def authorization_set_member_record_digest(
    material: Mapping[str, Any],
    key: DigestKey,
) -> str:
    """Bind stable membership plus any atomic release-use projection."""

    return _record_digest(_SET_MEMBER_DOMAIN, material, key)
