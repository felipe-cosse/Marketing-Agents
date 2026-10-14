"""Fenced, bounded physical detail erasure; lifecycle and dedupe rows survive."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from marketing_agents.application.ports.clock import Clock
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.retained_details import RetainedDetail, RetentionClaim, RetentionResult
from marketing_agents.domain.retention import RetentionCategory, RetentionPolicy
from marketing_agents.domain.validation import require_id, require_utc
from marketing_agents.infrastructure.db.models import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ArtifactParentRecord,
    ArtifactRecord,
    AuditEventRecord,
    CampaignBriefRecord,
    ConnectorActionReceiptRecord,
    ExecutionAttemptRecord,
    ExternalActionRecord,
    MaintenanceRunRecord,
    RetentionDetailRecord,
    RunRecord,
    RunWorkerClaimRecord,
    ScheduleRecord,
    WorkItemRecord,
)
from marketing_agents.infrastructure.db.retention_tracking import (
    _tracking_record,
    retention_digest,
    verify_retained,
)
from marketing_agents.security.digest_key import DigestKey

from .audit import SQLAlchemyAuditRepository, _record_to_domain, expire_optional_metadata

_TERMINAL_RUNS = ("completed", "failed", "rejected", "cancelled")
_TERMINAL_ACTIONS = ("succeeded", "failed", "rejected", "cancelled", "superseded")
_KINDS = frozenset(
    {"work", "action", "approval", "artifact", "receipt", "attempt", "audit", "brief"}
)
_LEGACY_POLICY = RetentionPolicy(7, 7, 7, 30, 30, 90)


class RetentionConflict(RuntimeError):
    """Stable maintenance conflict, never containing stored content."""


def work_binding(record: WorkItemRecord) -> dict[str, Any]:
    """Immutable work identity and non-reversible dedupe witnesses, never its body."""
    names = (
        "id",
        "source",
        "event_id",
        "agent_instance_id",
        "trigger_id",
        "workflow_id",
        "mode",
        "campaign_brief_id",
        "campaign_brief_revision",
        "configuration_revision",
        "input_schema_id",
        "input_schema_hash",
        "input_classification",
        "input_digest",
        "admission_digest",
        "digest_key_version",
        "input_projection_integrity_digest",
    )
    material = {name: getattr(record, name) for name in names}
    for name in ("created_at", "input_projection_created_at", "input_projection_expires_at"):
        material[name] = getattr(record, name).isoformat()
    return material


def _scope(claim: RetentionClaim) -> str:
    return canonical_json_bytes(
        {"cutoff_at": claim.cutoff_at.isoformat(), "batch_size": claim.batch_size}
    ).decode()


async def _write_transaction(session: AsyncSession) -> None:
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(text("BEGIN IMMEDIATE"))


class SQLAlchemyRetentionRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        key: DigestKey,
        *,
        retention_policy: RetentionPolicy | None = None,
        audit_factory: Callable[
            [AsyncSession], SQLAlchemyAuditRepository
        ] = SQLAlchemyAuditRepository,
    ) -> None:
        self._sessions, self._key = session_factory, key
        self._policy = retention_policy or RetentionPolicy()
        self._audits = audit_factory

    async def inspect(self, record_kind: str, record_id: str) -> RetainedDetail | None:
        if record_kind not in _KINDS:
            raise ValueError("retained detail kind is invalid")
        require_id(record_id, "retained detail ID")
        async with self._sessions() as session:
            record = await session.get(RetentionDetailRecord, (record_kind, record_id))
            return (
                None
                if record is None or record.purged_at is None
                else verify_retained(record, self._key)
            )

    async def claim(
        self, *, occurrence_key: str, owner: str, now: datetime, lease_seconds: int, batch_size: int
    ) -> RetentionClaim | RetentionResult | None:
        require_id(occurrence_key, "retention occurrence")
        require_id(owner, "retention owner")
        require_utc(now, "retention time")
        if (
            type(batch_size) is not int
            or not 1 <= batch_size <= 100
            or type(lease_seconds) is not int
            or not 1 <= lease_seconds <= 300
        ):
            raise ValueError("retention limits are invalid")
        job_id = "maintenance." + hashlib.sha256(occurrence_key.encode()).hexdigest()
        async with self._sessions() as session:
            await _write_transaction(session)
            record = await session.get(MaintenanceRunRecord, job_id, with_for_update=True)
            if record is None:
                claim = RetentionClaim(
                    job_id,
                    occurrence_key,
                    owner,
                    1,
                    now,
                    now + timedelta(seconds=lease_seconds),
                    now,
                    batch_size,
                )
                record = MaintenanceRunRecord(
                    id=job_id,
                    occurrence_key=occurrence_key,
                    job_kind="retention.v1",
                    state="running",
                    started_at=now,
                    completed_at=None,
                    lease_owner=owner,
                    lease_claimed_at=now,
                    lease_expires_at=claim.expires_at,
                    counts_json="{}",
                    scope_json=_scope(claim),
                    version=1,
                )
                session.add(record)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    return None
                return claim
            if record.job_kind != "retention.v1" or record.occurrence_key != occurrence_key:
                raise RetentionConflict("retention_occurrence_conflict")
            scope = json.loads(record.scope_json)
            if (
                type(scope) is not dict
                or set(scope) != {"batch_size", "cutoff_at"}
                or scope["batch_size"] != batch_size
            ):
                raise RetentionConflict("retention_scope_conflict")
            cutoff = datetime.fromisoformat(scope["cutoff_at"])
            require_utc(cutoff, "retention original cutoff")
            if record.state == "completed":
                if record.completed_at is None:
                    raise RetentionConflict("retention_completion_corrupt")
                return RetentionResult(
                    occurrence_key, json.loads(record.counts_json), record.completed_at, True
                )
            if (
                record.state != "running"
                or record.lease_expires_at is None
                or record.lease_expires_at > now
            ):
                return None
            previous_version = record.version
            claimed = await session.execute(
                update(MaintenanceRunRecord)
                .where(
                    MaintenanceRunRecord.id == job_id,
                    MaintenanceRunRecord.version == previous_version,
                    MaintenanceRunRecord.state == "running",
                    MaintenanceRunRecord.lease_expires_at <= now,
                )
                .values(
                    version=previous_version + 1,
                    lease_owner=owner,
                    lease_claimed_at=now,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
                .returning(MaintenanceRunRecord.id)
            )
            if claimed.scalar_one_or_none() != job_id:
                return None
            await session.commit()
            return RetentionClaim(
                job_id,
                occurrence_key,
                owner,
                previous_version + 1,
                now,
                now + timedelta(seconds=lease_seconds),
                cutoff,
                batch_size,
            )

    @staticmethod
    def _eligible_runs(cutoff: datetime) -> Select[tuple[str]]:
        successor = aliased(ExecutionAttemptRecord)
        superseded_retry = exists(
            select(successor.id).where(
                successor.run_id == ExecutionAttemptRecord.run_id,
                successor.step_id == ExecutionAttemptRecord.step_id,
                successor.operation_key == ExecutionAttemptRecord.operation_key,
                successor.attempt_number > ExecutionAttemptRecord.attempt_number,
            )
        )
        return select(RunRecord.id).where(
            RunRecord.state.in_(_TERMINAL_RUNS),
            ~exists(
                select(ExternalActionRecord.id).where(
                    ExternalActionRecord.run_id == RunRecord.id,
                    ExternalActionRecord.state.not_in(_TERMINAL_ACTIONS),
                )
            ),
            ~exists(
                select(ExecutionAttemptRecord.id).where(
                    ExecutionAttemptRecord.run_id == RunRecord.id,
                    or_(
                        ExecutionAttemptRecord.completed_at.is_(None),
                        and_(
                            ExecutionAttemptRecord.retry_not_before.is_not(None),
                            ExecutionAttemptRecord.terminal_reason_code.is_(None),
                            ~superseded_retry,
                        ),
                    ),
                )
            ),
            ~exists(
                select(RunWorkerClaimRecord.run_id).where(
                    RunWorkerClaimRecord.run_id == RunRecord.id,
                    RunWorkerClaimRecord.expires_at > cutoff,
                )
            ),
        )

    def _candidates(self, claim: RetentionClaim) -> Select[tuple[RetentionDetailRecord]]:
        eligible = self._eligible_runs(claim.cutoff_at)
        work_ids = select(RunRecord.work_item_id).where(RunRecord.id.in_(eligible))
        receipt_ids = (
            select(ConnectorActionReceiptRecord.receipt_id)
            .join(
                ExternalActionRecord,
                ExternalActionRecord.id == ConnectorActionReceiptRecord.external_action_id,
            )
            .where(ExternalActionRecord.run_id.in_(eligible))
        )
        # A brief attached to any noneligible work remains recovery material.
        unsafe_briefs = select(WorkItemRecord.campaign_brief_id).where(
            WorkItemRecord.campaign_brief_id.is_not(None), WorkItemRecord.id.not_in(work_ids)
        )
        inactive_global_audits = select(AuditEventRecord.id).where(
            AuditEventRecord.run_id.is_(None),
            or_(
                AuditEventRecord.schedule_id.is_(None),
                ~exists(
                    select(ScheduleRecord.id).where(
                        ScheduleRecord.id == AuditEventRecord.schedule_id,
                        ScheduleRecord.enabled.is_(True),
                    )
                ),
            ),
        )
        child_detail = aliased(RetentionDetailRecord)
        live_child = exists(
            select(ArtifactParentRecord.artifact_id)
            .outerjoin(
                child_detail,
                and_(
                    child_detail.record_kind == "artifact",
                    child_detail.record_id == ArtifactParentRecord.artifact_id,
                ),
            )
            .where(
                ArtifactParentRecord.parent_artifact_id == RetentionDetailRecord.record_id,
                child_detail.purged_at.is_(None),
            )
        )
        return (
            select(RetentionDetailRecord)
            .where(
                RetentionDetailRecord.purged_at.is_(None),
                RetentionDetailRecord.expires_at <= claim.cutoff_at,
                # Parent provenance must remain live until every descendant that
                # directly depends on it has itself been erased (leaf-first).
                or_(RetentionDetailRecord.record_kind != "artifact", ~live_child),
                or_(
                    RetentionDetailRecord.run_id.in_(eligible),
                    and_(
                        RetentionDetailRecord.record_kind == "work",
                        RetentionDetailRecord.record_id.in_(work_ids),
                    ),
                    and_(
                        RetentionDetailRecord.record_kind == "receipt",
                        RetentionDetailRecord.record_id.in_(receipt_ids),
                    ),
                    and_(
                        RetentionDetailRecord.record_kind == "brief",
                        RetentionDetailRecord.record_id.not_in(unsafe_briefs),
                    ),
                    and_(
                        RetentionDetailRecord.record_kind == "audit",
                        RetentionDetailRecord.record_id.in_(inactive_global_audits),
                    ),
                ),
            )
            .order_by(
                RetentionDetailRecord.expires_at,
                RetentionDetailRecord.record_kind,
                RetentionDetailRecord.record_id,
            )
            .limit(claim.batch_size)
            .with_for_update()
        )

    async def execute(self, claim: RetentionClaim, clock: Clock) -> RetentionResult:
        now = clock.now()
        require_utc(now, "retention execution time")
        if not claim.claimed_at <= now < claim.expires_at:
            raise RetentionConflict("retention_lease_expired")
        async with self._sessions() as session:
            await _write_transaction(session)
            record = await session.get(MaintenanceRunRecord, claim.job_id, with_for_update=True)
            if (
                record is None
                or record.state != "running"
                or record.version != claim.version
                or record.lease_owner != claim.owner
                or record.lease_claimed_at != claim.claimed_at
                or record.lease_expires_at != claim.expires_at
                or record.scope_json != _scope(claim)
            ):
                raise RetentionConflict("retention_stale_claim")
            await self._register_legacy(session, claim.batch_size)
            candidates = tuple((await session.scalars(self._candidates(claim))).all())
            counts = {category.value: 0 for category in RetentionCategory}
            for detail in candidates:
                await self._erase(session, detail, now)
                counts[detail.category] += 1
            completed_at = clock.now()
            require_utc(completed_at, "retention completion time")
            if not now <= completed_at < claim.expires_at:
                raise RetentionConflict("retention_lease_expired")
            event = AuditEventFactory(
                AuditContext.worker(claim.owner, correlation_id=claim.job_id),
                retention_policy=self._policy,
            ).retention_completed(job_id=claim.job_id, counts=counts, occurred_at=completed_at)
            await self._audits(session).append_global_many((event,))
            record.state, record.completed_at = "completed", completed_at
            record.lease_owner = record.lease_claimed_at = record.lease_expires_at = None
            record.counts_json = canonical_json_bytes(counts).decode()
            record.version += 1
            await session.commit()
            return RetentionResult(claim.occurrence_key, counts, completed_at)

    @staticmethod
    async def _register_legacy(session: AsyncSession, limit: int) -> None:
        """Register at most one bounded page using fixed historical default TTLs.

        Explicit Work/Audit/Brief expiries remain authoritative. This runs inside
        the maintenance transaction, never in migrations, and never rewrites an
        already-recorded expiry when the current installation policy changes.
        """
        sources: tuple[tuple[str, Any], ...] = (
            ("work", WorkItemRecord),
            ("action", ExternalActionRecord),
            ("approval", ApprovalRequestRecord),
            ("artifact", ArtifactRecord),
            ("receipt", ConnectorActionReceiptRecord),
            ("attempt", ExecutionAttemptRecord),
            ("brief", CampaignBriefRecord),
            ("audit", AuditEventRecord),
        )
        remaining = limit
        for kind, model in sources:
            if remaining == 0:
                break
            identity = model.receipt_id if kind == "receipt" else model.id
            query = select(model).where(
                ~exists(
                    select(RetentionDetailRecord.record_id).where(
                        RetentionDetailRecord.record_kind == kind,
                        RetentionDetailRecord.record_id == identity,
                    )
                )
            )
            if kind == "brief":
                # Old seed configuration snapshots are not admitted briefs.
                query = query.where(model.snapshot_json.like('%"expires_at"%'))
            rows = tuple((await session.scalars(query.order_by(identity).limit(remaining))).all())
            remaining -= len(rows)
            for row in rows:
                detail = _tracking_record(row, _LEGACY_POLICY)
                if detail is not None:
                    session.add(detail)
        await session.flush()

    async def _erase(
        self, session: AsyncSession, detail: RetentionDetailRecord, now: datetime
    ) -> None:
        kind, identity = detail.record_kind, detail.record_id
        public: dict[str, Any] = {}
        binding: dict[str, Any] = {}
        if kind == "work":
            from marketing_agents.application.services.work_admission import (
                _input_projection_integrity_digest,
            )

            from .work import _to_domain

            row = await session.get(WorkItemRecord, identity)
            if row is None:
                raise RetentionConflict("retention_source_missing")
            live = _to_domain(row)
            if not hmac.compare_digest(
                row.input_projection_integrity_digest,
                _input_projection_integrity_digest(live, self._key),
            ):
                raise RetentionConflict("retention_source_corrupt")
            binding = work_binding(row)
            binding["projection_digest"] = hashlib.sha256(
                canonical_json_bytes(row.redacted_input_projection)
            ).hexdigest()
            public = {
                name: binding[name]
                for name in (
                    "id",
                    "workflow_id",
                    "trigger_id",
                    "mode",
                    "input_schema_id",
                    "input_classification",
                )
            }
            detail.run_id = (
                await session.scalars(
                    select(RunRecord.id).where(RunRecord.work_item_id == identity)
                )
            ).one()
            row.admitted_payload, row.redacted_input_projection = {}, {}
        elif kind == "action":
            from .action import _to_domain as action_domain

            action = await session.get(ExternalActionRecord, identity)
            if action is None:
                raise RetentionConflict("retention_source_missing")
            action_domain(action)
            public = {
                name: getattr(action, name)
                for name in (
                    "id",
                    "step_id",
                    "state",
                    "capability_id",
                    "connector_family",
                    "delivery_attempt_count",
                )
            }
            binding = {
                "action_hash": action.action_hash,
                "semantic_action_hash": action.semantic_action_hash,
                "idempotency_key": action.idempotency_key,
            }
            action.canonical_envelope, action.redacted_projection = {}, {}
        elif kind == "approval":
            from .approval import _decision_from_record, _verify_request_record

            approval = await session.get(ApprovalRequestRecord, identity)
            if approval is None:
                raise RetentionConflict("retention_source_missing")
            _verify_request_record(approval, self._key)
            decision = (
                await session.scalars(
                    select(ApprovalDecisionRecord).where(
                        ApprovalDecisionRecord.request_id == identity
                    )
                )
            ).one_or_none()
            if decision is not None:
                _decision_from_record(decision, self._key)
                binding["decision_integrity_digest"] = decision.integrity_digest
                public.update(
                    decision=decision.decision,
                    decision_id=decision.id,
                    decided_at=decision.decided_at.isoformat(),
                )
                decision.reason = None
            binding["request_integrity_digest"] = approval.integrity_digest
            public.update(
                id=identity,
                action_id=approval.action_id,
                status=approval.status,
                requested_at=approval.requested_at.isoformat(),
            )
            approval.redacted_projection, approval.redacted_destination = {}, "[expired]"
        elif kind == "artifact":
            from .artifact import _to_domain as artifact_domain

            artifact = await session.get(ArtifactRecord, identity)
            if artifact is None:
                raise RetentionConflict("retention_source_missing")
            parent_ids = tuple(
                (
                    await session.scalars(
                        select(ArtifactParentRecord.parent_artifact_id)
                        .where(ArtifactParentRecord.artifact_id == identity)
                        .order_by(ArtifactParentRecord.ordinal)
                    )
                ).all()
            )
            envelope = artifact_domain(artifact, parent_ids)
            public = {
                "id": identity,
                "step_id": artifact.step_id,
                "output_schema_id": artifact.output_schema_id,
                "output_schema_version": artifact.output_schema_version,
                "classification": artifact.classification,
                "parent_artifact_ids": list(parent_ids),
                "workflow_id": envelope.provenance.workflow_id,
                "template_id": envelope.provenance.template_id,
            }
            binding = {
                "envelope_fingerprint": artifact.envelope_fingerprint,
                "payload_hash": artifact.payload_hash,
            }
            artifact.payload, artifact.provenance_snapshot = {}, {}
        elif kind == "receipt":
            from .action import _receipt_to_domain

            receipt = (
                await session.scalars(
                    select(ConnectorActionReceiptRecord).where(
                        ConnectorActionReceiptRecord.receipt_id == identity
                    )
                )
            ).one()
            _receipt_to_domain(receipt)
            action = await session.get(ExternalActionRecord, receipt.external_action_id)
            if (
                action is None
                or action.connector_receipt_id != identity
                or action.connector_safe_metadata != receipt.safe_metadata
            ):
                raise RetentionConflict("retention_receipt_corrupt")
            public = {
                "receipt_id": identity,
                "action_id": receipt.external_action_id,
                "status": receipt.status,
            }
            binding = {
                "action_hash": receipt.action_hash,
                "idempotency_key": receipt.idempotency_key,
                "connector_binding_id": receipt.connector_binding_id,
            }
            detail.run_id = action.run_id
            receipt.safe_metadata, action.connector_safe_metadata = {}, {}
        elif kind == "attempt":
            from .execution_control import SQLAlchemyExecutionControlRepository

            attempt = await session.get(ExecutionAttemptRecord, identity)
            if attempt is None:
                raise RetentionConflict("retention_source_missing")
            SQLAlchemyExecutionControlRepository(session, self._key)._attempt_from_record(attempt)
            public = {
                "id": identity,
                "step_id": attempt.step_id,
                "kind": attempt.kind,
                "outcome": attempt.outcome,
                "attempt_number": attempt.attempt_number,
                "output_artifact_id": attempt.output_artifact_id,
            }
            binding = {"integrity_digest": attempt.integrity_digest}
            attempt.redacted_input = {}
        elif kind == "audit":
            audit = (
                await session.scalars(
                    select(AuditEventRecord).where(AuditEventRecord.id == identity)
                )
            ).one()
            _record_to_domain(audit)
            binding = {
                "event_fingerprint": audit.event_fingerprint,
                "metadata_fingerprint": audit.metadata_fingerprint,
            }
            public = {
                "event_id": identity,
                "event_type": audit.event_type,
                "feed_sequence": audit.feed_sequence,
                "run_sequence": audit.run_sequence,
            }
            expire_optional_metadata(audit, now=now)
        elif kind == "brief":
            from marketing_agents.application.ports.campaign_briefs import snapshot_document

            from .campaign_briefs import _hydrate

            brief = await session.get(CampaignBriefRecord, (identity, 1))
            if brief is None:
                raise RetentionConflict("retention_source_missing")
            snapshot = _hydrate(brief)
            expected = hmac.new(
                self._key.bytes_for_digest(),
                b"marketing-agents:campaign-brief:snapshot:v1\0"
                + canonical_json_bytes(snapshot_document(snapshot)),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(brief.snapshot_hash, expected):
                raise RetentionConflict("retention_source_corrupt")
            binding = {
                "snapshot_hash": brief.snapshot_hash,
                "body_digest": snapshot.body_digest,
                "key_version": snapshot.key_version,
            }
            public = {
                "id": identity,
                "revision": 1,
                "classification": snapshot.classification.value,
            }
            brief.title = brief.objective = "[expired]"
            brief.snapshot_json = "{}"
        else:
            raise RetentionConflict("retention_source_kind_invalid")
        detail.skeleton = {"public": public, "binding": binding}
        detail.purged_at = now
        detail.integrity_digest = retention_digest(detail, self._key)
