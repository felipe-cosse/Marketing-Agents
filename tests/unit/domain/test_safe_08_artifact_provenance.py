"""SAFE-08: artifact lineage, versions, sensitivity, and payload hash remain intact."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.provenance import (
    ArtifactEnvelope,
    ArtifactProvenance,
    ProvenanceSource,
    ProviderVersion,
    artifact_payload_hash,
)
from pydantic import ValidationError

DIGEST = "a" * 64
CATALOG_HASH = "catalog-sha256-v1:" + "b" * 64


def _source(
    source_id: str = "work-item:1",
    *,
    kind: str = "work_input",
    classification: DataClassification = DataClassification.INTERNAL,
) -> ProvenanceSource:
    return ProvenanceSource.model_validate(
        {
            "kind": kind,
            "source_id": source_id,
            "integrity_digest": DIGEST,
            "classification": classification,
        }
    )


def _create(
    payload: dict[str, object] | None = None,
    *,
    sources: tuple[ProvenanceSource, ...] | None = None,
    parents: tuple[str, ...] = (),
    classification: DataClassification = DataClassification.INTERNAL,
) -> ArtifactEnvelope:
    return ArtifactEnvelope.create(
        payload=(payload if payload is not None else {"draft": "hello", "score": 1}),  # type: ignore[arg-type]
        artifact_id="artifact:1",
        work_item_id="work-item:1",
        run_id="run:1",
        step_id="step:draft",
        workflow_id="workflow:social-draft",
        workflow_version="v1",
        template_id="tpl.social-media.new-content.linkedin-post-drafter",
        instance_id="inst.social-media.new-content.linkedin-post-drafter.01",
        admitted_input_digest=DIGEST,
        catalog_hash=CATALOG_HASH,
        instance_config_revision=1,
        sources=sources or (_source(),),
        parent_artifact_ids=parents,
        providers=(
            ProviderVersion(provider_kind="llm", mode="mock", name="deterministic", version="v1"),
        ),
        output_schema_id="schema:social-draft:v1",
        output_schema_version="v1",
        output_schema_hash="schema-sha256-v1:" + ("e" * 64),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        classification=classification,
    )


def test_safe_08_equivalent_payloads_hash_identically_and_mutation_is_detected() -> None:
    left = {"draft": "hello", "metadata": {"b": 2, "a": 1}}
    right = {"metadata": {"a": 1, "b": 2}, "draft": "hello"}
    assert artifact_payload_hash(left) == artifact_payload_hash(right)  # type: ignore[arg-type]

    envelope = _create(left)
    assert envelope.verify_payload()
    assert envelope.verify_payload(right)  # type: ignore[arg-type]
    assert not envelope.verify_payload({"draft": "changed"})


def test_safe_08_child_artifact_retains_parent_and_source_lineage() -> None:
    sources = (
        _source(),
        _source("artifact:parent", kind="parent_artifact"),
    )
    envelope = _create(sources=sources, parents=("artifact:parent",))
    assert envelope.provenance.parent_artifact_ids == ("artifact:parent",)
    assert {item.source_id for item in envelope.provenance.sources} == {
        "work-item:1",
        "artifact:parent",
    }
    assert not hasattr(envelope.provenance.sources[0], "content")


def test_safe_08_sensitive_source_classification_propagates() -> None:
    sensitive = _source(classification=DataClassification.SENSITIVE)
    with pytest.raises(ValidationError, match="cannot be lower"):
        _create(sources=(sensitive,), classification=DataClassification.INTERNAL)
    envelope = _create(sources=(sensitive,), classification=DataClassification.SENSITIVE)
    assert envelope.provenance.classification is DataClassification.SENSITIVE


@pytest.mark.parametrize(
    "update",
    [
        {"sources": ()},
        {"sources": (_source(), _source())},
        {"parent_artifact_ids": ("artifact:1",)},
        {"payload_hash": "invalid"},
        {"created_at": datetime(2026, 1, 1)},
    ],
)
def test_safe_08_missing_duplicate_self_parent_hash_and_time_fail(
    update: dict[str, object],
) -> None:
    data = _create().provenance.model_dump()
    data.update(update)
    with pytest.raises(ValidationError):
        ArtifactProvenance.model_validate(data)
