"""Canonical content identities for trusted JSON Schema snapshots."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from marketing_agents.domain.canonical_json import canonical_json_bytes

SCHEMA_HASH_PREFIX = "schema-sha256-v1:"
_SCHEMA_HASH = re.compile(r"^schema-sha256-v1:[0-9a-f]{64}$")


def canonical_schema_hash(schema: Mapping[str, Any]) -> str:
    """Return the stable identity of one object-shaped JSON Schema body."""

    if not isinstance(schema, Mapping):
        raise ValueError("JSON Schema must be an object mapping")
    return SCHEMA_HASH_PREFIX + hashlib.sha256(canonical_json_bytes(schema)).hexdigest()


def require_schema_hash(value: str, name: str) -> None:
    """Reject schema identities outside the versioned canonical hash contract."""

    if not isinstance(value, str) or _SCHEMA_HASH.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical JSON Schema hash")


__all__ = ["SCHEMA_HASH_PREFIX", "canonical_schema_hash", "require_schema_hash"]
