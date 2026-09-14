"""OBJ-03 real API admission, process composition and schema-bound role output."""

import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.orchestration.executable_workflows import (
    CatalogRoleExecutionKind,
    ExecutableWorkflowHandler,
)
from marketing_agents.application.services.schedule_configuration import configured_schedule_id
from marketing_agents.domain.enums import RunState, TriggerKind
from marketing_agents.infrastructure.webhook_signatures import WEBHOOK_SIGNATURE_DOMAIN
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from marketing_agents.workers.runtime.scheduler import SchedulerWorker
from pydantic import SecretStr

from tests.integration.runtime.test_del_05_process_composition import Clock, _installation
from tests.support.api import browser_request


@pytest.mark.asyncio
async def test_obj_03_all_read_instances_execute_their_own_catalog_contract_after_restart(
    tmp_path: Path,
) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    submissions = []
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            for instance in runtime.catalog.instances:
                definition = runtime.workflows.for_catalog_role(
                    instance.template_id, TriggerKind.MANUAL
                )
                if definition.handler_kind is not ExecutableWorkflowHandler.CATALOG_ROLE:
                    continue
                payload = {
                    "request_id": f"request.obj03.{len(submissions)}",
                    "source_content": (
                        "An explicitly supplied launch outline: "
                        "offline reports and human approvals."
                    ),
                    "audience": "local operators",
                }
                response = await browser_request(
                    client,
                    "POST",
                    f"/api/v1/agent-instances/{instance.id}/dry-runs",
                    json={"input": payload},
                    headers={"Idempotency-Key": f"obj03-role-{len(submissions)}"},
                )
                assert response.status_code == 202, (instance.id, response.text)
                run_id = response.json()["runId"]
                async with runtime.dependencies.unit_of_work() as uow:
                    run = await uow.runs.get(run_id)
                    assert run is not None and run.state is RunState.RECEIVED
                    assert not await uow.artifacts.list_for_run(run_id)
                submissions.append((run_id, instance, definition, payload))
        assert len(submissions) == 37
        await runtime.close()
        runtime = await build_runtime(settings, clock=Clock())
        worker = RunWorker(runtime, "worker.obj03.restarted")
        for _ in submissions:
            assert await worker.drain_once()
        assert not await worker.drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            for run_id, instance, definition, payload in submissions:
                run = await uow.runs.get(run_id)
                assert run is not None and run.state is RunState.COMPLETED, instance.id
                work = await uow.works.get(run.work_item_id)
                assert work is not None and dict(work.admitted_payload) == payload
                assert work.workflow_id == definition.id
                steps = await uow.run_steps.validate_plan_for_execution(run_id)
                assert len(steps) == 1
                artifacts = await uow.artifacts.list_for_run(run_id)
                assert len(artifacts) == 1 and artifacts[0].verify_payload()
                provenance = artifacts[0].provenance
                assert provenance.template_id == instance.template_id
                assert provenance.instance_id == instance.id
                assert provenance.workflow_id == definition.id
                assert provenance.output_schema_id == definition.output_schema_id
                assert provenance.output_schema_hash == definition.output_schema_hash
                assert artifacts[0].payload["proposed_actions"] == []
                control = await uow.execution_control.get(run_id)
                assert control is not None and control.tool_calls == 0
                assert control.model_calls == definition.expected_model_calls
                attempts = await uow.execution_control.list_attempts(
                    steps[0].id, steps[0].runtime_policy.operation_key
                )
                assert len(attempts) == definition.expected_model_calls
                if definition.execution_kind is CatalogRoleExecutionKind.LOCAL_TRANSFORM:
                    assert provenance.providers[0].mode == "local"
                    assert not attempts
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_configured_signed_webhook_executes_exact_role_after_restart(
    tmp_path: Path,
) -> None:
    settings = await _installation(tmp_path)
    secret = "obj03-local-webhook-nonproduction-secret-value"
    settings = settings.model_copy(update={"webhook_hmac_secret": SecretStr(secret)})
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    instance_id = "inst.social-media.new-content.linkedin-comment-replier.01"
    source = "obj03.events"
    payload = {
        "request_id": "request.obj03.webhook",
        "source_content": "A comment asks how offline drafts are reviewed.",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            path = f"/api/v1/agent-instances/{instance_id}/configuration"
            current = await client.get(path)
            assert current.status_code == 200
            triggers = current.json()["configuration"]["triggerBindings"]
            triggers.append({"type": "webhook", "enabled": True, "eventSource": source})
            saved = await browser_request(
                client,
                "PATCH",
                path,
                headers={"If-Match": current.headers["etag"]},
                json={"triggerBindings": triggers},
            )
            assert saved.status_code == 200, saved.text
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        body = json.dumps({"eventId": "event.obj03.signed", "input": payload}).encode()
        timestamp = str(int(clock.now().timestamp()))
        signature = (
            "v1="
            + hmac.digest(
                secret.encode(),
                WEBHOOK_SIGNATURE_DOMAIN + timestamp.encode() + b"\x00" + body,
                "sha256",
            ).hex()
        )
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": signature,
        }
        path = f"/api/v1/webhooks/{source}/trigger.webhook.{source}.v1"
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            denied = await client.post(
                path, content=body, headers={**headers, "X-Webhook-Signature": "v1=" + "0" * 64}
            )
            assert denied.status_code == 401
            response = await client.post(path, content=body, headers=headers)
            assert response.status_code == 202, response.text
            repeated = await client.post(path, content=body, headers=headers)
            assert repeated.status_code == 202 and repeated.json()["disposition"] == "replayed"
            assert repeated.json()["deliveries"] == response.json()["deliveries"]
            assert len(response.json()["deliveries"]) == 1
            run_id = response.json()["deliveries"][0]["runId"]
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await RunWorker(runtime, "worker.obj03.webhook").drain_once()
        assert not await RunWorker(runtime, "worker.obj03.webhook").drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            work = await uow.works.get(run.work_item_id)
            assert dict(work.admitted_payload) == payload
            definition = runtime.workflows.for_catalog_role(
                "tpl.social-media.new-content.linkedin-comment-replier", TriggerKind.WEBHOOK
            )
            assert work.workflow_id == definition.id
            artifacts = await uow.artifacts.list_for_run(run_id)
            assert len(artifacts) == 1
            assert artifacts[0].provenance.workflow_id == definition.id
            assert artifacts[0].payload["provenance"]["source_request_id"] == payload["request_id"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_saved_schedule_uses_explicit_input_and_completes_after_restart(
    tmp_path: Path,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    instance_id = "inst.community.education.course-progress-reminders.01"
    payload = {
        "request_id": "request.obj03.recurring-reminder",
        "source_content": json.dumps(
            {
                "participant_name": "Morgan",
                "course_title": "Safety",
                "next_step": "Review lesson 2",
            }
        ),
    }
    try:
        path = f"/api/v1/agent-instances/{instance_id}/configuration"
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            current = await client.get(path)
            assert current.status_code == 200, current.text
            triggers = [
                item
                for item in current.json()["configuration"]["triggerBindings"]
                if item["type"] != "schedule"
            ]
            schedule = {
                "cron": "* * * * *",
                "timezone": "UTC",
                "misfirePolicy": "run_once",
                "misfireGraceSeconds": 60,
            }
            triggers.append({"type": "schedule", "enabled": True, **schedule})
            saved = await browser_request(
                client,
                "PATCH",
                path,
                headers={"If-Match": current.headers["etag"]},
                json={
                    "triggerBindings": triggers,
                    "schedule": schedule,
                    "scheduledInput": {"input": payload, "executionMode": "dry_run"},
                },
            )
            assert saved.status_code == 200, saved.text
        await runtime.close()
        clock.current += timedelta(minutes=1)
        runtime = await build_runtime(settings, clock=clock)
        scheduler = SchedulerWorker(runtime, "scheduler.obj03.restarted")
        assert await scheduler.drain_once()
        assert not await scheduler.drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            schedule_row = await uow.schedules.get(configured_schedule_id(instance_id))
            assert schedule_row is not None and schedule_row.configuration_revision == 2
            occurrence = await uow.schedules.get_occurrence_by_schedule_due(
                schedule_row.id, clock.current
            )
            assert occurrence is not None and occurrence.run_id is not None
            run_id = occurrence.run_id
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.RECEIVED
            work = await uow.works.get(run.work_item_id)
            assert work is not None and dict(work.admitted_payload) == payload
            definition = runtime.workflows.for_catalog_role(
                "tpl.community.education.course-progress-reminders", TriggerKind.SCHEDULE
            )
            assert work.workflow_id == definition.id
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await RunWorker(runtime, "worker.obj03.scheduled").drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            artifacts = await uow.artifacts.list_for_run(run_id)
            assert len(artifacts) == 1
            assert artifacts[0].provenance.workflow_id == definition.id
            assert artifacts[0].payload["provenance"]["source_request_id"] == payload["request_id"]
    finally:
        await runtime.close()
