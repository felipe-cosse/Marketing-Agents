"""DEL-05 real local composition: asynchronous admission, workers, and recovery."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.services.controlled_read_executor import ControlledReadExecutor
from marketing_agents.config import Settings
from marketing_agents.demos import DEMO_SCENARIOS
from marketing_agents.demos.email_signup_onboarding import EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID
from marketing_agents.domain.entities import Schedule
from marketing_agents.domain.enums import MisfirePolicy, RunState
from marketing_agents.domain.schedule_occurrence_identity import SCHEDULE_RECURRENCE_VERSION
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.db.models import (
    ConnectorActionReceiptRecord,
    RunWorkerClaimRecord,
)
from marketing_agents.infrastructure.runtime.run_claims import RunClaims
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.workers.process import _health
from marketing_agents.workers.runtime.composition import RuntimeNotReady, build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from marketing_agents.workers.runtime.scheduler import SchedulerWorker
from marketing_agents.workers.worker_health import healthy
from sqlalchemy import func, select

from tests.support.api import browser_request

CATALOG = Path(__file__).resolve().parents[3] / "catalog" / "v1"


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 8, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current


async def _installation(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}",
        marketing_agents_digest_key_path=tmp_path / "secrets" / "digest.key",
        catalog_root=CATALOG,
    )
    await migrate_local_database(settings.database_url, settings.marketing_agents_digest_key_path)
    database = create_database_runtime(settings.database_url)
    try:
        await seed_catalog(compile_catalog(CATALOG), database, CroniterRecurrenceCalculator())
    finally:
        await database.dispose()
    return settings


async def _submit(client, scenario_id):
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/demo-scenarios/{scenario_id}/runs",
        json={},
        headers={"Idempotency-Key": f"local-runtime-{scenario_id}"},
    )
    assert response.status_code == 202, response.text
    return response.json()["runId"]


@pytest.mark.asyncio
async def test_del_05_api_to_worker_all_read_only_artifacts_and_restart(tmp_path: Path) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_ids = []
            for scenario in DEMO_SCENARIOS.list():
                if scenario.id == EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID:
                    continue
                run_id = await _submit(client, scenario.id)
                run_ids.append(run_id)
                queued = await client.get(f"/api/v1/runs/{run_id}")
                assert queued.json()["state"] == "received"
        await runtime.close()
        runtime = await build_runtime(settings)
        worker = RunWorker(runtime, "worker.integration.restarted")
        for _ in run_ids:
            assert await worker.drain_once()
        assert not await worker.drain_once()
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            for run_id in run_ids:
                run = await unit_of_work.runs.get(run_id)
                artifacts = await unit_of_work.artifacts.list_for_run(run_id)
                assert run is not None and run.state is RunState.COMPLETED
                assert len(artifacts) == 1 and artifacts[0].verify_payload()
                history = await unit_of_work.runs.list_transitions(run_id)
                assert [item.new_state.value for item in history] == [
                    "received",
                    "validated",
                    "planned",
                    "executing",
                    "completed",
                ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_del_05_email_api_approval_resume_records_two_durable_receipts(
    tmp_path: Path,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            for instance_id, family in (
                ("inst.email.newsletter.newsletter-subscriber.01", "newsletter"),
                ("inst.email.lifecycle-marketing.customer-onboarder.01", "crm"),
            ):
                configuration = await unit_of_work.configurations.get(instance_id)
                assert configuration is not None
                assert configuration.configuration_revision == 1
                assert (
                    configuration.connector_bindings[family].binding_id == f"mock.{family}.default"
                )
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await _submit(client, EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID)
            assert await RunWorker(runtime, "worker.email.prepare").drain_once()
            async with runtime.dependencies.unit_of_work() as unit_of_work:
                run = await unit_of_work.runs.get(run_id)
                assert run is not None and run.state is RunState.AWAITING_APPROVAL
                selection = await unit_of_work.approvals.get_current_authorization_set(run_id)
                assert selection is not None
                requests = await unit_of_work.approvals.list_current_set(
                    run_id,
                    selection.authorization_set.plan_hash,
                    selection.authorization_set.proposal_revision,
                )
            async with runtime.database.session_factory() as session:
                assert (
                    await session.scalar(
                        select(func.count()).select_from(ConnectorActionReceiptRecord)
                    )
                    == 0
                )
            social_id = await _submit(client, "demo.social-media.content-draft.v1")
            clock.current += timedelta(seconds=2)
            assert await RunWorker(runtime, "worker.approval-fairness").drain_once()
            async with runtime.dependencies.unit_of_work() as unit_of_work:
                social = await unit_of_work.runs.get(social_id)
                assert social is not None and social.state is RunState.COMPLETED
            for stored in requests:
                approval = stored.request
                response = await browser_request(
                    client,
                    "POST",
                    f"/api/v1/approvals/{approval.id}/approve",
                    json={
                        "expected_generation": approval.generation,
                        "expected_payload_hash": approval.action_hash,
                    },
                )
                assert response.status_code == 200, response.text
        await runtime.close()
        clock.current += timedelta(seconds=2)
        runtime = await build_runtime(settings, clock=clock)
        assert await RunWorker(runtime, "worker.email.resume").drain_once()
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            assert len(await unit_of_work.artifacts.list_for_run(run_id)) == 1
        async with runtime.database.session_factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(ConnectorActionReceiptRecord))
                == 2
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_del_05_claim_race_expiry_and_stale_release_are_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            await _submit(client, "demo.social-media.content-draft.v1")
        claims = RunClaims(runtime, lease_seconds=5)
        results = await asyncio.gather(
            claims.claim_once("worker.one"), claims.claim_once("worker.two")
        )
        owners = [item for item in results if item is not None]
        assert len(owners) == 1
        first = owners[0]
        assert await claims.renew(first)
        clock.current += timedelta(seconds=6)
        second = await claims.claim_once("worker.three")
        assert second is not None and second.token != first.token
        assert not await claims.renew(first)
        assert not await claims.release(first)
        assert await claims.release(second)
        worker = RunWorker(runtime, "worker.stopped")
        worker.stop_claiming()
        assert not await worker.drain_once()
        clock.current += timedelta(seconds=2)
        cancelling = RunWorker(runtime, "worker.cancelled")
        started = asyncio.Event()

        async def interrupted_advance(run_id: str) -> None:
            del run_id
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(cancelling, "_advance", interrupted_advance)
        task = asyncio.create_task(cancelling.drain_once())
        await asyncio.wait_for(started.wait(), timeout=2)
        cancelling.stop_claiming()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert not await cancelling.drain_once()
        clock.current += timedelta(seconds=2)
        assert await RunWorker(runtime, "worker.after-cancellation").drain_once()
        async with runtime.database.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(RunWorkerClaimRecord)) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_state", (RunState.RECEIVED, RunState.EXECUTING))
@pytest.mark.parametrize("ownership", ("replaced", "expired", "current"))
async def test_del_05_delayed_failure_requires_current_unexpired_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_state: RunState,
    ownership: str,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    suspended = asyncio.Event()
    resume = asyncio.Event()
    old_worker = RunWorker(runtime, "worker.delayed-owner")

    async def delayed_failure(*args, **kwargs):
        del args, kwargs
        suspended.set()
        await resume.wait()
        raise ValueError("interrupted operation returned an error")

    if initial_state is RunState.RECEIVED:
        monkeypatch.setattr(old_worker, "_advance", delayed_failure)
    else:
        # Actual planning/activation commits first, then failure exercises the
        # terminal step cleanup branch rather than only primary Run lifecycle.
        monkeypatch.setattr(ControlledReadExecutor, "execute", delayed_failure)
    advancing = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await _submit(client, "demo.social-media.content-draft.v1")
        advancing = asyncio.create_task(old_worker.drain_once())
        await asyncio.wait_for(suspended.wait(), timeout=5)
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            before = await unit_of_work.runs.get(run_id)
            assert before is not None and before.state is initial_state
            steps_before = await unit_of_work.run_steps.list_for_run(run_id)
            transitions_before = await unit_of_work.runs.list_transitions(run_id)
        new_claim = None
        if ownership != "current":
            # Model a process/VM pause past the real 90s lease without changing
            # production timing or waiting for the 10s renewal task to wake.
            clock.current += timedelta(seconds=91)
        if ownership == "replaced":
            new_claim = await RunClaims(runtime).claim_once("worker.replacement")
            assert new_claim is not None and new_claim.run_id == run_id
        resume.set()
        assert await asyncio.wait_for(advancing, timeout=5)
        if new_claim is not None:
            assert await RunClaims(runtime).renew(new_claim)
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            after = await unit_of_work.runs.get(run_id)
            assert after is not None
            if ownership == "current":
                assert after.state is RunState.FAILED
                assert after.terminal_reason_code == "unclassified_failure"
            else:
                assert after == before
                assert await unit_of_work.run_steps.list_for_run(run_id) == steps_before
                assert await unit_of_work.runs.list_transitions(run_id) == transitions_before
                assert not await unit_of_work.artifacts.list_for_run(run_id)
    finally:
        if advancing is not None and not advancing.done():
            advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_del_05_startup_never_creates_missing_database_or_key(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'missing.db'}",
        catalog_root=CATALOG,
        marketing_agents_digest_key_path=tmp_path / "missing.key",
    )
    with pytest.raises(RuntimeNotReady):
        await build_runtime(settings)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_del_05_real_worker_process_health_and_sigterm_cleanup(tmp_path: Path) -> None:
    settings = await _installation(tmp_path)
    health_file = tmp_path / "process.health.json"
    environment = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "CATALOG_ROOT": str(settings.catalog_root),
        "MARKETING_AGENTS_DIGEST_KEY_PATH": str(settings.marketing_agents_digest_key_path),
        "LLM_PROVIDER": "mock",
        "CONNECTOR_MODE": "mock",
        "ALLOW_EXTERNAL_NETWORK": "false",
        "APP_ENV": "test",
        "AUTH_MODE": "local",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "marketing_agents.workers.run_worker",
        "--health-file",
        str(health_file),
        "--shutdown-seconds",
        "1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=tmp_path,
    )
    try:
        assert process.stdout is not None
        ready = await asyncio.wait_for(process.stdout.readline(), timeout=20)
        assert b'"status": "ready"' in ready
        assert healthy(health_file)
        process.send_signal(signal.SIGTERM)
        _, error = await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode == 0, error.decode()
        assert not healthy(health_file)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def test_del_05_worker_health_requires_fresh_live_process(tmp_path: Path) -> None:
    path = tmp_path / "worker.health.json"
    _health(path, "worker.health", "ready")
    assert healthy(path)
    assert path.stat().st_mode & 0o777 == 0o600
    _health(path, "worker.health", "stopped")
    assert not healthy(path)
    assert os.getpid() > 0


def test_del_05_migration_cannot_destructively_downgrade() -> None:
    migration = import_module(
        "marketing_agents.infrastructure.db.alembic.versions.0006_run_worker_claims"
    )
    with pytest.raises(RuntimeError, match="Destructive downgrades are unsupported"):
        migration.downgrade()


@pytest.mark.asyncio
async def test_del_05_scheduler_intake_is_durable_and_never_executes_inline(tmp_path: Path) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        schedule = Schedule(
            id="schedule.local.test",
            trigger_id="trigger.local.schedule",
            instance_id="inst.community.education.course-progress-reminders.01",
            workflow_id="workflow.local.schedule",
            cron="* * * * *",
            timezone="UTC",
            next_run_at_utc=clock.current,
            misfire_policy=MisfirePolicy.RUN_ONCE,
            misfire_grace_seconds=60,
            enabled=True,
            recurrence_version=SCHEDULE_RECURRENCE_VERSION,
        )
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            assert (await unit_of_work.schedules.add_or_get(schedule)).inserted
            await unit_of_work.commit()
        scheduler = SchedulerWorker(runtime, "scheduler.integration.one")
        assert await scheduler.drain_once()
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert not await SchedulerWorker(runtime, "scheduler.integration.restarted").drain_once()
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            occurrence = await unit_of_work.schedules.get_occurrence_by_schedule_due(
                schedule.id, schedule.next_run_at_utc
            )
            assert occurrence is not None and occurrence.run_id is not None
            run = await unit_of_work.runs.get(occurrence.run_id)
            assert run is not None and run.state is RunState.RECEIVED
            assert await unit_of_work.artifacts.list_for_run(run.id) == ()
        # Unsupported workflows terminate visibly instead of silently remaining queued.
        assert await RunWorker(runtime, "worker.unsupported").drain_once()
        async with runtime.dependencies.unit_of_work() as unit_of_work:
            failed = await unit_of_work.runs.get(run.id)
            assert failed is not None and failed.state is RunState.FAILED
    finally:
        await runtime.close()
