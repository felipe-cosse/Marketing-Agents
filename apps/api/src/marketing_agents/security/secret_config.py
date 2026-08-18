"""Central field-aware masking for configuration and diagnostic projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


REDACTED = "[REDACTED]"
SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "session_key",
    "token",
)


class SecretValue:
    """A small secret wrapper whose string forms never reveal the value."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("secret value must be a nonempty string")
        self.__value = value

    def reveal(self) -> str:
        """Return the value only at the narrow integration boundary that needs it."""
        return self.__value

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

    def __str__(self) -> str:
        return REDACTED


def is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_config(value: Any, *, field_name: str | None = None) -> Any:
    """Return a recursively redacted, serialization-safe diagnostic projection."""
    if field_name is not None and is_sensitive_key(field_name):
        return REDACTED
    if isinstance(value, SecretValue):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(key): redact_config(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_config(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"
