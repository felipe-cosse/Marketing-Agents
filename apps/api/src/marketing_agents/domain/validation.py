"""Small validation helpers shared by framework-independent domain modules."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any, cast

from marketing_agents.domain.canonical_json import canonical_json_bytes

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a stable nonempty identifier")


def require_text(value: str, field_name: str, *, maximum: int = 1_000) -> None:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > maximum:
        raise ValueError(f"{field_name} must be nonempty, trimmed, and bounded")


def require_digest(value: str, field_name: str) -> None:
    if DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def require_utc(value: datetime, field_name: str) -> None:
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")


def frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _deep_freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_deep_freeze_json(item) for item in value)
    return value


def frozen_json_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    """Validate one strict canonical-JSON object and recursively freeze its snapshot."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    normalized = json.loads(canonical_json_bytes(value))
    return cast(Mapping[str, Any], _deep_freeze_json(normalized))


def require_unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique identifiers")
    for value in values:
        require_id(value, field_name)


def require_json_pointers(values: tuple[str, ...], field_name: str) -> tuple[tuple[str, ...], ...]:
    """Validate unique, non-root RFC 6901 paths and reject overlapping leaves."""

    if type(values) is not tuple or any(type(value) is not str for value in values):
        raise ValueError(f"{field_name} must be an immutable string tuple")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique JSON pointers")
    decoded_paths: list[tuple[str, ...]] = []
    for pointer in values:
        if not pointer.startswith("/") or pointer == "/" or len(pointer) > 1_000:
            raise ValueError(f"{field_name} must contain non-root JSON pointers")
        encoded_tokens = pointer[1:].split("/")
        if any(not token or re.search(r"~(?![01])", token) is not None for token in encoded_tokens):
            raise ValueError(f"{field_name} contains an invalid JSON pointer")
        decoded = tuple(token.replace("~1", "/").replace("~0", "~") for token in encoded_tokens)
        if any(
            decoded[: len(existing)] == existing or existing[: len(decoded)] == decoded
            for existing in decoded_paths
        ):
            raise ValueError(f"{field_name} must not contain overlapping JSON pointers")
        decoded_paths.append(decoded)
    return tuple(decoded_paths)
