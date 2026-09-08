"""Verification-only local webhook/schedule admission fixtures, never startup seeds."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import sqlite3
import time
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

try:
    from .del_05_runtime_smoke import API_SOCKET, LocalClient, SmokeFailure, require
except ImportError:  # Direct execution inside the deployed API container.
    from del_05_runtime_smoke import API_SOCKET, LocalClient, SmokeFailure, require

# Public synthetic signing material for this temporary verification installation.
# It is not a credential and must never be configured by normal make up.
PUBLIC_FIXTURE_HMAC = "del-05-public-test-signing-material-never-use-as-a-secret"
SOURCE = "del05.events"
TRIGGER = "trigger.webhook.del05.events.v1"
SCHEDULE_ID = "schedule.del05.clean-restart"
WEBHOOK_INSTANCE = "inst.community.events.attendee-scheduler.01"
SCHEDULE_INSTANCE = "inst.community.events.event-stats-tracker.02"


def daily_fixture_timing(now: datetime) -> tuple[str, datetime]:
    """Make one occurrence due now and keep the next outside the bounded run."""
    due = now.astimezone(UTC).replace(second=0, microsecond=0)
    return f"{due.minute} {due.hour} * * *", due


def fixture_settings():
    from marketing_agents.config import Settings

    require(
        re.fullmatch(
            r"marketing-agents-del05-[0-9a-f]{16}", os.environ.get("DEL05_VERIFICATION_SCOPE", "")
        ),
        "fixture_requires_scoped_verification_installation",
    )
    settings = Settings(_env_file=None)
    require(
        settings.webhook_hmac_secret is not None
        and settings.webhook_hmac_secret.get_secret_value() == PUBLIC_FIXTURE_HMAC,
        "fixture_requires_public_test_signing_material",
    )
    require(
        settings.database_url
        == "sqlite+aiosqlite:////var/lib/marketing-agents/data/marketing_agents.db"
        and settings.marketing_agents_digest_key_path
        == Path("/var/lib/marketing-agents/secrets/digest.key"),
        "fixture_requires_fresh_compose_paths",
    )
    return settings


async def prepare() -> dict:
    from marketing_agents.domain.entities import Schedule
    from marketing_agents.domain.enums import MisfirePolicy, TriggerKind
    from marketing_agents.domain.instance_configuration import (
        InstanceSchedule,
        InstanceTriggerBinding,
    )
    from marketing_agents.domain.schedule_occurrence_identity import SCHEDULE_RECURRENCE_VERSION
    from marketing_agents.workers.runtime.composition import build_runtime

    runtime = await build_runtime(fixture_settings())
    try:
        cron, due = daily_fixture_timing(runtime.dependencies.clock.now())
        async with runtime.dependencies.unit_of_work() as unit:
            require(
                await unit.schedules.get(SCHEDULE_ID) is None, "fixture_schedule_already_exists"
            )
            for instance_id in (WEBHOOK_INSTANCE, SCHEDULE_INSTANCE):
                current = await unit.configurations.get(instance_id)
                require(
                    current is not None and current.configuration_revision == 1,
                    "fixture_requires_fresh_configuration",
                )
                if instance_id == WEBHOOK_INSTANCE:
                    updated = replace(
                        current,
                        configuration_revision=2,
                        trigger_bindings=(
                            InstanceTriggerBinding(kind=TriggerKind.WEBHOOK, event_source=SOURCE),
                        ),
                    )
                else:
                    schedule = InstanceSchedule(
                        cron=cron,
                        timezone="UTC",
                        misfire_policy=MisfirePolicy.RUN_ONCE,
                        misfire_grace_seconds=86400,
                    )
                    updated = replace(
                        current,
                        configuration_revision=2,
                        schedule=schedule,
                        trigger_bindings=(
                            InstanceTriggerBinding(
                                kind=TriggerKind.SCHEDULE,
                                cron=schedule.cron,
                                timezone=schedule.timezone,
                                misfire_policy=schedule.misfire_policy,
                                misfire_grace_seconds=schedule.misfire_grace_seconds,
                            ),
                        ),
                    )
                require(
                    await unit.configurations.compare_and_swap(current, updated),
                    "fixture_configuration_conflict",
                )
            inserted = await unit.schedules.add_or_get(
                Schedule(
                    id=SCHEDULE_ID,
                    trigger_id="trigger.del05.clean-schedule",
                    instance_id=SCHEDULE_INSTANCE,
                    workflow_id="workflow.del05.scheduled-admission.v1",
                    cron=cron,
                    timezone="UTC",
                    next_run_at_utc=due,
                    misfire_policy=MisfirePolicy.RUN_ONCE,
                    misfire_grace_seconds=86400,
                    enabled=True,
                    recurrence_version=SCHEDULE_RECURRENCE_VERSION,
                )
            )
            require(inserted.inserted, "fixture_schedule_not_fresh")
            await unit.commit()
        return {
            "scope": "synthetic-local-admission-only",
            "configurations_changed": 2,
            "schedule_id": SCHEDULE_ID,
        }
    finally:
        await runtime.database.dispose()


def deliver() -> dict:
    from marketing_agents.infrastructure.webhook_signatures import WEBHOOK_SIGNATURE_DOMAIN

    settings = fixture_settings()
    socket_path = os.environ.get("MARKETING_AGENTS_API_SOCKET")
    require(socket_path == API_SOCKET, "fixture_requires_fixed_api_socket_transport")
    client = LocalClient("http://127.0.0.1:8000", unix_socket=socket_path)
    client.ready()
    body = json.dumps(
        {
            "eventId": "event.del05.clean-restart.v1",
            "input": {
                "request_id": "request-del05-local-verification",
                "source_content": "Synthetic local verification input.",
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    timestamp = str(int(datetime.now(UTC).timestamp()))
    signature = hmac.digest(
        PUBLIC_FIXTURE_HMAC.encode(),
        WEBHOOK_SIGNATURE_DOMAIN + timestamp.encode("ascii") + b"\x00" + body,
        "sha256",
    ).hex()
    request = urllib.request.Request(
        client.origin + f"/api/v1/webhooks/{SOURCE}/{TRIGGER}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": "v1=" + signature,
        },
    )
    with client.opener.open(request, timeout=5) as response:
        receipt = json.load(response)
    require(len(receipt["deliveries"]) == 1, "fixture_expected_one_webhook_delivery")
    database = Path(settings.database_url.removeprefix("sqlite+aiosqlite:///"))
    deadline = time.monotonic() + 90
    occurrence = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT id, work_item_id, run_id, state FROM schedule_occurrences "
                "WHERE schedule_id = ?",
                (SCHEDULE_ID,),
            ).fetchall()
        require(len(rows) <= 1, "fixture_duplicate_schedule_occurrence")
        if rows and rows[0][1] is not None and rows[0][2] is not None:
            occurrence = rows[0]
            break
        time.sleep(0.2)
    require(occurrence is not None, "fixture_schedule_worker_deadline")
    return {
        "scope": "synthetic-local-admission-only",
        "webhook_receipt_id": receipt["receiptId"],
        "webhook_disposition": receipt["disposition"],
        "webhook_work_id": receipt["deliveries"][0]["workId"],
        "webhook_run_id": receipt["deliveries"][0]["runId"],
        "schedule_occurrence_id": occurrence[0],
        "schedule_work_id": occurrence[1],
        "schedule_run_id": occurrence[2],
        "schedule_state": occurrence[3],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "deliver"))
    args = parser.parse_args()
    try:
        result = asyncio.run(prepare()) if args.command == "prepare" else deliver()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": str(exc) if isinstance(exc, SmokeFailure) else "replay_fixture_failed",
                }
            )
        )
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
