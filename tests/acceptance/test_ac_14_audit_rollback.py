"""AC-14: real public mutations roll back when their already-flushed audit fails.

Every case installs the migrated, seeded default runtime. The only failure seam
is after the real audit repository has appended and flushed its records in the
mutation transaction. No approval, lifecycle, provider or HTTP result is faked.
This is SQLite transaction evidence, not a process-kill or PostgreSQL claim.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.domain.enums import ApprovalStatus, RunState
from marketing_agents.infrastructure.db import Base, SQLAlchemyAuditRepository
from marketing_agents.workers.runtime.composition import build_runtime
from sqlalchemy import select

from tests.acceptance.test_ac_07_real_composition_demos import (
    EMAIL,
    Clock,
    assert_email_zero_calls,
    current_requests,
    installation,
    observe_real_calls,
    submit,
)
from tests.acceptance.test_ac_09_approval_rejection import approve, prepare
from tests.support.api import browser_request

FAULT_CANARY = "ac14-private-audit-failure-canary"


async def snapshot_in_session(session):
    """Include every ORM table, not only business projections: no audit/allocator exclusions."""
    result = {}
    for table in Base.metadata.sorted_tables:
        rows = (
            (await session.execute(select(table).order_by(*table.primary_key.columns)))
            .mappings()
            .all()
        )
        result[table.name] = tuple(deepcopy(dict(row)) for row in rows)
    return result


async def full_snapshot(runtime):
    async with runtime.database.session_factory() as session:
        return await snapshot_in_session(session)


def own_run(snapshot, run_id):
    (run,) = [row for row in snapshot["runs"] if row["id"] == run_id]
    return run


def run_audits(snapshot, run_id):
    return tuple(
        sorted(
            (row for row in snapshot["audit_events"] if row["run_id"] == run_id),
            key=lambda row: row["run_sequence"],
        )
    )


def mutation_facts(events):
    """A retry gets fresh correlation/decision IDs, but witnesses the same ordered mutations."""
    fields = (
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "run_id",
        "step_id",
        "action_id",
        "approval_request_id",
        "mutation_version",
        "previous_state",
        "new_state",
        "run_transition_sequence",
        "step_transition_sequence",
        "run_sequence",
        "global_sequence",
        "feed_sequence",
        "actor_id",
        "actor_source",
        "auth_method",
    )
    return tuple(tuple(event[field] for field in fields) for event in events)


def assert_dense_allocators(snapshot, run_id):
    events = snapshot["audit_events"]
    assert events
    assert [row["global_sequence"] for row in events] == list(range(1, len(events) + 1))
    assert sorted(row["feed_sequence"] for row in events) == list(range(1, len(events) + 1))
    assert len({row["id"] for row in events}) == len(events)
    assert snapshot["audit_feed_sequence"] == ({"singleton_id": 1, "last_sequence": len(events)},)
    timeline = run_audits(snapshot, run_id)
    assert [row["run_sequence"] for row in timeline] == list(range(1, len(timeline) + 1))
    assert own_run(snapshot, run_id)["next_timeline_sequence"] == len(timeline)


class FailAfterRealAudit:
    def __init__(self, *, run_id, boundary):
        self.run_id = run_id
        self.boundary = boundary
        self.original = SQLAlchemyAuditRepository.append_many
        self.batches = []
        self.staged = []
        self.inserted_ids = []
        self.in_transaction = []

    def matches(self, events):
        if self.boundary == "first-approval":
            return any(event.event_type == "approval.approved" for event in events)
        if self.boundary == "final-approval":
            return any(event.event_type == "approval.consumed" for event in events)
        return any(
            event.event_type == "run.transitioned"
            and event.new_state == "cancelled"
            and event.safe_metadata.values.get("command") == "cancel"
            for event in events
        )

    def install(self, monkeypatch):
        async def append_then_fail(repository, events):
            inserted = await self.original(repository, events)
            self.batches.append(tuple(event.event_type for event in events))
            if events[0].run_id == self.run_id and self.matches(events):
                # The production append has already flushed its real INSERTs.
                # Capture SQL-visible staged mutations before raising, so a
                # pre-mutation error cannot satisfy the rollback assertions.
                self.in_transaction.append(repository._session.in_transaction())
                self.inserted_ids.append(tuple(event.id for event in inserted))
                self.staged.append(await snapshot_in_session(repository._session))
                raise RuntimeError(FAULT_CANARY)
            return inserted

        monkeypatch.setattr(SQLAlchemyAuditRepository, "append_many", append_then_fail)

    def restore(self, monkeypatch):
        monkeypatch.setattr(SQLAlchemyAuditRepository, "append_many", self.original)


def assert_real_staged_mutation(before, fault, *, run_id, boundary):
    assert len(fault.staged) == len(fault.inserted_ids) == 1
    assert fault.in_transaction == [True]
    staged = fault.staged[0]
    assert staged != before
    assert set(fault.inserted_ids[0]) <= {row["id"] for row in staged["audit_events"]}
    assert set(fault.inserted_ids[0]).isdisjoint(row["id"] for row in before["audit_events"])
    assert len(staged["audit_events"]) > len(before["audit_events"])
    assert (
        own_run(staged, run_id)["next_timeline_sequence"]
        > own_run(before, run_id)["next_timeline_sequence"]
    )
    assert (
        staged["audit_feed_sequence"][0]["last_sequence"]
        > before["audit_feed_sequence"][0]["last_sequence"]
    )
    assert_dense_allocators(staged, run_id)
    if boundary in {"first-approval", "final-approval"}:
        assert len(staged["approval_decisions"]) == len(before["approval_decisions"]) + 1
        assert staged["approval_requests"] != before["approval_requests"]
        assert staged["external_actions"] != before["external_actions"]
        if boundary == "first-approval":
            assert len(fault.batches) == 1
            assert Counter(fault.batches[0]) == {"action.approved": 1, "approval.approved": 1}
            assert own_run(staged, run_id)["state"] == "awaiting_approval"
            assert staged["approval_uses"] == ()
        else:
            assert len(fault.batches) == 2
            assert Counter(fault.batches[0]) == {"action.approved": 1, "approval.approved": 1}
            assert Counter(fault.batches[-1])["approval.consumed"] == 2
            assert len(staged["approval_uses"]) == 2 and before["approval_uses"] == ()
            assert own_run(staged, run_id)["state"] == "executing"
            assert staged["authorization_sets"][0]["status"] == "released"
            assert {row["state"] for row in staged["external_actions"]} == {"dispatch_reserved"}
            for table in ("run_state_transitions", "run_step_state_transitions"):
                assert len(staged[table]) > len(before[table]), table
    else:
        assert len(fault.batches) == 1
        assert own_run(staged, run_id)["state"] == "cancelled"
        assert own_run(staged, run_id)["version"] == own_run(before, run_id)["version"] + 1
        assert len(staged["run_state_transitions"]) == len(before["run_state_transitions"]) + 1
        assert staged["run_state_transitions"][-1]["command"] == "cancel"
        if boundary != "queued-cancellation":
            assert {row["state"] for row in staged["run_steps"]} == {"cancelled"}
            assert {row["state"] for row in staged["external_actions"]} == {"cancelled"}
            assert staged["run_execution_controls"][0]["cancel_requested_at"] is not None
            assert len(staged["run_step_state_transitions"]) > len(
                before["run_step_state_transitions"]
            )
    for table in (
        "execution_attempts",
        "external_action_dispatch_attempts",
        "connector_action_receipts",
        "artifacts",
    ):
        assert staged[table] == before[table] == (), table


async def assert_public_timeline(client, snapshot, run_id):
    timeline = []
    cursor = None
    for _ in range(20):
        params = {"limit": 7}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(f"/api/v1/runs/{run_id}/timeline", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        timeline.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    durable = run_audits(snapshot, run_id)
    assert [(event["id"], event["sequence"]) for event in timeline] == [
        (event["id"], event["run_sequence"]) for event in durable
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "first-approval",
        "final-approval",
        "queued-cancellation",
        "prebarrier-cancellation",
        "released-cancellation",
    ),
)
async def test_ac_14_public_mutation_and_flushed_audit_rollback_then_single_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app(), raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            if boundary == "queued-cancellation":
                run_id = await submit(client, EMAIL)
                requests = ()
            else:
                run_id, requests = await prepare(runtime, client, calls)
                if boundary != "first-approval":
                    first = await approve(client, requests[0])
                    assert first.status_code == 200, first.text
                if boundary == "released-cancellation":
                    second = await approve(client, requests[1])
                    assert second.status_code == 200, second.text

            async def mutate():
                if boundary in {"first-approval", "final-approval"}:
                    return await approve(client, requests[boundary == "final-approval"])
                return await browser_request(
                    client, "POST", f"/api/v1/runs/{run_id}/cancel", json={}
                )

            before = await full_snapshot(runtime)
            assert_dense_allocators(before, run_id)
            fault = FailAfterRealAudit(run_id=run_id, boundary=boundary)
            fault.install(monkeypatch)
            failed = await mutate()
            assert failed.status_code == 503, failed.text
            assert failed.headers["content-type"].startswith("application/problem+json")
            assert failed.headers["cache-control"] == "no-store"
            assert FAULT_CANARY not in failed.text
            assert_real_staged_mutation(before, fault, run_id=run_id, boundary=boundary)
            # A fresh connection sees neither staged business facts nor audit
            # INSERTs, and both allocators (plus all unrelated rows) are exact.
            assert await full_snapshot(runtime) == before
            await assert_public_timeline(client, before, run_id)
            assert await full_snapshot(runtime) == before
            assert calls.models == calls.reads == calls.writes == []

            fault.restore(monkeypatch)
            successful = await mutate()
            assert successful.status_code == 200, successful.text
            assert len(fault.staged) == 1  # Only the rejected transaction was injected.
            after = await full_snapshot(runtime)
            assert_dense_allocators(after, run_id)
            assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
            old_ids = {row["id"] for row in before["audit_events"]}
            new_events = [row for row in after["audit_events"] if row["id"] not in old_ids]
            assert len(new_events) == len(after["audit_events"]) - len(before["audit_events"])
            assert mutation_facts(new_events) == mutation_facts(
                fault.staged[0]["audit_events"][len(before["audit_events"]) :]
            )
            counts = Counter(row["event_type"] for row in new_events)
            if boundary in {"first-approval", "final-approval"}:
                current = await current_requests(runtime, run_id)
                assert {item.request.id for item in current} == {request.id for request in requests}
                assert len(after["approval_decisions"]) == len(before["approval_decisions"]) + 1
                assert counts["approval.approved"] == counts["action.approved"] == 1
                if boundary == "first-approval":
                    assert {item.status for item in current} == {
                        ApprovalStatus.APPROVED,
                        ApprovalStatus.PENDING,
                    }
                    assert counts["approval.consumed"] == 0
                    assert after["approval_uses"] == ()
                    state = RunState.AWAITING_APPROVAL
                else:
                    assert {item.status for item in current} == {ApprovalStatus.CONSUMED}
                    assert counts["approval.consumed"] == 2
                    assert len(after["approval_uses"]) == 2
                    assert after["authorization_sets"][0]["status"] == "released"
                    assert {row["state"] for row in after["external_actions"]} == {
                        "dispatch_reserved"
                    }
                    state = RunState.EXECUTING
            else:
                cancellations = [
                    row
                    for row in new_events
                    if row["event_type"] == "run.transitioned" and row["new_state"] == "cancelled"
                ]
                assert len(cancellations) == 1
                assert after["approval_decisions"] == before["approval_decisions"]
                assert after["approval_uses"] == before["approval_uses"]
                assert successful.json()["effects_reversed"] is False
                assert counts["action.cancelled"] == (0 if boundary == "queued-cancellation" else 2)
                state = RunState.CANCELLED
            assert own_run(after, run_id)["state"] == state.value
            if boundary == "queued-cancellation":
                # Queued cancellation never fabricated a plan or execution control.
                for table in ("run_plans", "run_steps", "run_execution_controls"):
                    assert after[table] == (), table
                assert calls.models == calls.reads == calls.writes == []
            else:
                await assert_email_zero_calls(runtime, run_id, calls, state)
            await assert_public_timeline(client, after, run_id)
            assert await full_snapshot(runtime) == after
        # Runtime reconstruction must retain the exact recovered transaction,
        # not repair missing audit evidence or consume sequence numbers.
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await full_snapshot(runtime) == after
        assert calls.models == calls.reads == calls.writes == []
    finally:
        await runtime.close()
