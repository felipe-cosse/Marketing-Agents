"""Domain-separated HMACs for durable runtime-control records."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.security.digest_key import DigestKey

_CONTROL_DOMAIN = b"marketing-agents:execution-control-record:hmac-sha256:v1\x00"
_OPERATION_DOMAIN = b"marketing-agents:execution-operation-record:hmac-sha256:v1\x00"
_ATTEMPT_DOMAIN = b"marketing-agents:execution-attempt-record:hmac-sha256:v1\x00"
_RATE_WINDOW_DOMAIN = b"marketing-agents:rate-window-record:hmac-sha256:v1\x00"


def _digest(domain: bytes, material: Mapping[str, Any], key: DigestKey) -> str:
    return hmac.new(
        key.bytes_for_digest(),
        domain + canonical_json_bytes(material),
        hashlib.sha256,
    ).hexdigest()


def execution_control_record_digest(material: Mapping[str, Any], key: DigestKey) -> str:
    return _digest(_CONTROL_DOMAIN, material, key)


def execution_operation_record_digest(material: Mapping[str, Any], key: DigestKey) -> str:
    return _digest(_OPERATION_DOMAIN, material, key)


def execution_attempt_record_digest(material: Mapping[str, Any], key: DigestKey) -> str:
    return _digest(_ATTEMPT_DOMAIN, material, key)


def rate_limit_window_record_digest(material: Mapping[str, Any], key: DigestKey) -> str:
    return _digest(_RATE_WINDOW_DOMAIN, material, key)
