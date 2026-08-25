"""SQLAlchemy work repository with atomic source-key insertion."""

from __future__ import annotations

import json
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import WorkInsertResult
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import WorkItem
from marketing_agents.domain.enums import WorkMode
from marketing_agents.infrastructure.db.models.work import WorkItemRecord


def _plain_payload(work_item: WorkItem) -> dict[str, Any]:
    value = json.loads(canonical_json_bytes(work_item.admitted_payload))
    return cast(dict[str, Any], value)


def _plain_projection(work_item: WorkItem) -> dict[str, Any]:
    value = json.loads(canonical_json_bytes(work_item.redacted_input_projection))
    return cast(dict[str, Any], value)


def _to_record(work_item: WorkItem) -> WorkItemRecord:
    return WorkItemRecord(
        id=work_item.id,
        source=work_item.source,
        event_id=work_item.event_id,
        agent_instance_id=work_item.instance_id,
        trigger_id=work_item.trigger_id,
        workflow_id=work_item.workflow_id,
        mode=work_item.mode.value,
        campaign_brief_id=work_item.brief_id,
        campaign_brief_revision=work_item.brief_revision,
        configuration_revision=work_item.configuration_revision,
        admitted_payload=_plain_payload(work_item),
        redacted_input_projection=_plain_projection(work_item),
        input_schema_id=work_item.input_schema_id,
        input_schema_hash=work_item.input_schema_hash,
        input_classification=work_item.input_classification.value,
        input_projection_created_at=work_item.input_projection_created_at,
        input_projection_expires_at=work_item.input_projection_expires_at,
        input_projection_integrity_digest=work_item.input_projection_integrity_digest,
        input_digest=work_item.input_digest,
        admission_digest=work_item.admission_digest,
        digest_key_version=work_item.digest_key_version,
        created_at=work_item.created_at,
    )


def _to_domain(record: WorkItemRecord) -> WorkItem:
    return WorkItem(
        id=record.id,
        source=record.source,
        event_id=record.event_id,
        instance_id=record.agent_instance_id,
        trigger_id=record.trigger_id,
        workflow_id=record.workflow_id,
        mode=WorkMode(record.mode),
        brief_id=record.campaign_brief_id,
        configuration_revision=record.configuration_revision,
        input_digest=record.input_digest,
        admission_digest=record.admission_digest,
        created_at=record.created_at,
        brief_revision=record.campaign_brief_revision,
        digest_key_version=record.digest_key_version,
        admitted_payload=record.admitted_payload,
        redacted_input_projection=record.redacted_input_projection,
        input_schema_id=record.input_schema_id,
        input_schema_hash=record.input_schema_hash,
        input_classification=DataClassification(record.input_classification),
        input_projection_created_at=record.input_projection_created_at,
        input_projection_expires_at=record.input_projection_expires_at,
        input_projection_integrity_digest=record.input_projection_integrity_digest,
    )


class SQLAlchemyWorkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, work_item_id: str) -> WorkItem | None:
        record = await self._session.get(WorkItemRecord, work_item_id)
        return None if record is None else _to_domain(record)

    async def get_by_source_key(
        self,
        source: str,
        event_id: str,
        instance_id: str,
    ) -> WorkItem | None:
        statement = select(WorkItemRecord).where(
            WorkItemRecord.source == source,
            WorkItemRecord.event_id == event_id,
            WorkItemRecord.agent_instance_id == instance_id,
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else _to_domain(record)

    async def add(self, work_item: WorkItem) -> None:
        self._session.add(_to_record(work_item))
        await self._session.flush()

    async def add_or_get(self, work_item: WorkItem) -> WorkInsertResult:
        try:
            async with self._session.begin_nested():
                self._session.add(_to_record(work_item))
                await self._session.flush()
        except IntegrityError:
            existing = await self.get_by_source_key(*work_item.source_idempotency_key)
            if existing is None:
                raise
            return WorkInsertResult(existing, inserted=False)
        return WorkInsertResult(work_item, inserted=True)
