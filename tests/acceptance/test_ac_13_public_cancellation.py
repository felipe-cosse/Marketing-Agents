"""AC-13: public cancellation fences work without rewriting real outcomes.

These journeys use migrated/seeded SQLite, public HTTP commands and the default
worker. Event gates delay actual production responses; interruption cancels an
in-process task, not an OS process. The receipt-absent case stops at genuine
gateway entry before its delegate, so it proves conservative unknown-outcome
recovery, not that a remote provider performed an unobserved effect.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.application.services.controlled_read_executor import (
    ControlledReadExecutor,
    ReadExecutionClassification,
)
from marketing_agents.application.services.external_action_dispatcher import (
    ExternalActionDispatcher,
)
from marketing_agents.domain.action_hash import canonical_action_hash
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import ApprovalStatus, ExternalActionState, RunState, StepState
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker

from tests.acceptance.test_ac_07_real_composition_demos import (
    EMAIL,
    READ_DEMOS,
    Clock,
    assert_email_zero_calls,
    current_requests,
    installation,
    observe_real_calls,
    submit,
)
from tests.acceptance.test_ac_09_approval_rejection import approve
from tests.acceptance.test_ac_10_webhook_replay import business_snapshot
from tests.acceptance.test_ac_11_mock_crash_recovery import (
    action_identity,
    actions_for,
    assert_authority_unchanged,
    audit_rows,
    client_for,
)
from tests.support.api import browser_request

FIRST_WRITE = "cap.newsletter.subscribe"
SECOND_WRITE = "cap.crm.upsert-contact"
WAIT_SECONDS = 10


async def cancel(client, run_id):
    response = await browser_request(client, "POST", f"/api/v1/runs/{run_id}/cancel", json={})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["run_id"] == run_id and body["state"] == "cancelled"
    assert body["effects_reversed"] is False
    assert body["outcome_unknown_effect_count_at_cancellation"] == 0
    assert body["run_url"] == f"/api/v1/runs/{run_id}"
    assert body["timeline_url"] == f"/api/v1/runs/{run_id}/timeline"
    return body


async def prepare_email(runtime, client, calls, *, release):
    run_id = await submit(client, EMAIL)
    assert await RunWorker(runtime, "worker.ac13.prepare").drain_once()
    await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
    plan, actions = await actions_for(runtime, run_id)
    assert {action.envelope.capability_id for action in actions} == {FIRST_WRITE, SECOND_WRITE}
    requests = await current_requests(runtime, run_id)
    assert len(requests) == 2 and {item.status for item in requests} == {ApprovalStatus.PENDING}
    for stored in requests if release else requests[:1]:
        response = await approve(client, stored.request)
        assert response.status_code == 200, response.text
    await assert_email_zero_calls(
        runtime, run_id, calls, RunState.EXECUTING if release else RunState.AWAITING_APPROVAL
    )
    return run_id, plan, {action.id: action for action in actions}


async def cancellation_projection(runtime, run_id, outcome):
    """Compare the complete public sequence to durable audit identities."""
    async with client_for(runtime) as client:
        response = await client.get(outcome["run_url"])
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["state"] == "cancelled" and detail["version"] == outcome["version"]
        assert detail["artifact_summaries"] == []
        transitions = [item for item in detail["transitions"] if item["command"] == "cancel"]
        assert len(transitions) == 1
        assert (
            transitions[0]["completed_effect_count"]
            == outcome["succeeded_effect_count_at_cancellation"]
        )
        assert transitions[0]["outcome_unknown_effect_count"] == 0
        timeline = []
        cursor = None
        for _ in range(5):
            params = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            page = await client.get(outcome["timeline_url"], params=params)
            assert page.status_code == 200, page.text
            timeline.extend(page.json()["items"])
            cursor = page.json()["next_cursor"]
            if cursor is None:
                break
        assert cursor is None
    durable = await audit_rows(runtime, run_id)
    assert [(item["id"], item["sequence"]) for item in timeline] == [
        (item["id"], item["run_sequence"]) for item in durable
    ]
    assert [item["sequence"] for item in timeline] == sorted(
        {item["sequence"] for item in timeline}
    )
    cancellations = [
        item
        for item in timeline
        if item["event_type"] == "run.transitioned" and item["metadata"].get("command") == "cancel"
    ]
    assert len(cancellations) == 1
    event = cancellations[0]
    expected_actor = AuditContext.authenticated_user(
        "local-operator", authentication_method="local_fixed", correlation_id="ac13.expected"
    ).actor_id
    assert event["actor_id"] == expected_actor and event["new_state"] == "cancelled"
    assert event["actor_source"] == "user" and event["auth_method"] == "local_fixed"
    assert event["outcome"] == "accepted"
    return detail, timeline, event


async def assert_idle_exact(runtime, run_id, *, label):
    state = await business_snapshot(runtime)
    events = await audit_rows(runtime, run_id)
    assert not await RunWorker(runtime, f"worker.ac13.{label}").drain_once()
    assert await business_snapshot(runtime) == state
    assert await audit_rows(runtime, run_id) == events


async def stop_task(task):
    if task is not None and not task.done():
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, WAIT_SECONDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("prebarrier", [False, True], ids=["queued", "one-of-two-approved"])
async def test_ac_13_public_cancellation_before_execution_has_zero_calls_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prebarrier: bool
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client_for(runtime) as client:
            if prebarrier:
                run_id, plan, original_actions = await prepare_email(
                    runtime, client, calls, release=False
                )
                before_requests = await current_requests(runtime, run_id)
                assert {item.status for item in before_requests} == {
                    ApprovalStatus.APPROVED,
                    ApprovalStatus.PENDING,
                }
            else:
                run_id = await submit(client, EMAIL)
            outcome = await cancel(client, run_id)
        assert outcome["succeeded_effect_count_at_cancellation"] == 0
        assert outcome["preserved_action_ids"] == outcome["preserved_step_ids"] == []
        state = await business_snapshot(runtime)
        assert len(state["work_items"]) == len(state["runs"]) == 1
        for table in (
            "approval_uses",
            "external_action_dispatch_attempts",
            "connector_action_receipts",
            "execution_attempts",
            "artifacts",
        ):
            assert state[table] == (), table
        if prebarrier:
            after_plan, actions = await actions_for(runtime, run_id)
            assert after_plan == plan
            assert set(outcome["cancelled_action_ids"]) == set(original_actions)
            assert len(outcome["cancelled_step_ids"]) == 3
            for action in actions:
                assert action.state is ExternalActionState.CANCELLED
                assert action_identity(action) == action_identity(original_actions[action.id])
                assert action.reservation is None and action.delivery_attempt_count == 0
            requests = await current_requests(runtime, run_id)
            assert {item.request.id for item in requests} == {
                item.request.id for item in before_requests
            }
            assert all(item.use is None for item in requests)
            assert [item.decision for item in requests] == [
                item.decision for item in before_requests
            ]
            assert state["authorization_sets"][0]["status"] == "cancelled"
        else:
            assert outcome["cancelled_action_ids"] == outcome["cancelled_step_ids"] == []
            for table in ("run_plans", "run_steps", "external_actions", "approval_requests"):
                assert state[table] == (), table
        await cancellation_projection(runtime, run_id, outcome)
        clock.current += timedelta(seconds=2)
        await assert_idle_exact(runtime, run_id, label="cancelled-original-idle")
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == state
        clock.current += timedelta(seconds=2)
        await assert_idle_exact(runtime, run_id, label="cancelled-restart-idle")
        async with client_for(runtime) as client:
            response = await browser_request(
                client, "POST", f"/api/v1/runs/{run_id}/cancel", json={}
            )
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "cancellation_conflict"
        after_rejection = deepcopy(state)
        after_rejection["runs"][0]["next_timeline_sequence"] += 1
        assert await business_snapshot(runtime) == after_rejection
        rejection = (await audit_rows(runtime, run_id))[-1]
        assert rejection["event_type"] == "run.transition_rejected"
        assert rejection["attempted_command"] == "cancel"
        # Planned writes are denied first by the approval-boundary lifecycle
        # guard; the public coordinator still returns the same terminal conflict.
        assert rejection["reason_code"] == (
            "invalid_transition" if prebarrier else "terminal_state_immutable"
        )
        assert calls.writes == calls.reads == calls.models == []
        assert runtime.email._connector_bundle.ledger.side_effect_count == 0
    finally:
        await runtime.close()


async def assert_cancelled_write_truth(
    runtime, run_id, original_actions, consumed, released, target_id, *, receipt_expected, proof
):
    _, actions = await actions_for(runtime, run_id)
    assert {action.id for action in actions} == set(original_actions)
    assert await current_requests(runtime, run_id) == consumed
    target = next(action for action in actions if action.id == target_id)
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        assert run is not None and run.state is RunState.CANCELLED
        control = await uow.execution_control.get(run_id)
        assert control is not None and control.cancel_requested_at is not None
        assert (control.model_calls, control.tool_calls) == (0, 1)
        steps = await uow.run_steps.list_for_run(run_id)
        assert len(steps) == 3
        for step in steps:
            expected = (
                (StepState.SUCCEEDED if receipt_expected else StepState.FAILED)
                if step.id == target.step_id
                else StepState.CANCELLED
            )
            assert step.state is expected
        for action in actions:
            assert action_identity(action) == action_identity(original_actions[action.id])
            stored = next(item for item in consumed if item.request.action_id == action.id)
            assert stored.status is ApprovalStatus.CONSUMED
            assert stored.decision is not None and stored.use is not None
            assert action.reservation is not None
            assert action.action_hash == canonical_action_hash(action.envelope)
            assert (
                stored.request.action_hash == stored.decision.action_hash == stored.use.action_hash
            )
            assert stored.use.action_hash == action.action_hash
            assert stored.request.id == stored.decision.request_id == stored.use.request_id
            assert stored.decision.id == action.reservation.approval_decision_id
            assert stored.request.id == action.reservation.approval_request_id
            assert stored.use.reservation_id == action.reservation.reservation_id
            assert stored.use.authorization_set_id == action.envelope.authorization_set_id
            receipt = await uow.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
            assert action.lease is None and action.call_started_at is None
            assert action.call_deadline_at is None
            if action.id != target_id:
                assert action.envelope.capability_id == SECOND_WRITE
                assert action.state is ExternalActionState.CANCELLED
                assert action.delivery_attempt_count == 0 and action.result is None
                assert receipt is None
                continue
            assert action.delivery_attempt_count == 1
            assert proof.action == action.envelope
            assert proof.action_hash == action.action_hash
            assert proof.idempotency_key == action.idempotency_key
            assert proof.approval_request_id == stored.request.id
            assert proof.approval_decision_id == stored.decision.id
            assert proof.reservation_id == stored.use.reservation_id
            if receipt_expected:
                assert action.state is ExternalActionState.SUCCEEDED
                assert receipt is not None and action.result is not None
                assert receipt.external_action_id == action.id
                assert receipt.action_hash == action.action_hash
                assert receipt.capability_id == FIRST_WRITE
                assert receipt.idempotency_key == action.idempotency_key
                assert receipt.receipt_id == action.result.receipt_id
                assert receipt.safe_metadata == action.result.safe_metadata
                assert receipt.safe_metadata["external_side_effect"] is False
            else:
                assert action.state is ExternalActionState.OUTCOME_UNKNOWN
                assert action.terminal_reason_code == "run_cancelled_after_call_start"
                assert action.result is None and receipt is None
    state = await business_snapshot(runtime)
    assert_authority_unchanged(state, released)
    assert len(state["external_actions"]) == len(state["approval_uses"]) == 2
    assert len(state["connector_action_receipts"]) == int(receipt_expected)
    assert state["execution_attempts"] == state["artifacts"] == ()
    (attempt,) = state["external_action_dispatch_attempts"]
    assert attempt["external_action_id"] == target_id and attempt["attempt_number"] == 1
    assert attempt["conclusion"] == ("succeeded" if receipt_expected else "outcome_unknown")
    assert attempt["completed_at"] is not None
    if receipt_expected:
        assert attempt["connector_receipt_id"] == target.result.receipt_id
    else:
        assert attempt["connector_receipt_id"] is None
    return target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ["completed-before-cancel", "timely-receipt", "interrupted-receipt", "interrupted-no-receipt"],
)
async def test_ac_13_real_write_cancellation_preserves_outcome_and_suppresses_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    calls = observe_real_calls(monkeypatch)
    original_gateway = RegistryConnectorWriteGateway.execute
    original_dispatch = ExternalActionDispatcher.dispatch_once
    reached = asyncio.Event()
    release_response = asyncio.Event()
    gateway_entries = []
    actual_results = {}
    target_id = None
    receipt_expected = boundary != "interrupted-no-receipt"

    async def gated_gateway(gateway, authorization):
        assert not gateway_entries, "cancelled workflow must never invoke another write"
        gateway_entries.append(authorization)
        assert authorization.action.capability_id == FIRST_WRITE
        if receipt_expected:
            result = await original_gateway(gateway, authorization)
            actual_results[authorization.action.action_id] = result
        if boundary != "completed-before-cancel":
            reached.set()
            await release_response.wait()
        # The receipt-absent task is interrupted, never given an invented result.
        assert receipt_expected
        return result

    async def gated_dispatch(dispatcher, action_id, *, lease_owner):
        result = await original_dispatch(dispatcher, action_id, lease_owner=lease_owner)
        if boundary == "completed-before-cancel" and action_id == target_id:
            assert result.action.state is ExternalActionState.SUCCEEDED
            reached.set()
            await release_response.wait()
        return result

    monkeypatch.setattr(RegistryConnectorWriteGateway, "execute", gated_gateway)
    monkeypatch.setattr(ExternalActionDispatcher, "dispatch_once", gated_dispatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    task = None
    try:
        async with client_for(runtime) as client:
            run_id, original_plan, original_actions = await prepare_email(
                runtime, client, calls, release=True
            )
            consumed = await current_requests(runtime, run_id)
            assert {item.status for item in consumed} == {ApprovalStatus.CONSUMED}
            released = await business_snapshot(runtime)
            target_id = next(
                action.id
                for action in original_actions.values()
                if action.envelope.capability_id == FIRST_WRITE
            )
            clock.current += timedelta(seconds=2)
            old_ledger = runtime.email._connector_bundle.ledger
            task = asyncio.create_task(RunWorker(runtime, "worker.ac13.live").drain_once())
            await asyncio.wait_for(reached.wait(), WAIT_SECONDS)
            assert not task.done()
            plan, live_actions = await actions_for(runtime, run_id)
            assert plan == original_plan
            live = next(action for action in live_actions if action.id == target_id)
            assert live.delivery_attempt_count == 1
            assert live.state is (
                ExternalActionState.SUCCEEDED
                if boundary == "completed-before-cancel"
                else ExternalActionState.DISPATCHING
            )
            before_cancel = await business_snapshot(runtime)
            assert len(before_cancel["connector_action_receipts"]) == int(receipt_expected)
            assert old_ledger.side_effect_count == int(receipt_expected)
            sibling = next(action for action in live_actions if action.id != target_id)
            assert sibling.state is ExternalActionState.DISPATCH_RESERVED
            assert sibling.delivery_attempt_count == 0
            outcome = await cancel(client, run_id)
        assert outcome["succeeded_effect_count_at_cancellation"] == int(
            boundary == "completed-before-cancel"
        )
        assert outcome["preserved_action_ids"] == [target_id]
        assert outcome["cancelled_action_ids"] == [sibling.id]
        assert outcome["preserved_step_ids"] == [live.step_id]
        assert len(outcome["cancelled_step_ids"]) == 2
        cancellation_state = await business_snapshot(runtime)
        cancellation_events = await audit_rows(runtime, run_id)
        assert_authority_unchanged(cancellation_state, released)

        if boundary.startswith("interrupted"):
            await stop_task(task)
            task = None
            assert live.call_deadline_at is not None and live.lease is not None
            recovery_at = max(live.call_deadline_at, live.lease.expires_at)
            await runtime.close()
            runtime = await build_runtime(settings, clock=clock)
            assert runtime.email._connector_bundle.ledger is not old_ledger
            assert runtime.email._connector_bundle.ledger.side_effect_count == 0
            assert await business_snapshot(runtime) == cancellation_state
            clock.current = recovery_at - timedelta(microseconds=1)
            await assert_idle_exact(runtime, run_id, label="write-before-recovery-fence")
            clock.current = recovery_at
            assert await RunWorker(runtime, "worker.ac13.write-recovery").drain_once()
            expected_event = (
                "action.receipt_reconciled" if receipt_expected else "action.outcome_unknown"
            )
        else:
            release_response.set()
            assert await asyncio.wait_for(task, WAIT_SECONDS)
            task = None
            expected_event = "action.succeeded"

        target = await assert_cancelled_write_truth(
            runtime,
            run_id,
            original_actions,
            consumed,
            released,
            target_id,
            receipt_expected=receipt_expected,
            proof=gateway_entries[0],
        )
        assert len(gateway_entries) == 1
        assert len(calls.writes) == len(actual_results) == int(receipt_expected)
        assert calls.models == calls.reads == []
        assert old_ledger.side_effect_count == int(receipt_expected)
        if receipt_expected:
            assert target.result.receipt_id == actual_results[target_id].receipt_id
        detail, timeline, cancelled_event = await cancellation_projection(runtime, run_id, outcome)
        assert detail["execution_control"]["tool_calls"] == 1
        assert detail["execution_control"]["model_calls"] == 0
        completed = [
            item
            for item in timeline
            if item["event_type"] == expected_event and item["action_id"] == target_id
        ]
        assert len(completed) == 1
        completed_event = completed[0]
        completion_worker = (
            "worker.ac13.write-recovery"
            if boundary.startswith("interrupted")
            else "worker.ac13.live"
        )
        completion_actor = AuditContext.worker(
            completion_worker, correlation_id="ac13.expected"
        ).actor_id
        assert completed_event["actor_id"] == completion_actor
        assert completed_event["actor_source"] == "worker"
        if boundary == "completed-before-cancel":
            assert completed_event["sequence"] < cancelled_event["sequence"]
        else:
            assert cancelled_event["sequence"] < completed_event["sequence"]
        own_audits = [
            item for item in await audit_rows(runtime, run_id) if item["action_id"] == target_id
        ]
        assert sum(item["event_type"] == "action.call_started" for item in own_audits) == 1
        assert not any(item["event_type"] == "action.cancelled" for item in own_audits)
        durable_completion = next(
            item for item in own_audits if item["event_type"] == expected_event
        )
        assert durable_completion["action_attempt_number"] == 1
        assert durable_completion["actor_id"] == completion_actor
        assert durable_completion["receipt_id"] == (
            target.result.receipt_id if receipt_expected else None
        )
        if boundary.startswith("interrupted"):
            assert (await audit_rows(runtime, run_id))[
                : len(cancellation_events)
            ] == cancellation_events
        async with client_for(runtime) as client:
            action_detail = await client.get(f"/api/v1/external-actions/{target_id}")
            assert action_detail.status_code == 200, action_detail.text
            assert action_detail.json()["state"] == target.state.value
            assert action_detail.json()["receipt_id"] == (
                target.result.receipt_id if receipt_expected else None
            )
        clock.current += timedelta(seconds=2)
        await assert_idle_exact(runtime, run_id, label="write-recovered-idle")
        final = await business_snapshot(runtime)
        final_events = await audit_rows(runtime, run_id)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == final
        assert await audit_rows(runtime, run_id) == final_events
        clock.current += timedelta(seconds=2)
        await assert_idle_exact(runtime, run_id, label="write-terminal-restart")
        assert runtime.email._connector_bundle.ledger.side_effect_count == 0
        assert len(gateway_entries) == 1
        assert len(calls.writes) == int(receipt_expected) and calls.models == calls.reads == []
    finally:
        await stop_task(task)
        await runtime.close()


@pytest.mark.asyncio
async def test_ac_13_cancelled_read_orphan_recovery_closes_original_attempt_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    original_complete = ControlledReadExecutor._complete
    returned = asyncio.Event()
    release_response = asyncio.Event()
    actual_results = []

    async def hold_real_completion(executor, reserved, classification, output, **kwargs):
        assert not actual_results, "cancelled READ must never regenerate output"
        # The genuine provider and adapter have returned and validated output.
        # Interrupt outside the provider try/except: cancelling the provider
        # itself intentionally performs eager cancellation completion, not an
        # orphan that a fresh worker must reconcile at the stored deadline.
        assert classification is ReadExecutionClassification.SUCCEEDED and output is not None
        actual_results.append(output)
        returned.set()
        await release_response.wait()
        return await original_complete(executor, reserved, classification, output, **kwargs)

    monkeypatch.setattr(ControlledReadExecutor, "_complete", hold_real_completion)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    task = None
    try:
        async with client_for(runtime) as client:
            run_id = await submit(client, READ_DEMOS[0][0])
            task = asyncio.create_task(RunWorker(runtime, "worker.ac13.read-live").drain_once())
            await asyncio.wait_for(returned.wait(), WAIT_SECONDS)
            assert not task.done()
            live = await business_snapshot(runtime)
            (attempt,) = live["execution_attempts"]
            assert attempt["outcome"] is None and attempt["completed_at"] is None
            assert attempt["kind"] == "model" and attempt["call_deadline_at"] > clock.current
            assert live["artifacts"] == ()
            outcome = await cancel(client, run_id)
        assert outcome["succeeded_effect_count_at_cancellation"] == 0
        assert outcome["preserved_step_ids"] == [attempt["step_id"]]
        assert outcome["cancelled_action_ids"] == outcome["preserved_action_ids"] == []
        await stop_task(task)
        task = None
        cancelled = await business_snapshot(runtime)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == cancelled
        clock.current = attempt["call_deadline_at"] - timedelta(microseconds=1)
        await assert_idle_exact(runtime, run_id, label="read-before-deadline")
        clock.current = attempt["call_deadline_at"]
        assert await RunWorker(runtime, "worker.ac13.read-orphan-recovery").drain_once()
        final = await business_snapshot(runtime)
        (closed,) = final["execution_attempts"]
        assert (
            closed["id"] == attempt["id"] and closed["attempt_number"] == attempt["attempt_number"]
        )
        assert closed["call_deadline_at"] == attempt["call_deadline_at"]
        assert closed["outcome"] == "cancelled" and closed["completed_at"] == clock.current
        assert (
            final["artifacts"]
            == final["external_actions"]
            == final["connector_action_receipts"]
            == ()
        )
        assert final["run_execution_controls"][0]["model_calls"] == 1
        assert final["run_execution_controls"][0]["tool_calls"] == 0
        step = next(item for item in final["run_steps"] if item["id"] == attempt["step_id"])
        assert step["state"] == "failed" and step["terminal_reason_code"] == "run_cancelled"
        assert closed["retry_not_before"] is None
        detail, timeline, cancel_event = await cancellation_projection(runtime, run_id, outcome)
        assert detail["execution_control"]["model_calls"] == 1
        completion = [item for item in timeline if item["event_type"] == "attempt.completed"]
        assert len(completion) == 1 and completion[0]["sequence"] > cancel_event["sequence"]
        expected_actor = AuditContext.worker(
            "worker.ac13.read-orphan-recovery", correlation_id="ac13.expected"
        ).actor_id
        assert completion[0]["actor_id"] == expected_actor
        assert completion[0]["actor_source"] == "worker"
        completed_audit = next(
            item
            for item in await audit_rows(runtime, run_id)
            if item["event_type"] == "attempt.completed"
        )
        assert completed_audit["actor_id"] == expected_actor
        assert completed_audit["attempt_id"] == attempt["id"]
        assert len(actual_results) == len(calls.models) == 1
        assert calls.reads == calls.writes == []
        clock.current += timedelta(seconds=2)
        await assert_idle_exact(runtime, run_id, label="read-recovered-idle")
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == final
        clock.current += timedelta(seconds=2)
        await assert_idle_exact(runtime, run_id, label="read-terminal-restart")
        assert len(calls.models) == 1 and calls.reads == calls.writes == []
    finally:
        await stop_task(task)
        await runtime.close()
