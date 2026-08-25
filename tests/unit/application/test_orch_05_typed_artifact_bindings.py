"""ORCH-05: steps receive typed declared input, never accumulated chat."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from marketing_agents.application.orchestration import (
    ArtifactInputBinding,
    BindingContext,
    BindingError,
    StepInputContract,
    TypedInputBinder,
    WorkInputBinding,
)
from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
    RuntimePolicyViolation,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.provenance import (
    ArtifactEnvelope,
    ProvenanceSource,
    ProviderVersion,
)

DIGEST = "a" * 64
CATALOG_HASH = "catalog-sha256-v1:" + "b" * 64
ARTIFACT_SCHEMA_ID = "schema.draft.v1"
ARTIFACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
}
INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topic", "draft"],
    "properties": {
        "topic": {"type": "string"},
        "draft": ARTIFACT_SCHEMA,
    },
}


def _graph() -> DependencyGraph:
    return DependencyGraph.build(
        (
            TopologyStep("collect", 1),
            TopologyStep("draft", 2, ("collect",)),
            TopologyStep("result", 3, ("draft",), terminal_result=True),
        ),
        workflow_max_steps=10,
        global_max_steps=20,
    )


def _split_graph() -> DependencyGraph:
    return DependencyGraph.build(
        (
            TopologyStep("collect", 1),
            TopologyStep("draft", 2, ("collect",)),
            TopologyStep("result", 3, ("draft",), terminal_result=True),
            TopologyStep("other", 4),
            TopologyStep("other-result", 5, ("other",), terminal_result=True),
        ),
        workflow_max_steps=10,
        global_max_steps=20,
    )


def _context(**updates: object) -> BindingContext:
    values: dict[str, object] = {
        "work_item_id": "work.1",
        "run_id": "run.1",
        "admitted_input_digest": DIGEST,
        "workflow_id": "workflow.demo",
        "workflow_version": "v1",
        "catalog_hash": CATALOG_HASH,
        "admitted_payload": {
            "campaign": {"topic": "Launch"},
            "chat_history": ["unbounded content must not pass implicitly"],
        },
        "admitted_classification": DataClassification.PERSONAL,
        "step_ids_by_key": {
            "collect": "step.runtime.collect",
            "draft": "step.runtime.draft",
            "result": "step.runtime.result",
            "other": "step.runtime.other",
            "other-result": "step.runtime.other-result",
        },
    }
    values.update(updates)
    return BindingContext(**values)  # type: ignore[arg-type]


def _artifact(
    payload: dict[str, object] | None = None,
    *,
    classification: DataClassification = DataClassification.SENSITIVE,
    **provenance_updates: object,
) -> ArtifactEnvelope:
    envelope = ArtifactEnvelope.create(
        payload=payload or {"text": "Schema-bound draft"},  # type: ignore[arg-type]
        artifact_id="artifact.1",
        work_item_id="work.1",
        run_id="run.1",
        step_id="step.runtime.draft",
        workflow_id="workflow.demo",
        workflow_version="v1",
        template_id="tpl.social-media.new-content.linkedin-post-drafter",
        instance_id="inst.social-media.new-content.linkedin-post-drafter.01",
        admitted_input_digest=DIGEST,
        catalog_hash=CATALOG_HASH,
        instance_config_revision=1,
        sources=(
            ProvenanceSource(
                kind="work_input",
                source_id="work.1",
                integrity_digest=DIGEST,
                classification=DataClassification.PERSONAL,
            ),
        ),
        parent_artifact_ids=(),
        providers=(
            ProviderVersion(provider_kind="planner", mode="local", name="fixture", version="v1"),
        ),
        output_schema_id=ARTIFACT_SCHEMA_ID,
        output_schema_version="v1",
        output_schema_hash="schema-sha256-v1:" + ("e" * 64),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        classification=classification,
    )
    if provenance_updates:
        envelope = envelope.model_copy(
            update={
                "provenance": envelope.provenance.model_copy(update=provenance_updates),
            }
        )
    return envelope


def _guard(**updates: object) -> RuntimePolicyGuard:
    values: dict[str, object] = {
        "allowed_capabilities": (
            CapabilityPolicy(
                capability_id="cap.artifact.transform-deterministic",
                effect="read",
                connector_family="artifact",
            ),
        ),
        "input_max_bytes": 4_096,
        "output_max_bytes": 4_096,
        "max_json_depth": 10,
        "max_content_parts": 8,
        "max_content_characters": 4_096,
        "max_model_calls": 2,
        "max_tool_calls": 2,
        "rate_window_max_calls": 10,
        "rate_window_seconds": 60,
        "step_timeout_seconds": 30,
        "run_timeout_seconds": 120,
    }
    values.update(updates)
    return RuntimePolicyGuard(RuntimePolicySnapshot.model_validate(values))


def _contract(*bindings: object, schema: dict[str, object] | None = None) -> StepInputContract:
    return StepInputContract(
        target_step_key="result",
        input_schema_id="schema.result-input.v1",
        input_schema=schema or INPUT_SCHEMA,
        bindings=bindings,  # type: ignore[arg-type]
    )


def _bind(
    *,
    contract: StepInputContract | None = None,
    context: BindingContext | None = None,
    graph: DependencyGraph | None = None,
    artifact: ArtifactEnvelope | None = None,
    artifact_schemas: dict[str, dict[str, object]] | None = None,
    guard: RuntimePolicyGuard | None = None,
):
    return TypedInputBinder().bind(
        contract=contract
        or _contract(
            WorkInputBinding("topic", "/campaign/topic"),
            ArtifactInputBinding("draft", "artifact.1", "draft", "", ARTIFACT_SCHEMA_ID),
        ),
        context=context or _context(),
        graph=graph or _graph(),
        artifacts={"artifact.1": artifact or _artifact()},
        artifact_schemas=artifact_schemas or {ARTIFACT_SCHEMA_ID: ARTIFACT_SCHEMA},
        guard=guard or _guard(),
    )


def test_orch_05_binds_declared_work_fields_and_ancestor_artifact_with_provenance() -> None:
    bound = _bind()

    assert bound.payload == {
        "topic": "Launch",
        "draft": {"text": "Schema-bound draft"},
    }
    assert "chat_history" not in bound.payload
    assert bound.classification is DataClassification.SENSITIVE
    assert len(bound.artifact_references) == 1
    reference = bound.artifact_references[0]
    assert reference.artifact_id == "artifact.1"
    assert reference.producer_step_key == "draft"
    assert reference.runtime_step_id == "step.runtime.draft"
    assert reference.schema_id == ARTIFACT_SCHEMA_ID
    assert reference.payload_hash == _artifact().provenance.payload_hash
    with pytest.raises(TypeError):
        bound.payload["topic"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        bound.payload["draft"]["text"] = "changed"  # type: ignore[index]


def test_orch_05_rejects_root_work_duplicate_targets_and_undeclared_chat() -> None:
    with pytest.raises(BindingError) as root_error:
        WorkInputBinding("payload", "")
    assert root_error.value.code == "unbounded_work_input"

    with pytest.raises(BindingError) as duplicate_error:
        _contract(
            WorkInputBinding("topic", "/campaign/topic"),
            ArtifactInputBinding("topic", "artifact.1", "draft", "/text", ARTIFACT_SCHEMA_ID),
        )
    assert duplicate_error.value.code == "duplicate_target_key"

    chat_contract = _contract(
        WorkInputBinding("topic", "/campaign/topic"),
        WorkInputBinding("chat_history", "/chat_history"),
    )
    with pytest.raises(RuntimePolicyViolation) as chat_error:
        _bind(contract=chat_contract)
    assert chat_error.value.code == "input_schema_invalid"


def test_orch_05_rejects_unrelated_or_drifted_artifacts() -> None:
    nonancestor_contract = _contract(
        WorkInputBinding("topic", "/campaign/topic"),
        ArtifactInputBinding("draft", "artifact.1", "other", "", ARTIFACT_SCHEMA_ID),
    )
    with pytest.raises(BindingError) as nonancestor:
        _bind(contract=nonancestor_contract, graph=_split_graph())
    assert nonancestor.value.code == "artifact_not_ancestor"

    for updates in (
        {"run_id": "run.other"},
        {"workflow_id": "workflow.other"},
        {"catalog_hash": "c" * 64},
    ):
        with pytest.raises(BindingError) as scope_error:
            _bind(artifact=_artifact(**updates))
        assert scope_error.value.code == "artifact_scope_mismatch"

    with pytest.raises(BindingError) as producer_error:
        _bind(artifact=_artifact(step_id="step.runtime.other"))
    assert producer_error.value.code == "artifact_producer_mismatch"

    with pytest.raises(BindingError) as schema_error:
        _bind(artifact=_artifact(output_schema_id="schema.other.v1"))
    assert schema_error.value.code == "artifact_schema_mismatch"

    original = _artifact()
    tampered = original.model_copy(update={"payload": {"text": "tampered"}})
    with pytest.raises(BindingError) as hash_error:
        _bind(artifact=tampered)
    assert hash_error.value.code == "artifact_hash_mismatch"


def test_orch_05_delegates_artifact_and_bound_input_schema_size_and_depth_to_runtime_guard() -> (
    None
):
    with pytest.raises(RuntimePolicyViolation) as output_schema_error:
        _bind(artifact=_artifact({"wrong": "shape"}))
    assert output_schema_error.value.code == "output_schema_invalid"

    bad_input = _context(admitted_payload={"campaign": {"topic": 42}})
    with pytest.raises(RuntimePolicyViolation) as input_schema_error:
        _bind(context=bad_input)
    assert input_schema_error.value.code == "input_schema_invalid"

    with pytest.raises(RuntimePolicyViolation) as output_size_error:
        _bind(guard=_guard(output_max_bytes=10))
    assert output_size_error.value.code == "output_byte_limit"

    nested_schema = {
        "type": "object",
        "required": ["nested"],
        "properties": {"nested": {}},
        "additionalProperties": False,
    }
    nested_contract = _contract(WorkInputBinding("nested", "/nested"), schema=nested_schema)
    nested_context = _context(admitted_payload={"nested": {"a": {"b": 1}}})
    with pytest.raises(RuntimePolicyViolation) as depth_error:
        _bind(contract=nested_contract, context=nested_context, guard=_guard(max_json_depth=2))
    assert depth_error.value.code == "json_depth_limit"


def test_orch_05_json_pointer_is_bounded_and_deterministic() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["slash", "second", "items"],
        "properties": {
            "slash": {"type": "string"},
            "second": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
            },
        },
    }
    contract = _contract(
        WorkInputBinding("slash", "/escaped/a~1b"),
        WorkInputBinding("second", "/items/1/name"),
        WorkInputBinding("items", "/items"),
        schema=schema,
    )
    context = _context(
        admitted_payload={
            "escaped": {"a/b": "selected"},
            "items": [{"name": "first"}, {"name": "second"}],
        }
    )
    bound = _bind(contract=contract, context=context)
    assert bound.payload["slash"] == "selected"
    assert bound.payload["second"] == "second"
    assert bound.payload["items"][0]["name"] == "first"
    assert isinstance(bound.payload["items"], tuple)

    with pytest.raises(BindingError) as invalid:
        WorkInputBinding("value", "not/a/pointer")
    assert invalid.value.code == "invalid_pointer"
    with pytest.raises(BindingError) as too_deep:
        WorkInputBinding("value", "/" + "/".join(["part"] * 17))
    assert too_deep.value.code == "invalid_pointer"
    missing_contract = _contract(WorkInputBinding("topic", "/campaign/missing"))
    with pytest.raises(BindingError) as missing:
        _bind(contract=missing_contract)
    assert missing.value.code == "source_pointer_missing"
