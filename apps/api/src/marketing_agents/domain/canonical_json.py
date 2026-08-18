"""Strict, Unicode-normalized canonical JSON for integrity and idempotency inputs."""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


class CanonicalJsonError(ValueError):
    """Raised when a value has no unambiguous canonical JSON representation."""


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalJsonError("non-finite numbers are not canonical JSON")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise CanonicalJsonError("canonical JSON object keys must be strings")
        normalized = {
            unicodedata.normalize("NFC", key): _normalize(item) for key, item in value.items()
        }
        if len(normalized) != len(value):
            raise CanonicalJsonError("object keys collide after Unicode normalization")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item) for item in value]
    raise CanonicalJsonError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _normalize(value)
    return json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
