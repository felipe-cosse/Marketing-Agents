"""AC-14: business history -> durable witnesses -> paginated public audit views.

The completeness oracle starts from independent persisted transition, approval,
attempt and artifact facts, not from the audit rows it is supposed to verify.
All journeys use public commands, the default worker and genuine mock providers.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import ApprovalStatus, RunState
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
)
from tests.acceptance.test_ac_10_webhook_replay import business_snapshot
from tests.acceptance.test_ac_11_mock_crash_recovery import audit_rows, client_for
from tests.support.api import browser_request

NAME_CANARY = "AcFourteen Private Person"
EMAIL_CANARY = "ac14-private-person@example.test"
CONTENT_CANARY = "ac14-private-content-never-in-audit"
REASON_CANARY = "ac14-private-reason-token-do-not-disclose"
CANARIES = (NAME_CANARY, EMAIL_CANARY, CONTENT_CANARY, REASON_CANARY)
PREPARE_WORKER = "worker.ac14.prepare"
EXECUTE_WORKER = "worker.ac14.execute"
EXPIRY_WORKER = "worker.ac14.expiry"


def human_context(correlation):
    return AuditContext.authenticated_user(
        "local-operator", authentication_method="local_fixed", correlation_id=correlation
    )


def assert_context(event, context):
    assert event["actor_id"] == context.actor_id
    assert event["actor_source"] == context.actor_source.value
    assert event["auth_method"] == context.auth_method
    assert event["correlation_id"] == context.correlation_id


def assert_worker(event, worker):
    expected = AuditContext.worker(worker, correlation_id="ac14.expected")
    assert event["actor_id"] == expected.actor_id
    assert event["actor_source"] == "worker" and event["auth_method"] == "internal"
    assert event["correlation_id"].startswith("audit-correlation-v1:")


async def submit_canary(client, scenario):
    overrides = (
        {"name": NAME_CANARY, "email": EMAIL_CANARY, "welcome_context": CONTENT_CANARY}
        if scenario == EMAIL
        else {"idea": CONTENT_CANARY}
    )
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/demo-scenarios/{scenario}/runs",
        json={"overrides": overrides},
        headers={"Idempotency-Key": f"ac14-{scenario}"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "accepted"
    correlation = response.headers["X-Correlation-ID"]
    assert correlation.startswith("correlation.api.")
    return response.json()["runId"], human_context(correlation)


async def decide(client, stored, decision, expected_contexts):
    request = stored.request
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/approvals/{request.id}/{decision}",
        json={
            "expected_generation": request.generation,
            "expected_payload_hash": request.action_hash,
            "reason": REASON_CANARY,
        },
    )
    assert response.status_code == 200, response.text
    context = human_context(response.headers["X-Correlation-ID"])
    expected_contexts[request.id] = context
    return context


def verify_business_witnesses(
    facts, events, *, admission_context, renewal_contexts, decision_contexts, boundary_context
):
    """Require exact 1:1 witnesses for facts whose cardinality is not audit-derived."""
    (run,) = facts["runs"]
    run_id = run["id"]
    assert events and all(event["run_id"] == run_id for event in events)
    assert [event["run_sequence"] for event in events] == list(range(1, len(events) + 1))
    # The allocator stores the last allocated sequence, despite its legacy name.
    assert run["next_timeline_sequence"] == len(events)
    witnessed = set()

    for aggregate_table, transition_table, identity_key in (
        ("runs", "run_state_transitions", "run_id"),
        ("run_steps", "run_step_state_transitions", "step_id"),
    ):
        for aggregate in facts[aggregate_table]:
            history = sorted(
                [row for row in facts[transition_table] if row[identity_key] == aggregate["id"]],
                key=lambda row: row["sequence"],
            )
            expected_versions = list(range(1, aggregate["version"] + 1))
            assert [row["sequence"] for row in history] == expected_versions
            assert [row["resulting_version"] for row in history] == expected_versions
            previous_state = None
            for row in history:
                assert row["previous_state"] == previous_state
                previous_state = row["new_state"]
            assert previous_state == aggregate["state"]
            assert history[-1]["occurred_at"] == aggregate["updated_at"]

    def witness(kind, aggregate, identity, version, **fields):
        matches = [
            event
            for event in events
            if event["event_type"] == kind
            and event["aggregate_type"] == aggregate
            and event["aggregate_id"] == identity
            and event["mutation_version"] == version
        ]
        assert len(matches) == 1, (kind, identity, version, matches)
        event = matches[0]
        assert event["outcome"] == "accepted"
        for key, value in fields.items():
            assert event[key] == value, (kind, identity, key, event[key], value)
        witnessed.add(event["id"])
        return event

    for table, aggregate, identity_key, sequence_key in (
        ("run_state_transitions", "run", "run_id", "run_transition_sequence"),
        ("run_step_state_transitions", "step", "step_id", "step_transition_sequence"),
    ):
        expected_ids = set()
        assert facts[table]
        for transition in facts[table]:
            version = transition["resulting_version"]
            assert version == transition["expected_version"] + 1
            if aggregate == "run":
                kind = (
                    "run.received"
                    if transition["sequence"] == 1
                    else "run.plan_recorded"
                    if transition["command"] == "record_plan"
                    else "run.transitioned"
                )
            else:
                kind = "step.recorded" if transition["sequence"] == 1 else "step.transitioned"
            event = witness(
                kind,
                aggregate,
                transition[identity_key],
                version,
                **{sequence_key: transition["sequence"]},
                previous_state=transition["previous_state"],
                new_state=transition["new_state"],
                reason_code=transition["reason_code"],
                occurred_at=transition["occurred_at"],
                **({"step_id": transition["step_id"]} if aggregate == "step" else {}),
            )
            expected_ids.add(event["id"])
            if kind != "step.recorded":
                assert event["safe_metadata"]["command"] == transition["command"]
            if kind == "run.received":
                assert_context(event, admission_context)
            if transition["command"] in {"release_approved_plan", "reject_approval"}:
                assert boundary_context is not None
                assert_context(event, boundary_context)
        assert expected_ids == {event["id"] for event in events if event[sequence_key] is not None}

    requests = {row["id"]: row for row in facts["approval_requests"]}
    decisions = {row["request_id"]: row for row in facts["approval_decisions"]}
    assert set(decisions) == set(decision_contexts)
    uses = {row["request_id"]: row for row in facts["approval_uses"]}
    approval_event_ids = set()
    for request_id, request in requests.items():
        binding = {
            "step_id": request["step_id"],
            "action_id": request["action_id"],
            "approval_request_id": request_id,
        }
        version = 1
        status = "pending"
        requested = witness(
            "approval.requested",
            "approval_request",
            request_id,
            version,
            **binding,
            occurred_at=request["requested_at"],
            previous_state=None,
            new_state=status,
        )
        approval_event_ids.add(requested["id"])
        assert requested["safe_metadata"]["generation"] == request["generation"]
        assert requested["safe_metadata"]["policy_id"] == request["policy_id"]
        if request_id in renewal_contexts:
            assert_context(requested, renewal_contexts[request_id])
        else:
            assert_worker(requested, PREPARE_WORKER)
        decision = decisions.get(request_id)
        if decision is not None:
            version += 1
            new_status = "approved" if decision["decision"] == "approve" else "rejected"
            decided = witness(
                f"approval.{new_status}",
                "approval_request",
                request_id,
                version,
                **binding,
                approval_decision_id=decision["id"],
                occurred_at=decision["decided_at"],
                previous_state=status,
                new_state=new_status,
                reason_code=decision["reason_code"],
            )
            approval_event_ids.add(decided["id"])
            assert decision["action_hash"] == request["action_hash"]
            assert decision["action_id"] == request["action_id"]
            assert decision["reason"] == REASON_CANARY
            context = decision_contexts[request_id]
            # Authority expectations come from the known local principal and
            # server response, never solely from the decision/audit under test.
            assert decision["actor_id"] == "local-operator"
            assert decision["authentication_method"] == "local_fixed"
            assert (
                human_context(decision["correlation_id"]).correlation_id == context.correlation_id
            )
            assert_context(decided, context)
            assert decided["safe_metadata"]["decision"] == decision["decision"]
            action_decisions = [
                event
                for event in events
                if event["event_type"] == f"action.{new_status}"
                and event["approval_decision_id"] == decision["id"]
            ]
            assert len(action_decisions) == 1
            assert_context(action_decisions[0], context)
            assert action_decisions[0]["action_id"] == request["action_id"]
            status = new_status
        for timestamp_key, event_kind, new_status in (
            ("expired_at", "approval.expired", "expired"),
            ("renewed_at", "approval.renewed", "expired"),
            ("superseded_at", "approval.superseded", "superseded"),
        ):
            if request[timestamp_key] is None:
                continue
            version += 1
            event = witness(
                event_kind,
                "approval_request",
                request_id,
                version,
                **binding,
                occurred_at=request[timestamp_key],
                previous_state=status,
                new_state=new_status,
            )
            approval_event_ids.add(event["id"])
            if event_kind == "approval.expired":
                assert_worker(event, EXPIRY_WORKER)
            if event_kind == "approval.renewed":
                replacement_id = request["replacement_request_id"]
                replacement = requests[replacement_id]
                assert event["safe_metadata"]["replacement_request_id"] == replacement_id
                assert replacement["generation"] == request["generation"] + 1
                assert replacement["action_hash"] == request["action_hash"]
                assert replacement["action_id"] == request["action_id"]
                assert_context(event, renewal_contexts[replacement_id])
            if event_kind == "approval.superseded":
                assert event["reason_code"] == request["superseded_reason_code"]
                assert boundary_context is not None
                assert_context(event, boundary_context)
            status = new_status
        use = uses.get(request_id)
        if use is not None:
            assert status == "approved" and decision is not None
            version += 1
            consumed = witness(
                "approval.consumed",
                "approval_request",
                request_id,
                version,
                **binding,
                approval_decision_id=decision["id"],
                occurred_at=use["used_at"],
                previous_state=status,
                new_state="consumed",
                reason_code="approval_consumed",
            )
            approval_event_ids.add(consumed["id"])
            assert boundary_context is not None
            assert_context(consumed, boundary_context)
            assert use["decision_id"] == decision["id"]
            assert use["action_hash"] == request["action_hash"]
            assert use["action_id"] == request["action_id"]
            assert consumed["safe_metadata"]["approval_use_id"] == use["id"]
            assert consumed["safe_metadata"]["approval_set_id"] == use["authorization_set_id"]
            assert consumed["safe_metadata"]["reservation_id"] == use["reservation_id"]
            status = "consumed"
        assert (status, version) == (request["status"], request["version"])
    assert approval_event_ids == {
        event["id"] for event in events if event["aggregate_type"] == "approval_request"
    }

    for action in facts["external_actions"]:
        history = sorted(
            [
                event
                for event in events
                if event["aggregate_type"] == "external_action"
                and event["aggregate_id"] == action["id"]
            ],
            key=lambda event: event["mutation_version"],
        )
        assert [event["mutation_version"] for event in history] == list(
            range(1, action["version"] + 1)
        )
        previous_state = None
        for event in history:
            assert event["action_id"] == action["id"] and event["step_id"] == action["step_id"]
            assert event["previous_state"] == previous_state
            previous_state = event["new_state"]
            witnessed.add(event["id"])
            if event["event_type"] == "action.dispatch_reserved":
                assert boundary_context is not None
                assert_context(event, boundary_context)
        assert history[0]["event_type"] == "action.proposed"
        assert previous_state == action["state"]
        assert history[-1]["occurred_at"] == action["updated_at"]

    attempts = {row["id"]: row for row in facts["execution_attempts"]}
    for attempt_id, attempt in attempts.items():
        assert attempt["outcome"] == "succeeded" and attempt["attempt_number"] == 1
        common = {"step_id": attempt["step_id"], "attempt_id": attempt_id}
        reserved = witness(
            "attempt.reserved",
            "execution_attempt",
            attempt_id,
            1,
            **common,
            occurred_at=attempt["reserved_at"],
            previous_state=None,
            new_state="reserved",
        )
        completed = witness(
            "attempt.completed",
            "execution_attempt",
            attempt_id,
            attempt["version"],
            **common,
            occurred_at=attempt["completed_at"],
            previous_state="reserved",
            new_state=attempt["outcome"],
            artifact_id=attempt["output_artifact_id"],
        )
        assert reserved["run_sequence"] < completed["run_sequence"]
        for event in (reserved, completed):
            assert event["safe_metadata"]["attempt_number"] == attempt["attempt_number"]
            assert event["safe_metadata"]["operation_key"] == attempt["operation_key"]
            assert_worker(event, EXECUTE_WORKER)
    assert len(
        [event for event in events if event["aggregate_type"] == "execution_attempt"]
    ) == 2 * len(attempts)
    for artifact in facts["artifacts"]:
        (attempt,) = [
            row for row in attempts.values() if row["output_artifact_id"] == artifact["id"]
        ]
        persisted = witness(
            "artifact.persisted",
            "artifact",
            artifact["id"],
            1,
            step_id=artifact["step_id"],
            artifact_id=artifact["id"],
            attempt_id=attempt["id"],
            occurred_at=artifact["created_at"],
            previous_state=None,
            new_state="persisted",
        )
        assert persisted["safe_metadata"]["output_schema_id"] == artifact["output_schema_id"]
        assert persisted["safe_metadata"]["output_schema_hash"] == artifact["output_schema_hash"]
        assert_worker(persisted, EXECUTE_WORKER)
    assert len([event for event in events if event["aggregate_type"] == "artifact"]) == len(
        facts["artifacts"]
    )

    receipts = {row["external_action_id"]: row for row in facts["connector_action_receipts"]}
    assert len(receipts) == len(facts["external_action_dispatch_attempts"])
    for attempt in facts["external_action_dispatch_attempts"]:
        action_id = attempt["external_action_id"]
        receipt = receipts[action_id]
        assert attempt["conclusion"] == "succeeded" and attempt["attempt_number"] == 1
        assert attempt["connector_receipt_id"] == receipt["receipt_id"]
        own = [event for event in events if event["action_id"] == action_id]
        for event_type, occurred_at in (
            ("action.dispatch_claimed", attempt["claimed_at"]),
            ("action.call_started", attempt["call_started_at"]),
            ("action.succeeded", attempt["completed_at"]),
        ):
            (event,) = [row for row in own if row["event_type"] == event_type]
            assert event["action_attempt_number"] == attempt["attempt_number"]
            assert event["occurred_at"] == occurred_at
            assert_worker(event, EXECUTE_WORKER)
            if event_type == "action.succeeded":
                assert event["receipt_id"] == receipt["receipt_id"]
    assert witnessed
    return witnessed


async def page(client, path, params):
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    result = response.json()
    assert len(result["items"]) <= params["limit"]
    return result


async def finish_pages(client, path, params, first):
    result = list(first["items"])
    cursor = first["next_cursor"]
    seen_cursors = set()
    while cursor is not None:
        assert cursor not in seen_cursors and len(seen_cursors) < 100
        seen_cursors.add(cursor)
        continuation = await page(client, path, {**params, "cursor": cursor})
        if "high_watermark" in first:
            assert continuation["high_watermark"] == first["high_watermark"]
        result.extend(continuation["items"])
        cursor = continuation["next_cursor"]
    assert (
        seen_cursors
    )  # Small pages must exercise actual continuation, not a single-page shortcut.
    assert len(result) == len({item["id"] for item in result})
    return result


def assert_projection(items, events, *, feed):
    ordered = (
        sorted(events, key=lambda event: event["feed_sequence"], reverse=True) if feed else events
    )
    assert [item["id"] for item in items] == [event["id"] for event in ordered]
    for item, event in zip(items, ordered, strict=True):
        assert item["sequence"] == event["feed_sequence" if feed else "run_sequence"]
        if feed:
            assert item["run_sequence"] == event["run_sequence"]
            assert item["run_id"] == event["run_id"]
            assert item["transition_sequence"] == (
                event["run_transition_sequence"] or event["step_transition_sequence"]
            )
        for key in (
            "schema_version",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "outcome",
            "actor_id",
            "actor_source",
            "auth_method",
            "correlation_id",
            "step_id",
            "action_id",
            "approval_request_id",
            "artifact_id",
            "attempted_command",
            "previous_state",
            "new_state",
            "reason_code",
            "metadata_classification",
        ):
            assert item[key] == event[key], (event["event_type"], key)
        if feed:
            for key in (
                "action_attempt_number",
                "receipt_id",
                "approval_decision_id",
                "attempt_id",
                "expected_version",
                "observed_version",
                "observed_state",
                "requested_state",
                "mutation_version",
            ):
                assert item[key] == event[key], (event["event_type"], key)
        assert datetime.fromisoformat(item["occurred_at"]) == event["occurred_at"]
        assert datetime.fromisoformat(item["metadata_expires_at"]) == event["metadata_expires_at"]
        assert item["metadata"] == event["safe_metadata"]
        assert item["metadata_expired"] is False
        run_id = event["run_id"]
        assert item["run_url"] == f"/api/v1/runs/{run_id}"
        for field, identity, prefix in (
            ("step_url", event["step_id"], f"/api/v1/runs/{run_id}/steps"),
            ("action_url", event["action_id"], "/api/v1/external-actions"),
            ("approval_url", event["approval_request_id"], "/api/v1/approvals"),
            ("artifact_url", event["artifact_id"], "/api/v1/artifacts"),
        ):
            assert item[field] == (None if identity is None else f"{prefix}/{identity}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "journey", ["read-success", "email-approved", "email-rejected", "email-renewed"]
)
async def test_ac_14_every_business_transition_and_approval_has_a_public_audit_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, journey: str
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    renewal_contexts = {}
    decision_contexts = {}
    boundary_context = None
    try:
        async with client_for(runtime) as client:
            scenario = READ_DEMOS[0][0] if journey == "read-success" else EMAIL
            run_id, admission = await submit_canary(client, scenario)
            if journey == "read-success":
                assert await RunWorker(runtime, EXECUTE_WORKER).drain_once()
            else:
                assert await RunWorker(runtime, PREPARE_WORKER).drain_once()
                await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
                requests = await current_requests(runtime, run_id)
                assert len(requests) == 2
                await decide(client, requests[0], "approve", decision_contexts)
                await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
                if journey == "email-rejected":
                    boundary_context = await decide(
                        client, requests[1], "reject", decision_contexts
                    )
                else:
                    if journey == "email-renewed":
                        clock.current = requests[0].request.expires_at
                        assert await RunWorker(runtime, EXPIRY_WORKER).drain_once()
                        expired = await current_requests(runtime, run_id)
                        assert {stored.status for stored in expired} == {ApprovalStatus.EXPIRED}
                        await assert_email_zero_calls(
                            runtime, run_id, calls, RunState.AWAITING_APPROVAL
                        )
                        for stored in expired:
                            prior = stored.request
                            response = await browser_request(
                                client,
                                "POST",
                                f"/api/v1/external-actions/{prior.action_id}/approval-requests",
                                json={
                                    "expected_generation": prior.generation,
                                    "expected_payload_hash": prior.action_hash,
                                },
                            )
                            assert response.status_code == 201, response.text
                            replacement_id = response.json()["approval"]["id"]
                            renewal_contexts[replacement_id] = human_context(
                                response.headers["X-Correlation-ID"]
                            )
                        requests = await current_requests(runtime, run_id)
                        assert {stored.request.generation for stored in requests} == {2}
                        await decide(client, requests[0], "approve", decision_contexts)
                    boundary_context = await decide(
                        client, requests[1], "approve", decision_contexts
                    )
                    await assert_email_zero_calls(runtime, run_id, calls, RunState.EXECUTING)
                    clock.current += timedelta(seconds=2)
                    assert await RunWorker(runtime, EXECUTE_WORKER).drain_once()

        facts = await business_snapshot(runtime)
        events = await audit_rows(runtime, run_id)
        terminal = "rejected" if journey == "email-rejected" else "completed"
        assert facts["runs"][0]["state"] == terminal
        assert CONTENT_CANARY in json.dumps(facts["work_items"], default=str)
        if scenario == EMAIL:
            assert EMAIL_CANARY in json.dumps(facts["work_items"], default=str)
            assert NAME_CANARY in json.dumps(facts["work_items"], default=str)
            assert all(row["reason"] == REASON_CANARY for row in facts["approval_decisions"])
        expected = {
            "read-success": (0, 0, 0, 0, 1, 1),
            "email-approved": (2, 2, 2, 2, 1, 1),
            "email-rejected": (2, 2, 0, 0, 0, 0),
            "email-renewed": (4, 3, 2, 2, 1, 1),
        }[journey]
        assert (
            tuple(
                len(facts[table])
                for table in (
                    "approval_requests",
                    "approval_decisions",
                    "approval_uses",
                    "connector_action_receipts",
                    "execution_attempts",
                    "artifacts",
                )
            )
            == expected
        )
        assert len(calls.writes) == expected[3] and len(calls.models) == expected[4]
        assert calls.reads == []
        assert max(Counter(event["occurred_at"] for event in events).values()) > 4
        witnesses = verify_business_witnesses(
            facts,
            events,
            admission_context=admission,
            renewal_contexts=renewal_contexts,
            decision_contexts=decision_contexts,
            boundary_context=boundary_context,
        )

        timeline_path = f"/api/v1/runs/{run_id}/timeline"
        feed_path = "/api/v1/audit-events"
        timeline_params, feed_params = {"limit": 3}, {"run_id": run_id, "limit": 4}
        async with client_for(runtime) as client:
            first_timeline = await page(client, timeline_path, timeline_params)
            first_feed = await page(client, feed_path, feed_params)
        assert first_timeline["next_cursor"] and first_feed["next_cursor"]
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await business_snapshot(runtime) == facts
        assert await audit_rows(runtime, run_id) == events
        async with client_for(runtime) as client:
            timeline = await finish_pages(client, timeline_path, timeline_params, first_timeline)
            feed = await finish_pages(client, feed_path, feed_params, first_feed)
        assert_projection(timeline, events, feed=False)
        assert_projection(feed, events, feed=True)
        assert witnesses <= {item["id"] for item in timeline} == {item["id"] for item in feed}
        assert all(item["run_id"] == run_id for item in feed)
        rendered = json.dumps((events, timeline, feed), default=str) + caplog.text
        for canary in CANARIES:
            assert canary not in rendered
        assert await business_snapshot(runtime) == facts
        assert await audit_rows(runtime, run_id) == events
        clock.current += timedelta(seconds=2)
        assert not await RunWorker(runtime, "worker.ac14.terminal-idle").drain_once()
        assert await business_snapshot(runtime) == facts
        assert await audit_rows(runtime, run_id) == events
    finally:
        await runtime.close()
