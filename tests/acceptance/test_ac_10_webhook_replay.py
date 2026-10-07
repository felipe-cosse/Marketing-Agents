"""AC-10: signed HTTP replay retains original work and never repeats execution.

All successful operations use the default
runtime and production methods. The receipt-version fault is private DB corruption,
not a supported key-rotation API. No process or external-network behavior is claimed.
"""

from __future__ import annotations

import hmac
import json
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.domain.enums import (
    ApprovalStatus,
    ExternalActionState,
    RunState,
    StepState,
    TriggerKind,
    WorkMode,
)
from marketing_agents.domain.webhook import WEBHOOK_DIGEST_KEY_VERSION_PREFIX
from marketing_agents.infrastructure.db.models import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ApprovalUseRecord,
    ArtifactRecord,
    AuditEventRecord,
    AuthorizationSetHeadRecord,
    AuthorizationSetMemberRecord,
    AuthorizationSetRecord,
    ConnectorActionReceiptRecord,
    ExecutionAttemptRecord,
    ExecutionOperationPolicyRecord,
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
    LocalRuntimeIdentityRecord,
    RateLimitWindowRecord,
    RunExecutionControlRecord,
    RunPlanRecord,
    RunRecord,
    RunStateTransitionRecord,
    RunStepRecord,
    RunStepStateTransitionRecord,
    WebhookReceiptDeliveryRecord,
    WebhookReceiptRecord,
    WorkItemRecord,
)
from marketing_agents.infrastructure.webhook_signatures import WEBHOOK_SIGNATURE_DOMAIN
from marketing_agents.infrastructure.webhook_sources import StrictJsonWebhookEnvelopeMapper
from marketing_agents.security.digest_key import load_or_create_digest_key
from marketing_agents.security.webhook_digest import derive_webhook_body_digest
from marketing_agents.workers.runtime.composition import RuntimeNotReady, build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from pydantic import SecretStr
from sqlalchemy import select, update

from tests.acceptance.test_ac_07_real_composition_demos import (
    Clock,
    current_requests,
    installation,
    observe_real_calls,
)
from tests.support.api import browser_request

SOURCE = "ac10.events"
TRIGGER = f"trigger.webhook.{SOURCE}.v1"
WEBHOOK_PATH = f"/api/v1/webhooks/{SOURCE}/{TRIGGER}"
SECRET = "ac10-local-nonproduction-webhook-secret-value"
MODEL_INSTANCE = "inst.social-media.new-content.linkedin-comment-replier.01"
MODEL_TEMPLATE = "tpl.social-media.new-content.linkedin-comment-replier"
WRITE_INSTANCES = (
    "inst.community.events.attendee-scheduler.01",
    "inst.community.events.attendee-scheduler.02",
)
WRITE_TEMPLATE = "tpl.community.events.attendee-scheduler"
MODEL_INPUT = {
    "request_id": "request.ac10.model",
    "source_content": "A comment asks how offline drafts are reviewed.",
}
WRITE_COMMAND = {"attendee_ref": "attendee.ac10.local", "session_ref": "session.ac10.local"}
WRITE_INPUT = {
    "request_id": "request.ac10.enroll",
    "source_content": json.dumps({"version": 1, "command": WRITE_COMMAND}),
}


def client_for(runtime):
    return AsyncClient(
        transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
    )


def body_for(event_id, payload):
    return json.dumps(
        {"eventId": event_id, "input": payload}, sort_keys=True, separators=(",", ":")
    ).encode()


def signed_headers(body, clock, *, secret=SECRET):
    timestamp = str(int(clock.now().timestamp()))
    signature = hmac.digest(
        secret.encode(), WEBHOOK_SIGNATURE_DOMAIN + timestamp.encode() + b"\x00" + body, "sha256"
    ).hex()
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": f"v1={signature}",
    }


async def configured_runtime(tmp_path, clock, instance_ids, *, write=False):
    """Install normally, change deployment through HTTP, reload source registration."""
    settings = (await installation(tmp_path)).model_copy(
        update={"webhook_hmac_secret": SecretStr(SECRET)}
    )
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client_for(runtime) as client:
            for instance_id in instance_ids:
                path = f"/api/v1/agent-instances/{instance_id}/configuration"
                current = await client.get(path)
                assert current.status_code == 200, current.text
                configuration = current.json()["configuration"]
                assert not any(
                    item["type"] == "webhook" for item in configuration["triggerBindings"]
                )
                patch = {
                    "triggerBindings": [
                        *configuration["triggerBindings"],
                        {"type": "webhook", "enabled": True, "eventSource": SOURCE},
                    ]
                }
                if write:
                    patch["connectorBindings"] = {
                        **configuration["connectorBindings"],
                        "events": {
                            "connectorFamily": "events",
                            "bindingId": "mock.events.default",
                            "enabled": True,
                        },
                    }
                response = await browser_request(
                    client,
                    "PATCH",
                    path,
                    headers={"If-Match": current.headers["etag"]},
                    json=patch,
                )
                assert response.status_code == 200, response.text
    finally:
        await runtime.close()
    return settings, await build_runtime(settings, clock=clock)


async def business_snapshot(runtime):
    """Exact business facts, excluding audits, session records and worker leases."""
    models = (
        LocalRuntimeIdentityRecord,
        WebhookReceiptRecord,
        WebhookReceiptDeliveryRecord,
        WorkItemRecord,
        RunRecord,
        RunStateTransitionRecord,
        RunPlanRecord,
        RunStepRecord,
        RunStepStateTransitionRecord,
        RunExecutionControlRecord,
        ExecutionOperationPolicyRecord,
        RateLimitWindowRecord,
        ExecutionAttemptRecord,
        ArtifactRecord,
        ExternalActionRecord,
        ExternalActionDispatchAttemptRecord,
        ConnectorActionReceiptRecord,
        ApprovalRequestRecord,
        ApprovalDecisionRecord,
        ApprovalUseRecord,
        AuthorizationSetRecord,
        AuthorizationSetHeadRecord,
        AuthorizationSetMemberRecord,
    )
    result = {}
    async with runtime.database.session_factory() as session:
        for model in models:
            table = model.__table__
            rows = (
                (await session.execute(select(table).order_by(*table.primary_key.columns)))
                .mappings()
                .all()
            )
            result[model.__tablename__] = tuple(deepcopy(dict(row)) for row in rows)
    return result


async def ingress_audits(runtime):
    async with runtime.database.session_factory() as session:
        return Counter(
            (
                await session.scalars(
                    select(AuditEventRecord.event_type).where(
                        AuditEventRecord.event_type.like("webhook.%")
                    )
                )
            ).all()
        )


def assert_calls(calls, *, models=0, reads=0, writes=0):
    assert (len(calls.models), len(calls.reads), len(calls.writes)) == (models, reads, writes)


async def accept(client, body, headers):
    response = await client.post(WEBHOOK_PATH, content=body, headers=headers)
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    document = response.json()
    assert document["status"] == "accepted" and document["disposition"] == "created"
    assert document["source"] == SOURCE and document["eventId"] == json.loads(body)["eventId"]
    return document


async def replay_unchanged(runtime, client, body, headers, original):
    before = await business_snapshot(runtime)
    audits = await ingress_audits(runtime)
    response = await client.post(WEBHOOK_PATH, content=body, headers=headers)
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {**original, "disposition": "replayed"}
    assert await business_snapshot(runtime) == before
    assert await ingress_audits(runtime) == audits + Counter(
        {"webhook.signature_validated": 1, "webhook.duplicate_suppressed": 1}
    )


async def reject_unchanged(runtime, client, body, headers, *, status, code):
    before = await business_snapshot(runtime)
    response = await client.post(WEBHOOK_PATH, content=body, headers=headers)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text and headers["X-Webhook-Signature"] not in response.text
    assert await business_snapshot(runtime) == before


async def assert_received(runtime, original, body, instance_ids, payload):
    deliveries = original["deliveries"]
    assert tuple(item["instanceId"] for item in deliveries) == tuple(sorted(instance_ids))
    assert len({item["workId"] for item in deliveries}) == len(instance_ids)
    assert len({item["runId"] for item in deliveries}) == len(instance_ids)
    digest = derive_webhook_body_digest(body, runtime.digest_key)
    async with runtime.dependencies.unit_of_work() as uow:
        receipt = await uow.webhook_receipts.get(original["receiptId"])
        assert receipt is not None
        assert (receipt.source, receipt.event_id, receipt.trigger_id) == (
            SOURCE,
            original["eventId"],
            TRIGGER,
        )
        assert (receipt.body_digest, receipt.digest_key_version) == (
            digest.value,
            digest.digest_key_version,
        )
        assert receipt.mapper_version == StrictJsonWebhookEnvelopeMapper.version
        assert tuple(
            (item.instance_id, item.work_item_id, item.run_id) for item in receipt.deliveries
        ) == tuple((item["instanceId"], item["workId"], item["runId"]) for item in deliveries)
        for delivery in deliveries:
            instance_id, work_id, run_id = (
                delivery["instanceId"],
                delivery["workId"],
                delivery["runId"],
            )
            assert delivery["instanceUrl"] == f"/api/v1/agent-instances/{instance_id}"
            assert delivery["runUrl"] == f"/api/v1/runs/{run_id}"
            work = await uow.works.get(work_id)
            run = await uow.runs.get(run_id)
            assert work is not None and run is not None
            assert run.work_item_id == work.id and work.instance_id == instance_id
            assert work.source == SOURCE and work.event_id == original["eventId"]
            assert work.trigger_id == TRIGGER and work.mode is WorkMode.MOCK_EXECUTION
            assert dict(work.admitted_payload) == payload
            assert run.state is RunState.RECEIVED
            assert run.configuration_revision == work.configuration_revision
            assert await uow.run_steps.get_plan(run_id) is None
            assert not await uow.artifacts.list_for_run(run_id)
            assert await uow.execution_control.get(run_id) is None
    state = await business_snapshot(runtime)
    assert len(state["webhook_receipts"]) == 1
    assert len(state["webhook_receipt_deliveries"]) == len(instance_ids)
    assert len(state["work_items"]) == len(state["runs"]) == len(instance_ids)
    assert len(state["run_state_transitions"]) == len(instance_ids)
    for table in (
        "execution_attempts",
        "external_actions",
        "external_action_dispatch_attempts",
        "connector_action_receipts",
        "artifacts",
        "approval_requests",
    ):
        assert state[table] == ()


async def assert_model_completed(runtime, original, calls):
    (delivery,) = original["deliveries"]
    run_id = delivery["runId"]
    definition = runtime.workflows.for_catalog_role(MODEL_TEMPLATE, TriggerKind.WEBHOOK)
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        work = await uow.works.get(delivery["workId"])
        assert run is not None and run.state is RunState.COMPLETED
        assert work is not None and work.workflow_id == definition.id
        steps = await uow.run_steps.validate_plan_for_execution(run_id)
        assert len(steps) == 1 and steps[0].state is StepState.SUCCEEDED
        artifacts = await uow.artifacts.list_for_run(run_id)
        assert len(artifacts) == 1 and artifacts[0].verify_payload()
        artifact = artifacts[0]
        assert (artifact.provenance.work_item_id, artifact.provenance.run_id) == (work.id, run_id)
        assert artifact.provenance.step_id == steps[0].id
        assert artifact.provenance.instance_id == MODEL_INSTANCE
        assert artifact.provenance.template_id == MODEL_TEMPLATE
        assert artifact.provenance.workflow_id == definition.id
        assert artifact.provenance.output_schema_hash == definition.output_schema_hash
        assert artifact.payload["provenance"]["source_request_id"] == MODEL_INPUT["request_id"]
        assert artifact.payload["proposed_actions"] == []
        control = await uow.execution_control.get(run_id)
        assert control is not None and (control.model_calls, control.tool_calls) == (1, 0)
        attempts = await uow.execution_control.list_attempts(
            steps[0].id, steps[0].runtime_policy.operation_key
        )
        assert len(attempts) == 1
    state = await business_snapshot(runtime)
    assert len(state["execution_attempts"]) == len(state["artifacts"]) == 1
    assert state["execution_attempts"][0]["outcome"] == "succeeded"
    assert state["execution_attempts"][0]["attempt_number"] == 1
    assert state["execution_attempts"][0]["output_artifact_id"] == artifact.provenance.artifact_id
    assert state["external_actions"] == state["connector_action_receipts"] == ()
    assert_calls(calls, models=1)
    assert calls.models[0].context.run_id == run_id
    assert calls.models[0].context.step_id == artifact.provenance.step_id


@pytest.mark.asyncio
async def test_ac_10_signed_model_replay_before_and_after_completed_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    clock = Clock()
    settings, runtime = await configured_runtime(tmp_path, clock, (MODEL_INSTANCE,))
    body = body_for("event.ac10.model", MODEL_INPUT)
    headers = signed_headers(body, clock)
    try:
        async with client_for(runtime) as client:
            await reject_unchanged(
                runtime,
                client,
                body,
                signed_headers(body, clock, secret="ac10-wrong-nonproduction-signing-secret"),
                status=401,
                code="webhook_authentication_failed",
            )
            assert_calls(calls)
            original = await accept(client, body, headers)
            await assert_received(runtime, original, body, (MODEL_INSTANCE,), MODEL_INPUT)
            await replay_unchanged(runtime, client, body, headers, original)
            assert_calls(calls)
            # Signature binds exact bytes; an old signature cannot authenticate edits.
            changed = body_for(original["eventId"], {**MODEL_INPUT, "source_content": "Changed."})
            await reject_unchanged(
                runtime,
                client,
                changed,
                headers,
                status=401,
                code="webhook_authentication_failed",
            )
            # Once correctly signed, the same source/event identity is a collision.
            for collision in (changed, body + b"\n"):
                audits = await ingress_audits(runtime)
                await reject_unchanged(
                    runtime,
                    client,
                    collision,
                    signed_headers(collision, clock),
                    status=409,
                    code="webhook_idempotency_conflict",
                )
                assert await ingress_audits(runtime) == audits + Counter(
                    {"webhook.signature_validated": 1, "webhook.idempotency_collision": 1}
                )
            assert_calls(calls)
        accepted_state = await business_snapshot(runtime)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == accepted_state
        async with client_for(runtime) as client:
            await replay_unchanged(runtime, client, body, headers, original)
        assert_calls(calls)
        assert await RunWorker(runtime, "worker.ac10.model").drain_once()
        assert not await RunWorker(runtime, "worker.ac10.model-idle").drain_once()
        await assert_model_completed(runtime, original, calls)
        completed = await business_snapshot(runtime)
        async with client_for(runtime) as client:
            await replay_unchanged(runtime, client, body, headers, original)
        assert not await RunWorker(runtime, "worker.ac10.model-replay").drain_once()
        assert await business_snapshot(runtime) == completed
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        async with client_for(runtime) as client:
            await replay_unchanged(runtime, client, body, headers, original)
        assert not await RunWorker(runtime, "worker.ac10.model-restarted").drain_once()
        assert await business_snapshot(runtime) == completed
        await assert_model_completed(runtime, original, calls)
    finally:
        await runtime.close()


async def assert_waiting_fanout(runtime, original, calls):
    requests = {}
    async with runtime.dependencies.unit_of_work() as uow:
        for delivery in original["deliveries"]:
            run_id = delivery["runId"]
            run = await uow.runs.get(run_id)
            plan = await uow.run_steps.get_plan(run_id)
            assert run is not None and run.state is RunState.AWAITING_APPROVAL
            assert plan is not None and plan.step_count == 1
            actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
            assert len(actions) == 1
            action = actions[0]
            assert action.state is ExternalActionState.AWAITING_APPROVAL
            assert action.envelope.capability_id == "cap.events.enroll-attendee"
            assert dict(action.envelope.minimized_payload) == WRITE_COMMAND
            assert action.reservation is None and action.result is None
            control = await uow.execution_control.get(run_id)
            assert control is not None and (control.model_calls, control.tool_calls) == (0, 0)
            assert not await uow.artifacts.list_for_run(run_id)
    for delivery in original["deliveries"]:
        current = await current_requests(runtime, delivery["runId"])
        assert len(current) == 1 and current[0].status is ApprovalStatus.PENDING
        assert current[0].decision is None and current[0].use is None
        requests[delivery["runId"]] = current[0].request
    state = await business_snapshot(runtime)
    assert len(state["external_actions"]) == len(state["approval_requests"]) == 2
    for table in (
        "execution_attempts",
        "external_action_dispatch_attempts",
        "connector_action_receipts",
        "artifacts",
        "approval_decisions",
        "approval_uses",
    ):
        assert state[table] == ()
    assert_calls(calls)
    assert runtime.catalog_writes._bundle.ledger.side_effect_count == 0
    return requests


async def assert_writes_completed(runtime, original, requests, calls):
    assert_calls(calls, writes=2)
    assert len({proof.action.action_id for proof in calls.writes}) == 2
    for delivery in original["deliveries"]:
        run_id = delivery["runId"]
        current = await current_requests(runtime, run_id)
        assert len(current) == 1
        stored = current[0]
        assert stored.request.id == requests[run_id].id
        assert stored.status is ApprovalStatus.CONSUMED
        assert stored.decision is not None and stored.use is not None
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            work = await uow.works.get(delivery["workId"])
            plan = await uow.run_steps.get_plan(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            assert work is not None and run.work_item_id == work.id
            assert (
                work.workflow_id
                == runtime.workflows.for_catalog_role(WRITE_TEMPLATE, TriggerKind.WEBHOOK).id
            )
            assert plan is not None
            actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
            assert len(actions) == 1 and actions[0].state is ExternalActionState.SUCCEEDED
            action = actions[0]
            assert action.id == stored.request.action_id and action.result is not None
            (proof,) = [item for item in calls.writes if item.action.action_id == action.id]
            assert proof.action == action.envelope
            assert proof.action_hash == action.action_hash == stored.request.action_hash
            assert proof.approval_request_id == stored.request.id
            assert proof.approval_decision_id == stored.decision.id
            assert proof.reservation_id == stored.use.reservation_id
            assert proof.idempotency_key == action.idempotency_key
            receipt = await uow.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
            assert receipt is not None and receipt.external_action_id == action.id
            assert receipt.action_hash == action.action_hash
            assert receipt.capability_id == "cap.events.enroll-attendee"
            assert receipt.receipt_id == action.result.receipt_id
            assert receipt.safe_metadata == {
                "mode": "mock",
                "external_side_effect": False,
                "capability_id": "cap.events.enroll-attendee",
                "provider_kind": "connector",
                "provider_name": "events",
                "provider_version": "v1",
            }
            steps = await uow.run_steps.validate_plan_for_execution(run_id)
            assert len(steps) == 1 and steps[0].state is StepState.SUCCEEDED
            control = await uow.execution_control.get(run_id)
            assert control is not None and (control.model_calls, control.tool_calls) == (0, 1)
            assert not await uow.artifacts.list_for_run(run_id)
    state = await business_snapshot(runtime)
    assert len(state["connector_action_receipts"]) == 2
    assert len(state["external_action_dispatch_attempts"]) == 2
    assert len(state["approval_decisions"]) == len(state["approval_uses"]) == 2
    assert state["execution_attempts"] == state["artifacts"] == ()
    assert all(
        row["attempt_number"] == 1 and row["conclusion"] == "succeeded"
        for row in state["external_action_dispatch_attempts"]
    )


@pytest.mark.asyncio
async def test_ac_10_signed_write_fanout_replay_keeps_two_exact_once_mock_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    clock = Clock()
    settings, runtime = await configured_runtime(tmp_path, clock, WRITE_INSTANCES, write=True)
    body = body_for("event.ac10.write-fanout", WRITE_INPUT)
    headers = signed_headers(body, clock)
    try:
        async with client_for(runtime) as client:
            original = await accept(client, body, headers)
            await assert_received(runtime, original, body, WRITE_INSTANCES, WRITE_INPUT)
            await replay_unchanged(runtime, client, body, headers, original)
            assert_calls(calls)
        accepted = await business_snapshot(runtime)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == accepted
        worker = RunWorker(runtime, "worker.ac10.fanout-plan")
        for _ in WRITE_INSTANCES:
            assert await worker.drain_once()
        requests = await assert_waiting_fanout(runtime, original, calls)
        waiting = await business_snapshot(runtime)
        async with client_for(runtime) as client:
            await replay_unchanged(runtime, client, body, headers, original)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == waiting
        async with client_for(runtime) as client:
            await replay_unchanged(runtime, client, body, headers, original)
            assert await assert_waiting_fanout(runtime, original, calls) == requests
            for request in requests.values():
                approved = await browser_request(
                    client,
                    "POST",
                    f"/api/v1/approvals/{request.id}/approve",
                    json={
                        "expected_generation": request.generation,
                        "expected_payload_hash": request.action_hash,
                    },
                )
                assert approved.status_code == 200, approved.text
            assert_calls(calls)
            assert not (await business_snapshot(runtime))["connector_action_receipts"]
            await replay_unchanged(runtime, client, body, headers, original)
        clock.current += timedelta(seconds=2)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert runtime.catalog_writes._bundle.ledger.side_effect_count == 0
        worker = RunWorker(runtime, "worker.ac10.fanout-approved")
        for _ in WRITE_INSTANCES:
            assert await worker.drain_once()
        assert not await worker.drain_once()
        await assert_writes_completed(runtime, original, requests, calls)
        assert runtime.catalog_writes._bundle.ledger.side_effect_count == 2
        completed = await business_snapshot(runtime)
        async with client_for(runtime) as client:
            await replay_unchanged(runtime, client, body, headers, original)
            changed_payload = {
                **WRITE_INPUT,
                "source_content": json.dumps(
                    {"version": 1, "command": {**WRITE_COMMAND, "session_ref": "session.changed"}}
                ),
            }
            changed = body_for(original["eventId"], changed_payload)
            await reject_unchanged(
                runtime,
                client,
                changed,
                signed_headers(changed, clock),
                status=409,
                code="webhook_idempotency_conflict",
            )
        assert not await worker.drain_once()
        assert await business_snapshot(runtime) == completed
        assert runtime.catalog_writes._bundle.ledger.side_effect_count == 2
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        async with client_for(runtime) as client:
            await replay_unchanged(runtime, client, body, headers, original)
        assert not await RunWorker(runtime, "worker.ac10.fanout-terminal").drain_once()
        assert await business_snapshot(runtime) == completed
        assert runtime.catalog_writes._bundle.ledger.side_effect_count == 0
        await assert_writes_completed(runtime, original, requests, calls)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ac_10_replay_requires_original_paired_installation_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    clock = Clock()
    settings, runtime = await configured_runtime(tmp_path, clock, (MODEL_INSTANCE,))
    body = body_for("event.ac10.key-pair", MODEL_INPUT)
    headers = signed_headers(body, clock)
    try:
        async with client_for(runtime) as client:
            original = await accept(client, body, headers)
            await assert_received(runtime, original, body, (MODEL_INSTANCE,), MODEL_INPUT)
        before = await business_snapshot(runtime)
        original_key = runtime.digest_key
        await runtime.close()
        alternate_path = tmp_path / "alternate-secrets" / "digest.key"
        alternate = load_or_create_digest_key(alternate_path)
        assert alternate != original_key
        wrong_settings = settings.model_copy(
            update={"marketing_agents_digest_key_path": alternate_path}
        )
        # Normal composition checks DB/key pairing before exposing an HTTP service.
        with pytest.raises(RuntimeNotReady, match="database_unavailable"):
            await build_runtime(wrong_settings, clock=clock)
        runtime = await build_runtime(settings, clock=clock)
        assert runtime.digest_key == original_key
        assert await business_snapshot(runtime) == before
        async with client_for(runtime) as client:
            await replay_unchanged(runtime, client, body, headers, original)
        assert_calls(calls)
        assert await RunWorker(runtime, "worker.ac10.correct-key").drain_once()
        await assert_model_completed(runtime, original, calls)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ac_10_corrupt_receipt_key_version_fails_closed_without_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    clock = Clock()
    settings, runtime = await configured_runtime(tmp_path, clock, (MODEL_INSTANCE,))
    body = body_for("event.ac10.receipt-key-version", MODEL_INPUT)
    headers = signed_headers(body, clock)
    try:
        async with client_for(runtime) as client:
            original = await accept(client, body, headers)
            await replay_unchanged(runtime, client, body, headers, original)
        expected = derive_webhook_body_digest(body, runtime.digest_key).digest_key_version
        replacement = WEBHOOK_DIGEST_KEY_VERSION_PREFIX + ("0" * 64)
        assert replacement != expected
        # Fault injection is restricted to the test's temporary database; keep its
        # actual installed key intact and do not pretend this is a public rotation.
        async with runtime.database.session_factory() as session, session.begin():
            await session.execute(
                update(WebhookReceiptRecord)
                .where(WebhookReceiptRecord.id == original["receiptId"])
                .values(digest_key_version=replacement)
            )
        corrupted = await business_snapshot(runtime)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == corrupted
        async with client_for(runtime) as client:
            await reject_unchanged(
                runtime,
                client,
                body,
                headers,
                status=503,
                code="webhook_unavailable",
            )
        assert_calls(calls)
        state = await business_snapshot(runtime)
        assert len(state["webhook_receipts"]) == len(state["work_items"]) == len(state["runs"]) == 1
        assert state["runs"][0]["state"] == "received"
        assert state["run_plans"] == state["execution_attempts"] == ()
        assert state["external_actions"] == state["connector_action_receipts"] == ()
    finally:
        await runtime.close()
