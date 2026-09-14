"""OBJ-03 explicit catalog commands retain approval, restart and receipt boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.ports.external_writes import ConnectorDeliveryFailure
from marketing_agents.application.services.approval_boundaries import ApprovalBoundaryService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.db.models import ConnectorActionReceiptRecord
from marketing_agents.infrastructure.db.models.step import RunPlanRecord
from marketing_agents.infrastructure.db.models.work import WorkItemRecord
from marketing_agents.infrastructure.runtime.catalog_write_workflows import (
    CATALOG_WRITE_CAPABILITIES,
    CatalogWriteWorkflowError,
    CatalogWriteWorkflowService,
    parse_catalog_write_command,
)
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from sqlalchemy import func, select, update

from tests.integration.runtime.test_del_05_process_composition import Clock, _installation
from tests.support.api import browser_request

_NEWSLETTER = "tpl.email.newsletter.newsletter-subscriber"
_COMMANDS = {
    _NEWSLETTER: {"contact_ref": "contact.local", "list_ref": "list.local"},
    "tpl.email.newsletter.unsubscribe-assistant": {
        "contact_ref": "contact.local",
        "list_ref": "list.local",
    },
    "tpl.community.events.attendee-scheduler": {
        "attendee_ref": "attendee.local",
        "session_ref": "session.local",
    },
    "tpl.community.education.course-cohort-onboarder": {
        "recipient_refs": ["participant.local"],
        "body": "Welcome to the local course.",
    },
}


def _source(template_id: str) -> str:
    return json.dumps({"version": 1, "command": _COMMANDS[template_id]})


def _service(runtime):
    return CatalogWriteWorkflowService(runtime.dependencies, runtime.catalog, runtime.workflows)


async def _submit(client, instance_id, source, *, mode="mock_execute"):
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/agent-instances/{instance_id}/dry-runs",
        json={
            "input": {"request_id": "request.obj03.write", "source_content": source},
            "executionMode": mode,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["runId"]


async def _resume(service, run_id):
    await service.resume_persisted(
        run_id,
        worker_id="worker.obj03.write",
        correlation_id="correlation.obj03.write",
    )


async def _requests(runtime, run_id):
    async with runtime.dependencies.unit_of_work() as uow:
        selection = await uow.approvals.get_current_authorization_set(run_id)
        assert selection is not None
        requests = await uow.approvals.list_current_set(
            run_id,
            selection.authorization_set.plan_hash,
            selection.authorization_set.proposal_revision,
        )
        assert len(requests) == 1
        return requests[0].request


async def _decide(client, request, decision="approve"):
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/approvals/{request.id}/{decision}",
        json={
            "expected_generation": request.generation,
            "expected_payload_hash": request.action_hash,
            "reason": "Local operator reviewed the exact command.",
        },
    )
    assert response.status_code == 200, response.text


async def _receipt_count(runtime):
    async with runtime.database.session_factory() as session:
        return await session.scalar(select(func.count()).select_from(ConnectorActionReceiptRecord))


@pytest.mark.asyncio
async def test_obj_03_all_six_write_instances_wait_for_approval_and_resume_after_restart(
    tmp_path: Path,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    submissions = []
    try:
        service = _service(runtime)
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            instances = [
                item
                for item in runtime.catalog.instances
                if item.template_id in CATALOG_WRITE_CAPABILITIES
            ]
            assert len(instances) == 6
            for instance in instances:
                # Only the demo's Newsletter instance is bound by the seed.
                # Exercise the real configuration API before admitting other roles.
                capability_id = CATALOG_WRITE_CAPABILITIES[instance.template_id]
                capability = next(
                    item for item in runtime.catalog.tool_capabilities if item.id == capability_id
                )
                family = capability.connector_family
                path = f"/api/v1/agent-instances/{instance.id}/configuration"
                configuration = await client.get(path)
                assert configuration.status_code == 200
                if family not in configuration.json()["configuration"]["connectorBindings"]:
                    configured = await browser_request(
                        client,
                        "PATCH",
                        path,
                        headers={"If-Match": configuration.headers["etag"]},
                        json={
                            "connectorBindings": {
                                family: {
                                    "connectorFamily": family,
                                    "bindingId": f"mock.{family}.default",
                                    "enabled": True,
                                }
                            }
                        },
                    )
                    assert configured.status_code == 200, configured.text
                run_id = await _submit(client, instance.id, _source(instance.template_id))
                assert await RunWorker(runtime, "worker.obj03.write-prepare").drain_once()
                await _resume(service, run_id)
                async with runtime.dependencies.unit_of_work() as uow:
                    run = await uow.runs.get(run_id)
                    assert run is not None and run.state is RunState.AWAITING_APPROVAL
                    plan = await uow.run_steps.get_plan(run_id)
                    assert plan is not None and plan.step_count == 1
                    actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
                    assert len(actions) == 1
                    action = actions[0]
                    assert action.state is ExternalActionState.AWAITING_APPROVAL
                    assert (
                        action.envelope.capability_id
                        == CATALOG_WRITE_CAPABILITIES[instance.template_id]
                    )
                    assert json.loads(json.dumps(_COMMANDS[instance.template_id])) == json.loads(
                        json.dumps(dict(action.envelope.minimized_payload), default=list)
                    )
                    control = await uow.execution_control.get(run_id)
                    assert control is not None and control.tool_calls == control.model_calls == 0
                    assert not await uow.artifacts.list_for_run(run_id)
                request = await _requests(runtime, run_id)
                submissions.append((run_id, request))
            assert await _receipt_count(runtime) == 0
            for _, request in submissions:
                await _decide(client, request)
            assert await _receipt_count(runtime) == 0  # Approval HTTP is never dispatch.
        await runtime.close()
        clock.current += timedelta(seconds=2)
        runtime = await build_runtime(settings, clock=clock)
        service = _service(runtime)
        for _ in submissions:
            assert await RunWorker(runtime, "worker.obj03.write-restarted").drain_once()
        for run_id, _ in submissions:
            await _resume(service, run_id)  # Completed replay does not call again.
            async with runtime.dependencies.unit_of_work() as uow:
                run = await uow.runs.get(run_id)
                assert run is not None and run.state is RunState.COMPLETED
                plan = await uow.run_steps.get_plan(run_id)
                assert plan is not None
                actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
                assert len(actions) == 1 and actions[0].state is ExternalActionState.SUCCEEDED
                action = actions[0]
                receipt = await uow.connector_receipts.get(
                    action.connector_binding_id,
                    action.idempotency_key,
                )
                assert receipt is not None and receipt.external_action_id == action.id
                assert receipt.safe_metadata["external_side_effect"] is False
                control = await uow.execution_control.get(run_id)
                assert control is not None and control.model_calls == 0 and control.tool_calls == 1
                steps = await uow.run_steps.validate_plan_for_execution(run_id)
                assert len(steps) == 1 and steps[0].state is StepState.SUCCEEDED
                assert not await uow.artifacts.list_for_run(run_id)
        assert await _receipt_count(runtime) == 6
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_rejected_write_stays_inert_after_restart(tmp_path: Path) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            run_id = await _submit(
                client, "inst.email.newsletter.newsletter-subscriber.01", _source(_NEWSLETTER)
            )
            await _resume(_service(runtime), run_id)
            await _decide(client, await _requests(runtime, run_id), "reject")
        await runtime.close()
        runtime = await build_runtime(settings, clock=Clock())
        await _resume(_service(runtime), run_id)
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.REJECTED
            assert not await uow.artifacts.list_for_run(run_id)
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_cancelled_write_never_calls_a_connector(tmp_path: Path) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            run_id = await _submit(
                client,
                "inst.email.newsletter.newsletter-subscriber.01",
                _source(_NEWSLETTER),
            )
        await _resume(_service(runtime), run_id)
        cancelled = await ApprovalBoundaryService(runtime.dependencies).cancel(
            run_id,
            audit_context=AuditContext.worker(
                "worker.obj03.cancel",
                correlation_id="correlation.obj03.cancel",
            ),
        )
        assert cancelled.run.state is RunState.CANCELLED
        await runtime.close()
        runtime = await build_runtime(settings, clock=Clock())
        await _resume(_service(runtime), run_id)
        assert await _receipt_count(runtime) == 0
        async with runtime.dependencies.unit_of_work() as uow:
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.tool_calls == control.model_calls == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_after_receipt", (False, True))
async def test_obj_03_write_recovers_committed_receipt_after_process_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disable_after_receipt: bool,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    original = RegistryConnectorWriteGateway.execute
    calls = 0

    async def interrupted_after_receipt(gateway, authorization):
        nonlocal calls
        calls += 1
        await original(gateway, authorization)
        # BaseException models worker process interruption, not provider failure.
        raise asyncio.CancelledError

    async def forbid_second_call(_gateway, _authorization):
        pytest.fail("receipt recovery must not repeat the provider call")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            run_id = await _submit(
                client,
                "inst.email.newsletter.newsletter-subscriber.01",
                _source(_NEWSLETTER),
            )
            await _resume(_service(runtime), run_id)
            await _decide(client, await _requests(runtime, run_id))
        monkeypatch.setattr(RegistryConnectorWriteGateway, "execute", interrupted_after_receipt)
        with pytest.raises(asyncio.CancelledError):
            await _resume(_service(runtime), run_id)
        assert calls == 1 and await _receipt_count(runtime) == 1
        async with runtime.dependencies.unit_of_work() as uow:
            plan = await uow.run_steps.get_plan(run_id)
            assert plan is not None
            actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
            assert actions[0].state is ExternalActionState.DISPATCHING
        await runtime.close()
        clock.current += timedelta(seconds=61)
        runtime = await build_runtime(settings, clock=clock)
        if disable_after_receipt:
            async with AsyncClient(
                transport=ASGITransport(app=runtime.create_app()),
                base_url="http://testserver",
            ) as client:
                path = (
                    "/api/v1/agent-instances/"
                    "inst.email.newsletter.newsletter-subscriber.01/configuration"
                )
                configuration = await client.get(path)
                changed = await browser_request(
                    client,
                    "PATCH",
                    path,
                    headers={"If-Match": configuration.headers["etag"]},
                    json={"enabled": False},
                )
                assert changed.status_code == 200, changed.text
                assert changed.json()["configuration"]["configurationRevision"] == 2
        monkeypatch.setattr(RegistryConnectorWriteGateway, "execute", forbid_second_call)
        await _resume(_service(runtime), run_id)
        await _resume(_service(runtime), run_id)
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.tool_calls == 1 and control.model_calls == 0
        assert await _receipt_count(runtime) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_failed_write_cannot_exceed_catalog_one_attempt_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    calls = 0

    async def fail_delivery(_gateway, _authorization):
        nonlocal calls
        calls += 1
        raise ConnectorDeliveryFailure(
            "connector_unavailable",
            "Injected bounded mock failure",
            request_may_have_left_process=False,
        )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            run_id = await _submit(
                client,
                "inst.email.newsletter.newsletter-subscriber.01",
                _source(_NEWSLETTER),
            )
            assert await RunWorker(runtime, "worker.obj03.failure-prepare").drain_once()
            await _decide(client, await _requests(runtime, run_id))
        monkeypatch.setattr(RegistryConnectorWriteGateway, "execute", fail_delivery)
        clock.current += timedelta(seconds=2)
        assert await RunWorker(runtime, "worker.obj03.failure-dispatch").drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.FAILED
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.tool_calls == 1 and control.model_calls == 0
            steps = await uow.run_steps.validate_plan_for_execution(run_id)
            assert steps[0].runtime_policy.retry.max_attempts == 1
        await _resume(_service(runtime), run_id)
        assert not await RunWorker(runtime, "worker.obj03.failure-no-retry").drain_once()
        assert calls == 1 and await _receipt_count(runtime) == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_changed_durable_workflow_definition_never_dispatches(tmp_path: Path) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            run_id = await _submit(
                client,
                "inst.email.newsletter.newsletter-subscriber.01",
                _source(_NEWSLETTER),
            )
            await _resume(_service(runtime), run_id)
            await _decide(client, await _requests(runtime, run_id))
        async with runtime.database.session_factory() as session:
            await session.execute(
                update(RunPlanRecord)
                .where(
                    RunPlanRecord.run_id == run_id,
                )
                .values(workflow_definition_hash="f" * 64)
            )
            await session.commit()
        with pytest.raises(RuntimeError):
            await _resume(_service(runtime), run_id)
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("input", "configuration"))
async def test_obj_03_stale_write_input_or_configuration_never_dispatches(
    tmp_path: Path,
    tamper: str,
) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    instance_id = "inst.email.newsletter.newsletter-subscriber.01"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            run_id = await _submit(client, instance_id, _source(_NEWSLETTER))
            await _resume(_service(runtime), run_id)
            await _decide(client, await _requests(runtime, run_id))
            if tamper == "input":
                async with runtime.dependencies.unit_of_work() as uow:
                    run = await uow.runs.get(run_id)
                    assert run is not None
                async with runtime.database.session_factory() as session:
                    await session.execute(
                        update(WorkItemRecord)
                        .where(
                            WorkItemRecord.id == run.work_item_id,
                        )
                        .values(
                            admitted_payload={
                                "request_id": "request.obj03.write",
                                "source_content": json.dumps(
                                    {
                                        "version": 1,
                                        "command": {
                                            "contact_ref": "changed.contact",
                                            "list_ref": "list.local",
                                        },
                                    }
                                ),
                            }
                        )
                    )
                    await session.commit()
            else:
                path = f"/api/v1/agent-instances/{instance_id}/configuration"
                current = await client.get(path)
                changed = await browser_request(
                    client,
                    "PATCH",
                    path,
                    headers={"If-Match": current.headers["etag"]},
                    json={"enabled": False},
                )
                assert changed.status_code == 200, changed.text
        with pytest.raises(
            CatalogWriteWorkflowError,
            match=(
                "catalog_write_action_invalid"
                if tamper == "input"
                else "catalog_write_configuration_stale"
            ),
        ):
            await _resume(_service(runtime), run_id)
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,source",
    (
        ("dry_run", _source(_NEWSLETTER)),
        ("mock_execute", "Please subscribe everyone in the source text."),
    ),
)
async def test_obj_03_write_service_rejects_dry_run_and_prose_before_planning(
    tmp_path: Path,
    mode: str,
    source: str,
) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            run_id = await _submit(
                client,
                "inst.email.newsletter.newsletter-subscriber.01",
                source,
                mode=mode,
            )
        with pytest.raises(CatalogWriteWorkflowError):
            await _resume(_service(runtime), run_id)
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.RECEIVED
            assert await uow.run_steps.get_plan(run_id) is None
            assert await uow.approvals.get_current_authorization_set(run_id) is None
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "source",
    (
        "not json",
        "[]",
        '{"version":1,"version":1,"command":{"contact_ref":"c","list_ref":"l"}}',
        '{"version":1,"command":{"contact_ref":"c","contact_ref":"other","list_ref":"l"}}',
        '{"version":true,"command":{"contact_ref":"c","list_ref":"l"}}',
        '{"version":1.0,"command":{"contact_ref":"c","list_ref":"l"}}',
        '{"version":2,"command":{"contact_ref":"c","list_ref":"l"}}',
        '{"version":1,"command":{"contact_ref":"c","list_ref":"l","capability_id":"other"}}',
        '{"version":1,"command":{"contact_ref":NaN,"list_ref":"l"}}',
        '{"version":1,"command":{"contact_ref":1,"list_ref":"l"}}',
        '{"version":1,"command":{"contact_ref":"c"}}',
        '{"version":1,"command":{"contact_ref":" c ","list_ref":"l"}}',
        '{"version":1,"command":{"contact_ref":"c","list_ref":"l"},"approved":true}',
        '{"version":1,"command":{"attendee_ref":"c","session_ref":"l"}}',
        " " * 16_385,
        "[" * 2_000,
    ),
)
def test_obj_03_write_command_parser_rejects_ambiguous_or_authority_bearing_input(
    source: str,
) -> None:
    with pytest.raises(CatalogWriteWorkflowError, match="catalog_write_command_invalid"):
        parse_catalog_write_command(_NEWSLETTER, source)


@pytest.mark.parametrize("recipients", (["same", "same"], [""], ["x" * 201], ["has\nnewline"]))
def test_obj_03_message_command_requires_bounded_unique_explicit_recipients(recipients) -> None:
    with pytest.raises(CatalogWriteWorkflowError, match="catalog_write_command_invalid"):
        parse_catalog_write_command(
            "tpl.community.education.course-cohort-onboarder",
            json.dumps(
                {"version": 1, "command": {"recipient_refs": recipients, "body": "Welcome"}}
            ),
        )
