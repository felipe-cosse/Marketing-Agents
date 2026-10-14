"""Alias-only inputs and redacted standalone campaign-brief resources."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CampaignBriefInput(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
    )
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=2000)
    constraints: dict[str, Any]
    source_refs: list[str] = Field(max_length=64)


class CampaignBriefApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class CampaignBriefDetails(CampaignBriefApiModel):
    title: str
    objective: str
    constraints: dict[str, Any]
    source_refs: str | tuple[str, ...]


class CampaignBriefResource(CampaignBriefApiModel):
    id: str
    revision: Literal[1]
    created_at: datetime
    expires_at: datetime
    classification: Literal["public", "internal", "personal", "sensitive"]
    expired: bool
    details: CampaignBriefDetails | None


class CampaignBriefCreated(CampaignBriefApiModel):
    disposition: Literal["created", "replayed"]
    resource: CampaignBriefResource
