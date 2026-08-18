"""Offline JSON Schema reference safety checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse


def reject_remote_or_escaped_refs(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "$ref":
                if not isinstance(item, str):
                    raise ValueError("JSON Schema $ref must be a string")
                parsed = urlparse(item)
                if parsed.scheme or parsed.netloc:
                    raise ValueError("remote JSON Schema references are forbidden")
                path = item.split("#", 1)[0]
                if path.startswith("/") or ".." in path.split("/"):
                    raise ValueError("JSON Schema reference escapes the catalog root")
            reject_remote_or_escaped_refs(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            reject_remote_or_escaped_refs(item)
