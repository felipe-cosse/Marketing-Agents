"""Strict, unambiguous JSON validation for bounded HTTP request bodies."""

from __future__ import annotations

import json
import math
import unicodedata
from typing import Any, NoReturn

from starlette.types import Scope


class StrictJsonTransportError(ValueError):
    """A bounded request body is not canonical, unambiguous JSON transport."""


def _invalid_json() -> StrictJsonTransportError:
    return StrictJsonTransportError("request body is not strict JSON")


def _reject_constant(_value: str) -> NoReturn:
    raise _invalid_json()


def _strict_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _invalid_json()
    return parsed


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    normalized_keys: set[str] = set()
    for key, value in pairs:
        try:
            key.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _invalid_json() from None
        normalized = unicodedata.normalize("NFC", key)
        if normalized in normalized_keys:
            raise _invalid_json()
        normalized_keys.add(normalized)
        result[key] = value
    return result


def _depth_is_bounded(raw_body: bytes, *, max_depth: int) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw_body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in {0x5B, 0x7B}:
            depth += 1
            if depth > max_depth:
                return False
        elif byte in {0x5D, 0x7D}:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _strings_are_unicode_scalars(value: object) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if type(current) is str:
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return False
        elif type(current) is dict:
            pending.extend(current.values())
        elif type(current) is list:
            pending.extend(current)
    return True


def validate_strict_json_body(raw_body: bytes, *, max_depth: int) -> None:
    """Reject ambiguous or unsafe JSON after the caller has bounded byte length."""

    if type(raw_body) is not bytes or not raw_body:
        raise _invalid_json()
    if type(max_depth) is not int or max_depth < 1:
        raise ValueError("max_depth must be one positive exact integer")
    if raw_body.startswith(b"\xef\xbb\xbf") or not _depth_is_bounded(
        raw_body,
        max_depth=max_depth,
    ):
        raise _invalid_json()
    try:
        decoded = raw_body.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
            parse_float=_strict_float,
        )
    except StrictJsonTransportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise _invalid_json() from None
    if not _strings_are_unicode_scalars(value):
        raise _invalid_json()


def strict_json_route_path(scope: Scope) -> str:
    """Return the route-visible path after applying the ASGI mount root."""

    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    if not isinstance(path, str) or not isinstance(root_path, str) or not root_path:
        return path if isinstance(path, str) else ""
    if not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if len(path) > len(root_path) and path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def _utf8_json_content_type(value: bytes) -> bool:
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        return False
    parts = decoded.split(";")
    if parts[0].strip().casefold() != "application/json":
        return False
    if len(parts) == 1:
        return True
    charset_seen = False
    for raw_parameter in parts[1:]:
        parameter = raw_parameter.strip()
        if not parameter or parameter.count("=") != 1:
            return False
        name, raw_value = (part.strip() for part in parameter.split("=", 1))
        if name.casefold() != "charset" or charset_seen:
            return False
        charset_seen = True
        if raw_value.startswith('"') or raw_value.endswith('"'):
            if len(raw_value) < 2 or not (raw_value.startswith('"') and raw_value.endswith('"')):
                return False
            raw_value = raw_value[1:-1]
        if raw_value.casefold() != "utf-8":
            return False
    return charset_seen


def strict_json_transport_headers_are_valid(scope: Scope) -> bool:
    """Require one UTF-8 JSON media type and no content transformation."""

    content_types = [
        value for name, value in scope.get("headers", ()) if name.lower() == b"content-type"
    ]
    if len(content_types) != 1 or not _utf8_json_content_type(content_types[0]):
        return False
    return not any(name.lower() == b"content-encoding" for name, _value in scope.get("headers", ()))


__all__ = [
    "StrictJsonTransportError",
    "strict_json_route_path",
    "strict_json_transport_headers_are_valid",
    "validate_strict_json_body",
]
