"""EXEC-05: dedicated routes retain configuration authority and real scheduler facts."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.api.routes import schedules as route
from marketing_agents.application.services.schedule_configuration import configured_schedule_id
from marketing_agents.application.services.schedule_control import (
    ScheduleControlError,
    ScheduleControlService,
    ScheduleMutation,
    SchedulePageQuery,
)
from marketing_agents.config import Settings
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.domain.schedule_occurrence_identity import (
    SCHEDULE_RECURRENCE_VERSION,
    schedule_occurrence_id,
)
from marketing_agents.infrastructure.db.models import AuditEventRecord, ScheduleRecord
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.scheduler import SchedulerWorker
from sqlalchemy import func, select, update

from tests.acceptance.test_ac_07_real_composition_demos import (
    Clock,
    installation,
    observe_real_calls,
)
from tests.acceptance.test_ac_12_scheduler_recovery import INPUT, INSTANCE, ZONE, snapshot
from tests.support.api import browser_request
from tests.support.identity import StaticIdentityProvider, human_principal, service_principal

SCHEDULE = configured_schedule_id(INSTANCE)
PATH = "/api/v1/schedules/" + SCHEDULE
CANARY = "exec05-private-schedule-input-canary"
PARAMETERS = {
    "cron": "15 * * * *",
    "timezone": ZONE,
    "misfirePolicy": "skip",
    "misfireGraceSeconds": 60,
}
BODY = {
    "instanceId": INSTANCE,
    "schedule": PARAMETERS,
    "scheduledInput": {"input": {**INPUT, "source_content": CANARY}, "executionMode": "dry_run"},
}


def client(application):
    return AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://testserver",
    )


def safe(response, expected):
    assert response.status_code == expected, response.text
    assert response.headers["cache-control"] == "no-store"
    assert CANARY not in response.text
    assert "bindingDigest" not in response.text and "leaseOwner" not in response.text
    if expected >= 400:
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["status"] == expected


async def create(connection, *, instance=INSTANCE, parameters=PARAMETERS):
    current = await connection.get(f"/api/v1/agent-instances/{instance}/configuration")
    assert current.status_code == 200, current.text
    return await browser_request(
        connection,
        "POST",
        "/api/v1/schedules",
        headers={"If-Match": current.headers["etag"]},
        json={**BODY, "instanceId": instance, "schedule": parameters},
    )


@pytest.mark.asyncio
async def test_exec05_schedule_real_routes_preserve_configuration_history_and_restart(
    tmp_path, monkeypatch
):
    settings = await installation(tmp_path)
    clock = Clock()
    calls = observe_real_calls(monkeypatch)
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client(runtime.create_app()) as connection:
            empty = await connection.get("/api/v1/schedules")
            safe(empty, 200)
            assert empty.json() == {"items": []}
            created = await create(connection)
            safe(created, 201)
            assert created.headers["location"] == PATH
            assert created.json()["id"] == SCHEDULE
            assert created.json()["enabled"] is True
            assert created.json()["configurationRevision"] == 2
            assert len(created.json()["previewUtc"]) == 3
            assert created.json()["timezone"] == ZONE
            executor = runtime.create_app().state.schedule_control_service
            for role in ("viewer", "operator", "approver", "local_admin"):
                async with client(
                    application(executor, human_principal(roles=frozenset({role})))
                ) as reader:
                    for path in ("/api/v1/schedules", PATH, PATH + "/occurrences"):
                        safe(await reader.get(path), 200)
            safe(await create(connection), 409)
            invalid = await browser_request(
                connection,
                "PATCH",
                PATH,
                headers={"If-Match": created.headers["etag"]},
                json={"schedule": {**PARAMETERS, "cron": "* * * * * *"}},
            )
            safe(invalid, 422)
            patched = await browser_request(
                connection,
                "PATCH",
                PATH,
                headers={"If-Match": created.headers["etag"]},
                json={"schedule": {**PARAMETERS, "cron": "30 * * * *"}},
            )
            safe(patched, 200)
            assert patched.json()["configurationRevision"] == 3
            stale = await browser_request(
                connection,
                "POST",
                PATH + "/disable",
                headers={"If-Match": created.headers["etag"]},
                json={},
            )
            safe(stale, 409)
            assert stale.json()["current_resource_version"] == 3
            async with runtime.dependencies.unit_of_work() as uow:
                before = await uow.configurations.get(INSTANCE)
            disabled = await browser_request(
                connection,
                "POST",
                PATH + "/disable",
                headers={"If-Match": patched.headers["etag"]},
                json={},
            )
            safe(disabled, 200)
            assert disabled.json()["enabled"] is False
            async with runtime.dependencies.unit_of_work() as uow:
                after = await uow.configurations.get(INSTANCE)
                retained = await uow.schedules.get(SCHEDULE)
            assert after.enabled == before.enabled is True
            assert after.scheduled_input == before.scheduled_input
            assert after.variant_label == before.variant_label
            assert after.connector_bindings == before.connector_bindings
            assert tuple(
                item for item in after.trigger_bindings if item.kind is not TriggerKind.SCHEDULE
            ) == tuple(
                item for item in before.trigger_bindings if item.kind is not TriggerKind.SCHEDULE
            )
            assert after.schedule is None and retained.cron == "30 * * * *"
            clock.current = retained.next_run_at_utc
            assert await SchedulerWorker(runtime, "scheduler.exec05.disabled").drain_once() is False
            denied = await browser_request(
                connection,
                "PATCH",
                PATH,
                headers={"If-Match": disabled.headers["etag"]},
                json={"schedule": PARAMETERS},
            )
            safe(denied, 409)
            assert denied.json()["code"] == "schedule_disabled"
            enabled = await browser_request(
                connection,
                "POST",
                PATH + "/enable",
                headers={"If-Match": disabled.headers["etag"]},
                json={},
            )
            safe(enabled, 200)
            assert enabled.json()["enabled"] is True
            assert enabled.json()["id"] == SCHEDULE
            assert enabled.json()["configurationRevision"] == 5
            assert datetime.fromisoformat(enabled.json()["nextRunAtUtc"]) > clock.now()
            expected_due = []
            for _ in range(2):
                async with runtime.dependencies.unit_of_work() as uow:
                    row = await uow.schedules.get(SCHEDULE)
                clock.current = row.next_run_at_utc
                expected_due.append(row.next_run_at_utc)
                assert await SchedulerWorker(runtime, "scheduler.exec05.real").drain_once() is True
                assert await SchedulerWorker(runtime, "scheduler.exec05.real").drain_once() is False
            # Lease and due advancement do not invalidate the configuration ETag.
            inspected = await connection.get(PATH)
            safe(inspected, 200)
            assert inspected.headers["etag"] == enabled.headers["etag"]
            first = await connection.get(PATH + "/occurrences", params={"limit": 1})
            safe(first, 200)
            cursor = first.json()["nextCursor"]
            occurrence_ids = [first.json()["items"][0]["id"]]
            assert first.json()["items"][0]["runId"]
            second_instance = INSTANCE[:-2] + "02"
            second_created = await create(
                connection, instance=second_instance, parameters={**PARAMETERS, "cron": "0 0 1 1 *"}
            )
            safe(second_created, 201)
            other = "/api/v1/schedules/" + second_created.json()["id"]
            safe(await connection.get(other + "/occurrences", params={"cursor": cursor}), 422)
            page1 = await connection.get("/api/v1/schedules", params={"limit": 1})
            page2 = await connection.get(
                "/api/v1/schedules", params={"limit": 1, "cursor": page1.json()["nextCursor"]}
            )
            assert [page1.json()["items"][0]["id"], page2.json()["items"][0]["id"]] == sorted(
                [SCHEDULE, second_created.json()["id"]]
            )
            assert "nextCursor" not in page2.json()
            safe(await connection.get("/api/v1/schedules", params={"cursor": cursor}), 422)
            persisted = await snapshot(runtime)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await snapshot(runtime) == persisted
        async with client(runtime.create_app()) as connection:
            second = await connection.get(
                PATH + "/occurrences", params={"limit": 1, "cursor": cursor}
            )
            safe(second, 200)
            assert "nextCursor" not in second.json()
            occurrence_ids.append(second.json()["items"][0]["id"])
            assert occurrence_ids == [
                schedule_occurrence_id(
                    SCHEDULE, due, recurrence_version=SCHEDULE_RECURRENCE_VERSION
                )
                for due in expected_due
            ]
            assert await snapshot(runtime) == persisted
            assert (
                await SchedulerWorker(runtime, "scheduler.exec05.restarted").drain_once() is False
            )
        assert calls.models == calls.reads == calls.writes == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_exec05_schedule_concurrent_cas_and_disabled_instance_never_reenabled(tmp_path):
    settings = await installation(tmp_path)
    clock = Clock()
    first = await build_runtime(settings, clock=clock)
    second = await build_runtime(settings, clock=clock)
    try:
        async with client(first.create_app()) as left, client(second.create_app()) as right:
            responses = await asyncio.gather(
                *(
                    browser_request(
                        connection,
                        "POST",
                        "/api/v1/schedules",
                        headers={"If-Match": '"instance-configuration-v1-1"'},
                        json=BODY,
                    )
                    for connection in (left, right)
                )
            )
            assert sorted(response.status_code for response in responses) == [201, 409]
            current = await left.get(PATH)
            responses = await asyncio.gather(
                *(
                    browser_request(
                        connection,
                        "PATCH",
                        PATH,
                        headers={"If-Match": current.headers["etag"]},
                        json={"schedule": {**PARAMETERS, "cron": cron}},
                    )
                    for connection, cron in ((left, "20 * * * *"), (right, "25 * * * *"))
                )
            )
            assert sorted(response.status_code for response in responses) == [200, 409]
            async with first.database.session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(ScheduleRecord)) == 1
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditEventRecord)
                        .where(AuditEventRecord.event_type == "instance.configuration_changed")
                    )
                    == 2
                )
            current = await left.get(f"/api/v1/agent-instances/{INSTANCE}/configuration")
            disabled = await browser_request(
                left,
                "PATCH",
                f"/api/v1/agent-instances/{INSTANCE}/configuration",
                headers={"If-Match": current.headers["etag"]},
                json={"enabled": False},
            )
            # The explicit administrator configuration endpoint deliberately returns input.
            assert disabled.status_code == 200, disabled.text
            before = await snapshot(first)
            denied = await browser_request(
                left,
                "POST",
                PATH + "/enable",
                headers={"If-Match": disabled.headers["etag"]},
                json={},
            )
            safe(denied, 409)
            assert denied.json()["code"] == "schedule_instance_disabled"
            assert await snapshot(first) == before
            # Corrupt persisted evidence is rejected through the existing hydrator.
            async with first.database.session_factory() as session:
                await session.execute(
                    update(ScheduleRecord)
                    .where(ScheduleRecord.id == SCHEDULE)
                    .values(integrity_digest="0" * 64)
                )
                await session.commit()
            safe(await left.get(PATH), 503)
            safe(await left.get(PATH + "/occurrences"), 503)
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_exec05_inactive_schedule_tracks_configuration_after_saved_input_is_cleared(tmp_path):
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with client(runtime.create_app()) as connection:
            created = await create(connection)
            safe(created, 201)
            configuration_path = f"/api/v1/agent-instances/{INSTANCE}/configuration"
            cleared = await browser_request(
                connection,
                "PATCH",
                configuration_path,
                headers={"If-Match": created.headers["etag"]},
                json={"scheduledInput": None},
            )
            assert cleared.status_code == 200, cleared.text
            changed_parameters = {**PARAMETERS, "cron": "35 * * * *"}
            changed = await browser_request(
                connection,
                "PATCH",
                configuration_path,
                headers={"If-Match": cleared.headers["etag"]},
                json={
                    "variantLabel": "Retained inactive schedule",
                    "schedule": changed_parameters,
                    "triggerBindings": [
                        {"type": "schedule", "enabled": True, **changed_parameters}
                    ],
                },
            )
            assert changed.status_code == 200, changed.text
            inspected = await connection.get(PATH)
            safe(inspected, 200)
            assert inspected.headers["etag"] == changed.headers["etag"]
            assert inspected.json()["configurationRevision"] == 4
            assert inspected.json()["cron"] == "35 * * * *"
            assert inspected.json()["enabled"] is False
            async with runtime.dependencies.unit_of_work() as uow:
                row = await uow.schedules.get(SCHEDULE)
                configuration = await uow.configurations.get(INSTANCE)
            assert row.configuration_revision == configuration.configuration_revision == 4
            assert configuration.scheduled_input is None
            before = await snapshot(runtime)
            denied = await browser_request(
                connection,
                "POST",
                PATH + "/enable",
                headers={"If-Match": inspected.headers["etag"]},
                json={},
            )
            safe(denied, 409)
            assert denied.json()["code"] == "schedule_input_required"
            assert await snapshot(runtime) == before
            clock.current = row.next_run_at_utc
            assert await SchedulerWorker(runtime, "scheduler.exec05.no-input").drain_once() is False
            disabled = await browser_request(
                connection,
                "POST",
                PATH + "/disable",
                headers={"If-Match": inspected.headers["etag"]},
                json={},
            )
            safe(disabled, 200)
            assert disabled.json()["configurationRevision"] == 5
            assert disabled.json()["enabled"] is False
    finally:
        await runtime.close()


class PoisonExecutor:
    def __init__(self):
        self.calls = 0

    async def read(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError(CANARY)

    async def list_schedules(self, *args, **kwargs):
        return await self.read()

    async def occurrences(self, *args, **kwargs):
        return await self.read()

    async def mutate(self, *args, **kwargs):
        return await self.read()


def application(executor, principal=None):
    return create_app(
        Settings(_env_file=None),
        schedule_control_service=executor,
        identity_provider=StaticIdentityProvider(
            principal or human_principal(roles=frozenset({"local_admin"}))
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal",
    [human_principal(roles=frozenset({role})) for role in ("viewer", "operator", "approver")]
    + [service_principal(roles=frozenset({"local_admin"}))],
)
async def test_exec05_schedule_mutation_authority_before_executor(principal):
    executor = PoisonExecutor()
    async with client(application(executor, principal)) as connection:
        for method, path, body in (
            ("POST", "/api/v1/schedules", BODY),
            ("PATCH", PATH, {"schedule": PARAMETERS}),
            ("POST", PATH + "/enable", {}),
            ("POST", PATH + "/disable", {}),
        ):
            response = await browser_request(
                connection,
                method,
                path,
                json=body,
                headers={"If-Match": '"instance-configuration-v1-1"'},
            )
            safe(response, 403)
    assert executor.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'{"instanceId":"x","instanceId":"y"}',
        b'{"value":NaN}',
        b'{"value":1e999}',
        b'{"value":"\\ud800"}',
        b"\xef\xbb\xbf{}",
        b'{"x":' + b"[" * 25 + b"0" + b"]" * 25 + b"}",
    ],
)
async def test_exec05_schedule_strict_json_never_reaches_executor(body):
    executor = PoisonExecutor()
    async with client(application(executor)) as connection:
        response = await browser_request(
            connection,
            "POST",
            "/api/v1/schedules",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        safe(response, 422)
    assert executor.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        dict(BODY, workflowId="forged"),
        dict(BODY, enabled=False),
        dict(BODY, actorId="forged"),
        {**BODY, "schedule": {**PARAMETERS, "misfireGraceSeconds": True}},
        {**BODY, "scheduledInput": {"input": INPUT, "executionMode": "mock_execution"}},
    ],
)
async def test_exec05_schedule_server_authority_and_types_are_not_body_fields(body):
    executor = PoisonExecutor()
    async with client(application(executor)) as connection:
        safe(await browser_request(connection, "POST", "/api/v1/schedules", json=body), 422)
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_exec05_schedule_header_query_bounds_csrf_and_safe_errors():
    executor = PoisonExecutor()
    async with client(application(executor)) as connection:
        safe(await browser_request(connection, "POST", "/api/v1/schedules", json=BODY), 428)
        for tag in ("*", 'W/"instance-configuration-v1-1"', '"instance-configuration-v1-0"'):
            safe(
                await browser_request(
                    connection, "POST", "/api/v1/schedules", json=BODY, headers={"If-Match": tag}
                ),
                400,
            )
        for query in ("limit=0", "limit=101", "limit=1&limit=2", "actor=forged"):
            safe(await connection.get("/api/v1/schedules?" + query), 422)
        safe(await connection.post("/api/v1/schedules", json=BODY), 403)
        safe(
            await browser_request(
                connection,
                "POST",
                "/api/v1/schedules",
                json=BODY,
                headers={"Origin": "https://untrusted.invalid"},
            ),
            403,
        )
        safe(
            await browser_request(
                connection,
                "POST",
                "/api/v1/schedules",
                content=b" " * 65537,
                headers={"Content-Type": "application/json"},
            ),
            413,
        )
        assert executor.calls == 0
        safe(await connection.get(PATH), 503)
        assert executor.calls == 1


@pytest.mark.asyncio
async def test_exec05_schedule_malformed_executor_results_fail_closed():
    class Malformed(PoisonExecutor):
        async def read(self, *args, **kwargs):
            return {"private": CANARY}

    async with client(application(Malformed())) as connection:
        for path in ("/api/v1/schedules", PATH, PATH + "/occurrences"):
            safe(await connection.get(path), 503)
        safe(
            await browser_request(
                connection,
                "POST",
                PATH + "/disable",
                json={},
                headers={"If-Match": '"instance-configuration-v1-1"'},
            ),
            503,
        )


@pytest.mark.asyncio
async def test_exec05_schedule_timeout_cancels_once_and_does_not_claim_no_commit(monkeypatch):
    class Waiting(PoisonExecutor):
        cancelled = 0

        async def mutate(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled += 1

    executor = Waiting()
    monkeypatch.setattr(route, "_TIMEOUT_SECONDS", 0.01)
    async with client(application(executor)) as connection:
        response = await browser_request(
            connection,
            "POST",
            PATH + "/disable",
            json={},
            headers={"If-Match": '"instance-configuration-v1-1"'},
        )
    safe(response, 503)
    assert "may already have committed" in response.json()["detail"]
    assert executor.cancelled == 1


@pytest.mark.asyncio
async def test_exec05_schedule_application_authorizes_without_route_guards():
    service = ScheduleControlService(reader=None, configurations=None, recurrence=None)
    for principal in (
        human_principal(roles=frozenset({"unrelated"})),
        service_principal(roles=frozenset({"local_admin", "viewer"})),
    ):
        for call in (
            service.read(SCHEDULE, principal=principal),
            service.list_schedules(SchedulePageQuery(), principal=principal),
            service.occurrences(SCHEDULE, SchedulePageQuery(), principal=principal),
            service.mutate(
                ScheduleMutation("disable", SCHEDULE, 1, "correlation.exec05"), principal=principal
            ),
        ):
            with pytest.raises(ScheduleControlError, match="schedule_forbidden"):
                await call
    with pytest.raises(ScheduleControlError, match="schedule_forbidden"):
        await service.mutate(
            ScheduleMutation("disable", SCHEDULE, 1, "correlation.exec05"),
            principal=human_principal(roles=frozenset({"viewer"})),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["not-a-schedule", "schedule.configuration." + "g" * 64, []])
async def test_exec05_schedule_cursor_rejects_non_schedule_identity_before_reader(key):
    raw = json.dumps(
        {"key": [key], "scope": "schedules", "version": 1}, sort_keys=True, separators=(",", ":")
    ).encode()
    cursor = "schedule-v1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    service = ScheduleControlService(reader=None, configurations=None, recurrence=None)
    with pytest.raises(ScheduleControlError, match="schedule_cursor_invalid"):
        await service.list_schedules(
            SchedulePageQuery(cursor=cursor), principal=human_principal(roles=frozenset({"viewer"}))
        )


def test_exec05_schedule_openapi_has_all_seven_typed_operations():
    document = create_app(Settings(_env_file=None)).openapi()
    expected = {
        "/api/v1/schedules": {"get", "post"},
        "/api/v1/schedules/{schedule_id}": {"get", "patch"},
        "/api/v1/schedules/{schedule_id}/enable": {"post"},
        "/api/v1/schedules/{schedule_id}/disable": {"post"},
        "/api/v1/schedules/{schedule_id}/occurrences": {"get"},
    }
    for path, methods in expected.items():
        assert set(document["paths"][path]) == methods
        for method in methods:
            operation = document["paths"][path][method]
            assert operation["operationId"]
            if method in ("post", "patch"):
                assert operation["requestBody"]["required"]
                assert any(
                    item["name"] == "If-Match" and item["required"]
                    for item in operation["parameters"]
                )
    schema = document["components"]["schemas"]["ScheduleResponse"]
    assert "scheduledInput" not in schema["properties"]
    assert "leaseOwner" not in schema["properties"]
