"""AC-14: real public audit pages preserve snapshot order and expired skeletons.

Clock advancement proves projection expiry, not a physical retention sweep.
No durable events, authority, responses or provider outcomes are manufactured.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.workers.runtime.composition import build_runtime

from tests.acceptance.test_ac_07_real_composition_demos import (
    Clock,
    installation,
    observe_real_calls,
)
from tests.acceptance.test_ac_09_approval_rejection import approve, prepare
from tests.acceptance.test_ac_11_mock_crash_recovery import audit_rows, client_for
from tests.acceptance.test_ac_13_public_cancellation import cancel


async def pages(client, path, *, run_id=None, first=None):
    params = {"limit": 3}
    if run_id is not None:
        params["run_id"] = run_id
    result = []
    watermark = None
    for index in range(100):
        if index == 0 and first is not None:
            body = first
        else:
            response = await client.get(path, params=params)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
        if "high_watermark" in body:
            if watermark is None:
                watermark = body["high_watermark"]
            assert body["high_watermark"] == watermark
        result.extend(body["items"])
        if body["next_cursor"] is None:
            assert len({item["id"] for item in result}) == len(result)
            return result
        params["cursor"] = body["next_cursor"]
    pytest.fail("bounded audit pagination did not terminate")


@pytest.mark.asyncio
async def test_ac_14_public_feed_excludes_real_appends_until_a_new_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client_for(runtime) as client:
            run_id, requests = await prepare(runtime, client, calls)
            before = await audit_rows(runtime, run_id)
            assert len(before) > 3
            # All genuine mutations share the injected UTC instant. Neither
            # timestamp sorting nor a last-timestamp cursor can prove completeness.
            assert {row["occurred_at"] for row in before} == {clock.current}
            response = await client.get(
                "/api/v1/audit-events", params={"run_id": run_id, "limit": 3}
            )
            assert response.status_code == 200, response.text
            first = response.json()
            assert first["next_cursor"] is not None
            assert first["high_watermark"] == max(row["feed_sequence"] for row in before)
            approved = await approve(client, requests[0])
            assert approved.status_code == 200, approved.text
            after = await audit_rows(runtime, run_id)
            assert after[: len(before)] == before and len(after) > len(before)
            assert any(row["event_type"] == "approval.approved" for row in after[len(before) :])
            assert all(
                row["feed_sequence"] > first["high_watermark"] for row in after[len(before) :]
            )
            original_page_set = await pages(
                client, "/api/v1/audit-events", run_id=run_id, first=first
            )
            assert [item["id"] for item in original_page_set] == [
                row["id"] for row in reversed(before)
            ]
            fresh = await pages(client, "/api/v1/audit-events", run_id=run_id)
            assert [item["id"] for item in fresh] == [row["id"] for row in reversed(after)]
            changed_filter = await client.get(
                "/api/v1/audit-events",
                params={
                    "run_id": run_id,
                    "event_type": "approval.approved",
                    "cursor": first["next_cursor"],
                },
            )
            assert changed_filter.status_code == 422, changed_filter.text
            assert changed_filter.json()["code"] == "audit_query_invalid"
        assert calls.models == calls.reads == calls.writes == []
        assert await audit_rows(runtime, run_id) == after
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ac_14_public_expiry_preserves_every_decision_and_transition_skeleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client_for(runtime) as client:
            run_id, requests = await prepare(runtime, client, calls)
            approved = await approve(client, requests[0])
            assert approved.status_code == 200, approved.text
            await cancel(client, run_id)
            durable = await audit_rows(runtime, run_id)
            expiries = {row["metadata_expires_at"] for row in durable}
            assert len(expiries) == 1
            expiry = expiries.pop()
            assert expiry == clock.current + timedelta(days=settings.retention_audit_metadata_days)
            clock.current = expiry - timedelta(microseconds=1)
            timeline_url = f"/api/v1/runs/{run_id}/timeline"
            before_timeline = await pages(client, timeline_url)
            before_feed = await pages(client, "/api/v1/audit-events", run_id=run_id)
            assert [item["id"] for item in before_timeline] == [row["id"] for row in durable]
            assert [item["id"] for item in before_feed] == [row["id"] for row in reversed(durable)]
            assert {item["event_type"] for item in before_timeline} >= {
                "run.transitioned",
                "step.transitioned",
                "approval.approved",
                "approval.requested",
            }
            for item in (*before_timeline, *before_feed):
                assert not item["metadata_expired"]
            assert any(item["metadata"] for item in before_timeline)
        clock.current = expiry
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        async with client_for(runtime) as client:
            after_timeline = await pages(client, timeline_url)
            after_feed = await pages(client, "/api/v1/audit-events", run_id=run_id)
        for before, after in ((before_timeline, after_timeline), (before_feed, after_feed)):
            assert len(after) == len(before)
            for previous, expired in zip(before, after, strict=True):
                assert expired == {**previous, "metadata": {}, "metadata_expired": True}
                assert expired["actor_id"] and expired["correlation_id"]
                assert expired["run_url"] == f"/api/v1/runs/{run_id}"
        assert await audit_rows(runtime, run_id) == durable
        assert calls.models == calls.reads == calls.writes == []
    finally:
        await runtime.close()
