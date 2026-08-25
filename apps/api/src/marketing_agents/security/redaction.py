"""Central schema-aware masking for configuration, audit, problems, and projections."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from marketing_agents.domain.canonical_json import canonical_json_bytes
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
_PERSONAL_KEY_FRAGMENTS = ("email", "full_name", "phone")
_SECRET_KEY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "api_token",
        "authorization",
        "bearer_token",
        "credential",
        "password",
        "private_key",
        "secret",
        "session_key",
        "token",
    }
)
_SECRET_KEY_FRAGMENTS = (
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
_POINTER_INDEX = re.compile(r"0|[1-9][0-9]*")
_MAX_POINTERS = 64
_MAX_POINTER_LENGTH = 1_000
_MAX_POINTER_SEGMENTS = 64


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


def _schema_classification(
    schema: Mapping[str, Any] | None,
) -> DataClassification | None:
    if schema is None:
        return None
    raw = schema.get("x-data-classification")
    if raw is not None:
        try:
            return DataClassification(raw)
        except ValueError as exc:
            raise ValueError("schema data classification is invalid") from exc
    if schema.get("x-sensitive") is True:
        return DataClassification.SENSITIVE
    return None


def _key_classification(field_name: str | None) -> DataClassification | None:
    if field_name is None:
        return None
    normalized = field_name.strip().lower().replace("-", "_")
    if normalized in _SECRET_KEY_NAMES:
        return DataClassification.SECRET
    if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
        # A suspicious key is a defense-in-depth signal, not proof that the
        # value is configured secret material. Only an explicit schema label
        # or SecretValue wrapper promotes data to the non-retainable class.
        return DataClassification.SENSITIVE
    if any(fragment in normalized for fragment in _PERSONAL_KEY_FRAGMENTS):
        return DataClassification.PERSONAL
    if is_sensitive_key(field_name):
        return DataClassification.SENSITIVE
    return None


def _higher_classification(
    left: DataClassification,
    right: DataClassification | None,
) -> DataClassification:
    if right is None:
        return left
    order = tuple(DataClassification)
    return right if order.index(right) > order.index(left) else left


def _mapping_child_schema(
    schema: Mapping[str, Any] | None,
    key: str,
) -> Mapping[str, Any] | None:
    if schema is None:
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return None
    exact = properties.get(key)
    if isinstance(exact, Mapping):
        return exact
    wildcard = properties.get("*")
    return wildcard if isinstance(wildcard, Mapping) else None


def _sequence_child_schema(
    schema: Mapping[str, Any] | None,
    index: int,
) -> Mapping[str, Any] | None:
    if schema is None:
        return None
    prefix_items = schema.get("prefixItems")
    if (
        isinstance(prefix_items, Sequence)
        and not isinstance(prefix_items, (str, bytes, bytearray))
        and index < len(prefix_items)
        and isinstance(prefix_items[index], Mapping)
    ):
        return cast(Mapping[str, Any], prefix_items[index])
    items = schema.get("items")
    if isinstance(items, Mapping):
        return items
    # Pointer-derived projection schemas represent array wildcards as a `*`
    # property. Supporting that shape keeps the central redactor compatible
    # with persisted operation metadata without weakening normal JSON Schema.
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        exact = properties.get(str(index))
        if isinstance(exact, Mapping):
            return exact
        wildcard = properties.get("*")
        if isinstance(wildcard, Mapping):
            return wildcard
    return None


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
        return {
            str(key): redact(
                item,
                schema=_mapping_child_schema(schema, str(key)),
                field_name=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            redact(item, schema=_sequence_child_schema(schema, index))
            for index, item in enumerate(value)
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def redact_config(value: Any, *, field_name: str | None = None) -> Any:
    """Backward-compatible configuration projection using the central redactor."""
    return redact(value, field_name=field_name)


def redaction_classification(
    value: Any,
    *,
    schema: Mapping[str, Any] | None = None,
    field_name: str | None = None,
) -> DataClassification:
    """Return the highest schema or defense-in-depth classification in a value."""

    classification = DataClassification.INTERNAL
    classification = _higher_classification(classification, _schema_classification(schema))
    classification = _higher_classification(classification, _key_classification(field_name))
    if isinstance(value, SecretValue):
        return DataClassification.SECRET
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = redaction_classification(
                item,
                schema=_mapping_child_schema(schema, str(key)),
                field_name=str(key),
            )
            classification = _higher_classification(classification, child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            child = redaction_classification(
                item,
                schema=_sequence_child_schema(schema, index),
            )
            classification = _higher_classification(classification, child)
    return classification


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        raise ValueError("redaction pointer must be a non-root RFC 6901 pointer")
    if len(pointer) > _MAX_POINTER_LENGTH:
        raise ValueError("redaction pointer is too long")
    encoded = pointer[1:].split("/")
    if not encoded or len(encoded) > _MAX_POINTER_SEGMENTS or any(not item for item in encoded):
        raise ValueError("redaction pointer has invalid segments")
    decoded: list[str] = []
    for token in encoded:
        if re.search(r"~(?![01])", token) is not None:
            raise ValueError("redaction pointer contains an invalid escape")
        decoded.append(token.replace("~1", "/").replace("~0", "~"))
    return tuple(decoded)


def _mask_pointer(value: Any, tokens: tuple[str, ...], pointer: str) -> Any:
    if value == REDACTED:
        return REDACTED
    if not tokens:
        return REDACTED
    token, remaining = tokens[0], tokens[1:]
    if isinstance(value, dict):
        if token == "*":
            return {key: _mask_pointer(item, remaining, pointer) for key, item in value.items()}
        if token not in value:
            return value
        mapping_copy = dict(value)
        mapping_copy[token] = _mask_pointer(mapping_copy[token], remaining, pointer)
        return mapping_copy
    if isinstance(value, list):
        if token == "*":
            return [_mask_pointer(item, remaining, pointer) for item in value]
        if _POINTER_INDEX.fullmatch(token) is None:
            raise ValueError(f"redaction pointer {pointer!r} has an invalid array index")
        index = int(token)
        if index >= len(value):
            return value
        sequence_copy = list(value)
        sequence_copy[index] = _mask_pointer(sequence_copy[index], remaining, pointer)
        return sequence_copy
    raise ValueError(f"redaction pointer {pointer!r} traverses a scalar value")


def redact_json_pointers(
    value: Any,
    pointers: Sequence[str],
    *,
    schema: Mapping[str, Any] | None = None,
) -> Any:
    """Copy strict JSON and mask exact or wildcard RFC 6901 pointer paths.

    The central schema and key heuristics run first. Pointer `*` tokens then
    match every object member or array item at that level. Missing optional
    members are harmless; malformed paths or paths that traverse scalar data
    fail closed instead of returning an ambiguously redacted projection.
    """

    if isinstance(pointers, (str, bytes, bytearray)) or not isinstance(pointers, Sequence):
        raise ValueError("redaction pointers must be a sequence of strings")
    if len(pointers) > _MAX_POINTERS or any(not isinstance(item, str) for item in pointers):
        raise ValueError("redaction pointers are invalid or unbounded")
    decoded = tuple((pointer, _decode_pointer(pointer)) for pointer in pointers)
    if len({pointer for pointer, _tokens in decoded}) != len(decoded):
        raise ValueError("redaction pointers must be unique")
    projected = json.loads(canonical_json_bytes(redact(value, schema=schema)))
    for pointer, tokens in decoded:
        projected = _mask_pointer(projected, tokens, pointer)
    return projected
