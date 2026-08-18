"""Typed, provenance-preserving step input bindings without accumulated chat."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from marketing_agents.application.policies.runtime_guard import RuntimePolicyGuard
from marketing_agents.domain.data_classification import (
    DataClassification,
    highest_classification,
)
from marketing_agents.domain.entities._validation import require_digest, require_id
from marketing_agents.domain.graph import DependencyGraph, DependencyGraphError
from marketing_agents.domain.provenance import ArtifactEnvelope

MAX_BINDINGS = 64
MAX_POINTER_LENGTH = 500
MAX_POINTER_SEGMENTS = 16
ARRAY_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)$")


class BindingError(ValueError):
    """A stable failure raised before typed step input can be constructed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_binding_id(value: str, field_name: str, code: str) -> None:
    try:
        require_id(value, field_name)
    except ValueError as exc:
        raise BindingError(code, str(exc)) from exc


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _plain_json_copy(value: Any) -> Any:
    """Copy a frozen selected value back to strict JSON before central validation."""

    if isinstance(value, Mapping):
        return {str(key): _plain_json_copy(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_json_copy(item) for item in value]
    return value


def _decode_pointer(pointer: str, *, allow_root: bool) -> tuple[str, ...]:
    if not isinstance(pointer, str) or len(pointer) > MAX_POINTER_LENGTH:
        raise BindingError("invalid_pointer", "source pointer must be a bounded JSON pointer")
    if pointer == "":
        if allow_root:
            return ()
        raise BindingError(
            "unbounded_work_input", "work input bindings must select a declared non-root field"
        )
    if not pointer.startswith("/"):
        raise BindingError("invalid_pointer", "source pointer must begin with a slash")
    raw_segments = pointer[1:].split("/")
    if len(raw_segments) > MAX_POINTER_SEGMENTS:
        raise BindingError("invalid_pointer", "source pointer has too many segments")

    decoded: list[str] = []
    for raw_segment in raw_segments:
        result: list[str] = []
        index = 0
        while index < len(raw_segment):
            character = raw_segment[index]
            if character != "~":
                result.append(character)
                index += 1
                continue
            if index + 1 >= len(raw_segment) or raw_segment[index + 1] not in {"0", "1"}:
                raise BindingError("invalid_pointer", "source pointer has an invalid escape")
            result.append("~" if raw_segment[index + 1] == "0" else "/")
            index += 2
        decoded.append("".join(result))
    return tuple(decoded)


def _resolve_pointer(value: Any, pointer: str, *, allow_root: bool) -> Any:
    current = value
    for segment in _decode_pointer(pointer, allow_root=allow_root):
        if isinstance(current, Mapping):
            if segment not in current:
                raise BindingError(
                    "source_pointer_missing", "source pointer does not resolve to a declared value"
                )
            current = current[segment]
            continue
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            if ARRAY_INDEX.fullmatch(segment) is None:
                raise BindingError(
                    "source_pointer_missing", "source pointer contains an invalid array index"
                )
            index = int(segment)
            if index >= len(current):
                raise BindingError(
                    "source_pointer_missing", "source pointer array index is out of bounds"
                )
            current = current[index]
            continue
        raise BindingError(
            "source_pointer_missing", "source pointer traverses a non-container value"
        )
    return current


def _catalog_digest(value: str) -> str:
    digest = value.removeprefix("catalog-sha256-v1:")
    try:
        require_digest(digest, "catalog hash")
    except ValueError as exc:
        raise BindingError("artifact_scope_mismatch", str(exc)) from exc
    return digest


@dataclass(frozen=True, slots=True)
class WorkInputBinding:
    """Select one explicit value from the admitted work payload."""

    target_key: str
    source_pointer: str

    def __post_init__(self) -> None:
        _require_binding_id(self.target_key, "binding target key", "invalid_target_key")
        _decode_pointer(self.source_pointer, allow_root=False)


@dataclass(frozen=True, slots=True)
class ArtifactInputBinding:
    """Select a value from one schema-identified ancestor artifact."""

    target_key: str
    artifact_id: str
    producer_step_key: str
    source_pointer: str
    expected_schema_id: str

    def __post_init__(self) -> None:
        _require_binding_id(self.target_key, "binding target key", "invalid_target_key")
        for value, label in (
            (self.artifact_id, "artifact ID"),
            (self.producer_step_key, "producer step key"),
            (self.expected_schema_id, "artifact schema ID"),
        ):
            _require_binding_id(value, label, "invalid_artifact_binding")
        _decode_pointer(self.source_pointer, allow_root=True)


type InputBinding = WorkInputBinding | ArtifactInputBinding


@dataclass(frozen=True, slots=True)
class StepInputContract:
    """The complete declared inputs and trusted schema for one target step."""

    target_step_key: str
    input_schema_id: str
    input_schema: Mapping[str, Any]
    bindings: tuple[InputBinding, ...]

    def __post_init__(self) -> None:
        _require_binding_id(self.target_step_key, "target step key", "invalid_target_step")
        _require_binding_id(self.input_schema_id, "input schema ID", "invalid_input_schema")
        if not isinstance(self.input_schema, Mapping):
            raise BindingError("invalid_input_schema", "input schema must be an object")
        if not isinstance(self.bindings, tuple) or not 1 <= len(self.bindings) <= MAX_BINDINGS:
            raise BindingError(
                "binding_limit", "step input contract must have one through 64 bindings"
            )
        target_keys: set[str] = set()
        for binding in self.bindings:
            if not isinstance(binding, WorkInputBinding | ArtifactInputBinding):
                raise BindingError("invalid_binding", "step input binding type is unsupported")
            if binding.target_key in target_keys:
                raise BindingError(
                    "duplicate_target_key", "a step input target may be bound only once"
                )
            target_keys.add(binding.target_key)
        object.__setattr__(self, "input_schema", _deep_freeze(self.input_schema))


@dataclass(frozen=True, slots=True)
class BindingContext:
    """Immutable admitted-input and runtime-snapshot identity used during binding."""

    work_item_id: str
    run_id: str
    admitted_input_digest: str
    workflow_id: str
    workflow_version: str
    catalog_hash: str
    admitted_payload: Mapping[str, Any]
    admitted_classification: DataClassification
    step_ids_by_key: Mapping[str, str]

    def __post_init__(self) -> None:
        for value, label in (
            (self.work_item_id, "work item ID"),
            (self.run_id, "run ID"),
            (self.workflow_id, "workflow ID"),
            (self.workflow_version, "workflow version"),
        ):
            _require_binding_id(value, label, "artifact_scope_mismatch")
        try:
            require_digest(self.admitted_input_digest, "admitted input digest")
        except ValueError as exc:
            raise BindingError("artifact_scope_mismatch", str(exc)) from exc
        _catalog_digest(self.catalog_hash)
        if not isinstance(self.admitted_payload, Mapping):
            raise BindingError("invalid_admitted_input", "admitted payload must be an object")
        if not isinstance(self.admitted_classification, DataClassification):
            raise BindingError("invalid_admitted_input", "admitted input classification is invalid")
        if not isinstance(self.step_ids_by_key, Mapping):
            raise BindingError("missing_step_runtime_id", "step runtime mapping is required")
        for step_key, step_id in self.step_ids_by_key.items():
            _require_binding_id(step_key, "step key", "missing_step_runtime_id")
            _require_binding_id(step_id, "runtime step ID", "missing_step_runtime_id")
        object.__setattr__(self, "admitted_payload", _deep_freeze(self.admitted_payload))
        object.__setattr__(self, "step_ids_by_key", _deep_freeze(self.step_ids_by_key))


@dataclass(frozen=True, slots=True)
class BoundArtifactReference:
    """Safe immutable lineage retained beside values consumed from an artifact."""

    artifact_id: str
    producer_step_key: str
    runtime_step_id: str
    schema_id: str
    payload_hash: str
    classification: DataClassification

    def __post_init__(self) -> None:
        for value, label in (
            (self.artifact_id, "artifact ID"),
            (self.producer_step_key, "producer step key"),
            (self.runtime_step_id, "runtime step ID"),
            (self.schema_id, "artifact schema ID"),
        ):
            _require_binding_id(value, label, "invalid_artifact_reference")
        try:
            require_digest(self.payload_hash, "artifact payload hash")
        except ValueError as exc:
            raise BindingError("artifact_hash_mismatch", str(exc)) from exc


@dataclass(frozen=True, slots=True)
class BoundStepInput:
    """Schema-valid bounded input plus explicit artifact lineage references."""

    schema_id: str
    payload: Mapping[str, Any]
    artifact_references: tuple[BoundArtifactReference, ...]
    classification: DataClassification

    def __post_init__(self) -> None:
        _require_binding_id(self.schema_id, "bound input schema ID", "invalid_input_schema")
        object.__setattr__(self, "payload", _deep_freeze(self.payload))


class TypedInputBinder:
    """Resolve only declared work fields and validated ancestor artifacts."""

    def bind(
        self,
        *,
        contract: StepInputContract,
        context: BindingContext,
        graph: DependencyGraph,
        artifacts: Mapping[str, ArtifactEnvelope],
        artifact_schemas: Mapping[str, Mapping[str, Any]],
        guard: RuntimePolicyGuard,
    ) -> BoundStepInput:
        try:
            graph.step(contract.target_step_key)
        except DependencyGraphError as exc:
            raise BindingError(
                "unknown_target_step", "target step is absent from the graph"
            ) from exc
        if contract.target_step_key not in context.step_ids_by_key:
            raise BindingError(
                "missing_step_runtime_id", "target step has no persisted runtime identity"
            )

        ancestors = graph.ancestors(contract.target_step_key)
        payload: dict[str, Any] = {}
        references: list[BoundArtifactReference] = []
        referenced_artifacts: set[str] = set()
        validated_artifacts: set[tuple[str, str]] = set()
        for binding in contract.bindings:
            if isinstance(binding, WorkInputBinding):
                payload[binding.target_key] = _plain_json_copy(
                    _resolve_pointer(
                        context.admitted_payload, binding.source_pointer, allow_root=False
                    )
                )
                continue

            if binding.producer_step_key not in ancestors:
                raise BindingError(
                    "artifact_not_ancestor", "artifact producer is not an ancestor of the target"
                )
            runtime_step_id = context.step_ids_by_key.get(binding.producer_step_key)
            if runtime_step_id is None:
                raise BindingError(
                    "missing_step_runtime_id", "artifact producer has no runtime step identity"
                )
            artifact = artifacts.get(binding.artifact_id)
            if artifact is None:
                raise BindingError("artifact_not_found", "bound artifact does not exist")
            if artifact.provenance.artifact_id != binding.artifact_id:
                raise BindingError(
                    "artifact_scope_mismatch", "artifact lookup key and provenance ID differ"
                )
            if not artifact.verify_payload():
                raise BindingError(
                    "artifact_hash_mismatch", "artifact payload no longer matches its provenance"
                )

            provenance = artifact.provenance
            if (
                provenance.run_id != context.run_id
                or provenance.work_item_id != context.work_item_id
                or provenance.admitted_input_digest != context.admitted_input_digest
                or provenance.workflow_id != context.workflow_id
                or provenance.workflow_version != context.workflow_version
                or _catalog_digest(provenance.catalog_hash) != _catalog_digest(context.catalog_hash)
            ):
                raise BindingError(
                    "artifact_scope_mismatch",
                    "artifact provenance does not match the admitted runtime snapshot",
                )
            if provenance.step_id != runtime_step_id:
                raise BindingError(
                    "artifact_producer_mismatch",
                    "artifact producer does not match the declared runtime step",
                )
            if provenance.output_schema_id != binding.expected_schema_id:
                raise BindingError(
                    "artifact_schema_mismatch",
                    "artifact schema does not match the declared binding schema",
                )
            artifact_schema = artifact_schemas.get(binding.expected_schema_id)
            if artifact_schema is None:
                raise BindingError(
                    "artifact_schema_missing", "declared artifact schema is not registered"
                )
            validation_key = (binding.artifact_id, binding.expected_schema_id)
            if validation_key not in validated_artifacts:
                guard.validate_output(artifact.payload, artifact_schema)
                validated_artifacts.add(validation_key)

            payload[binding.target_key] = _plain_json_copy(
                _resolve_pointer(artifact.payload, binding.source_pointer, allow_root=True)
            )
            if binding.artifact_id not in referenced_artifacts:
                references.append(
                    BoundArtifactReference(
                        artifact_id=binding.artifact_id,
                        producer_step_key=binding.producer_step_key,
                        runtime_step_id=runtime_step_id,
                        schema_id=binding.expected_schema_id,
                        payload_hash=provenance.payload_hash,
                        classification=provenance.classification,
                    )
                )
                referenced_artifacts.add(binding.artifact_id)

        guard.validate_input(payload, contract.input_schema)
        classification = highest_classification(
            context.admitted_classification,
            *(reference.classification for reference in references),
        )
        return BoundStepInput(
            schema_id=contract.input_schema_id,
            payload=payload,
            artifact_references=tuple(references),
            classification=classification,
        )
