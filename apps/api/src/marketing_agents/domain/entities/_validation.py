"""Small validation helpers shared only by pure domain entities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

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


def require_unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique identifiers")
    for value in values:
        require_id(value, field_name)
