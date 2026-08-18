"""Central schema-aware masking for configuration, audit, problems, and projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from marketing_agents.domain.data_classification import DataClassification

REDACTED = "[REDACTED]"
SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "email",
    "full_name",
    "password",
    "phone",
    "private_key",
    "secret",
    "session_key",
    "signature",
    "token",
)
REDACTED_CLASSIFICATIONS = {
    DataClassification.PERSONAL.value,
    DataClassification.SENSITIVE.value,
    DataClassification.SECRET.value,
}


class SecretValue:
    """A small secret wrapper whose display forms never reveal the value."""

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


def _schema_requires_redaction(schema: Mapping[str, Any] | None) -> bool:
    if schema is None:
        return False
    if schema.get("x-sensitive") is True:
        return True
    return schema.get("x-data-classification") in REDACTED_CLASSIFICATIONS


def redact(
    value: Any,
    *,
    schema: Mapping[str, Any] | None = None,
    field_name: str | None = None,
) -> Any:
    """Return a recursively redacted copy without mutating the source value."""
    if _schema_requires_redaction(schema):
        return REDACTED
    if field_name is not None and is_sensitive_key(field_name):
        return REDACTED
    if isinstance(value, SecretValue):
        return REDACTED
    if isinstance(value, Mapping):
        properties = schema.get("properties", {}) if schema is not None else {}
        return {
            str(key): redact(
                item,
                schema=properties.get(str(key)) if isinstance(properties, Mapping) else None,
                field_name=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items_schema = schema.get("items") if schema is not None else None
        item_mapping = items_schema if isinstance(items_schema, Mapping) else None
        return [redact(item, schema=item_mapping) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def redact_config(value: Any, *, field_name: str | None = None) -> Any:
    """Backward-compatible configuration projection using the central redactor."""
    return redact(value, field_name=field_name)
