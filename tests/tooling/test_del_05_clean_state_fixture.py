"""Verify the clean smoke's real persisted fixture setup and source authority."""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from marketing_agents.config import Settings
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.infrastructure.webhook_signatures import WEBHOOK_SIGNATURE_DOMAIN
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.scheduler import SchedulerWorker

from scripts import del_05_replay_fixture as fixture
from scripts.del_05_runtime_smoke import SmokeFailure, database_snapshot
from tests.support.api import api_request

ROOT = Path(__file__).resolve().parents[2]


def test_del_05_replay_fixture_refuses_normal_installations(monkeypatch):
    monkeypatch.delenv("DEL05_VERIFICATION_SCOPE", raising=False)
    with pytest.raises(SmokeFailure, match="scoped_verification_installation"):
        fixture.fixture_settings()


@pytest.mark.parametrize("hour,minute", [(0, 0), (12, 30), (23, 59)])
def test_del_05_fixture_never_repeats_at_midnight_during_bounded_verification(hour, minute):
    now = datetime(2026, 12, 31, hour, minute, 59, tzinfo=UTC)
    cron, due = fixture.daily_fixture_timing(now)
    following = CroniterRecurrenceCalculator().next_after(cron=cron, timezone="UTC", after_utc=now)
    assert timedelta(0) <= now - due < timedelta(minutes=1)
    assert following - now > timedelta(hours=23, minutes=59)


@pytest.mark.asyncio
async def test_del_05_fixture_preserves_seed_and_replays_after_composition_restart(
    tmp_path, monkeypatch
):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'data' / 'local.db'}",
        marketing_agents_digest_key_path=tmp_path / "keys" / "digest.key",
        catalog_root=ROOT / "catalog" / "v1",
        webhook_hmac_secret=fixture.PUBLIC_FIXTURE_HMAC,
    )
    await migrate_local_database(settings.database_url, settings.marketing_agents_digest_key_path)
    database = create_database_runtime(settings.database_url)
    catalog = compile_catalog(settings.catalog_root)
    await seed_catalog(catalog, database, CroniterRecurrenceCalculator())
    monkeypatch.setattr(fixture, "fixture_settings", lambda: settings)
    try:
        assert (await fixture.prepare())["configurations_changed"] == 2
        before = database_snapshot(
            tmp_path / "data" / "local.db", settings.marketing_agents_digest_key_path
        )
        reseed = await seed_catalog(catalog, database, CroniterRecurrenceCalculator())
        assert (reseed.inserted, reseed.updated, reseed.deleted, reseed.configuration_inserted) == (
            0,
            0,
            0,
            0,
        )
        assert (
            database_snapshot(
                tmp_path / "data" / "local.db", settings.marketing_agents_digest_key_path
            )
            == before
        )
        receipts = []
        for attempt in range(2):
            runtime = await build_runtime(settings)
            try:
                timestamp = str(int(runtime.dependencies.clock.now().timestamp()))
                body = json.dumps(
                    {
                        "eventId": "event.del05.fixture-restart",
                        "input": {
                            "request_id": "request-del05-fixture-restart",
                            "source_content": "Synthetic fixture",
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                signature = hmac.digest(
                    fixture.PUBLIC_FIXTURE_HMAC.encode(),
                    WEBHOOK_SIGNATURE_DOMAIN + timestamp.encode() + b"\x00" + body,
                    "sha256",
                ).hex()
                response = await api_request(
                    runtime.create_app(),
                    "POST",
                    f"/api/v1/webhooks/{fixture.SOURCE}/{fixture.TRIGGER}",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Timestamp": timestamp,
                        "X-Webhook-Signature": "v1=" + signature,
                    },
                )
                assert response.status_code == 202, response.text
                receipt = response.json()
                receipts.append(receipt)
                assert receipt["disposition"] == ("created" if attempt == 0 else "replayed")
                assert await SchedulerWorker(
                    runtime, f"worker.del05.fixture.{attempt}"
                ).drain_once() is (attempt == 0)
            finally:
                await runtime.database.dispose()
        assert receipts[0]["receiptId"] == receipts[1]["receiptId"]
        assert receipts[0]["deliveries"] == receipts[1]["deliveries"]
        after = database_snapshot(
            tmp_path / "data" / "local.db", settings.marketing_agents_digest_key_path
        )
        assert after["counts"]["webhook_receipts"] == after["counts"]["schedule_occurrences"] == 1
        assert before["key_fingerprint"] == after["key_fingerprint"]
    finally:
        await database.dispose()
