"""Fail-closed Draft 2020-12 JSON Schema compilation and instance validation."""

from __future__ import annotations

import json
import re
from asyncio import current_task
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from threading import get_ident
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.validation import frozen_json_mapping, require_id

DRAFT_2020_12_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_LOCAL_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef"})
_LEGACY_RECURSIVE_KEYWORDS = frozenset({"$recursiveAnchor", "$recursiveRef"})
_SAFE_POINTER_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SAFE_POINTER_ROOT = re.compile(r"^/[A-Za-z0-9_.-]{1,100}(?:/[A-Za-z0-9_.-]{1,100})*$")
_RFC3339_DATE_TIME = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)

_SCHEMA_CHECK_MAX_ENTRIES = 128
_SCHEMA_CHECK_MAX_BYTES = 1_048_576
_SchemaCheckKey = tuple[bytes, str | None]


def _current_task_identity() -> object | None:
    try:
        return current_task()
    except RuntimeError:
        return None


@dataclass(slots=True)
class _SchemaDefinitionChecks:
    owner_thread: int = field(default_factory=get_ident)
    owner_task: object | None = field(default_factory=_current_task_identity)
    successful: set[_SchemaCheckKey] = field(default_factory=set)
    retained_bytes: int = 0
    active: bool = True

    def belongs_to_current_call(self) -> bool:
        return (
            self.active
            and self.owner_thread == get_ident()
            and self.owner_task is _current_task_identity()
        )

    def remember(self, key: _SchemaCheckKey) -> None:
        size = len(key[0]) + (0 if key[1] is None else len(key[1].encode("utf-8")))
        if (
            self.belongs_to_current_call()
            and key not in self.successful
            and len(self.successful) < _SCHEMA_CHECK_MAX_ENTRIES
            and size <= _SCHEMA_CHECK_MAX_BYTES - self.retained_bytes
        ):
            self.successful.add(key)
            self.retained_bytes += size


_SCHEMA_DEFINITION_CHECKS: ContextVar[_SchemaDefinitionChecks | None] = ContextVar(
    "schema_definition_checks", default=None
)


@contextmanager
def schema_definition_check_scope() -> Iterator[None]:
    """AC-18: reuse successful structural checks only inside one owned build.

    Every compile still reads/canonicalizes its supplied mapping and checks its
    identity, dialect and references, then creates a fresh validator. No failed
    compilation is retained. Nested builds and inherited concurrent contexts
    cannot reuse another build's checks; escaped contexts expire on exit.
    """
    checks = _SchemaDefinitionChecks()
    token = _SCHEMA_DEFINITION_CHECKS.set(checks)
    try:
        yield
    finally:
        checks.active = False
        checks.successful.clear()
        checks.retained_bytes = 0
        _SCHEMA_DEFINITION_CHECKS.reset(token)


class JsonSchemaPolicyError(ValueError):
    """One safe, non-reflective schema-policy or instance-validation failure."""

    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.pointer = pointer


def _schema_policy_error(code: str, message: str) -> JsonSchemaPolicyError:
    return JsonSchemaPolicyError(code, message)


def _is_rfc3339_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if _RFC3339_DATE_TIME.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _format_checker() -> FormatChecker:
    checker = FormatChecker()
    if "date-time" not in checker.checkers:
        checker.checks("date-time")(_is_rfc3339_date_time)
    return checker


def _validate_schema_identity(
    schema: Mapping[str, Any], expected_schema_id: str | None
) -> str | None:
    expected_identity_invalid = False
    if expected_schema_id is not None:
        try:
            require_id(expected_schema_id, "expected JSON Schema ID")
        except (TypeError, ValueError):
            expected_identity_invalid = True
    if expected_identity_invalid:
        raise _schema_policy_error("schema_identity_mismatch", "JSON Schema identity is invalid")

    embedded_schema_id = schema.get("$id")
    if "$id" in schema and not isinstance(embedded_schema_id, str):
        raise _schema_policy_error("schema_identity_mismatch", "JSON Schema identity is invalid")
    if (
        expected_schema_id is not None
        and embedded_schema_id is not None
        and embedded_schema_id != expected_schema_id
    ):
        raise _schema_policy_error(
            "schema_identity_mismatch", "JSON Schema identity does not match"
        )
    return embedded_schema_id


def _validate_local_references(schema: Mapping[str, Any]) -> None:
    policy_failure: JsonSchemaPolicyError | None = None
    resolution_failed = False
    try:
        root_resource = DRAFT202012.create_resource(schema)
        base_uri = root_resource.id() or "urn:marketing-agents:json-schema:local"
        registry = Registry().with_resource(base_uri, root_resource).crawl()
        root_scope = id(root_resource.contents)
        anchors_by_scope: dict[int, set[str]] = {root_scope: set()}
        pending: list[tuple[Any, Any, int]] = [
            (root_resource, registry.resolver(base_uri), root_scope)
        ]
        while pending:
            resource, resolver, scope = pending.pop()
            contents = resource.contents
            if isinstance(contents, Mapping):
                dialect = contents.get("$schema")
                if "$schema" in contents and dialect != DRAFT_2020_12_DIALECT:
                    raise _schema_policy_error(
                        "schema_dialect_unsupported",
                        "JSON Schema dialect is unsupported",
                    )
                if any(keyword in contents for keyword in _LEGACY_RECURSIVE_KEYWORDS):
                    raise _schema_policy_error(
                        "schema_invalid",
                        "JSON Schema contains an unsupported reference keyword",
                    )
                anchors = anchors_by_scope[scope]
                for anchor_keyword in ("$anchor", "$dynamicAnchor"):
                    anchor = contents.get(anchor_keyword)
                    if isinstance(anchor, str):
                        if anchor in anchors:
                            raise _schema_policy_error(
                                "schema_invalid",
                                "JSON Schema local anchors must be unique",
                            )
                        anchors.add(anchor)
                for keyword in _LOCAL_REFERENCE_KEYWORDS:
                    if keyword not in contents:
                        continue
                    reference = contents[keyword]
                    if not isinstance(reference, str) or not reference.startswith("#"):
                        raise _schema_policy_error(
                            "schema_reference_nonlocal",
                            "JSON Schema references must remain within the schema document",
                        )
                    resolver.lookup(reference)
            for subresource in resource.subresources():
                subresource_scope = scope
                if subresource.id() is not None:
                    subresource_scope = id(subresource.contents)
                    anchors_by_scope.setdefault(subresource_scope, set())
                pending.append(
                    (
                        subresource,
                        resolver.in_subresource(subresource),
                        subresource_scope,
                    )
                )
    except JsonSchemaPolicyError as exc:
        policy_failure = exc
    except Exception:
        resolution_failed = True
    if policy_failure is not None:
        raise policy_failure
    if resolution_failed:
        raise _schema_policy_error(
            "schema_invalid", "JSON Schema contains an unresolved local reference"
        )


def _json_depth_exceeds(value: Any, maximum: int) -> bool:
    active_containers: set[int] = set()

    def visit(current: Any, depth: int) -> bool:
        if depth > maximum:
            return True
        is_mapping = isinstance(current, Mapping)
        is_sequence = isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        )
        if not is_mapping and not is_sequence:
            return False

        identity = id(current)
        if identity in active_containers:
            raise JsonSchemaPolicyError("instance_invalid", "payload is not an acyclic JSON value")
        active_containers.add(identity)
        try:
            values = current.values() if is_mapping else current
            return any(visit(item, depth + 1) for item in values)
        finally:
            active_containers.remove(identity)

    return visit(value, 1)


def _path_sort_token(value: Any) -> tuple[int, str | int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return (0, value)
    if isinstance(value, str) and _SAFE_POINTER_TOKEN.fullmatch(value):
        return (1, value)
    return (2, "")


def _validation_error_sort_key(error: Any) -> tuple[Any, ...]:
    instance_path = tuple(_path_sort_token(part) for part in error.absolute_path)
    schema_path = tuple(_path_sort_token(part) for part in error.absolute_schema_path)
    validator = error.validator
    safe_validator = (
        validator if isinstance(validator, str) and _SAFE_POINTER_TOKEN.fullmatch(validator) else ""
    )
    return (instance_path, schema_path, safe_validator)


def _safe_error_pointer(error: Any, pointer_root: str) -> str:
    tokens: list[str] = []
    for part in error.absolute_path:
        if isinstance(part, int) and not isinstance(part, bool) and part >= 0:
            tokens.append(str(part))
        elif isinstance(part, str) and _SAFE_POINTER_TOKEN.fullmatch(part):
            tokens.append(part.replace("~", "~0").replace("/", "~1"))
        else:
            return pointer_root
    if not tokens:
        return pointer_root
    return pointer_root + "/" + "/".join(tokens)


@dataclass(frozen=True, slots=True)
class CompiledJsonSchema:
    """Canonical immutable schema snapshot paired with its checked validator."""

    schema: Mapping[str, Any]
    schema_id: str | None
    _validator: Draft202012Validator = field(repr=False, compare=False)

    def validate(self, instance: Any, *, pointer_root: str, max_depth: int) -> None:
        """Validate one instance and emit only deterministic, non-reflective failures."""

        if (
            not isinstance(pointer_root, str)
            or len(pointer_root) > 1_000
            or _SAFE_POINTER_ROOT.fullmatch(pointer_root) is None
        ):
            raise ValueError("JSON Schema pointer root must be a safe non-root pointer")
        if (
            not isinstance(max_depth, int)
            or isinstance(max_depth, bool)
            or not 1 <= max_depth <= 64
        ):
            raise ValueError("JSON Schema maximum depth must be an integer from 1 through 64")

        depth_failure: tuple[str, str] | None = None
        depth_evaluation_failed = False
        try:
            exceeds_depth = _json_depth_exceeds(instance, max_depth)
        except JsonSchemaPolicyError as exc:
            depth_failure = (exc.code, str(exc))
            exceeds_depth = False
        except Exception:
            depth_evaluation_failed = True
            exceeds_depth = False
        if depth_failure is not None:
            raise JsonSchemaPolicyError(depth_failure[0], depth_failure[1], pointer=pointer_root)
        if depth_evaluation_failed:
            raise JsonSchemaPolicyError(
                "instance_invalid",
                "payload could not be traversed safely",
                pointer=pointer_root,
            )
        if exceeds_depth:
            raise JsonSchemaPolicyError(
                "json_depth_limit",
                "payload nesting limit exceeded",
                pointer=pointer_root,
            )

        canonical_instance: Any = None
        instance_canonicalization_failed = False
        try:
            canonical_instance = json.loads(canonical_json_bytes(instance))
        except Exception:
            instance_canonicalization_failed = True
        if instance_canonicalization_failed:
            raise JsonSchemaPolicyError(
                "instance_invalid",
                "payload is not strict canonical JSON",
                pointer=pointer_root,
            )

        selected_error: Any | None = None
        selected_key: tuple[Any, ...] | None = None
        evaluation_failed = False
        try:
            for error in self._validator.iter_errors(canonical_instance):
                key = _validation_error_sort_key(error)
                if selected_key is None or key < selected_key:
                    selected_error = error
                    selected_key = key
        except Exception:
            evaluation_failed = True
        if evaluation_failed:
            raise JsonSchemaPolicyError(
                "schema_invalid",
                "JSON Schema could not be evaluated safely",
                pointer=pointer_root,
            )

        if selected_error is not None:
            raise JsonSchemaPolicyError(
                "instance_invalid",
                "payload does not conform to its schema",
                pointer=_safe_error_pointer(selected_error, pointer_root),
            )


def compile_json_schema(
    schema: Mapping[str, Any], *, expected_schema_id: str | None = None
) -> CompiledJsonSchema:
    """Canonicalize and compile one object-shaped Draft 2020-12 JSON Schema."""

    if not isinstance(schema, Mapping):
        raise _schema_policy_error("schema_invalid", "JSON Schema must be an object")
    canonical_schema: Any = None
    canonical_bytes = b""
    schema_canonicalization_failed = False
    try:
        canonical_bytes = canonical_json_bytes(schema)
        canonical_schema = json.loads(canonical_bytes)
    except Exception:
        schema_canonicalization_failed = True
    if schema_canonicalization_failed:
        raise _schema_policy_error("schema_invalid", "JSON Schema must be strict canonical JSON")
    if not isinstance(canonical_schema, dict):
        raise _schema_policy_error("schema_invalid", "JSON Schema must be an object")

    dialect = canonical_schema.get("$schema")
    if "$schema" in canonical_schema and dialect != DRAFT_2020_12_DIALECT:
        raise _schema_policy_error(
            "schema_dialect_unsupported", "JSON Schema dialect is unsupported"
        )
    embedded_schema_id = _validate_schema_identity(canonical_schema, expected_schema_id)
    checks = _SCHEMA_DEFINITION_CHECKS.get()
    if checks is not None and (
        not checks.belongs_to_current_call()
        or (expected_schema_id is not None and type(expected_schema_id) is not str)
    ):
        # Preserve accepted string-subclass behavior without invoking custom
        # hashing/encoding methods for an optional optimization.
        checks = None
    check_key = (canonical_bytes, expected_schema_id)

    validator: Draft202012Validator | None = None
    schema_definition_invalid = False
    try:
        if checks is None or check_key not in checks.successful:
            Draft202012Validator.check_schema(canonical_schema)
        validator = Draft202012Validator(canonical_schema, format_checker=_format_checker())
    except (SchemaError, TypeError, ValueError):
        schema_definition_invalid = True
    if schema_definition_invalid or validator is None:
        raise _schema_policy_error("schema_invalid", "JSON Schema definition is invalid")
    _validate_local_references(canonical_schema)

    compiled = CompiledJsonSchema(
        schema=frozen_json_mapping(canonical_schema, "JSON Schema"),
        schema_id=embedded_schema_id,
        _validator=validator,
    )
    if checks is not None:
        checks.remember(check_key)
    return compiled


__all__ = [
    "DRAFT_2020_12_DIALECT",
    "CompiledJsonSchema",
    "JsonSchemaPolicyError",
    "compile_json_schema",
    "schema_definition_check_scope",
]
