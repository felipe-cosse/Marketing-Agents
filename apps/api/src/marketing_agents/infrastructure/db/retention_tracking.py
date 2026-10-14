"""Transactional immutable expiry tracking and authenticated erased-detail reads."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.retained_details import DetailExpired, RetainedDetail
from marketing_agents.domain.retention import RetentionCategory, RetentionPolicy
from marketing_agents.infrastructure.db.models.action import (
    ConnectorActionReceiptRecord,
    ExternalActionRecord,
)
from marketing_agents.infrastructure.db.models.approval import ApprovalRequestRecord
from marketing_agents.infrastructure.db.models.artifact import ArtifactRecord
from marketing_agents.infrastructure.db.models.audit import AuditEventRecord
from marketing_agents.infrastructure.db.models.campaign_brief import CampaignBriefRecord
from marketing_agents.infrastructure.db.models.execution_control import ExecutionAttemptRecord
from marketing_agents.infrastructure.db.models.retention import RetentionDetailRecord
from marketing_agents.infrastructure.db.models.work import WorkItemRecord
from marketing_agents.security.digest_key import DigestKey

_DOMAIN = b"marketing-agents:retained-detail:v1\x00"


def retention_material(record: RetentionDetailRecord) -> dict[str, Any]:
    return {
        "category": record.category,
        "record_kind": record.record_kind,
        "record_id": record.record_id,
        "run_id": record.run_id,
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "purged_at": None if record.purged_at is None else record.purged_at.isoformat(),
        "skeleton": record.skeleton,
    }


def retention_digest(record: RetentionDetailRecord, key: DigestKey) -> str:
    return hmac.new(
        key.bytes_for_digest(),
        _DOMAIN + canonical_json_bytes(retention_material(record)),
        hashlib.sha256,
    ).hexdigest()


def verify_retained(record: RetentionDetailRecord, key: DigestKey) -> RetainedDetail:
    if record.purged_at is None or record.integrity_digest is None:
        raise ValueError("retained detail is not erased")
    if not hmac.compare_digest(record.integrity_digest, retention_digest(record, key)):
        raise ValueError("retained detail integrity mismatch")
    if type(record.skeleton) is not dict or set(record.skeleton) != {"public", "binding"}:
        raise ValueError("retained detail shape is invalid")
    return RetainedDetail(
        RetentionCategory(record.category),
        record.record_kind,
        record.record_id,
        record.created_at,
        record.expires_at,
        record.purged_at,
        record.run_id,
        record.skeleton["public"],
    )


async def erased_detail(
    session: AsyncSession, kind: str, record_id: str
) -> RetentionDetailRecord | None:
    if not session.info.get("retention_enabled"):
        return None
    record = await session.get(RetentionDetailRecord, (kind, record_id))
    if record is None or record.purged_at is None:
        return None
    key = session.info.get("retention_key")
    if type(key) is not DigestKey:
        raise ValueError("retained detail verification key unavailable")
    verify_retained(record, key)
    return record


async def require_live_detail(session: AsyncSession, kind: str, record_id: str) -> None:
    if await erased_detail(session, kind, record_id) is not None:
        raise DetailExpired(kind, record_id)


def _tracking_record(record: Any, policy: RetentionPolicy) -> RetentionDetailRecord | None:
    category: RetentionCategory
    expiry: datetime | None = None
    created_at = getattr(record, "created_at", None)
    run_id = getattr(record, "run_id", None)
    if isinstance(record, WorkItemRecord):
        kind, category = "work", RetentionCategory.ADMITTED_PAYLOAD
        expiry = record.input_projection_expires_at
    elif isinstance(record, ExternalActionRecord):
        kind, category = "action", RetentionCategory.EXTERNAL_ACTION_PAYLOAD
    elif isinstance(record, ApprovalRequestRecord):
        kind, category = "approval", RetentionCategory.APPROVAL_DETAIL
        created_at = record.requested_at
    elif isinstance(record, ArtifactRecord):
        kind, category = "artifact", RetentionCategory.ARTIFACT_DETAIL
    elif isinstance(record, ConnectorActionReceiptRecord):
        kind, category = "receipt", RetentionCategory.CONNECTOR_RECEIPT_DETAIL
    elif isinstance(record, ExecutionAttemptRecord):
        kind, category = "attempt", RetentionCategory.ADMITTED_PAYLOAD
        created_at = record.reserved_at
    elif isinstance(record, AuditEventRecord):
        kind, category = "audit", RetentionCategory.AUDIT_METADATA
        created_at, expiry = record.occurred_at, record.metadata_expires_at
    elif isinstance(record, CampaignBriefRecord):
        kind, category = "brief", RetentionCategory.ADMITTED_PAYLOAD
        document = json.loads(record.snapshot_json)
        # Existing pre-EXEC-05 seed snapshots are configuration, not admitted briefs.
        if (
            type(document) is not dict
            or document.get("schema_version") != 1
            or "expires_at" not in document
        ):
            return None
        expiry = datetime.fromisoformat(document["expires_at"])
    else:
        return None
    if created_at is None:
        raise ValueError("retained detail requires its original creation time")
    record_id = record.receipt_id if kind == "receipt" else record.id
    return RetentionDetailRecord(
        record_kind=kind,
        record_id=record_id,
        category=category.value,
        run_id=run_id,
        created_at=created_at,
        expires_at=expiry or created_at + policy.ttl_for(category),
        skeleton={},
    )


@event.listens_for(Session, "before_flush")
def _track_new_details(session: Session, _flush_context: Any, _instances: Any) -> None:
    policy = session.info.get("retention_policy")
    if not session.info.get("retention_enabled") or type(policy) is not RetentionPolicy:
        return
    for record in tuple(session.new):
        tracked = _tracking_record(record, policy)
        if tracked is not None:
            session.add(tracked)
