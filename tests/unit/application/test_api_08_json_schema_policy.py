"""API-08 / AC-18: fail-closed schema policies and bounded build-local reuse."""

from __future__ import annotations

import asyncio
from collections.abc import ItemsView, Iterator, Mapping, ValuesView
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import MappingProxyType
from typing import Any, cast

import pytest
from marketing_agents.application.policies import json_schema as schema_policy
from marketing_agents.application.policies.json_schema import (
    DRAFT_2020_12_DIALECT,
    JsonSchemaPolicyError,
    compile_json_schema,
    schema_definition_check_scope,
)
from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
    RuntimePolicyViolation,
)
from marketing_agents.application.ports.runtime_inputs import RuntimeInputContract
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification

_HOSTILE_MAPPING_CANARY = "provider-secret-canary"


class _HostileMapping(Mapping[str, Any]):
    def __init__(self, failing_method: str) -> None:
        self._failing_method = failing_method

    def __getitem__(self, key: str) -> Any:
        if key != "safe":
            raise KeyError(key)
        return "value"

    def __iter__(self) -> Iterator[str]:
        return iter(("safe",))

    def __len__(self) -> int:
        return 1

    def items(self) -> ItemsView[str, Any]:
        if self._failing_method == "items":
            raise RuntimeError(_HOSTILE_MAPPING_CANARY)
        return super().items()

    def values(self) -> ValuesView[Any]:
        if self._failing_method == "values":
            raise RuntimeError(_HOSTILE_MAPPING_CANARY)
        return super().values()


def _schema(*, schema_id: str | None = "schema.api-08.output.v1") -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": DRAFT_2020_12_DIALECT,
        "additionalProperties": False,
        "properties": {
            "published_at": {"format": "date-time", "type": "string"},
        },
        "required": ["published_at"],
        "type": "object",
    }
    if schema_id is not None:
        schema["$id"] = schema_id
    return schema


@pytest.fixture
def structural_checks(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    calls: list[bytes] = []
    original = schema_policy.Draft202012Validator.check_schema

    def check(schema: Any) -> None:
        calls.append(canonical_json_bytes(schema))
        original(schema)

    monkeypatch.setattr(schema_policy.Draft202012Validator, "check_schema", check)
    return calls


def test_ac_18_standalone_compilations_never_reuse_checks(structural_checks: list[bytes]) -> None:
    compile_json_schema(_schema())
    compile_json_schema(_schema())
    assert len(structural_checks) == 2


def test_ac_18_exact_schema_reuse_keeps_fresh_validators_and_payload_checks(
    structural_checks: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    references: list[bytes] = []
    original = schema_policy._validate_local_references

    def check_references(schema: Any) -> None:
        references.append(canonical_json_bytes(schema))
        original(schema)

    monkeypatch.setattr(schema_policy, "_validate_local_references", check_references)
    with schema_definition_check_scope():
        first = compile_json_schema(_schema())
        second = compile_json_schema(dict(reversed(tuple(_schema().items()))))
        assert first == second
        assert first is not second
        assert first._validator is not second._validator
        assert first._validator.schema is not second._validator.schema
        for compiled in (first, second):
            compiled.validate(
                {"published_at": "2026-10-19T12:00:00Z"}, pointer_root="/payload", max_depth=4
            )
            with pytest.raises(JsonSchemaPolicyError, match="does not conform"):
                compiled.validate(
                    {"published_at": "not-a-timestamp"}, pointer_root="/payload", max_depth=4
                )
    assert len(structural_checks) == 1
    assert len(references) == 2


def test_ac_18_reuse_keys_include_expected_identity(structural_checks: list[bytes]) -> None:
    schema = _schema(schema_id=None)
    with schema_definition_check_scope():
        for expected in ("schema.ac-18.one", "schema.ac-18.two", "schema.ac-18.one", None):
            compile_json_schema(schema, expected_schema_id=expected)
    assert len(structural_checks) == 3


def test_ac_18_string_subclass_identity_falls_back_without_new_hash_requirements(
    structural_checks: list[bytes],
) -> None:
    class UnhashableIdentity(str):
        __hash__ = None

    with schema_definition_check_scope():
        for _ in range(2):
            compile_json_schema(
                _schema(), expected_schema_id=UnhashableIdentity("schema.api-08.output.v1")
            )
    assert len(structural_checks) == 2


def test_ac_18_fresh_content_and_identity_are_checked_after_a_cache_hit(
    structural_checks: list[bytes],
) -> None:
    schema = _schema()
    with schema_definition_check_scope():
        compile_json_schema(schema, expected_schema_id="schema.api-08.output.v1")
        compile_json_schema(schema, expected_schema_id="schema.api-08.output.v1")
        with pytest.raises(JsonSchemaPolicyError) as wrong_id:
            compile_json_schema(schema, expected_schema_id="schema.ac-18.other")
        assert wrong_id.value.code == "schema_identity_mismatch"
        schema["type"] = "not-a-json-type"
        for _ in range(2):
            with pytest.raises(JsonSchemaPolicyError) as invalid:
                compile_json_schema(schema, expected_schema_id="schema.api-08.output.v1")
            assert invalid.value.code == "schema_invalid"
        schema["type"] = "object"
        compile_json_schema(schema, expected_schema_id="schema.api-08.output.v1")
    assert len(structural_checks) == 3


@pytest.mark.parametrize(
    ("mutation", "code", "expected_checks"),
    [
        ({"$ref": "https://unapproved.invalid/schema"}, "schema_reference_nonlocal", 3),
        ({"$ref": "#/$defs/missing"}, "schema_invalid", 3),
        ({"$schema": "unsupported-dialect"}, "schema_dialect_unsupported", 1),
        (
            {"$defs": {"nested": {"$schema": "unsupported-dialect"}}},
            "schema_dialect_unsupported",
            3,
        ),
    ],
)
def test_ac_18_policy_failures_are_fresh_and_never_cached(
    structural_checks: list[bytes], mutation: dict[str, Any], code: str, expected_checks: int
) -> None:
    schema = _schema()
    with schema_definition_check_scope():
        compile_json_schema(schema)
        schema.update(mutation)
        for _ in range(2):
            with pytest.raises(JsonSchemaPolicyError) as invalid:
                compile_json_schema(schema)
            assert invalid.value.code == code
    assert len(structural_checks) == expected_checks


def test_ac_18_validator_construction_failure_is_not_cached(
    structural_checks: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_checker() -> Any:
        raise TypeError("test-only checker failure")

    monkeypatch.setattr(schema_policy, "_format_checker", failing_checker)
    with schema_definition_check_scope():
        for _ in range(2):
            with pytest.raises(JsonSchemaPolicyError, match="definition is invalid"):
                compile_json_schema(_schema())
    assert len(structural_checks) == 2


def test_ac_18_snapshot_failure_is_not_cached(
    structural_checks: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_snapshot(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("test-only snapshot failure")

    monkeypatch.setattr(schema_policy, "frozen_json_mapping", failing_snapshot)
    with schema_definition_check_scope():
        for _ in range(2):
            with pytest.raises(ValueError, match="snapshot failure"):
                compile_json_schema(_schema())
    assert len(structural_checks) == 2


@pytest.mark.parametrize("limit", ["entries", "bytes"])
def test_ac_18_capacity_exhaustion_falls_back_to_full_validation(
    structural_checks: list[bytes], monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    monkeypatch.setattr(
        schema_policy,
        "_SCHEMA_CHECK_MAX_ENTRIES" if limit == "entries" else "_SCHEMA_CHECK_MAX_BYTES",
        1,
    )
    with schema_definition_check_scope():
        compile_json_schema(_schema())
        for _ in range(2):
            compile_json_schema(_schema(schema_id="schema.ac-18.another"))
        with pytest.raises(JsonSchemaPolicyError):
            compile_json_schema({"type": "invalid"})
        checks = schema_policy._SCHEMA_DEFINITION_CHECKS.get()
        assert checks is not None
        assert len(checks.successful) <= (1 if limit == "entries" else 0)
        assert checks.retained_bytes <= schema_policy._SCHEMA_CHECK_MAX_BYTES
    assert len(structural_checks) == 4


def test_ac_18_byte_capacity_includes_identity_and_enforces_cumulative_exact_boundary(
    structural_checks: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = _schema(schema_id=None)
    identity = "schema.ac-18.one"
    capacity = len(canonical_json_bytes(schema)) + len(identity.encode("utf-8"))
    monkeypatch.setattr(schema_policy, "_SCHEMA_CHECK_MAX_BYTES", capacity)
    with schema_definition_check_scope():
        for _ in range(2):
            compile_json_schema(schema, expected_schema_id=identity)
        for _ in range(2):
            compile_json_schema(schema, expected_schema_id="schema.ac-18.two")
        checks = schema_policy._SCHEMA_DEFINITION_CHECKS.get()
        assert checks is not None
        assert checks.retained_bytes == capacity
        assert len(checks.successful) == 1
    assert len(structural_checks) == 3


@pytest.mark.parametrize("fail", [False, True])
def test_ac_18_nested_scopes_restore_only_the_live_outer_build(
    structural_checks: list[bytes], fail: bool
) -> None:
    with schema_definition_check_scope():
        compile_json_schema(_schema())
        try:
            with schema_definition_check_scope():
                compile_json_schema(_schema())
                compile_json_schema(_schema())
                if fail:
                    raise RuntimeError("test-only nested failure")
        except RuntimeError:
            assert fail
        compile_json_schema(_schema())
    compile_json_schema(_schema())
    compile_json_schema(_schema())
    assert len(structural_checks) == 4


@pytest.mark.parametrize("fail", [False, True])
def test_ac_18_scope_teardown_expires_captured_contexts_and_successful_data(
    structural_checks: list[bytes], fail: bool
) -> None:
    captured = None
    checks = None
    try:
        with schema_definition_check_scope():
            compile_json_schema(_schema())
            checks = schema_policy._SCHEMA_DEFINITION_CHECKS.get()
            captured = copy_context()
            if fail:
                raise RuntimeError("test-only build failure")
    except RuntimeError:
        assert fail
    assert captured is not None and checks is not None
    assert not checks.active
    assert not checks.successful
    assert checks.retained_bytes == 0
    assert schema_policy._SCHEMA_DEFINITION_CHECKS.get() is None
    for _ in range(2):
        captured.run(compile_json_schema, _schema())
    assert not checks.successful
    assert len(structural_checks) == 3


def test_ac_18_inherited_thread_context_cannot_reuse_another_build(
    structural_checks: list[bytes],
) -> None:
    def compile_twice() -> None:
        compile_json_schema(_schema())
        compile_json_schema(_schema())

    with schema_definition_check_scope():
        compile_json_schema(_schema())
        captured = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(captured.run, compile_twice).result(timeout=10)
        compile_json_schema(_schema())
    assert len(structural_checks) == 3


@pytest.mark.asyncio
async def test_ac_18_concurrent_task_scopes_and_inherited_contexts_are_isolated(
    structural_checks: list[bytes],
) -> None:
    async def inherited() -> None:
        compile_json_schema(_schema())
        await asyncio.sleep(0)
        compile_json_schema(_schema())

    async def own_build() -> None:
        with schema_definition_check_scope():
            compile_json_schema(_schema())
            await asyncio.sleep(0)
            compile_json_schema(_schema())

    with schema_definition_check_scope():
        compile_json_schema(_schema())
        await asyncio.gather(inherited(), own_build(), own_build())
        compile_json_schema(_schema())
    assert len(structural_checks) == 5


def _guard() -> RuntimePolicyGuard:
    return RuntimePolicyGuard(
        RuntimePolicySnapshot(
            allowed_capabilities=(
                CapabilityPolicy(
                    capability_id="social.read_posts",
                    effect="read",
                    connector_family="social",
                ),
            ),
            input_max_bytes=4_096,
            output_max_bytes=4_096,
            max_json_depth=4,
            max_content_parts=4,
            max_content_characters=4_096,
            max_model_calls=2,
            max_tool_calls=2,
            rate_window_max_calls=2,
            rate_window_seconds=60,
            step_timeout_seconds=10,
            run_timeout_seconds=60,
        )
    )


def _output_contract(schema: dict[str, Any]) -> RuntimeOutputContract:
    return RuntimeOutputContract(
        schema_id="schema.api-08.output.v1",
        schema_version="v1",
        schema=schema,
        classification=DataClassification.INTERNAL,
        provider_kind="llm",
        provider_mode="mock",
        provider_name="deterministic",
        provider_version="v1",
    )


def _input_contract() -> RuntimeInputContract:
    return RuntimeInputContract(
        schema_id="schema.api-08.input.v1",
        schema_version="v1",
        schema={"type": "object"},
        classification=DataClassification.INTERNAL,
    )


def test_api_08_compilation_canonicalizes_and_freezes_an_independent_snapshot() -> None:
    original = _schema()
    compiled = compile_json_schema(
        original,
        expected_schema_id="schema.api-08.output.v1",
    )

    original["type"] = "array"

    assert compiled.schema_id == "schema.api-08.output.v1"
    assert compiled.schema["type"] == "object"
    with pytest.raises(TypeError):
        cast(Any, compiled.schema)["type"] = "array"
    with pytest.raises(TypeError):
        cast(Any, compiled.schema["properties"])["new"] = {}


def test_api_08_optional_embedded_id_may_be_absent_but_must_match_when_present() -> None:
    compiled = compile_json_schema(
        _schema(schema_id=None),
        expected_schema_id="schema.api-08.output.v1",
    )
    assert compiled.schema_id is None

    mismatched = _schema(schema_id="schema.api-08.other.v1")
    with pytest.raises(JsonSchemaPolicyError) as captured:
        compile_json_schema(mismatched, expected_schema_id="schema.api-08.output.v1")
    assert captured.value.code == "schema_identity_mismatch"

    with pytest.raises(JsonSchemaPolicyError) as malformed:
        compile_json_schema({"$id": None})
    assert malformed.value.code == "schema_identity_mismatch"


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "other-schema.json#/$defs/value",
        "https://schemas.example.invalid/value.json",
        "file:///tmp/value.json",
    ],
)
def test_api_08_nonlocal_schema_references_are_rejected(reference: str) -> None:
    schema = {
        "$schema": DRAFT_2020_12_DIALECT,
        "$dynamicRef": reference,
    }

    with pytest.raises(JsonSchemaPolicyError) as captured:
        compile_json_schema(schema)

    assert captured.value.code == "schema_reference_nonlocal"
    if reference:
        assert reference not in str(captured.value)


@pytest.mark.parametrize(
    "reference",
    ["#/$defs/missing", "#/properties/items/99", "#missing-anchor"],
)
def test_api_08_unresolved_local_references_fail_during_compilation(reference: str) -> None:
    with pytest.raises(JsonSchemaPolicyError) as captured:
        compile_json_schema({"$ref": reference})

    assert captured.value.code == "schema_invalid"
    assert reference not in str(captured.value)


def test_api_08_local_references_and_format_checks_are_enforced() -> None:
    compiled = compile_json_schema(
        {
            "$schema": DRAFT_2020_12_DIALECT,
            "$defs": {
                "timestamp": {"format": "date-time", "type": "string"},
            },
            "additionalProperties": False,
            "properties": {"published_at": {"$ref": "#/$defs/timestamp"}},
            "required": ["published_at"],
            "type": "object",
        }
    )

    compiled.validate(
        MappingProxyType({"published_at": "2026-08-28T12:00:00Z"}),
        pointer_root="/output",
        max_depth=4,
    )
    with pytest.raises(JsonSchemaPolicyError) as captured:
        compiled.validate(
            {"published_at": "not-a-date-secret-canary"},
            pointer_root="/output",
            max_depth=4,
        )
    assert captured.value.code == "instance_invalid"
    assert captured.value.pointer == "/output/published_at"
    assert "secret-canary" not in str(captured.value)


def test_api_08_local_anchors_and_nested_resources_resolve_at_compile_time() -> None:
    compile_json_schema(
        {
            "$defs": {
                "anchored": {"$anchor": "local-value", "type": "string"},
                "nested": {
                    "$anchor": "nested-value",
                    "$id": "schema.api-08.nested.v1",
                    "$ref": "#nested-value",
                },
            },
            "properties": {"value": {"$ref": "#local-value"}},
            "type": "object",
        }
    )


def test_api_08_duplicate_anchors_in_one_resource_are_rejected() -> None:
    with pytest.raises(JsonSchemaPolicyError) as captured:
        compile_json_schema(
            {
                "$defs": {
                    "left": {"$anchor": "duplicate", "type": "string"},
                    "right": {"$anchor": "duplicate", "type": "integer"},
                },
                "$ref": "#duplicate",
            }
        )

    assert captured.value.code == "schema_invalid"


def test_api_08_reference_shaped_example_data_is_not_treated_as_a_schema_reference() -> None:
    compile_json_schema(
        {
            "default": {
                "$recursiveAnchor": True,
                "$recursiveRef": "https://example.invalid/literal-data",
                "$ref": "https://example.invalid/literal-data",
                "$schema": "http://json-schema.org/draft-07/schema#",
            },
            "type": "object",
        }
    )


def test_api_08_invalid_dialect_and_schema_definition_fail_during_compilation() -> None:
    with pytest.raises(JsonSchemaPolicyError) as dialect_error:
        compile_json_schema({"$schema": "http://json-schema.org/draft-07/schema#"})
    assert dialect_error.value.code == "schema_dialect_unsupported"

    with pytest.raises(JsonSchemaPolicyError) as definition_error:
        compile_json_schema({"type": "not-a-json-schema-type"})
    assert definition_error.value.code == "schema_invalid"


def test_api_08_nested_schema_cannot_switch_to_an_unsupported_dialect() -> None:
    with pytest.raises(JsonSchemaPolicyError) as captured:
        compile_json_schema(
            {
                "$defs": {
                    "legacy": {
                        "$id": "schema.api-08.legacy.v1",
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "type": "object",
                        "unevaluatedProperties": False,
                    }
                },
                "properties": {"payload": {"$ref": "#/$defs/legacy"}},
                "type": "object",
            }
        )

    assert captured.value.code == "schema_dialect_unsupported"


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("$recursiveRef", "#"), ("$recursiveAnchor", True)],
)
def test_api_08_legacy_recursive_keywords_are_rejected_in_actual_subschemas(
    keyword: str,
    value: Any,
) -> None:
    with pytest.raises(JsonSchemaPolicyError) as captured:
        compile_json_schema(
            {
                "properties": {"value": {keyword: value}},
                "type": "object",
            }
        )

    assert captured.value.code == "schema_invalid"


def test_api_08_error_selection_and_pointers_are_deterministic_and_non_reflective() -> None:
    left = compile_json_schema(
        {
            "properties": {"b": {"type": "integer"}, "a": {"type": "integer"}},
            "type": "object",
        }
    )
    right = compile_json_schema(
        {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        }
    )
    pointers: list[str | None] = []
    for compiled in (left, right):
        with pytest.raises(JsonSchemaPolicyError) as captured:
            compiled.validate(
                {"b": "wrong-b", "a": "wrong-a"},
                pointer_root="/input",
                max_depth=4,
            )
        pointers.append(captured.value.pointer)
    assert pointers == ["/input/a", "/input/a"]

    hostile_key = "secret/canary"
    hostile_schema = compile_json_schema(
        {"properties": {hostile_key: {"type": "integer"}}, "type": "object"}
    )
    with pytest.raises(JsonSchemaPolicyError) as captured:
        hostile_schema.validate(
            {hostile_key: "Bearer secret-token-canary"},
            pointer_root="/input",
            max_depth=4,
        )
    assert captured.value.pointer == "/input"
    assert "secret" not in str(captured.value)


def test_api_08_depth_limit_is_inclusive_and_rejects_limit_plus_one() -> None:
    compiled = compile_json_schema({})
    compiled.validate({"a": {"b": 1}}, pointer_root="/input", max_depth=3)

    with pytest.raises(JsonSchemaPolicyError) as captured:
        compiled.validate({"a": {"b": 1}}, pointer_root="/input", max_depth=2)
    assert captured.value.code == "json_depth_limit"
    assert captured.value.pointer == "/input"


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    [
        (lambda guard: guard.validate_input({}, {"type": "not-a-type"}), "input_schema_invalid"),
        (
            lambda guard: guard.validate_output({}, {"$ref": "https://example.invalid/schema"}),
            "output_schema_invalid",
        ),
    ],
)
def test_api_08_runtime_guard_maps_schema_policy_failures_to_safe_codes(
    operation: Any,
    expected_code: str,
) -> None:
    with pytest.raises(RuntimePolicyViolation) as captured:
        operation(_guard())
    assert captured.value.code == expected_code
    assert captured.value.pointer in {"/input", "/output"}
    assert "not-a-type" not in str(captured.value)
    assert "example.invalid" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_api_08_policy_failures_do_not_retain_hostile_exception_context() -> None:
    with pytest.raises(JsonSchemaPolicyError) as malformed_schema:
        compile_json_schema({"secret-canary": object()})
    assert malformed_schema.value.__cause__ is None
    assert malformed_schema.value.__context__ is None

    compiled = compile_json_schema({})
    with pytest.raises(JsonSchemaPolicyError) as malformed_instance:
        compiled.validate(object(), pointer_root="/input", max_depth=4)
    assert malformed_instance.value.__cause__ is None
    assert malformed_instance.value.__context__ is None

    with pytest.raises(RuntimePolicyViolation) as malformed_payload:
        _guard().validate_input({1: "secret-canary"}, {})
    assert malformed_payload.value.code == "invalid_json"
    assert malformed_payload.value.__cause__ is None
    assert malformed_payload.value.__context__ is None


def test_api_08_hostile_mapping_methods_fail_closed_without_retaining_canaries() -> None:
    with pytest.raises(JsonSchemaPolicyError) as malformed_schema:
        compile_json_schema(_HostileMapping("items"))
    assert malformed_schema.value.code == "schema_invalid"
    assert _HOSTILE_MAPPING_CANARY not in str(malformed_schema.value)
    assert _HOSTILE_MAPPING_CANARY not in repr(malformed_schema.value)
    assert malformed_schema.value.__cause__ is None
    assert malformed_schema.value.__context__ is None

    compiled = compile_json_schema({})
    for failing_method in ("items", "values"):
        hostile_payload = _HostileMapping(failing_method)
        with pytest.raises(JsonSchemaPolicyError) as malformed_instance:
            compiled.validate(hostile_payload, pointer_root="/input", max_depth=4)
        assert malformed_instance.value.code == "instance_invalid"
        assert _HOSTILE_MAPPING_CANARY not in str(malformed_instance.value)
        assert _HOSTILE_MAPPING_CANARY not in repr(malformed_instance.value)
        assert malformed_instance.value.__cause__ is None
        assert malformed_instance.value.__context__ is None

    with pytest.raises(JsonSchemaPolicyError) as wrapped_input:
        _input_contract().validate(_HostileMapping("values"), max_depth=4)
    assert wrapped_input.value.code == "instance_invalid"
    assert _HOSTILE_MAPPING_CANARY not in repr(wrapped_input.value)
    assert wrapped_input.value.__cause__ is None
    assert wrapped_input.value.__context__ is None

    with pytest.raises(RuntimePolicyViolation) as guarded_input:
        _guard().validate_input(_HostileMapping("items"), {})
    assert guarded_input.value.code == "invalid_json"
    assert _HOSTILE_MAPPING_CANARY not in repr(guarded_input.value)
    assert guarded_input.value.__cause__ is None
    assert guarded_input.value.__context__ is None


@pytest.mark.parametrize(
    ("schema", "expected_code"),
    [
        ({"type": "not-a-type"}, "schema_invalid"),
        ({"$schema": "http://json-schema.org/draft-07/schema#"}, "schema_dialect_unsupported"),
        ({"$ref": "https://example.invalid/schema"}, "schema_reference_nonlocal"),
        ({"$ref": "#/$defs/missing"}, "schema_invalid"),
        ({"$id": "schema.api-08.other.v1"}, "schema_identity_mismatch"),
    ],
)
def test_api_08_runtime_output_contract_rejects_invalid_or_mismatched_schemas(
    schema: dict[str, Any],
    expected_code: str,
) -> None:
    with pytest.raises(JsonSchemaPolicyError) as captured:
        _output_contract(schema)
    assert captured.value.code == expected_code


def test_api_08_runtime_output_contract_accepts_matching_or_absent_embedded_id() -> None:
    matching = _output_contract(_schema())
    absent = _output_contract(_schema(schema_id=None))

    assert matching.schema["$id"] == matching.schema_id
    assert "$id" not in absent.schema
    assert matching.schema_hash != absent.schema_hash
