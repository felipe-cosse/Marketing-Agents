"""EXEC-06: six-category erasure, scoped leases, safe skeletons and rollback."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.services.retention_maintenance import RetentionMaintenanceService
from marketing_agents.domain.retained_details import RetainedWorkItem, RetentionClaim
from marketing_agents.domain.retention import RetentionCategory, RetentionPolicy
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.migrations import upgrade_database
from marketing_agents.infrastructure.db.models import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ArtifactRecord,
    AuditEventRecord,
    CampaignBriefRecord,
    ConnectorActionReceiptRecord,
    ExecutionAttemptRecord,
    ExternalActionRecord,
    RetentionDetailRecord,
    RunRecord,
    RunWorkerClaimRecord,
    WorkItemRecord,
)
from marketing_agents.infrastructure.db.repositories.audit import SQLAlchemyAuditRepository
from marketing_agents.infrastructure.db.repositories.retention import (
    RetentionConflict,
    SQLAlchemyRetentionRepository,
)
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.retention import RetentionWorker
from marketing_agents.workers.runtime.run_loop import RunWorker
from sqlalchemy import delete, func, select, update

from tests.acceptance.test_ac_07_real_composition_demos import (
    EMAIL,
    READ_DEMOS,
    Clock,
    current_requests,
    installation,
    submit,
)
from tests.integration.db.test_exec_05_campaign_briefs import KEY, OPERATOR, _command, _service
from tests.support.api import browser_request


class FailingAudit(SQLAlchemyAuditRepository):
    async def append_global_many(self, events):
        await super().append_global_many(events)
        raise RuntimeError("injected retention audit failure")


class ExpiringClock:
    def __init__(self, claim):
        self.values = iter((claim.claimed_at, claim.expires_at))

    def now(self):
        return next(self.values)


async def _brief_database(tmp_path):
    database = create_database_runtime(
        f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}",
        retention_policy=RetentionPolicy(),
        retention_key=KEY,
    )
    await upgrade_database(database)
    await _service(database).create(principal=OPERATOR, command=_command())
    return database


async def _assert_recovery_selection_fences(database, repository, clock, terminal_run, active_run):
    """Exercise each exclusion predicate against real SQL rows, rollback each probe."""
    async with database.session_factory() as session:

        async def eligible():
            return set((await session.scalars(repository._eligible_runs(clock.now()))).all())

        assert terminal_run in await eligible() and active_run not in await eligible()
        transaction = await session.begin_nested()
        existing = await session.get(RunWorkerClaimRecord, terminal_run)
        if existing is None:
            session.add(
                RunWorkerClaimRecord(
                    run_id=terminal_run,
                    owner="worker.retention.fence",
                    token="a" * 64,
                    claimed_at=clock.now(),
                    expires_at=clock.now() + timedelta(minutes=1),
                    available_at=clock.now(),
                    version=1,
                )
            )
            await session.flush()
        else:
            await session.execute(
                update(RunWorkerClaimRecord)
                .where(
                    RunWorkerClaimRecord.run_id == terminal_run,
                )
                .values(claimed_at=clock.now(), expires_at=clock.now() + timedelta(minutes=1))
            )
        assert terminal_run not in await eligible()
        await transaction.rollback()
        assert terminal_run in await eligible()

        transaction = await session.begin_nested()
        await session.execute(
            update(ExternalActionRecord)
            .where(
                ExternalActionRecord.run_id == terminal_run,
            )
            .values(
                state="outcome_unknown",
                connector_receipt_id=None,
                connector_result_status=None,
                connector_safe_metadata=None,
                completed_at=None,
                terminal_reason_code="outcome_unknown",
            )
        )
        assert terminal_run not in await eligible()
        await transaction.rollback()
        assert terminal_run in await eligible()

        for values in (
            dict(
                outcome=None,
                completed_at=None,
                retry_not_before=None,
                terminal_reason_code=None,
                safe_error_code=None,
                output_artifact_id=None,
                version=1,
            ),
            dict(
                outcome="transient_failure",
                retry_not_before=clock.now(),
                terminal_reason_code=None,
                output_artifact_id=None,
                version=2,
            ),
        ):
            transaction = await session.begin_nested()
            await session.execute(
                update(ExecutionAttemptRecord)
                .where(
                    ExecutionAttemptRecord.run_id == terminal_run,
                )
                .values(**values)
            )
            assert terminal_run not in await eligible()
            await transaction.rollback()
            assert terminal_run in await eligible()


@pytest.mark.asyncio
async def test_retention_leases_scope_fencing_repetition_and_audit_failure_rollback(tmp_path):
    database = await _brief_database(tmp_path)
    clock = Clock()
    clock.current = datetime(2027, 1, 20, tzinfo=UTC)
    repository = SQLAlchemyRetentionRepository(database.session_factory, KEY)
    try:
        claims = await asyncio.gather(
            *[
                repository.claim(
                    occurrence_key="retention.concurrent",
                    owner=f"worker.{number}",
                    now=clock.now(),
                    lease_seconds=10,
                    batch_size=1,
                )
                for number in range(2)
            ]
        )
        assert sum(isinstance(claim, RetentionClaim) for claim in claims) == 1
        first = next(claim for claim in claims if isinstance(claim, RetentionClaim))
        with pytest.raises(RetentionConflict, match="scope"):
            await repository.claim(
                occurrence_key=first.occurrence_key,
                owner="worker.other",
                now=clock.now(),
                lease_seconds=10,
                batch_size=2,
            )
        clock.current += timedelta(seconds=10)
        replacement = await repository.claim(
            occurrence_key=first.occurrence_key,
            owner="worker.replacement",
            now=clock.now(),
            lease_seconds=10,
            batch_size=1,
        )
        assert isinstance(replacement, RetentionClaim) and replacement.version == first.version + 1
        assert replacement.cutoff_at == first.cutoff_at
        with pytest.raises(RetentionConflict):
            await repository.execute(first, clock)
        with pytest.raises(RetentionConflict, match="stale"):
            await repository.execute(replace(replacement, owner="worker.forged"), clock)
        with pytest.raises(RetentionConflict, match="lease_expired"):
            await repository.execute(replacement, ExpiringClock(replacement))
        failing = SQLAlchemyRetentionRepository(
            database.session_factory, KEY, audit_factory=FailingAudit
        )
        with pytest.raises(RuntimeError, match="injected retention"):
            await failing.execute(replacement, clock)
        async with database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RetentionDetailRecord)
                    .where(RetentionDetailRecord.purged_at.is_not(None))
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEventRecord)
                    .where(AuditEventRecord.event_type == "retention.completed")
                )
                == 0
            )
            row = (await session.scalars(select(CampaignBriefRecord))).one()
            assert row.objective != "[expired]"
        result = await repository.execute(replacement, clock)
        assert sum(result.counts.values()) == 1
        replay = await repository.claim(
            occurrence_key=first.occurrence_key,
            owner="worker.restart",
            now=clock.now(),
            lease_seconds=10,
            batch_size=1,
        )
        assert replay.replayed and replay.counts == result.counts
        with pytest.raises(RetentionConflict, match="stale"):
            await repository.execute(replacement, clock)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_retention_all_categories_keep_run_index_timeline_and_safe_authenticated_skeletons(
    tmp_path,
):
    settings = (await installation(tmp_path)).model_copy(
        update={
            "retention_admitted_payload_days": 1,
            "retention_external_action_payload_days": 2,
            "retention_approval_detail_days": 3,
            "retention_artifact_detail_days": 4,
            "retention_connector_receipt_detail_days": 5,
            "retention_audit_metadata_days": 6,
        }
    )
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            email_id = await submit(client, EMAIL)
            assert await RunWorker(runtime, "worker.retention.email").drain_once()
            requests = await current_requests(runtime, email_id)
            for stored in requests:
                response = await browser_request(
                    client,
                    "POST",
                    f"/api/v1/approvals/{stored.request.id}/approve",
                    json={
                        "expected_generation": stored.request.generation,
                        "expected_payload_hash": stored.request.action_hash,
                        "reason": "expired-reason-canary",
                    },
                )
                assert response.status_code == 200, response.text
            clock.current += timedelta(seconds=2)
            assert await RunWorker(runtime, "worker.retention.email").drain_once()
            social_id = await submit(client, READ_DEMOS[0][0])
            assert await RunWorker(runtime, "worker.retention.social").drain_once()
            # A second admitted Run is left active, so all its expired rows must survive.
            active = await browser_request(
                client,
                "POST",
                f"/api/v1/demo-scenarios/{EMAIL}/runs",
                json={},
                headers={"Idempotency-Key": "retention-active-email"},
            )
            assert active.status_code == 202
            active_id = active.json()["runId"]
            assert await RunWorker(runtime, "worker.retention.active").drain_once()
            created_brief = await browser_request(
                client,
                "POST",
                "/api/v1/campaign-briefs",
                json={
                    "title": "Expired brief",
                    "objective": "expired-objective-canary",
                    "constraints": {},
                    "sourceRefs": [],
                },
                headers={"Idempotency-Key": "retention-expired-brief"},
            )
            assert created_brief.status_code == 201, created_brief.text
            original_timeline = await client.get(f"/api/v1/runs/{email_id}/timeline?limit=100")
            assert original_timeline.status_code == 200
            live = await client.get(f"/api/v1/runs/{email_id}")
            assert live.status_code == 200 and live.json()["details_expired"] is False
            async with runtime.database.session_factory() as session:
                tracked = tuple((await session.scalars(select(RetentionDetailRecord))).all())
                assert {record.category for record in tracked} == {
                    item.value for item in RetentionCategory
                }
                for record in tracked:
                    assert (
                        record.expires_at - record.created_at
                        == settings.retention_policy.ttl_for(RetentionCategory(record.category))
                    )
                # One legacy action has no expiry yet. Registration uses the
                # historical seven-day policy, not today's configured two days.
                legacy_action = next(
                    record.record_id for record in tracked if record.record_kind == "action"
                )
                await session.execute(
                    delete(RetentionDetailRecord).where(
                        RetentionDetailRecord.record_kind == "action",
                        RetentionDetailRecord.record_id == legacy_action,
                    )
                )
                await session.commit()
            clock.current += timedelta(days=91)
            repository = SQLAlchemyRetentionRepository(
                runtime.database.session_factory, runtime.digest_key
            )
            await _assert_recovery_selection_fences(
                runtime.database, repository, clock, email_id, active_id
            )
            maintenance = RetentionMaintenanceService(
                repository, clock, owner="worker.retention", batch_size=100
            )
            totals = {category.value: 0 for category in RetentionCategory}
            for number in range(4):
                result = await maintenance.run_once(occurrence_key=f"retention.test.{number}")
                assert result is not None and sum(result.counts.values()) <= 100
                for category, count in result.counts.items():
                    totals[category] += count
                if not sum(result.counts.values()):
                    break
            assert all(totals.values()), totals
            worker = RetentionWorker(runtime, "worker.retention.process")
            assert await worker.drain_once()
            assert not await worker.drain_once()
            assert not await RetentionWorker(runtime, "worker.retention.restart").drain_once()
            clock.current += timedelta(minutes=1)
            assert await worker.drain_once()
            index = await client.get("/api/v1/runs?limit=100")
            assert index.status_code == 200, index.text
            assert {item["id"] for item in index.json()["items"]} >= {
                email_id,
                social_id,
                active_id,
            }
            timeline = await client.get(f"/api/v1/runs/{email_id}/timeline?limit=100")
            assert timeline.status_code == 200, timeline.text
            original = original_timeline.json()["items"]
            assert [
                (i["id"], i["sequence"], i["event_type"]) for i in timeline.json()["items"]
            ] == [(i["id"], i["sequence"], i["event_type"]) for i in original]
            historical = await client.get(f"/api/v1/runs/{email_id}")
            assert historical.status_code == 200, historical.text
            assert historical.json()["details_expired"] is True
            assert historical.json()["state"] == "completed"
            assert all(item["metadata_expired"] for item in timeline.json()["items"])
            async with runtime.database.session_factory() as session:
                erased = tuple(
                    (
                        await session.scalars(
                            select(RetentionDetailRecord).where(
                                RetentionDetailRecord.purged_at.is_not(None)
                            )
                        )
                    ).all()
                )
                assert {record.record_kind for record in erased} == {
                    "work",
                    "action",
                    "approval",
                    "artifact",
                    "receipt",
                    "attempt",
                    "audit",
                    "brief",
                }
                assert all(record.run_id != active_id for record in erased)
                legacy = next(record for record in erased if record.record_id == legacy_action)
                assert legacy.expires_at - legacy.created_at == timedelta(days=7)
                for model, column in (
                    (ArtifactRecord, ArtifactRecord.payload),
                    (ExternalActionRecord, ExternalActionRecord.canonical_envelope),
                    (ApprovalRequestRecord, ApprovalRequestRecord.redacted_projection),
                    (ExecutionAttemptRecord, ExecutionAttemptRecord.redacted_input),
                ):
                    values = (
                        await session.scalars(
                            select(column).where(model.run_id.in_((email_id, social_id)))
                        )
                    ).all()
                    assert values and all(value == {} for value in values)
                assert all(
                    value is None
                    for value in (
                        await session.scalars(
                            select(ApprovalDecisionRecord.reason).where(
                                ApprovalDecisionRecord.run_id == email_id
                            )
                        )
                    ).all()
                )
                assert all(
                    value == {}
                    for value in (
                        await session.scalars(
                            select(ExternalActionRecord.connector_safe_metadata).where(
                                ExternalActionRecord.run_id == email_id,
                            )
                        )
                    ).all()
                )
                assert all(
                    value == {}
                    for value in (
                        await session.scalars(select(ConnectorActionReceiptRecord.safe_metadata))
                    ).all()
                )
                run = await session.get(RunRecord, email_id)
                work_id = run.work_item_id
                work = await session.get(WorkItemRecord, work_id)
                assert work.admitted_payload == work.redacted_input_projection == {}
            async with runtime.dependencies.unit_of_work() as uow:
                retained = await uow.works.get(work_id)
                assert isinstance(retained, RetainedWorkItem) and not hasattr(
                    retained, "admitted_payload"
                )
            for record in erased:
                skeleton = await repository.inspect(record.record_kind, record.record_id)
                assert skeleton is not None
                public_response = await client.get(
                    f"/api/v1/retained-details/{record.record_kind}/{record.record_id}"
                )
                assert public_response.status_code == 200, public_response.text
                rendered = str(dict(skeleton.metadata))
                assert (
                    "expired-reason-canary" not in rendered
                    and "expired-objective-canary" not in rendered
                )
            for kind, resource in (
                ("artifact", "artifacts"),
                ("approval", "approvals"),
                ("action", "external-actions"),
                ("brief", "campaign-briefs"),
            ):
                record = next(item for item in erased if item.record_kind == kind)
                expired = await client.get(f"/api/v1/{resource}/{record.record_id}")
                assert expired.status_code == 410, (kind, expired.text)
                assert expired.json()["code"] == "detail_expired"
            replay = await browser_request(
                client,
                "POST",
                f"/api/v1/demo-scenarios/{EMAIL}/runs",
                json={},
                headers={"Idempotency-Key": f"ac07-{EMAIL}"},
            )
            assert replay.status_code == 410, replay.text
            async with runtime.database.session_factory() as session:
                before_runs = await session.scalar(select(func.count()).select_from(RunRecord))
                before_events = await session.scalar(
                    select(func.count()).select_from(AuditEventRecord)
                )
            collision = await browser_request(
                client,
                "POST",
                f"/api/v1/demo-scenarios/{EMAIL}/runs",
                json={"overrides": {"welcome_context": "changed valid context"}},
                headers={"Idempotency-Key": f"ac07-{EMAIL}"},
            )
            assert collision.status_code == 409, collision.text
            async with runtime.database.session_factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(RunRecord)) == before_runs
                )
                assert (
                    await session.scalar(select(func.count()).select_from(AuditEventRecord))
                    == before_events + 2
                )
            # Tampering with an authenticated skeleton cannot turn it into live execution data.
            async with runtime.database.session_factory() as session, session.begin():
                await session.execute(
                    update(RetentionDetailRecord)
                    .where(
                        RetentionDetailRecord.record_kind == "work",
                        RetentionDetailRecord.record_id == work_id,
                    )
                    .values(skeleton={"public": {}, "binding": {}})
                )
            with pytest.raises(ValueError, match="integrity"):
                await repository.inspect("work", work_id)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_retention_independent_receipt_expiry_keeps_live_action_identity(tmp_path):
    settings = (await installation(tmp_path)).model_copy(
        update={
            "retention_admitted_payload_days": 30,
            "retention_external_action_payload_days": 30,
            "retention_approval_detail_days": 30,
            "retention_artifact_detail_days": 30,
            "retention_connector_receipt_detail_days": 1,
        }
    )
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await submit(client, EMAIL)
            await RunWorker(runtime, "worker.retention.independent").drain_once()
            for stored in await current_requests(runtime, run_id):
                response = await browser_request(
                    client,
                    "POST",
                    f"/api/v1/approvals/{stored.request.id}/approve",
                    json={
                        "expected_generation": stored.request.generation,
                        "expected_payload_hash": stored.request.action_hash,
                    },
                )
                assert response.status_code == 200, response.text
            clock.current += timedelta(seconds=2)
            await RunWorker(runtime, "worker.retention.independent").drain_once()
            clock.current += timedelta(days=2)
            repository = SQLAlchemyRetentionRepository(
                runtime.database.session_factory, runtime.digest_key
            )
            result = await RetentionMaintenanceService(
                repository, clock, owner="worker.retention", batch_size=100
            ).run_once()
            assert result.counts["connector_receipt_detail"] == 2
            assert result.counts["external_action_payload"] == 0
            assert result.counts["artifact_detail"] == 0
            async with runtime.dependencies.unit_of_work() as uow:
                plan = await uow.run_steps.get_inspectable_plan(run_id)
                actions = await uow.external_actions.list_run_plan(run_id, plan.plan.plan_hash)
                assert len(actions) == 2
                assert all(
                    action.result is not None and not action.result.safe_metadata
                    for action in actions
                )
            historical = await client.get(f"/api/v1/runs/{run_id}")
            assert historical.status_code == 200 and historical.json()["details_expired"] is True
            async with runtime.database.session_factory() as session:
                assert all(
                    not item
                    for item in (
                        await session.scalars(select(ConnectorActionReceiptRecord.safe_metadata))
                    ).all()
                )
                assert all(
                    not item
                    for item in (
                        await session.scalars(select(ExternalActionRecord.connector_safe_metadata))
                    ).all()
                )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_retention_populated_0010_upgrade_and_bounded_immutable_legacy_registration(tmp_path):
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        await upgrade_database(database, revision="0010")
        await _service(database).create(principal=OPERATOR, command=_command())
        async with database.session_factory() as session:
            original = (await session.scalars(select(CampaignBriefRecord))).one()
            original_snapshot = original.snapshot_json
        assert await upgrade_database(database) == "0011"
        async with database.session_factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(RetentionDetailRecord)) == 0
            )
            assert (
                await session.get(CampaignBriefRecord, (original.id, original.revision))
            ).snapshot_json == original_snapshot
        clock = Clock()
        clock.current = datetime(2026, 10, 15, tzinfo=UTC)
        repository = SQLAlchemyRetentionRepository(
            database.session_factory, KEY, retention_policy=RetentionPolicy(1, 1, 1, 1, 1, 1)
        )
        service = RetentionMaintenanceService(
            repository, clock, owner="worker.legacy", batch_size=1
        )
        result = await service.run_once(occurrence_key="retention.legacy.first")
        assert sum(result.counts.values()) == 0
        async with database.session_factory() as session:
            first = tuple((await session.scalars(select(RetentionDetailRecord))).all())
            assert len(first) == 1
            first_expiry = first[0].expires_at
        assert (await service.run_once(occurrence_key="retention.legacy.first")).replayed
        # Original brief and audit persisted expiries survive current policy changes.
        for number in range(3):
            await service.run_once(occurrence_key=f"retention.legacy.next.{number}")
        async with database.session_factory() as session:
            assert (
                await session.get(RetentionDetailRecord, (first[0].record_kind, first[0].record_id))
            ).expires_at == first_expiry
            brief = await session.get(RetentionDetailRecord, ("brief", original.id))
            assert brief.expires_at - brief.created_at == timedelta(days=7)
            assert brief.purged_at is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_retention_preserves_expired_parent_while_younger_child_remains_live(tmp_path):
    from marketing_agents.application.orchestration import OrchestrationDependencies
    from marketing_agents.application.services.run_cancellation import RunCancellationService

    from tests.integration.db.test_run_06_artifact_persistence import (
        NOW,
        IncrementingClock,
        IncrementingIds,
        _artifact,
        _audit_context,
        _persist,
        _prepare_run,
        _uow_factory,
    )

    database = create_database_runtime(
        f"sqlite+aiosqlite:///{tmp_path / 'lineage.db'}",
        retention_policy=RetentionPolicy(),
        retention_key=KEY,
    )
    try:
        await upgrade_database(database)
        work, persisted = await _prepare_run(database, "retention.lineage", dependent_steps=True)
        factory = _uow_factory(database)
        parent = _artifact(
            artifact_id="artifact.retention.parent", work_item=work, persisted=persisted
        )
        child = _artifact(
            artifact_id="artifact.retention.child",
            work_item=work,
            persisted=persisted,
            step_key="summarize",
            parents=(parent,),
            created_at=NOW + timedelta(hours=2),
        )
        await _persist(factory, parent)
        await _persist(factory, child)
        dependencies = OrchestrationDependencies(
            IncrementingClock(NOW + timedelta(hours=3)),
            IncrementingIds("retention.cancel"),
            factory,
        )
        await RunCancellationService(dependencies).request(
            persisted.run.id,
            audit_context=_audit_context("retention.cancel"),
        )
        clock = Clock()
        clock.current = NOW + timedelta(days=30, hours=1, minutes=30)
        repository = SQLAlchemyRetentionRepository(database.session_factory, KEY)
        service = RetentionMaintenanceService(
            repository, clock, owner="worker.retention.lineage", batch_size=100
        )
        result = await service.run_once(occurrence_key="retention.lineage.parent_due")
        assert result.counts["artifact_detail"] == 0
        async with factory() as uow:
            assert await uow.artifacts.get(parent.provenance.artifact_id) == parent
            assert await uow.artifacts.get(child.provenance.artifact_id) == child
        clock.current += timedelta(hours=1)
        result = await service.run_once(occurrence_key="retention.lineage.child_due")
        assert result.counts["artifact_detail"] == 1
        assert await repository.inspect("artifact", child.provenance.artifact_id) is not None
        assert await repository.inspect("artifact", parent.provenance.artifact_id) is None
        result = await service.run_once(occurrence_key="retention.lineage.parent_unblocked")
        assert result.counts["artifact_detail"] == 1
        assert await repository.inspect("artifact", parent.provenance.artifact_id) is not None
    finally:
        await database.dispose()
