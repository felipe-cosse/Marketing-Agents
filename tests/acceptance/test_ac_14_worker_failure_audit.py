"""AC-14: actual worker terminal cleanup has complete, atomic public audit witnesses."""

from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.application.services.external_action_dispatcher import (
    ExternalActionDispatcher,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import ApprovalStatus, RunState
from marketing_agents.infrastructure.db import Base, SQLAlchemyAuditRepository
from marketing_agents.infrastructure.db.models import AuditEventRecord, AuditFeedSequenceRecord
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
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
from tests.acceptance.test_ac_09_approval_rejection import approve
from tests.acceptance.test_ac_10_webhook_replay import business_snapshot
from tests.acceptance.test_ac_11_mock_crash_recovery import (
    assert_authority_unchanged,
    client_for,
)

CANARY = "ac14-provider-private-unexpected-error"
WORKER_ID = "worker.ac14.unexpected-failure"


async def snapshot(runtime):
    """Include both audit allocators; exclude unrelated session and worker-lease rows."""
    state = await business_snapshot(runtime)
    async with runtime.database.session_factory() as session:
        for model in (AuditEventRecord, AuditFeedSequenceRecord):
            table = model.__table__
            rows = (
                await session.execute(select(table).order_by(*table.primary_key.columns))
            ).mappings()
            state[table.name] = tuple(deepcopy(dict(row)) for row in rows)
    return state


def assert_transition_witnesses(state, run_id):
    """Start from independent transition rows, not an expected audit-event list."""
    audits = tuple(row for row in state["audit_events"] if row["run_id"] == run_id)
    assert tuple(row["run_sequence"] for row in audits) == tuple(range(1, len(audits) + 1))
    run = next(row for row in state["runs"] if row["id"] == run_id)
    assert run["next_timeline_sequence"] == len(audits)
    for table_name, aggregate, identity_key, sequence_key in (
        ("run_state_transitions", "run", "run_id", "run_transition_sequence"),
        ("run_step_state_transitions", "step", "step_id", "step_transition_sequence"),
    ):
        rows = tuple(row for row in state[table_name] if row["run_id"] == run_id)
        witnesses = tuple(row for row in audits if row[sequence_key] is not None)
        assert len(witnesses) == len(rows)
        for transition in rows:
            matches = tuple(
                row
                for row in witnesses
                if row["aggregate_type"] == aggregate
                and row["aggregate_id"] == transition[identity_key]
                and row[sequence_key] == transition["sequence"]
            )
            assert len(matches) == 1, transition
            event = matches[0]
            assert event["mutation_version"] == transition["resulting_version"]
            for key in ("previous_state", "new_state", "reason_code", "occurred_at"):
                assert event[key] == transition[key], (key, transition)
            if event["event_type"] == "step.recorded":
                assert transition["command"] == "initialize"
                assert transition["sequence"] == 1
            else:
                assert event["safe_metadata"]["command"] == transition["command"]
    return audits


async def assert_public_projection(runtime, run_id, audits):
    async with client_for(runtime) as client:
        items = []
        cursor = None
        for _ in range(100):
            params = {"limit": 3}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(f"/api/v1/runs/{run_id}/timeline", params=params)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert CANARY not in response.text
            items.extend(response.json()["items"])
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break
        assert cursor is None
    assert [(item["id"], item["sequence"]) for item in items] == [
        (row["id"], row["run_sequence"]) for row in audits
    ]
    for item, row in zip(items, audits, strict=True):
        for key in (
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "actor_id",
            "actor_source",
            "auth_method",
            "correlation_id",
            "previous_state",
            "new_state",
            "reason_code",
            "step_id",
            "action_id",
            "approval_request_id",
        ):
            assert item[key] == row[key], key
        assert item["metadata"] == row["safe_metadata"]
        assert item["metadata_expired"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_audit", (False, True), ids=("complete-witnesses", "audit-rollback"))
async def test_ac_14_real_worker_unexpected_failure_is_completely_and_atomically_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_audit: bool
) -> None:
    assert Path(inspect.getfile(RunWorker)).resolve() == (
        Path(__file__).resolve().parents[2]
        / "apps/api/src/marketing_agents/workers/runtime/run_loop.py"
    )
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    dispatch_entries = []
    flushed_batches = []
    staged_cleanup = []
    try:
        async with client_for(runtime) as client:
            run_id = await submit(client, EMAIL)
            assert await RunWorker(runtime, "worker.ac14.plan").drain_once()
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            requests = await current_requests(runtime, run_id)
            assert len(requests) == 2
            for stored in requests:
                response = await approve(client, stored.request)
                assert response.status_code == 200, response.text
        await assert_email_zero_calls(runtime, run_id, calls, RunState.EXECUTING)
        consumed = await current_requests(runtime, run_id)
        assert {item.status for item in consumed} == {ApprovalStatus.CONSUMED}
        released = await snapshot(runtime)
        assert {row["state"] for row in released["external_actions"]} == {"dispatch_reserved"}

        async def unexpected_failure(dispatcher, action_id, *, lease_owner):
            # This is an injected failure, never a fabricated successful call or authority.
            dispatch_entries.append((action_id, lease_owner))
            raise RuntimeError(CANARY)

        original_append = SQLAlchemyAuditRepository.append_many

        async def fail_after_cleanup_flush(repository, events):
            result = await original_append(repository, events)
            if any(
                event.run_id == run_id
                and event.event_type == "run.transitioned"
                and event.new_state == "failed"
                for event in events
            ):
                flushed_batches.append(result)
                staged = {}
                for name in ("runs", "run_steps", "external_actions", "audit_feed_sequence"):
                    table = Base.metadata.tables[name]
                    rows = (
                        await repository._session.execute(
                            select(table).order_by(*table.primary_key.columns)
                        )
                    ).mappings()
                    staged[name] = tuple(deepcopy(dict(row)) for row in rows)
                staged_cleanup.append(staged)
                raise RuntimeError("ac14 injected failure after cleanup audit flush")
            return result

        monkeypatch.setattr(ExternalActionDispatcher, "dispatch_once", unexpected_failure)
        clock.current += timedelta(seconds=2)
        worker = RunWorker(runtime, WORKER_ID)
        if fail_audit:
            monkeypatch.setattr(SQLAlchemyAuditRepository, "append_many", fail_after_cleanup_flush)
            with pytest.raises(RuntimeError, match="after cleanup audit flush"):
                await worker.drain_once()
            assert len(flushed_batches) == 1 and len(flushed_batches[0]) == 6
            assert len(staged_cleanup) == 1
            staged = staged_cleanup[0]
            assert staged["runs"][0]["state"] == "failed"
            assert sorted(row["state"] for row in staged["run_steps"]) == [
                "failed",
                "skipped",
                "skipped",
            ]
            assert {row["state"] for row in staged["external_actions"]} == {"cancelled"}
            assert (
                staged["runs"][0]["next_timeline_sequence"]
                == released["runs"][0]["next_timeline_sequence"] + 6
            )
            assert (
                staged["audit_feed_sequence"][0]["last_sequence"]
                == released["audit_feed_sequence"][0]["last_sequence"] + 6
            )
            assert await snapshot(runtime) == released
            await assert_public_projection(
                runtime, run_id, assert_transition_witnesses(released, run_id)
            )
            monkeypatch.setattr(SQLAlchemyAuditRepository, "append_many", original_append)
            clock.current += timedelta(seconds=2)

        assert await worker.drain_once()
        final = await snapshot(runtime)
        assert len(dispatch_entries) == 1 + int(fail_audit)
        assert dispatch_entries[0][0] in {row["id"] for row in released["external_actions"]}
        assert len(set(dispatch_entries)) == 1
        assert await current_requests(runtime, run_id) == consumed
        assert_authority_unchanged(final, released)
        run = next(row for row in final["runs"] if row["id"] == run_id)
        assert run["state"] == "failed" and run["terminal_reason_code"] == "unclassified_failure"
        assert sorted(row["state"] for row in final["run_steps"]) == [
            "failed",
            "skipped",
            "skipped",
        ]
        audits = assert_transition_witnesses(final, run_id)
        original_events = tuple(row for row in released["audit_events"] if row["run_id"] == run_id)
        assert audits[: len(original_events)] == original_events
        new_events = audits[len(original_events) :]
        assert len(new_events) == 6
        actor = AuditContext.worker(WORKER_ID, correlation_id="ac14.expected").actor_id
        assert {row["actor_id"] for row in new_events} == {actor}
        assert {row["actor_source"] for row in new_events} == {"worker"}
        assert len({row["correlation_id"] for row in new_events}) == 1
        assert CANARY not in repr(final["audit_events"])
        for action in final["external_actions"]:
            previous = next(
                row for row in released["external_actions"] if row["id"] == action["id"]
            )
            assert action["state"] == "cancelled" and action["version"] == previous["version"] + 1
            for key in ("idempotency_key", "action_hash", "reservation_id"):
                assert action[key] == previous[key]
            witnesses = tuple(
                row
                for row in new_events
                if row["event_type"] == "action.cancelled" and row["action_id"] == action["id"]
            )
            assert len(witnesses) == 1
            event = witnesses[0]
            assert event["mutation_version"] == action["version"]
            assert event["previous_state"] == previous["state"]
            assert event["new_state"] == action["state"]
            assert event["occurred_at"] == action["updated_at"]
        for table in (
            "external_action_dispatch_attempts",
            "connector_action_receipts",
            "execution_attempts",
            "artifacts",
        ):
            assert final[table] == ()
        assert final["run_execution_controls"] == released["run_execution_controls"]
        await assert_public_projection(runtime, run_id, audits)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await snapshot(runtime) == final
        clock.current += timedelta(seconds=2)
        assert not await RunWorker(runtime, "worker.ac14.terminal-idle").drain_once()
        assert await snapshot(runtime) == final
        await assert_public_projection(runtime, run_id, audits)
        assert calls.models == calls.reads == calls.writes == []
        assert len(dispatch_entries) == 1 + int(fail_audit)
        assert runtime.email._connector_bundle.ledger.side_effect_count == 0
    finally:
        await runtime.close()
