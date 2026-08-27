"""Strict safe transport projections for immutable artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

Classification = Literal["public", "internal", "personal", "sensitive", "secret"]
RetainableClassification = Literal["public", "internal", "personal", "sensitive"]


class ArtifactApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArtifactSummaryView(ArtifactApiModel):
    id: str = Field(min_length=1, max_length=240)
    work_item_id: str = Field(min_length=1, max_length=240)
    run_id: str = Field(min_length=1, max_length=240)
    step_id: str = Field(min_length=1, max_length=240)
    workflow_id: str = Field(min_length=1, max_length=240)
    workflow_version: str = Field(min_length=1, max_length=100)
    template_id: str = Field(min_length=1, max_length=240)
    instance_id: str = Field(min_length=1, max_length=260)
    output_schema_id: str = Field(min_length=1, max_length=240)
    output_schema_version: str = Field(min_length=1, max_length=100)
    classification: Classification
    created_at: datetime
    artifact_url: str
    run_url: str
    step_url: str
    template_url: str
    instance_url: str


class ArtifactSourceView(ArtifactApiModel):
    kind: Literal["work_input", "external_observation", "parent_artifact"]
    source_id: str = Field(min_length=1, max_length=240)
    classification: RetainableClassification


class ArtifactProviderView(ArtifactApiModel):
    provider_kind: Literal["llm", "connector", "planner"]
    mode: Literal["mock", "real", "local"]
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)


class ArtifactResourceView(ArtifactSummaryView):
    classification: RetainableClassification
    catalog_hash: str = Field(pattern=r"^(?:catalog-sha256-v1:)?[0-9a-f]{64}$")
    instance_config_revision: int = Field(ge=1)
    sources: tuple[ArtifactSourceView, ...] = Field(min_length=1, max_length=256)
    parent_artifact_ids: tuple[str, ...] = Field(max_length=256)
    providers: tuple[ArtifactProviderView, ...] = Field(min_length=1, max_length=32)
    output_schema_hash: str = Field(pattern=r"^schema-sha256-v1:[0-9a-f]{64}$")
    redacted_payload: dict[str, JsonValue]
    payload_digest: str = Field(pattern=r"^artifact-hmac-sha256-v1:[0-9a-f]{64}$")


class ArtifactListResponse(ArtifactApiModel):
    run_id: str = Field(min_length=1, max_length=240)
    items: tuple[ArtifactSummaryView, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=1_024)


class ArtifactProblemDetail(ArtifactApiModel):
    code: str
    message: str


class ArtifactHttpError(ArtifactApiModel):
    detail: ArtifactProblemDetail


class ArtifactPlainHttpError(ArtifactApiModel):
    detail: str


__all__ = [
    "ArtifactHttpError",
    "ArtifactListResponse",
    "ArtifactPlainHttpError",
    "ArtifactProviderView",
    "ArtifactResourceView",
    "ArtifactSourceView",
    "ArtifactSummaryView",
]
