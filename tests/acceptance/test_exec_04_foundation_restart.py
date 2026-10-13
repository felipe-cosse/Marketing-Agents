"""EXEC-04: one real M3/M4 installation, restart, artifact and audit witness.

Runtime reconstruction is explicit; this does not claim an OS-kill test. Existing
AC-07/AC-14 oracles inspect real schema/provenance and persisted business facts.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker

from tests.acceptance.test_ac_07_real_composition_demos import (
    READ_DEMOS,
    Clock,
    assert_artifact_api,
    installation,
    observe_real_calls,
    persisted_artifact,
)
from tests.acceptance.test_ac_10_webhook_replay import business_snapshot
from tests.acceptance.test_ac_11_mock_crash_recovery import audit_rows, client_for
from tests.acceptance.test_ac_14_audit_timeline import (
    CANARIES,
    CONTENT_CANARY,
    EXECUTE_WORKER,
    assert_projection,
    finish_pages,
    page,
    submit_canary,
    verify_business_witnesses,
)
from tests.support.api import browser_request


@pytest.mark.asyncio
async def test_exec_04_reseed_restart_schema_artifact_and_complete_public_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    scenario, _, instance_id, _ = READ_DEMOS[0]
    configuration_path = f"/api/v1/agent-instances/{instance_id}/configuration"
    try:
        # The installation helper really migrates and seeds an owned SQLite DB.
        # Change only deployment metadata through the public, authorized API.
        async with client_for(runtime) as client:
            current = await client.get(configuration_path)
            assert current.status_code == 200, current.text
            updated = await browser_request(
                client,
                "PATCH",
                configuration_path,
                headers={"If-Match": current.headers["etag"]},
                json={"variantLabel": "EXEC-04 retained local deployment"},
            )
            assert updated.status_code == 200, updated.text
        async with runtime.dependencies.unit_of_work() as uow:
            configuration = await uow.configurations.get(instance_id)
            assert configuration is not None
            assert configuration.configuration_revision == 2
            assert configuration.variant_label == "EXEC-04 retained local deployment"
        reseed = await seed_catalog(
            runtime.catalog, runtime.database, CroniterRecurrenceCalculator()
        )
        assert (reseed.inserted, reseed.updated, reseed.deleted) == (0, 0, 0)
        assert (reseed.configuration_inserted, reseed.configuration_preserved) == (0, 43)
        async with runtime.dependencies.unit_of_work() as uow:
            assert await uow.configurations.get(instance_id) == configuration

        async with client_for(runtime) as client:
            run_id, admission = await submit_canary(client, scenario)
        queued_facts = await business_snapshot(runtime)
        queued_events = await audit_rows(runtime, run_id)
        assert len(queued_facts["work_items"]) == len(queued_facts["runs"]) == 1
        assert queued_facts["runs"][0]["state"] == "received"
        assert queued_facts["run_plans"] == queued_facts["execution_attempts"] == ()
        assert queued_facts["artifacts"] == queued_facts["connector_action_receipts"] == ()
        assert calls.models == calls.reads == calls.writes == []

        # No worker executes until the old runtime is closed and rebuilt from DB.
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == queued_facts
        assert await audit_rows(runtime, run_id) == queued_events
        async with runtime.dependencies.unit_of_work() as uow:
            assert await uow.configurations.get(instance_id) == configuration
        # AC-14's independent oracle explicitly expects this internal actor ID.
        # Use that actual worker; do not monkeypatch the oracle or its audit rows.
        assert await RunWorker(runtime, EXECUTE_WORKER).drain_once()
        artifact = await persisted_artifact(runtime, run_id, scenario, clock)
        await assert_artifact_api(runtime, artifact)
        assert artifact.provenance.instance_config_revision == configuration.configuration_revision
        assert len(calls.models) == 1 and calls.reads == calls.writes == []

        facts = await business_snapshot(runtime)
        events = await audit_rows(runtime, run_id)
        assert len(facts["work_items"]) == len(facts["runs"]) == len(facts["run_plans"]) == 1
        assert facts["runs"][0]["state"] == "completed"
        assert len(facts["execution_attempts"]) == len(facts["artifacts"]) == 1
        for table in (
            "external_actions",
            "external_action_dispatch_attempts",
            "connector_action_receipts",
            "approval_requests",
            "approval_decisions",
            "approval_uses",
        ):
            assert facts[table] == ()
        assert CONTENT_CANARY in json.dumps(facts["work_items"], default=str)
        witnesses = verify_business_witnesses(
            facts,
            events,
            admission_context=admission,
            renewal_contexts={},
            decision_contexts={},
            boundary_context=None,
        )
        timeline_path = f"/api/v1/runs/{run_id}/timeline"
        feed_path = "/api/v1/audit-events"
        timeline_params, feed_params = {"limit": 2}, {"run_id": run_id, "limit": 3}
        async with client_for(runtime) as client:
            first_timeline = await page(client, timeline_path, timeline_params)
            first_feed = await page(client, feed_path, feed_params)
        assert first_timeline["next_cursor"] and first_feed["next_cursor"]

        # Continue real public pagination through a second rebuilt runtime.
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == facts
        assert await audit_rows(runtime, run_id) == events
        assert await persisted_artifact(runtime, run_id, scenario, clock) == artifact
        await assert_artifact_api(runtime, artifact)
        async with client_for(runtime) as client:
            timeline = await finish_pages(client, timeline_path, timeline_params, first_timeline)
            feed = await finish_pages(client, feed_path, feed_params, first_feed)
        assert_projection(timeline, events, feed=False)
        assert_projection(feed, events, feed=True)
        assert witnesses <= {item["id"] for item in timeline} == {item["id"] for item in feed}
        rendered = json.dumps((events, timeline, feed), default=str) + caplog.text
        for canary in CANARIES:
            assert canary not in rendered
        async with runtime.dependencies.unit_of_work() as uow:
            assert await uow.configurations.get(instance_id) == configuration
        clock.current += timedelta(seconds=2)
        assert not await RunWorker(runtime, "worker.exec04.terminal-idle").drain_once()
        assert await business_snapshot(runtime) == facts
        assert await audit_rows(runtime, run_id) == events
        assert len(calls.models) == 1 and calls.reads == calls.writes == []
    finally:
        await runtime.close()
