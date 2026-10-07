"""AC-11: simulated post-receipt interruption cannot repeat a mock action.

Fault injection cancels an in-process worker
after a real connector receipt commits, then reconstructs the default runtime.
This is not an OS-kill test or a universal real-provider exactly-once guarantee.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.domain.action_hash import canonical_action_hash
from marketing_agents.domain.enums import ApprovalStatus, ExternalActionState, RunState, StepState
from marketing_agents.infrastructure.adapters.connectors.composition import (
    build_durable_connector_bundle,
)
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.db.models import AuditEventRecord
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from sqlalchemy import select

from tests.acceptance.test_ac_07_real_composition_demos import (
    EMAIL,
    Clock,
    assert_artifact_api,
    assert_email_zero_calls,
    current_requests,
    installation,
    observe_real_calls,
    persisted_artifact,
    submit,
)
from tests.acceptance.test_ac_10_webhook_replay import business_snapshot
from tests.support.api import browser_request

WRITE_ORDER = ("cap.newsletter.subscribe", "cap.crm.upsert-contact")
AUTHORITY_TABLES = (
    "approval_requests",
    "approval_decisions",
    "approval_uses",
    "authorization_sets",
    "authorization_set_heads",
    "authorization_set_members",
)


def client_for(runtime):
    return AsyncClient(
        transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
    )


async def actions_for(runtime, run_id):
    async with runtime.dependencies.unit_of_work() as uow:
        plan = await uow.run_steps.get_plan(run_id)
        assert plan is not None
        return plan, await uow.external_actions.list_run_plan(run_id, plan.plan_hash)


def action_identity(action):
    """State may advance; the authorized command, key, plan and binding may not."""
    return (
        action.id,
        action.run_id,
        action.step_id,
        action.envelope,
        action.action_hash,
        action.idempotency_key,
        action.delivery_contract,
    )


def assert_authority_unchanged(state, released):
    for table in AUTHORITY_TABLES:
        assert state[table] == released[table], table


async def audit_rows(runtime, run_id):
    table = AuditEventRecord.__table__
    async with runtime.database.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(table)
                    .where(AuditEventRecord.run_id == run_id)
                    .order_by(AuditEventRecord.run_sequence)
                )
            )
            .mappings()
            .all()
        )
    return tuple(deepcopy(dict(row)) for row in rows)


async def assert_completed_lineage(
    runtime, run_id, original_actions, consumed, worker_calls, artifact, crashed_id
):
    current = await current_requests(runtime, run_id)
    assert current == consumed
    by_action = {stored.request.action_id: stored for stored in current}
    _, actions = await actions_for(runtime, run_id)
    assert len(actions) == 2
    refs = {row["action_id"]: row for row in artifact.payload["mock_receipt_refs"]}
    assert set(refs) == set(original_actions) == set(by_action)
    assert Counter(proof.action.action_id for proof in worker_calls) == Counter(
        {action_id: 1 for action_id in original_actions}
    )
    receipts = {}
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        assert run is not None and run.state is RunState.COMPLETED
        assert await uow.artifacts.list_for_run(run_id) == (artifact,)
        control = await uow.execution_control.get(run_id)
        assert control is not None and (control.model_calls, control.tool_calls) == (1, 2)
        steps = await uow.run_steps.validate_plan_for_execution(run_id)
        assert len(steps) == 3 and all(step.state is StepState.SUCCEEDED for step in steps)
        for action in actions:
            assert action_identity(action) == action_identity(original_actions[action.id])
            assert action.state is ExternalActionState.SUCCEEDED
            assert action.delivery_attempt_count == 1
            assert action.lease is None and action.call_started_at is None
            assert action.call_deadline_at is None
            assert action.result is not None and action.reservation is not None
            stored = by_action[action.id]
            request, decision, use = stored.request, stored.decision, stored.use
            assert stored.status is ApprovalStatus.CONSUMED
            assert decision is not None and use is not None
            assert request.action_hash == decision.action_hash == use.action_hash
            assert (
                request.action_hash == action.action_hash == canonical_action_hash(action.envelope)
            )
            assert request.id == decision.request_id == use.request_id
            assert decision.id == use.decision_id == action.reservation.approval_decision_id
            assert request.id == action.reservation.approval_request_id
            assert use.reservation_id == action.reservation.reservation_id
            assert use.authorization_set_id == action.envelope.authorization_set_id
            receipt = await uow.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
            assert receipt is not None
            assert receipt.external_action_id == action.id
            assert receipt.action_hash == action.action_hash
            assert receipt.capability_id == action.envelope.capability_id
            assert receipt.idempotency_key == action.idempotency_key
            assert receipt.receipt_id == action.result.receipt_id == refs[action.id]["receipt_id"]
            assert receipt.safe_metadata == action.result.safe_metadata
            assert receipt.safe_metadata["external_side_effect"] is False
            assert receipt.safe_metadata["provider_name"] == action.envelope.connector_family
            assert receipt.safe_metadata["provider_version"] == "v1"
            (proof,) = [item for item in worker_calls if item.action.action_id == action.id]
            assert proof.action == action.envelope
            assert proof.action_hash == receipt.action_hash
            assert proof.idempotency_key == receipt.idempotency_key
            assert proof.approval_request_id == request.id
            assert proof.approval_decision_id == decision.id
            assert proof.reservation_id == use.reservation_id
            receipts[action.id] = receipt
    state = await business_snapshot(runtime)
    assert len(state["work_items"]) == len(state["runs"]) == len(state["run_plans"]) == 1
    assert len(state["external_actions"]) == len(state["connector_action_receipts"]) == 2
    assert len(state["approval_requests"]) == len(state["approval_decisions"]) == 2
    assert len(state["approval_uses"]) == 2
    attempts = state["external_action_dispatch_attempts"]
    assert len(attempts) == 2
    assert {row["external_action_id"] for row in attempts} == set(original_actions)
    assert all(row["attempt_number"] == 1 and row["conclusion"] == "succeeded" for row in attempts)
    assert {(row["external_action_id"], row["connector_receipt_id"]) for row in attempts} == {
        (action_id, receipt.receipt_id) for action_id, receipt in receipts.items()
    }
    assert len(state["execution_attempts"]) == len(state["artifacts"]) == 1
    assert state["execution_attempts"][0]["kind"] == "model"
    assert state["execution_attempts"][0]["outcome"] == "succeeded"
    events = await audit_rows(runtime, run_id)
    for action in actions:
        own = [row for row in events if row["action_id"] == action.id]
        assert sum(row["event_type"] == "action.call_started" for row in own) == 1
        expected_event = (
            "action.receipt_reconciled" if action.id == crashed_id else "action.succeeded"
        )
        completed = [row for row in own if row["event_type"] == expected_event]
        assert len(completed) == 1
        event = completed[0]
        assert event["receipt_id"] == receipts[action.id].receipt_id
        assert event["action_attempt_number"] == 1
        assert event["mutation_version"] == action.version
        assert event["previous_state"] == "dispatching" and event["new_state"] == "succeeded"
    assert sum(row["event_type"] == "action.receipt_reconciled" for row in events) == 1
    assert sum(row["event_type"] == "approval.consumed" for row in events) == 2
    consumed_sequences = [
        row["run_sequence"] for row in events if row["event_type"] == "approval.consumed"
    ]
    call_sequences = [
        row["run_sequence"] for row in events if row["event_type"] == "action.call_started"
    ]
    assert max(consumed_sequences) < min(call_sequences)
    return receipts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupted_capability", "effects_before_interruption"),
    ((WRITE_ORDER[0], 1), (WRITE_ORDER[1], 2)),
    ids=("after-newsletter-receipt", "after-crm-receipt"),
)
async def test_ac_11_approved_email_interruption_restart_and_exact_receipt_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_capability: str,
    effects_before_interruption: int,
) -> None:
    calls = observe_real_calls(monkeypatch)
    observed_real_execute = RegistryConnectorWriteGateway.execute
    actual_results = {}
    interrupted_ids = []

    async def interrupt_after_real_receipt(gateway, authorization):
        # This invokes the AC-07 observer, which always invokes the original gateway.
        # The original mock has committed its independent receipt before returning.
        result = await observed_real_execute(gateway, authorization)
        actual_results[authorization.action.action_id] = result
        if authorization.action.capability_id == interrupted_capability and not interrupted_ids:
            interrupted_ids.append(authorization.action.action_id)
            # Cancellation is not an ordinary connector failure; leave local success
            # uncommitted and let the real worker propagate interruption.
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(RegistryConnectorWriteGateway, "execute", interrupt_after_real_receipt)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client_for(runtime) as client:
            run_id = await submit(client, EMAIL)
            assert await RunWorker(runtime, "worker.ac11.prepare").drain_once()
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            original_plan, proposed = await actions_for(runtime, run_id)
            assert len(proposed) == 2
            original_actions = {action.id: action for action in proposed}
            requests = await current_requests(runtime, run_id)
            assert {stored.request.action_id for stored in requests} == set(original_actions)
            assert {stored.status for stored in requests} == {ApprovalStatus.PENDING}
            for stored in requests:
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
            # Final approval reserves both actions but never executes inline.
            await assert_email_zero_calls(runtime, run_id, calls, RunState.EXECUTING)
        consumed = await current_requests(runtime, run_id)
        assert len(consumed) == 2 and {stored.status for stored in consumed} == {
            ApprovalStatus.CONSUMED
        }
        assert all(stored.decision is not None and stored.use is not None for stored in consumed)
        released = await business_snapshot(runtime)
        assert len(released["approval_uses"]) == 2
        clock.current += timedelta(seconds=2)
        old_ledger = runtime.email._connector_bundle.ledger
        assert old_ledger.side_effect_count == 0
        with pytest.raises(asyncio.CancelledError):
            await RunWorker(runtime, "worker.ac11.interrupted").drain_once()

        assert len(interrupted_ids) == 1
        crashed_id = interrupted_ids[0]
        assert len(calls.writes) == effects_before_interruption
        assert (
            tuple(proof.action.capability_id for proof in calls.writes)
            == WRITE_ORDER[:effects_before_interruption]
        )
        assert calls.models == calls.reads == []
        assert len(actual_results) == old_ledger.side_effect_count == effects_before_interruption
        plan, crashed_actions = await actions_for(runtime, run_id)
        assert plan == original_plan
        assert {action.id for action in crashed_actions} == set(original_actions)
        crashed = next(action for action in crashed_actions if action.id == crashed_id)
        assert crashed.state is ExternalActionState.DISPATCHING and crashed.result is None
        assert crashed.delivery_attempt_count == 1
        assert crashed.lease is not None and crashed.call_started_at is not None
        assert crashed.call_deadline_at is not None
        assert crashed.call_started_at < crashed.call_deadline_at < crashed.lease.expires_at
        assert crashed.reservation is not None
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.EXECUTING
            original_work_id = run.work_item_id
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.model_calls == 0
            assert control.tool_calls == effects_before_interruption
            assert control.deadline_at is not None
            recovery_at = max(crashed.call_deadline_at, crashed.lease.expires_at)
            assert recovery_at < control.deadline_at
            assert not await uow.artifacts.list_for_run(run_id)
            for action in crashed_actions:
                assert action_identity(action) == action_identity(original_actions[action.id])
                stored = next(item for item in consumed if item.request.action_id == action.id)
                assert stored.use is not None and action.reservation is not None
                assert action.reservation.reservation_id == stored.use.reservation_id
                receipt = await uow.connector_receipts.get(
                    action.connector_binding_id, action.idempotency_key
                )
                step = await uow.run_steps.get(action.step_id)
                assert step is not None
                if action.id == crashed_id:
                    assert receipt is not None and step.state is StepState.EXECUTING
                    assert receipt.external_action_id == crashed_id
                    assert receipt.action_hash == action.action_hash
                    assert receipt.receipt_id == actual_results[crashed_id].receipt_id
                    assert receipt.safe_metadata == actual_results[crashed_id].safe_metadata
                elif effects_before_interruption == 2:
                    assert action.state is ExternalActionState.SUCCEEDED
                    assert receipt is not None and action.result is not None
                    assert step.state is StepState.SUCCEEDED
                    assert receipt.receipt_id == action.result.receipt_id
                else:
                    assert action.state is ExternalActionState.DISPATCH_RESERVED
                    assert action.result is None and receipt is None
                    assert action.delivery_attempt_count == 0 and step.state is StepState.READY
        interrupted = await business_snapshot(runtime)
        assert_authority_unchanged(interrupted, released)
        assert len(interrupted["connector_action_receipts"]) == effects_before_interruption
        assert len(interrupted["external_action_dispatch_attempts"]) == effects_before_interruption
        (crashed_attempt,) = [
            row
            for row in interrupted["external_action_dispatch_attempts"]
            if row["external_action_id"] == crashed_id
        ]
        assert crashed_attempt["attempt_number"] == 1
        assert crashed_attempt["conclusion"] is None and crashed_attempt["completed_at"] is None
        assert crashed_attempt["call_started_at"] == crashed.call_started_at
        assert crashed_attempt["call_deadline_at"] == crashed.call_deadline_at
        assert interrupted["execution_attempts"] == interrupted["artifacts"] == ()
        persisted_receipts_before_recovery = interrupted["connector_action_receipts"]

        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert runtime.email._connector_bundle.ledger is not old_ledger
        assert runtime.email._connector_bundle.ledger.side_effect_count == 0
        assert await business_snapshot(runtime) == interrupted
        # Worker run claims are released with a one-second delay. This exact call
        # deadline is later, but remains before the action's own dispatch lease.
        clock.current = crashed.call_deadline_at
        assert clock.current < recovery_at
        assert await RunWorker(runtime, "worker.ac11.too-early").drain_once()
        assert await business_snapshot(runtime) == interrupted
        assert len(calls.writes) == effects_before_interruption
        assert calls.models == calls.reads == []
        assert runtime.email._connector_bundle.ledger.side_effect_count == 0

        clock.current = recovery_at
        assert await RunWorker(runtime, "worker.ac11.recovered").drain_once()
        assert not await RunWorker(runtime, "worker.ac11.recovered-idle").drain_once()
        assert len(calls.writes) == 2 and len(calls.models) == 1 and calls.reads == []
        worker_calls = tuple(calls.writes)
        worker_results = dict(actual_results)
        assert set(worker_results) == set(original_actions)
        assert old_ledger.side_effect_count == effects_before_interruption
        new_ledger = runtime.email._connector_bundle.ledger
        assert new_ledger.side_effect_count == 2 - effects_before_interruption
        assert old_ledger.side_effect_count + new_ledger.side_effect_count == 2
        artifact = await persisted_artifact(runtime, run_id, EMAIL, clock)
        await assert_artifact_api(runtime, artifact)
        assert artifact.payload["email_send_status"] == "not_sent"
        assert artifact.provenance.work_item_id == original_work_id
        receipts = await assert_completed_lineage(
            runtime, run_id, original_actions, consumed, worker_calls, artifact, crashed_id
        )
        completed = await business_snapshot(runtime)
        assert_authority_unchanged(completed, released)
        assert all(
            row in completed["connector_action_receipts"]
            for row in persisted_receipts_before_recovery
        )
        assert {row["id"] for row in completed["run_steps"]} == {
            row["id"] for row in released["run_steps"]
        }
        assert completed["run_plans"] == released["run_plans"]
        terminal_audits = await audit_rows(runtime, run_id)

        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == completed
        assert runtime.email._connector_bundle.ledger.side_effect_count == 0
        assert not await RunWorker(runtime, "worker.ac11.terminal-restart").drain_once()
        assert await audit_rows(runtime, run_id) == terminal_audits
        async with client_for(runtime) as client:
            # Reuse precisely the admission key/body from the AC-07 submit helper.
            replay = await browser_request(
                client,
                "POST",
                f"/api/v1/demo-scenarios/{EMAIL}/runs",
                json={},
                headers={"Idempotency-Key": f"ac07-{EMAIL}"},
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["disposition"] == "replayed"
            assert replay.json()["runId"] == run_id
            assert replay.json()["workId"] == original_work_id
            inspection = await client.get(f"/api/v1/runs/{run_id}")
            assert inspection.status_code == 200 and inspection.json()["state"] == "completed"
        assert not await RunWorker(runtime, "worker.ac11.terminal-replay").drain_once()
        # Manual receipt replay appends exactly two linked ingress/work events.
        # Its run timeline allocator advances, but all other business facts stay
        # exact; do not silently exclude the run record from the comparison.
        after_replay = deepcopy(completed)
        assert len(after_replay["runs"]) == 1 and after_replay["runs"][0]["id"] == run_id
        prior_sequence = completed["runs"][0]["next_timeline_sequence"]
        after_replay["runs"][0]["next_timeline_sequence"] = prior_sequence + 2
        assert await business_snapshot(runtime) == after_replay
        replay_audits = await audit_rows(runtime, run_id)
        assert replay_audits[:-2] == terminal_audits
        assert tuple(row["event_type"] for row in replay_audits[-2:]) == (
            "ingress.manual_received",
            "work.duplicate_returned",
        )
        assert tuple(row["run_sequence"] for row in replay_audits[-2:]) == (
            prior_sequence + 1,
            prior_sequence + 2,
        )
        assert all(
            row["safe_metadata"]["work_item_id"] == original_work_id
            and row["safe_metadata"]["receipt_disposition"] == "replayed"
            and row["run_id"] == run_id
            for row in replay_audits[-2:]
        )
        assert (
            replay_audits[-2]["safe_metadata"]["manual_attempt_id"]
            == replay_audits[-1]["safe_metadata"]["manual_attempt_id"]
        )
        completed = after_replay
        assert tuple(calls.writes) == worker_calls and len(calls.models) == 1
        assert calls.reads == []
        await assert_completed_lineage(
            runtime, run_id, original_actions, consumed, worker_calls, artifact, crashed_id
        )

        # Separate connector-level retry phase: transport invocation may repeat,
        # but the genuine captured proof/key still maps to exactly one mock effect.
        # This is not a second human approval or a new worker dispatch permission.
        retry_bundle = build_durable_connector_bundle(
            runtime.catalog,
            unit_of_work_factory=runtime.dependencies.unit_of_work_factory,
            clock=runtime.dependencies.clock,
        )
        assert retry_bundle.ledger.side_effect_count == 0
        gateway = RegistryConnectorWriteGateway(
            retry_bundle.registry,
            retry_bundle,
            binding_configuration_revisions={
                action.connector_binding_id: action.delivery_contract.binding_configuration_revision
                for action in original_actions.values()
            },
        )
        retry_start = len(calls.writes)
        before_retry_audits = await audit_rows(runtime, run_id)
        for _ in range(2):
            for proof in worker_calls:
                repeated = await gateway.execute(proof)
                receipt = receipts[proof.action.action_id]
                assert repeated == worker_results[proof.action.action_id]
                assert repeated.receipt_id == receipt.receipt_id
                assert repeated.status == receipt.status
                assert repeated.safe_metadata == receipt.safe_metadata
        transport_retries = calls.writes[retry_start:]
        assert len(transport_retries) == 4
        assert Counter(proof.action.action_id for proof in transport_retries) == Counter(
            {action_id: 2 for action_id in original_actions}
        )
        assert tuple(calls.writes[:retry_start]) == worker_calls
        assert retry_start == 2 and len(calls.writes) == 6
        assert retry_bundle.ledger.side_effect_count == 0
        assert runtime.email._connector_bundle.ledger.side_effect_count == 0
        assert old_ledger.side_effect_count + new_ledger.side_effect_count == 2
        assert await business_snapshot(runtime) == completed
        assert await audit_rows(runtime, run_id) == before_retry_audits
        assert not await RunWorker(runtime, "worker.ac11.after-transport-retry").drain_once()
        assert len(calls.writes) == 6 and len(calls.models) == 1 and calls.reads == []
        assert await business_snapshot(runtime) == completed
    finally:
        await runtime.close()
