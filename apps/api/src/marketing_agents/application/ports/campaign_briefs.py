"""Immutable brief storage contracts; restricted content never belongs in repr."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import CampaignBrief


@dataclass(frozen=True, slots=True)
class CampaignBriefSnapshot:
    brief: CampaignBrief = field(repr=False)
    revision: int
    created_at: datetime
    expires_at: datetime
    classification: DataClassification
    projection: Mapping[str, Any] = field(repr=False)
    body_digest: str = field(repr=False)
    key_version: str = field(repr=False)
    integrity_digest: str = field(repr=False)


def brief_content(brief: CampaignBrief) -> dict[str, Any]:
    return {
        "title": brief.title,
        "objective": brief.objective,
        "constraints": brief.constraints,
        "source_refs": brief.source_refs,
    }


def snapshot_document(snapshot: CampaignBriefSnapshot) -> dict[str, Any]:
    """The versioned integrity material excludes only its detached MAC."""
    return {
        "schema_version": 1,
        "id": snapshot.brief.id,
        "revision": snapshot.revision,
        "content": brief_content(snapshot.brief),
        "created_at": snapshot.created_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
        "classification": snapshot.classification.value,
        "projection": snapshot.projection,
        "body_digest": snapshot.body_digest,
        "key_version": snapshot.key_version,
    }


@dataclass(frozen=True, slots=True)
class CampaignBriefInsertResult:
    snapshot: CampaignBriefSnapshot = field(repr=False)
    inserted: bool


class CampaignBriefRepository(Protocol):
    async def get(self, brief_id: str) -> CampaignBriefSnapshot | None: ...

    async def add_or_get(self, snapshot: CampaignBriefSnapshot) -> CampaignBriefInsertResult: ...
