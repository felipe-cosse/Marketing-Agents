"""AC-09: real public approval rejection, durable exact renewal, and safe recovery.

No product responses, providers, approval services, or repositories are stubbed.
Corruption is injected only into each test's private temporary database.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.domain.enums import ApprovalStatus, ExternalActionState, RunState
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
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
    RunRecord,
    RunStepRecord,
)
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from sqlalchemy import select, update

from tests.acceptance.test_ac_07_real_composition_demos import (
    EMAIL,
    Clock,
    assert_email_zero_calls,
    current_requests,
    installation,
    observe_real_calls,
    submit,
)
from tests.support.api import browser_request


def client_for(runtime):
    return AsyncClient(
        transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
    )


async def approve(client, request):
    return await browser_request(
        client,
        "POST",
        f"/api/v1/approvals/{request.id}/approve",
        json={
            "expected_generation": request.generation,
            "expected_payload_hash": request.action_hash,
        },
    )


async def request_again(client, request, *, generation):
    return await browser_request(
        client,
        "POST",
        f"/api/v1/external-actions/{request.action_id}/approval-requests",
        json={"expected_generation": generation, "expected_payload_hash": request.action_hash},
    )


def conflict(response, code, *, status=409):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"


async def actions_for(runtime, run_id):
    async with runtime.dependencies.unit_of_work() as uow:
        plan = await uow.run_steps.get_plan(run_id)
        assert plan is not None
        return await uow.external_actions.list_run_plan(run_id, plan.plan_hash)


async def raw_authority_snapshot(runtime, run_id):
    """Bounded raw evidence works even when deliberate corruption blocks hydration.

    Capture exact approval/action/run/step/set/head/member facts and lifecycle
    audits, not unrelated session or worker-claim bookkeeping.
    """
    action_ids = select(ExternalActionRecord.id).where(ExternalActionRecord.run_id == run_id)
    selectors = (
        (RunRecord, RunRecord.id == run_id),
        (RunStepRecord, RunStepRecord.run_id == run_id),
        (ExternalActionRecord, ExternalActionRecord.run_id == run_id),
        (ApprovalRequestRecord, ApprovalRequestRecord.run_id == run_id),
        (ApprovalDecisionRecord, ApprovalDecisionRecord.run_id == run_id),
        (ApprovalUseRecord, ApprovalUseRecord.run_id == run_id),
        (AuthorizationSetRecord, AuthorizationSetRecord.run_id == run_id),
        (AuthorizationSetHeadRecord, AuthorizationSetHeadRecord.run_id == run_id),
        (AuthorizationSetMemberRecord, AuthorizationSetMemberRecord.run_id == run_id),
        (ArtifactRecord, ArtifactRecord.run_id == run_id),
        (ExecutionAttemptRecord, ExecutionAttemptRecord.run_id == run_id),
        (
            ExternalActionDispatchAttemptRecord,
            ExternalActionDispatchAttemptRecord.external_action_id.in_(action_ids),
        ),
        (
            ConnectorActionReceiptRecord,
            ConnectorActionReceiptRecord.external_action_id.in_(action_ids),
        ),
        (
            AuditEventRecord,
            (AuditEventRecord.run_id == run_id)
            & (
                AuditEventRecord.event_type.like("approval.%")
                | AuditEventRecord.event_type.like("action.%")
                | AuditEventRecord.event_type.like("external_action.%")
                | (AuditEventRecord.event_type == "connector.receipt_committed")
            ),
        ),
    )
    result = {}
    async with runtime.database.session_factory() as session:
        for model, predicate in selectors:
            rows = (
                await session.scalars(
                    select(model).where(predicate).order_by(*model.__table__.primary_key.columns)
                )
            ).all()
            result[model.__tablename__] = tuple(
                tuple(
                    (column.name, deepcopy(getattr(row, column.name)))
                    for column in model.__table__.columns
                )
                for row in rows
            )
    return result


async def prepare(runtime, client, calls):
    run_id = await submit(client, EMAIL)
    assert await RunWorker(runtime, "worker.ac09.prepare").drain_once()
    await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
    requests = await current_requests(runtime, run_id)
    assert len(requests) == 2 and {item.status for item in requests} == {ApprovalStatus.PENDING}
    return run_id, tuple(item.request for item in requests)


async def complete_after_fresh_approvals(runtime, run_id, calls, clock, expected_current_ids):
    """Nonvacuous control: rejected attempts must not prevent legitimate completion."""
    await assert_email_zero_calls(runtime, run_id, calls, RunState.EXECUTING)
    consumed = await current_requests(runtime, run_id)
    assert {item.request.id for item in consumed} == set(expected_current_ids)
    assert {item.status for item in consumed} == {ApprovalStatus.CONSUMED}
    assert all(item.decision is not None and item.use is not None for item in consumed)
    clock.current += timedelta(seconds=2)
    assert await RunWorker(runtime, "worker.ac09.approved").drain_once()
    assert len(calls.writes) == 2 and len(calls.models) == 1 and calls.reads == []
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        artifacts = await uow.artifacts.list_for_run(run_id)
        assert run is not None and run.state is RunState.COMPLETED
        assert len(artifacts) == 1 and artifacts[0].verify_payload()
        assert artifacts[0].payload["artifact_type"] == "email_onboarding_summary"
        assert artifacts[0].payload["email_send_status"] == "not_sent"
    actions = await actions_for(runtime, run_id)
    assert len(actions) == 2 and {action.state for action in actions} == {
        ExternalActionState.SUCCEEDED
    }
    for action in actions:
        stored = next(item for item in consumed if item.request.action_id == action.id)
        assert action.result is not None and stored.decision is not None and stored.use is not None
        proofs = [proof for proof in calls.writes if proof.action.action_id == action.id]
        assert len(proofs) == 1
        proof = proofs[0]
        assert proof.action == action.envelope
        assert proof.action_hash == action.action_hash == stored.request.action_hash
        assert proof.approval_request_id == stored.request.id
        assert proof.approval_decision_id == stored.decision.id
        assert proof.reservation_id == stored.use.reservation_id
        assert proof.idempotency_key == action.idempotency_key
        async with runtime.dependencies.unit_of_work() as uow:
            receipt = await uow.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
            assert receipt is not None
            assert receipt.external_action_id == action.id
            assert receipt.action_hash == action.action_hash
            assert receipt.receipt_id == action.result.receipt_id
    async with runtime.database.session_factory() as session:
        assert len((await session.scalars(select(ConnectorActionReceiptRecord))).all()) == 2
        attempts = (await session.scalars(select(ExternalActionDispatchAttemptRecord))).all()
        assert len(attempts) == 2
        assert all(item.attempt_number == 1 and item.conclusion == "succeeded" for item in attempts)


@pytest.mark.asyncio
async def test_ac_09_public_reused_decision_rejected_before_and_after_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client_for(runtime) as client:
            run_id, (first, second) = await prepare(runtime, client, calls)
            accepted = await approve(client, first)
            assert accepted.status_code == 200, accepted.text
            before = await raw_authority_snapshot(runtime, run_id)
            conflict(await approve(client, first), "approval_decision_conflict")
            assert await raw_authority_snapshot(runtime, run_id) == before
            current = await current_requests(runtime, run_id)
            approved = next(item for item in current if item.request.id == first.id)
            sibling = next(item for item in current if item.request.id == second.id)
            assert approved.decision is not None
            assert approved.decision.id == accepted.json()["decision_id"]
            assert approved.status is ApprovalStatus.APPROVED and approved.use is None
            assert sibling.status is ApprovalStatus.PENDING and sibling.decision is None
            clock.current += timedelta(seconds=2)
            assert await RunWorker(runtime, "worker.ac09.reused-partial").drain_once()
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            accepted_second = await approve(client, second)
            assert accepted_second.status_code == 200, accepted_second.text
        await complete_after_fresh_approvals(runtime, run_id, calls, clock, (first.id, second.id))

        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        before = await raw_authority_snapshot(runtime, run_id)
        async with client_for(runtime) as client:
            for consumed in (first, second):
                conflict(await approve(client, consumed), "approval_decision_conflict")
        assert await raw_authority_snapshot(runtime, run_id) == before
        assert not await RunWorker(runtime, "worker.ac09.reused-completed").drain_once()
        assert len(calls.writes) == 2 and len(calls.models) == 1 and calls.reads == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ac_09_exact_expiry_rejects_and_unchanged_renewal_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client_for(runtime) as client:
            run_id, old = await prepare(runtime, client, calls)
            first, second = old
            original_actions = await actions_for(runtime, run_id)
            original_envelopes = {action.id: action.envelope for action in original_actions}
            assert (await approve(client, first)).status_code == 200
            # The fixed planning clock gives both leaves the same exact expiry.
            assert len({request.expires_at for request in old}) == 1
            clock.current = first.expires_at
            before = await raw_authority_snapshot(runtime, run_id)
            conflict(await approve(client, second), "approval_expired")
            conflict(await request_again(client, first, generation=0), "approval_expired")
            assert await raw_authority_snapshot(runtime, run_id) == before
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            assert await RunWorker(runtime, "worker.ac09.expiry").drain_once()
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            expired = await current_requests(runtime, run_id)
            assert {item.status for item in expired} == {ApprovalStatus.EXPIRED}
            assert all(item.expired_at == clock.now() and item.use is None for item in expired)
            actions = await actions_for(runtime, run_id)
            assert {action.state for action in actions} == {ExternalActionState.AWAITING_APPROVAL}
            assert all(action.reservation is None for action in actions)
            assert {action.id: action.envelope for action in actions} == original_envelopes
            async with runtime.database.session_factory() as session:
                expiry_events = (
                    await session.scalars(
                        select(AuditEventRecord).where(
                            AuditEventRecord.run_id == run_id,
                            AuditEventRecord.event_type == "approval.expired",
                        )
                    )
                ).all()
                assert {event.approval_request_id for event in expiry_events} == {
                    request.id for request in old
                }
                assert len(expiry_events) == 2

        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
        replacements = {}
        async with client_for(runtime) as client:
            # Durable EXPIRED status takes precedence over clock expiry for reuse.
            conflict(await request_again(client, first, generation=0), "approval_request_conflict")
            for prior in old:
                renewal = await request_again(client, prior, generation=prior.generation)
                assert renewal.status_code == 201, renewal.text
                body = renewal.json()
                assert body["disposition"] == "renewed"
                projected = body["approval"]
                assert projected["id"] != prior.id
                assert projected["generation"] == prior.generation + 1
                assert projected["action_id"] == prior.action_id
                assert projected["payload_hash"] == prior.action_hash
                assert (
                    projected["status"] == "pending" and projected["one_time_use_state"] == "unused"
                )
                assert projected["is_actionable"] and not projected["is_expired"]
                assert all(
                    projected[key] is None
                    for key in (
                        "decision_id",
                        "decision_kind",
                        "decided_at",
                        "consumed_at",
                        "expired_at",
                        "replacement_approval_id",
                        "renewed_at",
                        "superseded_at",
                    )
                )
                assert renewal.headers["location"] == projected["approval_url"]
                replay_before = await raw_authority_snapshot(runtime, run_id)
                replay = await request_again(client, prior, generation=prior.generation)
                assert replay.status_code == 200, replay.text
                assert replay.json() == {"disposition": "existing", "approval": projected}
                assert await raw_authority_snapshot(runtime, run_id) == replay_before
                conflict(await approve(client, prior), "approval_decision_conflict")
                async with runtime.dependencies.unit_of_work() as uow:
                    source = await uow.approvals.get(prior.id)
                    replacement = await uow.approvals.get(projected["id"])
                assert source is not None and replacement is not None
                assert source.status is ApprovalStatus.EXPIRED
                assert source.replacement_request_id == replacement.request.id
                assert source.use is None and source.renewed_at == clock.now()
                current = replacement.request
                assert (
                    current.action_id,
                    current.action_hash,
                    current.authorization_set_id,
                    current.plan_hash,
                    current.proposal_revision,
                    current.step_id,
                ) == (
                    prior.action_id,
                    prior.action_hash,
                    prior.authorization_set_id,
                    prior.plan_hash,
                    prior.proposal_revision,
                    prior.step_id,
                )
                assert replacement.status is ApprovalStatus.PENDING
                assert replacement.decision is None and replacement.use is None
                replacements[prior.id] = current
            async with runtime.database.session_factory() as session:
                assert len((await session.scalars(select(ApprovalRequestRecord))).all()) == 4
                assert len((await session.scalars(select(AuthorizationSetRecord))).all()) == 1
                assert len((await session.scalars(select(ExternalActionRecord))).all()) == 2
            renewed_first, renewed_second = replacements[first.id], replacements[second.id]
            assert (await approve(client, renewed_first)).status_code == 200
            clock.current += timedelta(seconds=2)
            assert await RunWorker(runtime, "worker.ac09.renewed-partial").drain_once()
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            assert (await approve(client, renewed_second)).status_code == 200
        await complete_after_fresh_approvals(
            runtime, run_id, calls, clock, (renewed_first.id, renewed_second.id)
        )
        assert {
            action.id: action.envelope for action in await actions_for(runtime, run_id)
        } == original_envelopes
    finally:
        await runtime.close()


@pytest.mark.parametrize("field", ("destination", "minimized_payload", "binding_id"))
@pytest.mark.asyncio
async def test_ac_09_changed_persisted_approved_action_cannot_release_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    canary = f"ac09-corruption-{field}-must-not-be-reflected"
    try:
        async with client_for(runtime) as client:
            run_id, (approved, sibling) = await prepare(runtime, client, calls)
            assert (await approve(client, approved)).status_code == 200
            # Fault injection, not a supported public action edit: keep old hashes
            # and decision while changing one persisted canonical envelope field.
            async with runtime.database.session_factory() as session, session.begin():
                row = await session.get(ExternalActionRecord, approved.action_id)
                assert row is not None
                changed = deepcopy(row.canonical_envelope)
                if field == "minimized_payload":
                    changed[field]["contact_ref"] = canary
                else:
                    changed[field] = canary
                await session.execute(
                    update(ExternalActionRecord)
                    .where(ExternalActionRecord.id == approved.action_id)
                    .values(canonical_envelope=changed)
                )
            before = await raw_authority_snapshot(runtime, run_id)
            denied = await approve(client, sibling)
            conflict(denied, "approval_conflict")
            assert canary not in denied.text
            assert await raw_authority_snapshot(runtime, run_id) == before
            # Corrupt actions cannot hydrate through domain repositories. Bounded
            # raw checks prove that rejection neither releases nor creates effects.
            async with runtime.database.session_factory() as session:
                run = await session.get(RunRecord, run_id)
                actions = (
                    await session.scalars(
                        select(ExternalActionRecord).where(ExternalActionRecord.run_id == run_id)
                    )
                ).all()
                requests = (
                    await session.scalars(
                        select(ApprovalRequestRecord).where(ApprovalRequestRecord.run_id == run_id)
                    )
                ).all()
                decisions = (
                    await session.scalars(
                        select(ApprovalDecisionRecord).where(
                            ApprovalDecisionRecord.run_id == run_id
                        )
                    )
                ).all()
                sets = (
                    await session.scalars(
                        select(AuthorizationSetRecord).where(
                            AuthorizationSetRecord.run_id == run_id
                        )
                    )
                ).all()
                assert run is not None and run.state == "awaiting_approval"
                assert len(actions) == len(requests) == 2
                assert {action.state for action in actions} == {"approved", "awaiting_approval"}
                assert all(
                    action.reservation_id is None and action.connector_receipt_id is None
                    for action in actions
                )
                assert {request.status for request in requests} == {"approved", "pending"}
                assert all(request.generation == 1 for request in requests)
                assert len(decisions) == 1 and decisions[0].request_id == approved.id
                assert len(sets) == 1 and sets[0].status == "open" and sets[0].release_hash is None
                for model in (
                    ApprovalUseRecord,
                    ExternalActionDispatchAttemptRecord,
                    ConnectorActionReceiptRecord,
                    ArtifactRecord,
                    ExecutionAttemptRecord,
                ):
                    assert not (await session.scalars(select(model))).all()
            assert calls.models == calls.reads == calls.writes == []
        # Never repair this corrupted database or manufacture a replacement leaf.
        # Successful reuse/renewal controls are the separate real journeys above.
    finally:
        await runtime.close()
