"""Real API, worker, transactional audit and scheduler telemetry with privacy canaries."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from io import StringIO

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.application.services.run_lifecycle import RunLifecycleService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.run_lifecycle import NoRunTransitionContext, RunLifecycleCommand
from marketing_agents.infrastructure.db.repositories.audit import SQLAlchemyAuditRepository
from marketing_agents.observability import LocalTelemetry
from marketing_agents.observability.logging import OperationalLogger, safe_server_logging_config
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from marketing_agents.workers.runtime.scheduler import SchedulerWorker

from tests.acceptance.test_ac_07_real_composition_demos import (
    EMAIL,
    READ_DEMOS,
    Clock,
    current_requests,
    installation,
    submit,
)
from tests.integration.api.test_exec_05_schedule_control import create as create_schedule
from tests.support.api import browser_request
from tests.unit.observability.test_exec_06_telemetry import BrokenSink, total


def client_for(app):
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


def test_production_server_config_replaces_exception_formatter_without_touching_root(capsys):
    import uvicorn

    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in names
    }
    root_handlers = list(logging.getLogger().handlers)
    try:
        uvicorn.Config(create_app(), log_config=safe_server_logging_config(), access_log=False)
        error = RuntimeError("private-server-exception-canary")
        logging.getLogger("uvicorn.error").error(
            "private-server-message %s", "private-argument", exc_info=(RuntimeError, error, None)
        )
        output = capsys.readouterr().err
        assert json.loads(output) == {"event": "api_server_diagnostic", "level": "error"}
        assert logging.getLogger().handlers == root_handlers
    finally:
        for name, (handlers, level, propagate, disabled) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


@pytest.mark.asyncio
async def test_real_email_journey_emits_committed_safe_metrics_and_redaction_canaries(tmp_path):
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    stream = StringIO()
    runtime.telemetry.logger = OperationalLogger(stream)
    try:
        async with client_for(runtime.create_app()) as client:
            run_id = await submit(client, EMAIL)
            assert total(runtime.telemetry, "run_transitions") == 1
            duplicate = await browser_request(
                client,
                "POST",
                f"/api/v1/demo-scenarios/{EMAIL}/runs",
                json={},
                headers={"Idempotency-Key": f"ac07-{EMAIL}"},
            )
            assert duplicate.status_code == 202
            assert total(runtime.telemetry, "idempotency") == 1
            assert await RunWorker(runtime, "worker.exec06.telemetry").drain_once()
            requests = await current_requests(runtime, run_id)
            clock.current += timedelta(seconds=30)
            for stored in requests:
                response = await browser_request(
                    client,
                    "POST",
                    f"/api/v1/approvals/{stored.request.id}/approve",
                    json={
                        "expected_generation": stored.request.generation,
                        "expected_payload_hash": stored.request.action_hash,
                        "reason": "private-approval-reason-canary",
                    },
                )
                assert response.status_code == 200, response.text
            assert total(runtime.telemetry, "approval_decisions") == 2
            assert total(runtime.telemetry, "approval_queue_seconds") == 60
            assert await RunWorker(runtime, "worker.exec06.telemetry").drain_once()
            assert total(runtime.telemetry, "connector_results") >= 2
            assert total(runtime.telemetry, "redactions") > 0
            assert total(runtime.telemetry, "requests") >= 6
            observed = runtime.telemetry.metrics.snapshot()
            assert any(
                s.name == "run_transitions"
                and dict(s.labels)["state"] == "completed"
                and dict(s.labels)["workflow"] == EMAIL
                and dict(s.labels)["template"] != "unknown"
                for s in observed
            )
            body = stream.getvalue()
            assert "private-approval-reason-canary" not in body
            for stored in requests:
                assert stored.request.action_hash not in body
            assert any(json.loads(line).get("run_id") == run_id for line in body.splitlines())
            assert all(
                set(dict(s.labels))
                <= {
                    "status",
                    "state",
                    "workflow",
                    "template",
                    "decision",
                    "result",
                    "boundary",
                    "kind",
                }
                for s in observed
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_request_paths_headers_bodies_and_raw_errors_never_enter_logs():
    stream = StringIO()
    telemetry = LocalTelemetry(stream=stream)
    app = create_app(telemetry=telemetry)
    canaries = (
        "email-canary@example.invalid",
        "Bearer secret-auth-canary",
        "private-body-canary",
        "unsafe-error-canary",
    )

    @app.get("/raise-telemetry-canary")
    async def injected_failure():
        raise RuntimeError(canaries[3])

    async with client_for(app) as client:
        missing = await client.get(
            "/missing/" + canaries[0], headers={"authorization": canaries[1]}
        )
        assert missing.status_code == 404
        invalid = await client.post("/api/v1/campaign-briefs", json={"objective": canaries[2]})
        assert invalid.status_code >= 400
        broken = await client.get("/raise-telemetry-canary")
        assert broken.status_code == 500
    assert total(telemetry, "requests") == 3
    for canary in canaries:
        assert canary not in stream.getvalue() + repr(telemetry.metrics.snapshot())
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len({r["correlation_id"] for r in records}) == 3
    assert all(0 <= r["duration_seconds"] < 30 for r in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("savepoint", (False, True))
async def test_rolled_back_audit_transition_is_not_observed_even_after_outer_commit(
    tmp_path, savepoint
):
    runtime = await build_runtime(await installation(tmp_path), clock=Clock())
    runtime.telemetry.logger = OperationalLogger(StringIO())
    try:
        async with client_for(runtime.create_app()) as client:
            run_id = await submit(client, READ_DEMOS[0][0])
        before = total(runtime.telemetry, "run_transitions")
        async with runtime.dependencies.unit_of_work() as uow:
            nested = await uow._require_session().begin_nested() if savepoint else None
            attempt = await RunLifecycleService(runtime.dependencies).attempt_advance_in_uow(
                uow,
                run_id,
                1,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
                audit_context=AuditContext.worker(
                    "worker.telemetry", correlation_id="corr.telemetry"
                ),
            )
            assert attempt.result is not None
            assert total(runtime.telemetry, "run_transitions") == before
            if nested is not None:
                await nested.rollback()
                await uow.commit()
            # Otherwise implicit outer rollback on exit.
        assert total(runtime.telemetry, "run_transitions") == before
        async with runtime.dependencies.unit_of_work() as uow:
            assert (await uow.runs.get(run_id)).state.value == "received"
        # A legitimate commit is counted once; a broken log sink cannot undo it.
        runtime.telemetry.logger = OperationalLogger(BrokenSink())
        await RunLifecycleService(runtime.dependencies).advance(
            run_id,
            1,
            RunLifecycleCommand.MARK_VALIDATED,
            NoRunTransitionContext(),
            audit_context=AuditContext.worker("worker.telemetry", correlation_id="corr.telemetry"),
        )
        assert total(runtime.telemetry, "run_transitions") == before + 1
        assert total(runtime.telemetry, "diagnostic_failures") == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_audit_failure_counts_safe_code_without_hiding_rollback(tmp_path, monkeypatch):
    runtime = await build_runtime(await installation(tmp_path), clock=Clock())
    stream = StringIO()
    runtime.telemetry.logger = OperationalLogger(stream)
    try:
        async with client_for(runtime.create_app()) as client:
            run_id = await submit(client, READ_DEMOS[0][0])
        before = total(runtime.telemetry, "run_transitions")
        original = SQLAlchemyAuditRepository.append_many

        async def fail(self, events):
            await original(self, events)
            raise RuntimeError("private-database-error-canary")

        monkeypatch.setattr(SQLAlchemyAuditRepository, "append_many", fail)
        with pytest.raises(RuntimeError, match="private-database-error-canary"):
            await RunLifecycleService(runtime.dependencies).advance(
                run_id,
                1,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
                audit_context=AuditContext.worker(
                    "worker.telemetry", correlation_id="corr.telemetry"
                ),
            )
        assert total(runtime.telemetry, "audit_write_failures") == 1
        assert total(runtime.telemetry, "run_transitions") == before
        assert "private-database-error-canary" not in stream.getvalue()
        async with runtime.dependencies.unit_of_work() as uow:
            assert (await uow.runs.get(run_id)).state.value == "received"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_scheduler_claim_lag_and_misfire_are_real_committed_observations(tmp_path):
    clock = Clock()
    runtime = await build_runtime(await installation(tmp_path), clock=clock)
    runtime.telemetry.logger = OperationalLogger(StringIO())
    try:
        async with client_for(runtime.create_app()) as client:
            created = await create_schedule(client)
            assert created.status_code == 201
            from datetime import datetime

            clock.current = datetime.fromisoformat(created.json()["nextRunAtUtc"]) + timedelta(
                minutes=5
            )
        assert await SchedulerWorker(runtime, "scheduler.exec06.telemetry").drain_once()
        assert total(runtime.telemetry, "scheduler_claims") == 1
        assert total(runtime.telemetry, "scheduler_lag_seconds") == 300
        assert total(runtime.telemetry, "scheduler_misfires") == 1
        assert not await SchedulerWorker(runtime, "scheduler.exec06.telemetry").drain_once()
        assert total(runtime.telemetry, "scheduler_claims") == 1
    finally:
        await runtime.close()
