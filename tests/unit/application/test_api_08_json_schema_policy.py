"""API-08: every object schema and structured instance is validated fail-closed."""

from __future__ import annotations

from collections.abc import ItemsView, Iterator, Mapping, ValuesView
from types import MappingProxyType
from typing import Any, cast

import pytest
from marketing_agents.application.policies.json_schema import (
    DRAFT_2020_12_DIALECT,
    JsonSchemaPolicyError,
    compile_json_schema,
)
from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
    RuntimePolicyViolation,
)
from marketing_agents.application.ports.runtime_inputs import RuntimeInputContract
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
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
