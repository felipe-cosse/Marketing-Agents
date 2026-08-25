"""Immutable artifact envelopes with complete, classification-preserving provenance."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import (
    DataClassification,
    highest_classification,
)

ARTIFACT_HASH_DOMAIN = b"marketing-agents:artifact-payload:v1\x00"


def artifact_payload_hash(payload: dict[str, JsonValue]) -> str:
    return hashlib.sha256(ARTIFACT_HASH_DOMAIN + canonical_json_bytes(payload)).hexdigest()


class ProvenanceSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["work_input", "external_observation", "parent_artifact"]
    source_id: str = Field(min_length=1, max_length=240)
    integrity_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    classification: DataClassification


class ProviderVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_kind: Literal["llm", "connector", "planner"]
    mode: Literal["mock", "real", "local"]
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)


class ArtifactProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str = Field(min_length=1, max_length=240)
    work_item_id: str = Field(min_length=1, max_length=240)
    run_id: str = Field(min_length=1, max_length=240)
    step_id: str = Field(min_length=1, max_length=240)
    workflow_id: str = Field(min_length=1, max_length=240)
    workflow_version: str = Field(min_length=1, max_length=100)
    template_id: str = Field(min_length=1, max_length=240)
    instance_id: str = Field(min_length=1, max_length=260)
    admitted_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_hash: str = Field(pattern=r"^(?:catalog-sha256-v1:)?[0-9a-f]{64}$")
    instance_config_revision: int = Field(ge=1)
    sources: tuple[ProvenanceSource, ...] = Field(min_length=1, max_length=256)
    parent_artifact_ids: tuple[str, ...] = Field(default=(), max_length=256)
    providers: tuple[ProviderVersion, ...] = Field(min_length=1, max_length=32)
    output_schema_id: str = Field(min_length=1, max_length=240)
    output_schema_version: str = Field(min_length=1, max_length=100)
    output_schema_hash: str = Field(pattern=r"^schema-sha256-v1:[0-9a-f]{64}$")
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    classification: DataClassification

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("artifact creation time must be UTC")
        return value

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("provenance source IDs must be unique")
        if len(self.parent_artifact_ids) != len(set(self.parent_artifact_ids)):
            raise ValueError("parent artifact IDs must be unique")
        if self.artifact_id in self.parent_artifact_ids:
            raise ValueError("artifact cannot be its own parent")
        expected_classification = highest_classification(
            *(source.classification for source in self.sources)
        )
        ranks = {item: index for index, item in enumerate(DataClassification)}
        if ranks[self.classification] < ranks[expected_classification]:
            raise ValueError("artifact classification cannot be lower than a source")
        parent_sources = {
            source.source_id for source in self.sources if source.kind == "parent_artifact"
        }
        if parent_sources != set(self.parent_artifact_ids):
            raise ValueError("parent artifact sources and parent IDs must match")
        return self


class ArtifactEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    payload: dict[str, JsonValue]
    provenance: ArtifactProvenance

    @classmethod
    def create(
        cls,
        *,
        payload: dict[str, JsonValue],
        artifact_id: str,
        work_item_id: str,
        run_id: str,
        step_id: str,
        workflow_id: str,
        workflow_version: str,
        template_id: str,
        instance_id: str,
        admitted_input_digest: str,
        catalog_hash: str,
        instance_config_revision: int,
        sources: tuple[ProvenanceSource, ...],
        parent_artifact_ids: tuple[str, ...],
        providers: tuple[ProviderVersion, ...],
        output_schema_id: str,
        output_schema_version: str,
        output_schema_hash: str,
        created_at: datetime,
        classification: DataClassification,
    ) -> ArtifactEnvelope:
        provenance = ArtifactProvenance(
            artifact_id=artifact_id,
            work_item_id=work_item_id,
            run_id=run_id,
            step_id=step_id,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            template_id=template_id,
            instance_id=instance_id,
            admitted_input_digest=admitted_input_digest,
            catalog_hash=catalog_hash,
            instance_config_revision=instance_config_revision,
            sources=sources,
            parent_artifact_ids=parent_artifact_ids,
            providers=providers,
            output_schema_id=output_schema_id,
            output_schema_version=output_schema_version,
            output_schema_hash=output_schema_hash,
            payload_hash=artifact_payload_hash(payload),
            created_at=created_at,
            classification=classification,
        )
        return cls(payload=payload, provenance=provenance)

    def verify_payload(self, payload: dict[str, JsonValue] | None = None) -> bool:
        candidate = self.payload if payload is None else payload
        return artifact_payload_hash(candidate) == self.provenance.payload_hash
