"""AC-12: real configured scheduler races, durable DST facts, and lease recovery.

Workers use two independent default runtimes over the same migrated SQLite/key
pair. Observation hooks always call production methods; no claim, admission,
recurrence, response, or model result is fabricated. These are in-process mock
journeys, not operating-system process isolation or live-provider qualification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.services.schedule_claiming import ScheduleClaimService
from marketing_agents.application.services.schedule_configuration import configured_schedule_id
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import (
    OccurrenceState,
    RunState,
    StepState,
    TriggerKind,
    WorkMode,
)
from marketing_agents.domain.schedule_occurrence_identity import schedule_occurrence_id
from marketing_agents.infrastructure.db.models import (
    AuditEventRecord,
    ScheduleOccurrenceRecord,
    ScheduleRecord,
)
from marketing_agents.infrastructure.db.repositories.schedule import SQLAlchemyScheduleRepository
from marketing_agents.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from marketing_agents.workers.runtime.scheduler import SchedulerWorker
from sqlalchemy import select

from tests.acceptance.test_ac_07_real_composition_demos import (
    Clock,
    installation,
    observe_real_calls,
)
from tests.acceptance.test_ac_10_webhook_replay import business_snapshot
from tests.support.api import browser_request

INSTANCE = "inst.community.education.course-progress-reminders.01"
TEMPLATE = "tpl.community.education.course-progress-reminders"
ZONE = "America/Los_Angeles"
SCHEDULE_ID = configured_schedule_id(INSTANCE)
PRIVATE_INPUT = "ac12-private-scheduled-business-input"
INPUT = {
    "request_id": "request.ac12.private-recurring-reminder",
    "source_content": json.dumps(
        {"participant_name": "Fixture", "course_title": "Safety", "next_step": PRIVATE_INPUT}
    ),
}


def utc(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    assert result.tzinfo is UTC
    return result


def client_for(runtime):
    return AsyncClient(
        transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
    )


def gap(nominal: str, resolved: datetime) -> dict:
    """Independent expected public representation of one nonexistent wall time."""
    return {
        "schema_version": 1,
        "reason": "nonexistent_local_time",
        "nominal_local": nominal,
        "timezone": ZONE,
        "resolved_at_utc": resolved.isoformat(timespec="microseconds"),
    }


def resolution_value(value):
    if value is None:
        return None
    assert value.schema_version == 1
    return {
        "schema_version": value.schema_version,
        "reason": value.reason,
        "nominal_local": value.nominal_local,
        "timezone": value.timezone,
        "resolved_at_utc": value.resolved_at_utc.isoformat(timespec="microseconds"),
    }


def assert_recurrence(result, due, resolution=None):
    assert result is not None and result.schema_version == 1
    assert result.scheduled_for_utc == due and result.scheduled_for_utc.tzinfo is UTC
    assert resolution_value(result.resolution) == resolution


def assert_calls(calls, count=0):
    assert len(calls.models) == count
    assert calls.reads == calls.writes == []


async def snapshot(runtime):
    """Exact business, scheduler and audit rows; omit ephemeral run-worker leases."""
    result = await business_snapshot(runtime)
    async with runtime.database.session_factory() as session:
        for model in (ScheduleRecord, ScheduleOccurrenceRecord, AuditEventRecord):
            table = model.__table__
            rows = (
                (await session.execute(select(table).order_by(*table.primary_key.columns)))
                .mappings()
                .all()
            )
            result[model.__tablename__] = tuple(deepcopy(dict(row)) for row in rows)
    return result


async def configure(runtime, cron, policy):
    path = f"/api/v1/agent-instances/{INSTANCE}/configuration"
    async with client_for(runtime) as client:
        current = await client.get(path)
        assert current.status_code == 200, current.text
        parameters = {
            "cron": cron,
            "timezone": ZONE,
            "misfirePolicy": policy,
            "misfireGraceSeconds": 60,
        }
        triggers = [
            item
            for item in current.json()["configuration"]["triggerBindings"]
            if item["type"] != "schedule"
        ]
        triggers.append({"type": "schedule", "enabled": True, **parameters})
        saved = await browser_request(
            client,
            "PATCH",
            path,
            headers={"If-Match": current.headers["etag"]},
            json={
                "triggerBindings": triggers,
                "schedule": parameters,
                "scheduledInput": {"input": INPUT, "executionMode": "dry_run"},
            },
        )
        assert saved.status_code == 200, saved.text
        assert "no-store" in saved.headers["cache-control"]
    async with runtime.dependencies.unit_of_work() as uow:
        schedule = await uow.schedules.get(SCHEDULE_ID)
        configuration = await uow.configurations.get(INSTANCE)
        assert schedule is not None and configuration is not None
        assert schedule.configuration_revision == configuration.configuration_revision
        assert schedule.cron == cron and schedule.timezone == ZONE and schedule.enabled
        assert schedule.misfire_policy.value == policy and schedule.misfire_grace_seconds == 60
        assert configuration.scheduled_input is not None
        assert dict(configuration.scheduled_input.admitted_payload) == INPUT
        assert configuration.scheduled_input.mode is WorkMode.DRY_RUN
        assert await uow.schedules.get_claim(SCHEDULE_ID) is None
        return schedule


async def race_schedulers(first, second, monkeypatch):
    """Both real repositories read the same due version before their real CAS."""
    assert first is not second and first.database is not second.database
    assert first.database.engine is not second.database.engine
    original_scan = SQLAlchemyScheduleRepository.list_claimable_due
    original_claim = ScheduleClaimService.claim_due_once
    original_exit = SQLAlchemyUnitOfWork.__aexit__
    arrivals = []
    claimed = []
    scanned_sessions = set()
    closed_scans = []
    released = asyncio.Event()

    async def scan(self, *, now, limit, configuration_bound_only=False):
        found = await original_scan(
            self, now=now, limit=limit, configuration_bound_only=configuration_bound_only
        )
        assert configuration_bound_only is True
        assert len(found) == 1 and found[0].id == SCHEDULE_ID
        arrivals.append(found[0])
        scanned_sessions.add(self._session)
        return found

    async def release_then_wait(self, exc_type, exc, traceback):
        session = self._session
        observed_scan = session in scanned_sessions
        # Default runtime scans use BEGIN IMMEDIATE. Release the original real
        # transaction before waiting, so the other default runtime can read.
        await original_exit(self, exc_type, exc, traceback)
        if observed_scan and exc_type is None:
            scanned_sessions.remove(session)
            closed_scans.append(session)
            if len(closed_scans) == 2:
                released.set()
            await released.wait()

    async def claim(self, *, lease_owner):
        found = await original_claim(self, lease_owner=lease_owner)
        if found is not None:
            claimed.append(found)
        return found

    with monkeypatch.context() as hooks:
        hooks.setattr(SQLAlchemyScheduleRepository, "list_claimable_due", scan)
        hooks.setattr(ScheduleClaimService, "claim_due_once", claim)
        hooks.setattr(SQLAlchemyUnitOfWork, "__aexit__", release_then_wait)
        results = await asyncio.wait_for(
            asyncio.gather(
                SchedulerWorker(first, "scheduler.ac12.first").drain_once(),
                SchedulerWorker(second, "scheduler.ac12.second").drain_once(),
            ),
            timeout=30,
        )
    assert sorted(results) == [False, True]
    assert len(arrivals) == 2 and arrivals[0] == arrivals[1]
    assert len(closed_scans) == 2 and not scanned_sessions
    assert len(claimed) == 1
    assert claimed[0].version == arrivals[0].version + 1
    return claimed[0]


async def assert_audits(runtime, occurrence, next_due, adjustments, scheduled_resolution, policy):
    event_type = {
        None: "schedule.occurrence_created",
        "skip": "schedule.misfire_skipped",
        "run_once": "schedule.misfire_run_once",
    }[policy]
    expected_types = {event_type, "schedule.next_occurrence_persisted"}
    async with runtime.database.session_factory() as session:
        persisted = list(
            await session.scalars(
                select(AuditEventRecord).where(AuditEventRecord.schedule_id == SCHEDULE_ID)
            )
        )
    assert len(persisted) == 2 and {row.event_type for row in persisted} == expected_types
    expected_summary = {
        "schema_version": 1,
        "scheduled_calculation_known": True,
        "range_observed": True,
        "scheduled_resolution": scheduled_resolution,
        "next_resolution": None,
        "adjustment_count": len(adjustments),
        "first_adjustment": adjustments[0] if adjustments else None,
        "last_adjustment": adjustments[-1] if adjustments else None,
        "adjustments_sha256": hashlib.sha256(canonical_json_bytes(list(adjustments))).hexdigest(),
    }
    async with client_for(runtime) as client:
        for row in persisted:
            expected_correlation = AuditContext.worker(
                "scheduler.ac12.expected", correlation_id=occurrence.id
            ).correlation_id
            assert row.occurrence_id == occurrence.id and row.correlation_id == expected_correlation
            assert row.safe_metadata["next_run_at_utc"] == next_due.isoformat(
                timespec="microseconds"
            )
            if adjustments:
                assert row.safe_metadata["recurrence_resolution"] == expected_summary
            else:
                assert "recurrence_resolution" not in row.safe_metadata
            response = await client.get(
                "/api/v1/audit-events", params={"event_type": row.event_type, "limit": 100}
            )
            assert response.status_code == 200, response.text
            assert "no-store" in response.headers["cache-control"]
            assert response.json()["next_cursor"] is None
            (item,) = response.json()["items"]
            assert item["id"] == row.id and item["schedule_id"] == SCHEDULE_ID
            assert item["occurrence_id"] == occurrence.id
            assert item["metadata"] == row.safe_metadata and item["metadata_expired"] is False
            for private in (PRIVATE_INPUT, INPUT["request_id"], INPUT["source_content"]):
                assert private not in response.text


async def assert_outcome(
    runtime,
    initial,
    due,
    next_due,
    final_version,
    *,
    adjustments=(),
    scheduled_resolution=None,
    policy=None,
    evaluated_at=None,
):
    async with runtime.dependencies.unit_of_work() as uow:
        schedule = await uow.schedules.get(SCHEDULE_ID)
        assert schedule is not None
        assert schedule.version == final_version
        assert schedule.cron == initial.cron and schedule.timezone == initial.timezone == ZONE
        assert schedule.configuration_revision == initial.configuration_revision
        assert schedule.last_scheduled_at_utc == due and schedule.next_run_at_utc == next_due
        assert_recurrence(schedule.next_recurrence, next_due)
        assert await uow.schedules.get_claim(SCHEDULE_ID) is None
        occurrence = await uow.schedules.get_occurrence_by_schedule_due(SCHEDULE_ID, due)
        assert occurrence is not None
        assert occurrence.id == schedule_occurrence_id(
            SCHEDULE_ID, due, recurrence_version=initial.recurrence_version
        )
        assert occurrence.timezone == ZONE and occurrence.timezone_fold == 0
        assert occurrence.scheduled_recurrence == initial.next_recurrence
        assert_recurrence(occurrence.scheduled_recurrence, due, scheduled_resolution)
        assert_recurrence(occurrence.next_recurrence, next_due)
        assert occurrence.recurrence_resolutions is not None
        assert tuple(resolution_value(item) for item in occurrence.recurrence_resolutions) == tuple(
            adjustments
        )
        if policy is None:
            assert occurrence.misfire_policy_applied is None
            assert occurrence.first_missed_at_utc is occurrence.last_missed_at_utc is None
            assert occurrence.missed_count is None and occurrence.misfire_evaluated_at_utc is None
        else:
            assert occurrence.misfire_policy_applied.value == policy
            assert occurrence.misfire_grace_seconds == 60
            assert occurrence.misfire_evaluated_at_utc == evaluated_at
            assert occurrence.first_missed_at_utc == utc("2026-03-07T10:30:00+00:00")
            assert occurrence.last_missed_at_utc == utc("2026-03-09T09:30:00+00:00")
            assert occurrence.missed_count == 3
        if policy == "skip":
            assert occurrence.state is OccurrenceState.SKIPPED
            assert occurrence.work_item_id is occurrence.run_id is None
        else:
            assert occurrence.state is OccurrenceState.ENQUEUED
            work = await uow.works.get(occurrence.work_item_id)
            run = await uow.runs.get(occurrence.run_id)
            assert work is not None and run is not None and run.state is RunState.RECEIVED
            assert work.id == run.work_item_id and work.event_id == occurrence.id
            assert work.source == "schedule" and work.instance_id == INSTANCE
            assert work.trigger_id == initial.trigger_id and work.mode is WorkMode.DRY_RUN
            assert work.configuration_revision == initial.configuration_revision
            assert work.workflow_id == initial.workflow_id
            assert run.configuration_revision == initial.configuration_revision
            assert dict(work.admitted_payload) == INPUT
            assert await uow.run_steps.get_plan(run.id) is None
            assert await uow.execution_control.get(run.id) is None
            assert not await uow.artifacts.list_for_run(run.id)
    state = await snapshot(runtime)
    assert len(state["schedule_occurrences"]) == 1
    expected = 0 if policy == "skip" else 1
    assert (
        len(state["work_items"])
        == len(state["runs"])
        == len(state["run_state_transitions"])
        == expected
    )
    for table in (
        "execution_attempts",
        "artifacts",
        "external_actions",
        "connector_action_receipts",
        "approval_requests",
    ):
        assert state[table] == ()
    await assert_audits(runtime, occurrence, next_due, adjustments, scheduled_resolution, policy)
    return occurrence


async def assert_completed(runtime, occurrence, calls):
    definition = runtime.workflows.for_catalog_role(TEMPLATE, TriggerKind.SCHEDULE)
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(occurrence.run_id)
        work = await uow.works.get(occurrence.work_item_id)
        assert run is not None and run.state is RunState.COMPLETED
        assert work is not None and work.workflow_id == definition.id
        assert await uow.schedules.get_occurrence(occurrence.id) == occurrence
        steps = await uow.run_steps.validate_plan_for_execution(run.id)
        assert len(steps) == 1 and steps[0].state is StepState.SUCCEEDED
        (artifact,) = await uow.artifacts.list_for_run(run.id)
        assert artifact.verify_payload()
        provenance = artifact.provenance
        assert (provenance.work_item_id, provenance.run_id, provenance.step_id) == (
            work.id,
            run.id,
            steps[0].id,
        )
        assert (provenance.instance_id, provenance.template_id) == (INSTANCE, TEMPLATE)
        assert provenance.workflow_id == definition.id
        assert provenance.output_schema_hash == definition.output_schema_hash
        assert artifact.payload["provenance"]["source_request_id"] == INPUT["request_id"]
        assert artifact.payload["proposed_actions"] == []
        control = await uow.execution_control.get(run.id)
        assert control is not None and (control.model_calls, control.tool_calls) == (1, 0)
    state = await snapshot(runtime)
    assert len(state["artifacts"]) == len(state["execution_attempts"]) == 1
    (attempt,) = state["execution_attempts"]
    assert (attempt["attempt_number"], attempt["outcome"]) == (1, "succeeded")
    assert attempt["output_artifact_id"] == provenance.artifact_id
    assert (
        state["external_actions"]
        == state["connector_action_receipts"]
        == state["approval_requests"]
        == ()
    )
    assert_calls(calls, 1)
    assert calls.models[0].context.run_id == run.id
    assert calls.models[0].context.step_id == steps[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_at", "cron", "due_at", "next_at", "local", "nominal", "later_at"),
    (
        (
            "2026-03-08T09:59:00+00:00",
            "30 2 * * *",
            "2026-03-08T10:00:00+00:00",
            "2026-03-09T09:30:00+00:00",
            "2026-03-08T03:00:00.000000",
            "2026-03-08T02:30:00.000000",
            "2026-03-08T10:30:00+00:00",
        ),
        (
            "2026-11-01T07:00:00+00:00",
            "30 1 * * *",
            "2026-11-01T08:30:00+00:00",
            "2026-11-02T09:30:00+00:00",
            "2026-11-01T01:30:00.000000",
            None,
            "2026-11-01T09:30:00+00:00",
        ),
    ),
    ids=("spring-gap", "fall-first-fold-only"),
)
async def test_ac_12_configured_dst_race_executes_once_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_at,
    cron,
    due_at,
    next_at,
    local,
    nominal,
    later_at,
):
    calls = observe_real_calls(monkeypatch)
    clock = Clock()
    clock.current = utc(configured_at)
    settings = await installation(tmp_path)
    first = await build_runtime(settings, clock=clock)
    second = None
    due, next_due = utc(due_at), utc(next_at)
    resolution = None if nominal is None else gap(nominal, due)
    adjustments = () if resolution is None else (resolution,)
    try:
        initial = await configure(first, cron, "run_once")
        assert initial.next_run_at_utc == due
        assert_recurrence(initial.next_recurrence, due, resolution)
        assert_calls(calls)
        pending = await snapshot(first)
        await first.close()
        first = await build_runtime(settings, clock=clock)
        assert await snapshot(first) == pending
        clock.current = due
        second = await build_runtime(settings, clock=clock)
        claim = await race_schedulers(first, second, monkeypatch)
        assert claim.scheduled_for_utc == claim.claimed_at_utc == due
        assert claim.version == initial.version + 1
        assert_calls(calls)
        occurrence = await assert_outcome(
            first,
            initial,
            due,
            next_due,
            initial.version + 2,
            adjustments=adjustments,
            scheduled_resolution=resolution,
        )
        assert occurrence.scheduled_local == local
        queued = await snapshot(first)
        await second.close()
        second = None
        await first.close()
        first = await build_runtime(settings, clock=clock)
        assert await snapshot(first) == queued
        assert await RunWorker(first, "worker.ac12.original").drain_once()
        assert not await RunWorker(first, "worker.ac12.idle").drain_once()
        await assert_completed(first, occurrence, calls)
        completed = await snapshot(first)
        await first.close()
        clock.current = utc(later_at)
        first = await build_runtime(settings, clock=clock)
        assert not await SchedulerWorker(first, "scheduler.ac12.second-fold").drain_once()
        assert not await RunWorker(first, "worker.ac12.restarted").drain_once()
        assert await snapshot(first) == completed
        await assert_completed(first, occurrence, calls)
        await assert_audits(first, occurrence, next_due, adjustments, resolution, None)
    finally:
        if second is not None:
            await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_ac_12_disabled_timezone_edit_rebinds_pending_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Disable alone preserves history; editing the timezone selects matching facts."""
    calls = observe_real_calls(monkeypatch)
    clock = Clock()
    clock.current = utc("2026-03-08T09:59:00+00:00")
    settings = await installation(tmp_path)
    runtime = await build_runtime(settings, clock=clock)
    path = f"/api/v1/agent-instances/{INSTANCE}/configuration"
    due = utc("2026-03-08T10:00:00+00:00")
    expected_utc = utc("2026-03-09T02:30:00+00:00")

    async def patch(body):
        async with client_for(runtime) as client:
            current = await client.get(path)
            assert current.status_code == 200, current.text
            changed = await browser_request(
                client, "PATCH", path, headers={"If-Match": current.headers["etag"]}, json=body
            )
            assert changed.status_code == 200, changed.text
            assert "no-store" in changed.headers["cache-control"]
        async with runtime.dependencies.unit_of_work() as uow:
            selected = await uow.schedules.get(SCHEDULE_ID)
            configuration = await uow.configurations.get(INSTANCE)
            assert selected is not None and configuration is not None
            assert selected.configuration_revision == configuration.configuration_revision
            assert await uow.schedules.get_claim(SCHEDULE_ID) is None
            assert dict(configuration.scheduled_input.admitted_payload) == INPUT
            return selected

    try:
        original = await configure(runtime, "30 2 * * *", "run_once")
        assert_recurrence(original.next_recurrence, due, gap("2026-03-08T02:30:00.000000", due))
        disabled = await patch({"enabled": False})
        assert disabled.enabled is False and disabled.timezone == ZONE
        assert disabled.version == original.version + 1
        assert disabled.next_run_at_utc == due
        assert disabled.next_recurrence == original.next_recurrence
        clock.current = due + timedelta(minutes=1)
        before = await snapshot(runtime)
        assert not await SchedulerWorker(runtime, "scheduler.ac12.disabled").drain_once()
        assert await snapshot(runtime) == before

        async with client_for(runtime) as client:
            current = await client.get(path)
            assert current.status_code == 200, current.text
            configuration = current.json()["configuration"]
        parameters = {**configuration["schedule"], "timezone": "UTC"}
        triggers = [
            {**item, "timezone": "UTC"} if item["type"] == "schedule" else item
            for item in configuration["triggerBindings"]
        ]
        changed = await patch({"schedule": parameters, "triggerBindings": triggers})
        assert changed.enabled is False and changed.timezone == "UTC"
        assert changed.version == disabled.version + 1
        assert changed.cron == original.cron and changed.next_run_at_utc == expected_utc
        assert_recurrence(changed.next_recurrence, expected_utc)
        updated = await snapshot(runtime)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await snapshot(runtime) == updated
        assert not await SchedulerWorker(runtime, "scheduler.ac12.disabled-edited").drain_once()
        assert await snapshot(runtime) == updated

        enabled = await patch({"enabled": True})
        assert enabled.enabled is True and enabled.timezone == "UTC"
        assert enabled.version == changed.version + 1
        assert enabled.next_run_at_utc == expected_utc
        assert_recurrence(enabled.next_recurrence, expected_utc)
        assert enabled.next_recurrence == changed.next_recurrence
        assert not await SchedulerWorker(runtime, "scheduler.ac12.reenabled").drain_once()
        assert not await RunWorker(runtime, "worker.ac12.no-admission").drain_once()
        state = await snapshot(runtime)
        assert state["schedule_occurrences"] == state["work_items"] == state["runs"] == ()
        assert state["artifacts"] == state["execution_attempts"] == ()
        assert_calls(calls)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ("skip", "run_once"))
async def test_ac_12_abandoned_claim_strict_expiry_retains_interior_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
):
    calls = observe_real_calls(monkeypatch)
    clock = Clock()
    clock.current = utc("2026-03-07T09:00:00+00:00")
    settings = await installation(tmp_path)
    first = await build_runtime(settings, clock=clock)
    second = None
    due = utc("2026-03-07T10:30:00+00:00")
    next_due = utc("2026-03-10T09:30:00+00:00")
    adjustments = (gap("2026-03-08T02:30:00.000000", utc("2026-03-08T10:00:00+00:00")),)
    try:
        initial = await configure(first, "30 2 * * *", policy)
        assert_recurrence(initial.next_recurrence, due)
        clock.current = utc("2026-03-09T10:00:00+00:00")
        abandoned = await ScheduleClaimService(
            first.dependencies, require_configuration_binding=True
        ).claim_due_once(lease_owner="scheduler.ac12.abandoned")
        assert abandoned is not None and abandoned.version == initial.version + 1
        assert abandoned.scheduled_for_utc == due
        assert abandoned.lease_expires_at_utc == clock.current + timedelta(minutes=2)
        claimed = await snapshot(first)
        assert claimed["schedule_occurrences"] == claimed["work_items"] == claimed["runs"] == ()
        assert_calls(calls)
        await first.close()
        first = await build_runtime(settings, clock=clock)
        second = await build_runtime(settings, clock=clock)
        assert await snapshot(first) == claimed
        for when in (
            abandoned.lease_expires_at_utc - timedelta(microseconds=1),
            abandoned.lease_expires_at_utc,
        ):
            clock.current = when
            assert not await SchedulerWorker(first, "scheduler.ac12.before-expiry").drain_once()
            assert not await SchedulerWorker(second, "scheduler.ac12.at-expiry").drain_once()
            async with first.dependencies.unit_of_work() as uow:
                assert await uow.schedules.get_claim(SCHEDULE_ID) == abandoned
            assert await snapshot(first) == claimed
        clock.current = abandoned.lease_expires_at_utc + timedelta(microseconds=1)
        replacement = await race_schedulers(first, second, monkeypatch)
        assert replacement.scheduled_for_utc == abandoned.scheduled_for_utc
        assert replacement.recurrence_version == abandoned.recurrence_version
        assert replacement.version == abandoned.version + 1
        assert replacement.claimed_at_utc == clock.current
        assert replacement.lease_owner != abandoned.lease_owner
        assert_calls(calls)
        occurrence = await assert_outcome(
            first,
            initial,
            due,
            next_due,
            abandoned.version + 2,
            adjustments=adjustments,
            policy=policy,
            evaluated_at=clock.current,
        )
        assert occurrence.scheduled_local == "2026-03-07T02:30:00.000000"
        committed = await snapshot(first)
        async with first.dependencies.unit_of_work() as uow:
            assert not await uow.schedules.fence_claim(abandoned, now=clock.current)
        assert await snapshot(first) == committed
        await second.close()
        second = None
        await first.close()
        first = await build_runtime(settings, clock=clock)
        assert await snapshot(first) == committed
        assert not await SchedulerWorker(first, "scheduler.ac12.recovered-idle").drain_once()
        worker = RunWorker(first, "worker.ac12.recovered")
        assert await worker.drain_once() is (policy == "run_once")
        assert not await worker.drain_once()
        if policy == "run_once":
            await assert_completed(first, occurrence, calls)
        else:
            assert_calls(calls)
            assert await snapshot(first) == committed
        completed = await snapshot(first)
        await first.close()
        first = await build_runtime(settings, clock=clock)
        assert not await SchedulerWorker(first, "scheduler.ac12.final").drain_once()
        assert not await RunWorker(first, "worker.ac12.final").drain_once()
        assert await snapshot(first) == completed
        assert_calls(calls, int(policy == "run_once"))
        await assert_audits(first, occurrence, next_due, adjustments, None, policy)
    finally:
        if second is not None:
            await second.close()
        await first.close()
