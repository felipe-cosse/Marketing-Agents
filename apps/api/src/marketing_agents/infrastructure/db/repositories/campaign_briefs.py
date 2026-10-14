"""Revision-one brief persistence using the existing immutable snapshot table."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.campaign_briefs import (
    CampaignBriefInsertResult,
    CampaignBriefSnapshot,
    snapshot_document,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import CampaignBrief
from marketing_agents.domain.validation import frozen_json_mapping
from marketing_agents.infrastructure.db.models.campaign_brief import CampaignBriefRecord
from marketing_agents.infrastructure.db.retention_tracking import require_live_detail


def _hydrate(record: CampaignBriefRecord) -> CampaignBriefSnapshot:
    document = json.loads(record.snapshot_json)
    if (
        type(document) is not dict
        or set(document)
        != {
            "schema_version",
            "id",
            "revision",
            "content",
            "created_at",
            "expires_at",
            "classification",
            "projection",
            "body_digest",
            "key_version",
        }
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or document["id"] != record.id
        or document["revision"] != record.revision
        or document["created_at"] != record.created_at.isoformat()
        or canonical_json_bytes(document).decode("utf-8") != record.snapshot_json
    ):
        raise ValueError("campaign brief snapshot is invalid")
    content = document["content"]
    if (
        type(content) is not dict
        or set(content) != {"title", "objective", "constraints", "source_refs"}
        or content["title"] != record.title
        or content["objective"] != record.objective
    ):
        raise ValueError("campaign brief snapshot content is invalid")
    return CampaignBriefSnapshot(
        brief=CampaignBrief(
            record.id,
            record.title,
            record.objective,
            frozen_json_mapping(content["constraints"], "brief constraints"),
            tuple(content["source_refs"]),
        ),
        revision=document["revision"],
        created_at=record.created_at,
        expires_at=datetime.fromisoformat(document["expires_at"]),
        classification=DataClassification(document["classification"]),
        projection=frozen_json_mapping(document["projection"], "brief projection"),
        body_digest=document["body_digest"],
        key_version=document["key_version"],
        integrity_digest=record.snapshot_hash,
    )


class SQLAlchemyCampaignBriefRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, brief_id: str) -> CampaignBriefSnapshot | None:
        record = await self._session.get(CampaignBriefRecord, (brief_id, 1))
        if record is not None:
            await require_live_detail(self._session, "brief", brief_id)
        return None if record is None else _hydrate(record)

    async def add_or_get(self, snapshot: CampaignBriefSnapshot) -> CampaignBriefInsertResult:
        record = CampaignBriefRecord(
            id=snapshot.brief.id,
            revision=snapshot.revision,
            title=snapshot.brief.title,
            objective=snapshot.brief.objective,
            snapshot_json=canonical_json_bytes(snapshot_document(snapshot)).decode("utf-8"),
            snapshot_hash=snapshot.integrity_digest,
            created_at=snapshot.created_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError:
            existing = await self.get(snapshot.brief.id)
            if existing is None:
                raise
            return CampaignBriefInsertResult(existing, inserted=False)
        return CampaignBriefInsertResult(_hydrate(record), inserted=True)
