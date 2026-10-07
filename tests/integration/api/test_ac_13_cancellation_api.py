"""AC-13: the cancellation transport and direct application authority boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.api.correlation import install_scope_correlation
from marketing_agents.api.routes import run_cancellation as route
from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.identity import IdentityAuthenticationError
from marketing_agents.application.services.cancellation import (
    RunCancellationCoordinatorError,
    RunCancellationOutcome,
)
from marketing_agents.application.services.run_cancellation_command import (
    RunCancellationCommand,
    RunCancellationCommandError,
    RunCancellationCommandService,
)
from marketing_agents.config import Settings
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run
from marketing_agents.domain.enums import RunState

from tests.support.api import browser_request
from tests.support.identity import StaticIdentityProvider, human_principal, service_principal

RUN_ID = "run.ac13.cancel"
PATH = f"/api/v1/runs/{RUN_ID}/cancel"
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
CANARY = "ac13-secret-user-input-canary"


def operator():
    return human_principal(roles=frozenset({"operator"}), scopes=frozenset())


def outcome():
    return RunCancellationOutcome(
        run=Run(
            id=RUN_ID,
            work_item_id="work.ac13.original",
            state=RunState.CANCELLED,
            catalog_hash="a" * 64,
            configuration_revision=1,
            created_at=NOW - timedelta(seconds=1),
            updated_at=NOW,
            version=2,
            terminal_reason_code="operator_cancelled",
        ),
        cancelled_at=NOW,
        cancelled_step_ids=("step.ac13.queued",),
        preserved_step_ids=("step.ac13.open", "step.ac13.completed"),
        cancelled_action_ids=("action.ac13.queued",),
        preserved_action_ids=("action.ac13.open", "action.ac13.completed"),
        succeeded_effect_count=1,
        outcome_unknown_effect_count=0,
    )


class Executor:
    def __init__(self, *, error=None, result=None):
        self.error = error
        self.result = outcome() if result is None else result
        self.calls = []

    async def request(self, command, *, principal):
        self.calls.append((command, principal))
        if self.error is not None:
            raise self.error
        return self.result


def app(executor, principal=None):
    return create_app(
        Settings(_env_file=None),
        identity_provider=StaticIdentityProvider(operator() if principal is None else principal),
        run_cancellation_service=executor,
    )


def client(application):
    return AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://testserver",
    )


def assert_safe(response, status):
    assert response.status_code == status, response.text
    assert "no-store" in response.headers["cache-control"]
    assert CANARY not in response.text
    if status >= 400:
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["status"] == status


@pytest.mark.asyncio
async def test_ac_13_operator_receives_honest_snapshot_and_server_correlation():
    executor = Executor()
    principal = operator()
    async with client(app(executor, principal)) as connection:
        response = await browser_request(
            connection, "POST", PATH, json={}, headers={"X-Correlation-ID": CANARY}
        )
    assert_safe(response, 200)
    body = response.json()
    assert body == {
        "run_id": RUN_ID,
        "state": "cancelled",
        "version": 2,
        "cancelled_at": "2026-10-07T12:00:00Z",
        "cancelled_step_ids": ["step.ac13.queued"],
        "preserved_step_ids": ["step.ac13.open", "step.ac13.completed"],
        "cancelled_action_ids": ["action.ac13.queued"],
        "preserved_action_ids": ["action.ac13.open", "action.ac13.completed"],
        "succeeded_effect_count_at_cancellation": 1,
        "outcome_unknown_effect_count_at_cancellation": 0,
        "effects_reversed": False,
        "run_url": f"/api/v1/runs/{RUN_ID}",
        "timeline_url": f"/api/v1/runs/{RUN_ID}/timeline",
    }
    ((command, observed_principal),) = executor.calls
    assert observed_principal is principal and command.run_id == RUN_ID
    assert command.correlation_id == response.headers["x-correlation-id"]
    assert command.correlation_id != CANARY


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("viewer", "approver", "local_admin"))
async def test_ac_13_other_human_roles_cannot_resolve_executor(role):
    class PoisonExecutor:
        @property
        def request(self):
            raise AssertionError("unauthorized executor was resolved")

    async with client(
        app(PoisonExecutor(), human_principal(roles=frozenset({role})))
    ) as connection:
        response = await browser_request(connection, "POST", PATH, json={})
    assert_safe(response, 403)
    assert response.json()["code"] == "cancellation_forbidden"


@pytest.mark.asyncio
async def test_ac_13_service_operator_is_forbidden_before_executor():
    executor = Executor()
    async with client(
        app(executor, service_principal(roles=frozenset({"operator"})))
    ) as connection:
        response = await browser_request(connection, "POST", PATH, json={})
    assert_safe(response, 403)
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ("X-Actor-ID", "X-Roles", "X-Scope", "X-Principal"))
async def test_ac_13_identity_spoof_headers_never_reach_cancellation(header):
    executor = Executor()
    async with client(app(executor)) as connection:
        response = await browser_request(
            connection, "POST", PATH, json={}, headers={header: CANARY}
        )
    assert_safe(response, 400)
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    (
        b"",
        b"null",
        b"[]",
        b"true",
        b"{",
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b"[" * 9 + b"0" + b"]" * 9,
        b"\xef\xbb\xbf{}",
        b'{"x":"\\ud800"}',
        *(
            json_body.encode()
            for json_body in (
                '{"actor_id":"' + CANARY + '"}',
                '{"roles":["operator"]}',
                '{"reason":"' + CANARY + '"}',
                '{"expected_version":1}',
            )
        ),
    ),
)
async def test_ac_13_only_strict_empty_object_is_accepted(body):
    executor = Executor()
    async with client(app(executor)) as connection:
        response = await browser_request(
            connection, "POST", PATH, content=body, headers={"Content-Type": "application/json"}
        )
    assert_safe(response, 422)
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    (
        ({"Content-Type": "text/plain"}, b"{}", 403),
        ({"Content-Type": "application/json; charset=latin-1"}, b"{}", 403),
        ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, b"{}", 403),
        ({"Content-Type": "application/json"}, b" " * 1024 + b"{}", 413),
        ({"Content-Type": "application/json", "Content-Length": "-1"}, b"{}", 400),
    ),
)
async def test_ac_13_unsafe_transport_is_rejected(headers, body, expected):
    executor = Executor()
    async with client(app(executor)) as connection:
        response = await browser_request(connection, "POST", PATH, content=body, headers=headers)
    assert_safe(response, expected)
    assert executor.calls == []


@pytest.mark.asyncio
async def test_ac_13_missing_authentication_precedes_executor_resolution():
    class MissingIdentity:
        async def authenticate(self, evidence):
            raise IdentityAuthenticationError("authentication_required", CANARY)

    executor = Executor()
    application = create_app(
        Settings(_env_file=None),
        identity_provider=MissingIdentity(),
        run_cancellation_service=executor,
    )
    async with client(application) as connection:
        response = await browser_request(connection, "POST", PATH, json={}, csrf_app=application)
    assert_safe(response, 401)
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "chunks", "expected"),
    (
        ([(b"content-type", b"text/plain")], [b"{}"], 415),
        (
            [(b"content-type", b"application/json"), (b"content-type", b"application/json")],
            [b"{}"],
            415,
        ),
        (
            [
                (b"content-type", b"application/json"),
                (b"content-length", b"2"),
                (b"content-length", b"2"),
            ],
            [b"{}"],
            400,
        ),
        ([(b"content-type", b"application/json")], [b" " * 700, b" " * 400, b"{}"], 413),
    ),
)
async def test_ac_13_bounds_component_rejects_duplicate_headers_and_stream_overflow(
    headers, chunks, expected
):
    """Exercise inner bounds independently; real app browser policy also rejects these."""
    sent = []
    remaining = list(chunks)

    async def receive():
        return {"type": "http.request", "body": remaining.pop(0), "more_body": bool(remaining)}

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):
        raise AssertionError("invalid request reached the route")

    scope = install_scope_correlation(
        {"type": "http", "method": "POST", "path": PATH, "headers": headers},
        "correlation.api." + "a" * 32,
    )
    await route.RunCancellationRequestBoundsMiddleware(downstream)(scope, receive, send)
    assert sent[0]["status"] == expected
    assert CANARY.encode() not in sent[-1]["body"]
    assert (b"cache-control", b"no-store") in sent[0]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("missing_csrf", "wrong_csrf", "foreign_origin", "query"))
async def test_ac_13_browser_and_query_authority_cannot_be_bypassed(mode):
    executor = Executor()
    async with client(app(executor)) as connection:
        if mode == "missing_csrf":
            response = await connection.post(PATH, json={})
        elif mode == "wrong_csrf":
            response = await browser_request(
                connection, "POST", PATH, json={}, headers={"X-CSRF-Token": CANARY}
            )
        elif mode == "foreign_origin":
            response = await browser_request(
                connection, "POST", PATH, json={}, headers={"Origin": "https://foreign.invalid"}
            )
        else:
            response = await browser_request(connection, "POST", PATH + "?actor=" + CANARY, json={})
    assert_safe(response, 422 if mode == "query" else 403)
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected", "code"),
    (
        (RunCancellationCommandError("run_not_found"), 404, "run_not_found"),
        (RunCancellationCommandError("cancellation_conflict"), 409, "cancellation_conflict"),
        (RunCancellationCommandError(CANARY), 503, "cancellation_unavailable"),
        (RuntimeError(CANARY), 503, "cancellation_unavailable"),
    ),
)
async def test_ac_13_executor_failures_are_safe_and_do_not_retry(error, expected, code):
    executor = Executor(error=error)
    async with client(app(executor)) as connection:
        response = await browser_request(connection, "POST", PATH, json={})
    assert_safe(response, expected)
    assert response.json()["code"] == code and len(executor.calls) == 1
    if expected == 503:
        assert "may already have committed" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("executor", (None, object(), {"request": "not-callable"}))
async def test_ac_13_missing_or_malformed_executor_fails_closed(executor):
    async with client(app(executor)) as connection:
        response = await browser_request(connection, "POST", PATH, json={})
    assert_safe(response, 503)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect", ("mapping", "wrong_run", "unsafe_id", "overlap", "boolean_count")
)
async def test_ac_13_malformed_executor_result_cannot_escape(defect):
    result = outcome()
    if defect == "mapping":
        result = {"run_id": RUN_ID}
    elif defect == "wrong_run":
        result = replace(result, run=replace(result.run, id="run.ac13.other"))
    elif defect == "unsafe_id":
        object.__setattr__(result, "preserved_step_ids", ("../" + CANARY,))
    elif defect == "overlap":
        object.__setattr__(result, "preserved_step_ids", result.cancelled_step_ids)
    else:
        object.__setattr__(result, "succeeded_effect_count", True)
    async with client(app(Executor(result=result))) as connection:
        response = await browser_request(connection, "POST", PATH, json={})
    assert_safe(response, 503)


@pytest.mark.asyncio
async def test_ac_13_timeout_reports_unconfirmed_commit_without_background_retry(monkeypatch):
    class DelayedExecutor:
        calls = 0
        committed = False
        stopped = False

        async def request(self, command, *, principal):
            self.calls += 1
            self.committed = True
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped = True

    executor = DelayedExecutor()
    monkeypatch.setattr(route, "_CANCELLATION_TIMEOUT_SECONDS", 0.01)
    async with client(app(executor)) as connection:
        response = await browser_request(connection, "POST", PATH, json={})
    assert_safe(response, 503)
    assert "may already have committed" in response.json()["detail"]
    assert executor.calls == 1 and executor.committed and executor.stopped


class Coordinator:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def request(self, run_id, *, audit_context):
        self.calls.append((run_id, audit_context))
        if self.error is not None:
            raise self.error
        return outcome()


def command_service(coordinator):
    service = RunCancellationCommandService(cast(OrchestrationDependencies, object()))
    service._coordinator = coordinator
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("viewer", "service", "not_principal", "tampered"))
async def test_ac_13_direct_application_requires_intact_human_operator_before_lookup(kind):
    principal = operator()
    if kind == "viewer":
        principal = human_principal(roles=frozenset({"viewer"}))
    elif kind == "service":
        principal = service_principal(roles=frozenset({"operator"}))
    elif kind == "not_principal":
        principal = object()
    else:
        object.__setattr__(principal, "roles", frozenset({"operator", "forged"}))
    coordinator = Coordinator()
    with pytest.raises(RunCancellationCommandError) as caught:
        await command_service(coordinator).request(
            RunCancellationCommand(RUN_ID, "correlation.ac13.direct"), principal=principal
        )
    assert caught.value.code == "cancellation_forbidden" and coordinator.calls == []


@pytest.mark.asyncio
async def test_ac_13_direct_wrapper_derives_audit_actor_and_delegates_exact_command():
    principal = operator()
    coordinator = Coordinator()
    command = RunCancellationCommand(RUN_ID, "correlation.ac13.direct")
    result = await command_service(coordinator).request(command, principal=principal)
    assert result == outcome()
    ((run_id, context),) = coordinator.calls
    assert run_id == RUN_ID and type(context) is AuditContext
    assert context.binds_authenticated_user(
        actor_id=principal.actor_id,
        authentication_method=principal.authentication_method.value,
        correlation_id=command.correlation_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("run_not_found", "run_not_found"),
        ("terminal_state_immutable", "cancellation_conflict"),
        ("cancellation_conflict", "cancellation_conflict"),
        ("cancellation_result_invalid", "cancellation_unavailable"),
    ),
)
async def test_ac_13_direct_wrapper_maps_only_known_coordinator_failures(code, expected):
    coordinator = Coordinator(RunCancellationCoordinatorError(code, CANARY, run_id=RUN_ID))
    with pytest.raises(RunCancellationCommandError) as caught:
        await command_service(coordinator).request(
            RunCancellationCommand(RUN_ID, "correlation.ac13.direct"), principal=operator()
        )
    assert caught.value.code == expected and CANARY not in str(caught.value)
    assert len(coordinator.calls) == 1


def test_ac_13_openapi_declares_empty_input_and_honest_cancel_operation():
    document = app(Executor()).openapi()
    operation = document["paths"]["/api/v1/runs/{run_id}/cancel"]["post"]
    assert operation["operationId"] == "cancelRun"
    assert operation["requestBody"]["required"] is True
    request_schema = document["components"]["schemas"]["RunCancellationInput"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"] == {}
    assert set(operation["responses"]) >= {
        "200",
        "401",
        "403",
        "404",
        "409",
        "413",
        "415",
        "422",
        "503",
    }
    response = document["components"]["schemas"]["RunCancellationResponse"]
    assert response["properties"]["effects_reversed"]["const"] is False
    assert "counts are snapshots" in operation["description"]
