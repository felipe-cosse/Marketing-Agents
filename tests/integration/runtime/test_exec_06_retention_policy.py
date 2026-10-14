"""Configured TTLs survive real intake/worker/approval composition and restarts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.services.schedule_processing import (
    ScheduleClaimProcessingDisposition,
    ScheduleClaimProcessingService,
)
from marketing_agents.config import Settings
from marketing_agents.demos.email_signup_onboarding import EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID
from marketing_agents.domain.audit import AuditEvent
from marketing_agents.domain.entities import WorkItem
from marketing_agents.domain.enums import RunState
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.db.models import AuditEventRecord, ConnectorActionReceiptRecord
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.workers.runtime.composition import LocalRuntime, build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from marketing_agents.workers.runtime.scheduler import SchedulerWorker
from sqlalchemy import func, select

from tests.support.api import browser_request

CATALOG = Path(__file__).resolve().parents[3] / "catalog/v1"
SOCIAL = "demo.social-media.content-draft.v1"


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 10, 14, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current


async def _installation(tmp_path: Path) -> Settings:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}",
        marketing_agents_digest_key_path=tmp_path / "secrets/digest.key",
        catalog_root=CATALOG,
        retention_admitted_payload_days=2,
        retention_audit_metadata_days=3,
    )
    await migrate_local_database(settings.database_url, settings.marketing_agents_digest_key_path)
    database = create_database_runtime(settings.database_url)
    try:
        # Compile only once for this seed; each real build_runtime performs its
        # own production readiness/catalog verification without test replacement.
        await seed_catalog(compile_catalog(CATALOG), database, CroniterRecurrenceCalculator())
    finally:
        await database.dispose()
    return settings


async def _submit(client: AsyncClient, scenario_id: str) -> dict:
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/demo-scenarios/{scenario_id}/runs",
        json={},
        headers={"Idempotency-Key": f"exec-06-retention-{scenario_id}"},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def _work_and_events(
    runtime: LocalRuntime, run_id: str
) -> tuple[WorkItem, tuple[AuditEvent, ...]]:
    async with runtime.dependencies.unit_of_work() as unit_of_work:
        run = await unit_of_work.runs.get(run_id)
        assert run is not None
        work = await unit_of_work.works.get(run.work_item_id)
        assert work is not None
        events = await unit_of_work.audits.list_run(run_id, limit=100)
        assert events and len(events) < 100, "test must inspect the complete audit history"
        return work, events


def _assert_ttls(
    work: WorkItem, events: tuple[AuditEvent, ...], *, input_days: int, audit_days: int
) -> None:
    assert work.input_projection_created_at == work.created_at
    assert work.input_projection_expires_at - work.input_projection_created_at == timedelta(
        days=input_days
    )
    for event in events:
        event.draft.verify_integrity()
        assert event.draft.safe_metadata.expires_at - event.draft.occurred_at == timedelta(
            days=audit_days
        ), event.draft.event_type


@pytest.mark.asyncio
async def test_exec_06_runtime_nondefault_ttls_and_historical_decisions_survive_policy_restart(
    tmp_path: Path,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        assert runtime.dependencies.retention_policy == settings.retention_policy
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            first = await _submit(client, EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID)
            email_id = first["runId"]
            admitted, admission_events = await _work_and_events(runtime, email_id)
            _assert_ttls(admitted, admission_events, input_days=2, audit_days=3)
            assert {event.event_type for event in admission_events} >= {
                "work.created",
                "run.received",
            }

            assert await RunWorker(runtime, "worker.retention.initial").drain_once()
            run_response = await client.get(first["runUrl"])
            assert run_response.status_code == 200, run_response.text
            assert run_response.json()["state"] == "awaiting_approval"
            assert all(
                action["delivery_attempt_count"] == 0 and action["receipt_id"] is None
                for action in run_response.json()["external_actions"]
            )
            pending = run_response.json()["pending_approvals"]
            assert len(pending) == 2

            async def approve_current(approval_url: str, active_client: AsyncClient) -> None:
                detail = await active_client.get(approval_url)
                assert detail.status_code == 200, detail.text
                approval = detail.json()
                response = await browser_request(
                    active_client,
                    "POST",
                    approval_url + "/approve",
                    json={
                        "expected_generation": approval["generation"],
                        "expected_payload_hash": approval["payload_hash"],
                    },
                )
                assert response.status_code == 200, response.text

            await approve_current(pending[0]["approval_url"], client)
            old_work, old_events = await _work_and_events(runtime, email_id)
            _assert_ttls(old_work, old_events, input_days=2, audit_days=3)
            assert {event.event_type for event in old_events} >= {
                "run.plan_recorded",
                "approval.requested",
                "action.approved",
                "approval.approved",
            }
            partial = await client.get(first["runUrl"])
            assert partial.status_code == 200, partial.text
            assert partial.json()["state"] == "awaiting_approval"
            assert all(
                action["delivery_attempt_count"] == 0 and action["receipt_id"] is None
                for action in partial.json()["external_actions"]
            )

        old_by_id = {event.id: event for event in old_events}
        await runtime.close()
        clock.current += timedelta(seconds=2)
        changed_settings = settings.model_copy(
            update={"retention_admitted_payload_days": 7, "retention_audit_metadata_days": 11}
        )
        runtime = await build_runtime(changed_settings, clock=clock)
        assert runtime.dependencies.retention_policy == changed_settings.retention_policy
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            unchanged_work, unchanged_events = await _work_and_events(runtime, email_id)
            assert unchanged_work == old_work
            assert unchanged_events == old_events
            # Reading a prior decision reconstructs its historical audit facts;
            # changing today's TTL must not make valid signed history corrupt.
            prior = await client.get(pending[0]["approval_url"])
            assert prior.status_code == 200, prior.text
            assert prior.json()["status"] == "approved"
            await approve_current(pending[1]["approval_url"], client)
            assert await RunWorker(runtime, "worker.retention.restarted").drain_once()
            completed = await client.get(first["runUrl"])
            assert completed.status_code == 200, completed.text
            assert completed.json()["state"] == "completed"
            assert len(completed.json()["external_actions"]) == 2
            assert all(
                action["state"] == "succeeded"
                and action["delivery_attempt_count"] == 1
                and action["receipt_id"] is not None
                for action in completed.json()["external_actions"]
            )

            replay = await _submit(client, EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID)
            assert replay["disposition"] == "replayed"
            assert replay["runId"] == email_id
            assert replay["workId"] == first["workId"]
            new_receipt = await _submit(client, SOCIAL)
            assert await RunWorker(runtime, "worker.retention.new-admission").drain_once()
            new_work, new_events = await _work_and_events(runtime, new_receipt["runId"])
            _assert_ttls(new_work, new_events, input_days=7, audit_days=11)
            async with runtime.dependencies.unit_of_work() as unit_of_work:
                social_run = await unit_of_work.runs.get(new_receipt["runId"])
                assert social_run is not None and social_run.state is RunState.COMPLETED

            final_work, final_events = await _work_and_events(runtime, email_id)
            assert final_work == old_work
            final_by_id = {event.id: event for event in final_events}
            assert {key: final_by_id[key] for key in old_by_id} == old_by_id
            new_email_events = tuple(event for event in final_events if event.id not in old_by_id)
            assert {event.event_type for event in new_email_events} >= {
                "action.approved",
                "approval.approved",
                "approval.consumed",
                "run.transitioned",
            }
            # The old Work keeps its 2-day input TTL while newly emitted facts
            # for the same Run use the restarted runtime's 11-day audit TTL.
            _assert_ttls(final_work, new_email_events, input_days=2, audit_days=11)
            async with runtime.database.session_factory() as session:
                assert (
                    await session.scalar(
                        select(func.count()).select_from(ConnectorActionReceiptRecord)
                    )
                    == 2
                )

            clock.current = old_work.created_at + timedelta(days=3, seconds=1)
            timeline = await client.get(f"/api/v1/runs/{email_id}/timeline?limit=100")
            assert timeline.status_code == 200, timeline.text
            assert timeline.json()["next_cursor"] is None
            projected = {item["id"]: item for item in timeline.json()["items"]}
            assert set(projected) == set(final_by_id)
            for event_id, event in final_by_id.items():
                item = projected[event_id]
                assert datetime.fromisoformat(
                    item["metadata_expires_at"].replace("Z", "+00:00")
                ) == (event.safe_metadata.expires_at)
                assert item["metadata_expired"] is (event_id in old_by_id)
                if event_id in old_by_id:
                    assert item["metadata"] == {}
            # Projection expiry never rewrites persisted timestamps/fingerprints.
            assert await _work_and_events(runtime, email_id) == (final_work, final_events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_exec_06_scheduler_committed_retry_uses_recorded_not_restarted_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.acceptance.test_ac_12_scheduler_recovery import configure, snapshot

    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    observed = []
    original = ScheduleClaimProcessingService.process_claimed_once

    async def observe(service, command):
        # Observe the real worker's validator/command and committed result;
        # never replace admission, recurrence, a claim, or any persistence call.
        result = await original(service, command)
        observed.append((service._validator, command, result))
        return result

    try:
        schedule = await configure(runtime, "15 * * * *", "skip")
        clock.current = schedule.next_run_at_utc
        with monkeypatch.context() as hooks:
            hooks.setattr(ScheduleClaimProcessingService, "process_claimed_once", observe)
            assert await SchedulerWorker(runtime, "scheduler.retention.initial").drain_once()
        assert len(observed) == 1
        validator, command, processed = observed[0]
        assert processed.disposition is ScheduleClaimProcessingDisposition.PROCESSED
        assert processed.run is not None and processed.work_item is not None
        old_work, old_events = await _work_and_events(runtime, processed.run.id)
        _assert_ttls(old_work, old_events, input_days=2, audit_days=3)
        assert {event.event_type for event in processed.audit_events} == {
            "schedule.occurrence_created",
            "schedule.next_occurrence_persisted",
        }
        for event in processed.audit_events:
            assert event.safe_metadata.expires_at - event.occurred_at == timedelta(days=3)
        async with runtime.database.session_factory() as session:
            configured_events = tuple(
                await session.scalars(
                    select(AuditEventRecord).where(
                        AuditEventRecord.event_type == "instance.configuration_changed"
                    )
                )
            )
            assert len(configured_events) == 1
            assert configured_events[0].metadata_expires_at - configured_events[0].occurred_at == (
                timedelta(days=3)
            )
        before = await snapshot(runtime)
        await runtime.close()
        clock.current += timedelta(seconds=2)
        changed = settings.model_copy(
            update={"retention_admitted_payload_days": 7, "retention_audit_metadata_days": 11}
        )
        runtime = await build_runtime(changed, clock=clock)
        replay = await ScheduleClaimProcessingService(
            runtime.dependencies,
            runtime.digest_key,
            validator,
            CroniterRecurrenceCalculator(),
            current_catalog_hash=runtime.catalog.content_hash,
        ).process_claimed_once(command)
        assert replay.disposition is ScheduleClaimProcessingDisposition.DUPLICATE_SUPPRESSED
        assert replay.occurrence == processed.occurrence
        assert replay.run == processed.run and replay.work_item == processed.work_item
        assert replay.resulting_schedule == processed.resulting_schedule
        assert replay.audit_events == processed.audit_events
        assert await snapshot(runtime) == before
        assert not await SchedulerWorker(runtime, "scheduler.retention.restarted").drain_once()
        assert await snapshot(runtime) == before
        assert await RunWorker(runtime, "worker.retention.scheduled").drain_once()
        work, events = await _work_and_events(runtime, processed.run.id)
        assert work == old_work
        retained = {event.id: event for event in events}
        assert all(retained[event.id] == event for event in old_events)
        new_events = tuple(
            event for event in events if event.id not in {old.id for old in old_events}
        )
        assert new_events
        _assert_ttls(work, new_events, input_days=2, audit_days=11)
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            completed = await unit_of_work.runs.get(processed.run.id)
            assert completed is not None and completed.state is RunState.COMPLETED
    finally:
        await runtime.close()
