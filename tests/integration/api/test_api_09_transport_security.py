"""API-09: process-wide transport, browser, timeout, and Problem Details policy."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import create_app
from marketing_agents.api.correlation import request_correlation_id
from marketing_agents.api.csrf import ProcessLocalCsrfToken
from marketing_agents.api.middleware import Api09TransportSecurityMiddleware
from marketing_agents.config import Settings
from pydantic import ValidationError
from starlette.types import Message, Receive, Scope, Send

from tests.integration.api.test_api_04_manual_dry_runs import PATH as MANUAL_PATH
from tests.integration.api.test_api_04_manual_dry_runs import FakeManualDryRunExecutor
from tests.integration.api.test_api_04_manual_dry_runs import _app as manual_app

CORRELATION_PATTERN = re.compile(r"^correlation\.api\.[0-9a-f]{32}$")
CANARY = "api-09-private-attacker-canary"
SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
}


def _settings(*, timeout: float = 30.0) -> Settings:
    return Settings(_env_file=None, api_request_timeout_seconds=timeout)


def _assert_security_headers(response: Response) -> None:
    for name, expected in SECURITY_HEADERS.items():
        assert response.headers[name] == expected


def _assert_problem(
    response: Response,
    *,
    status_code: int,
    code: str,
) -> dict[str, Any]:
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    _assert_security_headers(response)
    correlation_id = response.headers["x-correlation-id"]
    assert CORRELATION_PATTERN.fullmatch(correlation_id)
    payload = response.json()
    assert payload["status"] == status_code
    assert payload["code"] == code
    assert payload["correlation_id"] == correlation_id
    assert payload["instance"] == f"urn:marketing-agents:request:{correlation_id}"
    assert payload["type"] == f"urn:marketing-agents:problem:{code}"
    assert set(payload).issuperset(
        {"type", "title", "status", "detail", "instance", "code", "correlation_id"}
    )
    return payload


async def _csrf_headers(client: AsyncClient) -> dict[str, str]:
    session = await client.get("/api/v1/session")
    assert session.status_code == 200
    return {
        "Origin": "http://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": session.json()["csrfToken"],
    }


def _add_control_route(app: FastAPI, calls: list[str]) -> None:
    @app.post("/api/v1/api-09-control")
    async def api_09_control(request: Request) -> dict[str, object]:
        calls.append(request_correlation_id(request))
        return {"accepted": True}


@pytest.mark.asyncio
async def test_api_09_session_is_private_bounded_and_per_process() -> None:
    first_app = create_app(_settings())
    second_app = create_app(_settings())
    async with AsyncClient(
        transport=ASGITransport(app=first_app),
        base_url="http://testserver",
    ) as first_client:
        first = await first_client.get("/api/v1/session")
        repeated = await first_client.get("/api/v1/session")
    async with AsyncClient(
        transport=ASGITransport(app=second_app),
        base_url="http://testserver",
    ) as second_client:
        second = await second_client.get("/api/v1/session")

    assert first.status_code == repeated.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["vary"] == "Authorization, Origin"
    _assert_security_headers(first)
    payload = first.json()
    assert payload == {
        "actorId": "local-operator",
        "roles": ["approver", "local_admin", "operator", "viewer"],
        "scopes": [
            "approvals:decide",
            "approvals:read",
            "approvals:request",
            "scope.external-write",
        ],
        "authMode": "local",
        "environment": "local",
        "modelMode": "mock",
        "connectorMode": "mock",
        "networkPermission": False,
        "warning": "Local identity — not production authentication",
        "csrfToken": payload["csrfToken"],
        "csrfHeaderName": "X-CSRF-Token",
    }
    assert len(payload["csrfToken"]) >= 32
    assert repeated.json()["csrfToken"] == payload["csrfToken"]
    assert second.json()["csrfToken"] != payload["csrfToken"]
    assert payload["csrfToken"] not in repr(first_app.state.csrf_token)


@pytest.mark.parametrize(
    "origin",
    [
        "http://user:password@testserver",
        "http://testserver/path",
        "http://testserver?query=1",
        "http://testserver#fragment",
        "http://testserver:not-a-port",
        "http://testserver:0",
        "HTTP://testserver",
        "http://testserver:80",
        "http://TESTSERVER",
    ],
)
def test_api_09_settings_reject_noncanonical_trusted_origins(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_trusted_origins=(origin,))


def test_api_09_settings_reject_origins_duplicate_after_normalization() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            api_trusted_origins=("http://testserver", "http://testserver"),
        )


def test_api_09_openapi_uses_one_problem_details_media_type() -> None:
    schema = create_app(_settings()).openapi()
    problem = schema["components"]["schemas"]["ProblemDetails"]
    assert set(problem["required"]) == {
        "type",
        "title",
        "status",
        "detail",
        "instance",
        "code",
        "correlation_id",
    }
    assert problem["additionalProperties"] is False
    session_responses = schema["paths"]["/api/v1/session"]["get"]["responses"]
    assert session_responses["default"]["content"] == {
        "application/problem+json": {
            "schema": {"$ref": "#/components/schemas/ProblemDetails"},
        }
    }
    manual_responses = schema["paths"]["/api/v1/agent-instances/{instance_id}/dry-runs"]["post"][
        "responses"
    ]
    for status_code, response in manual_responses.items():
        if status_code == "default" or int(status_code) >= 400:
            assert set(response["content"]) == {"application/problem+json"}


@pytest.mark.asyncio
async def test_api_09_framework_and_route_errors_share_strict_problem_details() -> None:
    app = create_app(_settings())

    @app.get("/api/v1/api-09-http-exception")
    async def unsafe_exception() -> None:
        raise HTTPException(status_code=400, detail=CANARY, headers={"X-Canary": CANARY})

    @app.get("/api/v1/api-09-direct-error")
    async def direct_error() -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "code": "api_09_direct_conflict",
                "message": CANARY,
                "current_revision": 0,
            },
        )

    @app.get("/api/v1/api-09-unhandled")
    async def unhandled_error() -> None:
        raise RuntimeError(CANARY)

    @app.get("/api/v1/api-09-validation")
    async def validation(value: int = Query()) -> dict[str, int]:
        return {"value": value}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        missing = await client.get("/api/v1/api-09-missing")
        method = await client.post("/health/live")
        exception = await client.get("/api/v1/api-09-http-exception")
        direct = await client.get("/api/v1/api-09-direct-error")
        unhandled = await client.get("/api/v1/api-09-unhandled")
        validation_response = await client.get(
            "/api/v1/api-09-validation",
            params={"value": CANARY},
        )

    _assert_problem(missing, status_code=404, code="resource_not_found")
    _assert_problem(method, status_code=405, code="method_not_allowed")
    _assert_problem(exception, status_code=400, code="request_invalid")
    assert "x-canary" not in exception.headers
    assert CANARY not in exception.text
    direct_payload = _assert_problem(direct, status_code=409, code="api_09_direct_conflict")
    assert direct_payload["current_resource_version"] == 0
    assert CANARY not in direct.text
    _assert_problem(unhandled, status_code=500, code="internal_server_error")
    assert CANARY not in unhandled.text
    validation_payload = _assert_problem(
        validation_response,
        status_code=422,
        code="request_validation_failed",
    )
    assert validation_payload["field_errors"] == [
        {
            "pointer": "/query",
            "code": "int_parsing",
            "message": "invalid request field",
        }
    ]
    assert CANARY not in validation_response.text


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"Host": "evil.example"}, "host_header_invalid"),
        ({"Host": "localhost:not-a-port"}, "host_header_invalid"),
        ({"Host": "localhost:70000"}, "host_header_invalid"),
        ({"Forwarded": "for=127.0.0.1"}, "forwarded_header_forbidden"),
        ({"X-Forwarded-For": "127.0.0.1"}, "forwarded_header_forbidden"),
        ({"X-Forwarded-Host": "testserver"}, "forwarded_header_forbidden"),
        ({"X-Forwarded-Proto": "http"}, "forwarded_header_forbidden"),
    ],
)
@pytest.mark.asyncio
async def test_api_09_host_and_direct_proxy_rejections_precede_handlers(
    headers: dict[str, str],
    code: str,
) -> None:
    app = create_app(_settings())
    calls: list[str] = []

    @app.get("/api/v1/api-09-host-probe")
    async def host_probe() -> dict[str, bool]:
        calls.append("called")
        return {"called": True}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/api-09-host-probe", headers=headers)

    _assert_problem(response, status_code=400, code=code)
    assert calls == []


@pytest.mark.asyncio
async def test_api_09_duplicate_host_and_caller_correlation_are_not_trusted() -> None:
    app = create_app(_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        duplicate = await client.get(
            "/health/live",
            headers=[("Host", "testserver"), ("Host", "localhost")],
        )
        spoofed = await client.get(
            "/health/live",
            headers={"X-Correlation-ID": CANARY},
        )

    _assert_problem(duplicate, status_code=400, code="host_header_invalid")
    assert spoofed.status_code == 200
    assert CORRELATION_PATTERN.fullmatch(spoofed.headers["x-correlation-id"])
    assert spoofed.headers["x-correlation-id"] != CANARY
    assert CANARY not in spoofed.text


@pytest.mark.parametrize("host", ["testserver:443", "LOCALHOST:8000", "[::1]:8000"])
@pytest.mark.asyncio
async def test_api_09_accepts_only_canonical_trusted_host_names(host: str) -> None:
    app = create_app(_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health/live", headers={"Host": host})

    assert response.status_code == 200
    _assert_security_headers(response)


@pytest.mark.parametrize(
    "mutation_headers",
    [
        {},
        {"Origin": "http://testserver"},
        {
            "Origin": "http://evil.example",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": "wrong",
        },
        {
            "Origin": "http://testserver",
            "Sec-Fetch-Site": "cross-site",
            "X-CSRF-Token": "wrong",
        },
        {
            "Origin": "null",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": "wrong",
        },
    ],
)
@pytest.mark.asyncio
async def test_api_09_browser_mutation_rejections_are_generic_and_pre_handler(
    mutation_headers: dict[str, str],
) -> None:
    app = create_app(_settings())
    calls: list[str] = []
    _add_control_route(app, calls)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/api-09-control",
            json={"value": CANARY},
            headers=mutation_headers,
        )

    _assert_problem(response, status_code=403, code="browser_request_forbidden")
    assert CANARY not in response.text
    assert calls == []


@pytest.mark.asyncio
async def test_api_09_browser_mutation_accepts_only_current_same_origin_token() -> None:
    old_app = create_app(_settings())
    app = create_app(_settings())
    calls: list[str] = []
    _add_control_route(app, calls)
    async with AsyncClient(
        transport=ASGITransport(app=old_app),
        base_url="http://testserver",
    ) as old_client:
        stale_headers = await _csrf_headers(old_client)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        current_headers = await _csrf_headers(client)
        stale = await client.post(
            "/api/v1/api-09-control",
            json={"value": "safe"},
            headers=stale_headers,
        )
        valid = await client.post(
            "/api/v1/api-09-control",
            json={"value": "safe"},
            headers=current_headers,
        )
        simple_form = await client.post(
            "/api/v1/api-09-control",
            content=b"value=safe",
            headers={**current_headers, "Content-Type": "application/x-www-form-urlencoded"},
        )

    _assert_problem(stale, status_code=403, code="csrf_token_invalid")
    assert stale.headers["vary"] == "Authorization, Origin"
    assert valid.status_code == 200
    assert valid.json() == {"accepted": True}
    _assert_security_headers(valid)
    assert calls == [valid.headers["x-correlation-id"]]
    _assert_problem(simple_form, status_code=403, code="browser_request_forbidden")


@pytest.mark.asyncio
async def test_api_09_duplicate_browser_security_headers_fail_closed() -> None:
    app = create_app(_settings())
    calls: list[str] = []
    _add_control_route(app, calls)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        current = await _csrf_headers(client)
        duplicate_origin = await client.post(
            "/api/v1/api-09-control",
            content=b"{}",
            headers=[
                ("Content-Type", "application/json"),
                ("Origin", "http://testserver"),
                ("Origin", "http://testserver"),
                ("Sec-Fetch-Site", "same-origin"),
                ("X-CSRF-Token", current["X-CSRF-Token"]),
            ],
        )
        duplicate_csrf = await client.post(
            "/api/v1/api-09-control",
            content=b"{}",
            headers=[
                ("Content-Type", "application/json"),
                ("Origin", "http://testserver"),
                ("Sec-Fetch-Site", "same-origin"),
                ("X-CSRF-Token", current["X-CSRF-Token"]),
                ("X-CSRF-Token", current["X-CSRF-Token"]),
            ],
        )

    _assert_problem(duplicate_origin, status_code=403, code="browser_request_forbidden")
    _assert_problem(duplicate_csrf, status_code=403, code="browser_request_forbidden")
    assert calls == []


@pytest.mark.asyncio
async def test_api_09_real_route_command_reuses_response_correlation() -> None:
    executor = FakeManualDryRunExecutor()
    app = manual_app(executor)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        headers = await _csrf_headers(client)
        headers["Idempotency-Key"] = "retry-key-API09-0001"
        response = await client.post(
            MANUAL_PATH,
            json={"input": {"topic": "safe"}},
            headers=headers,
        )

    assert response.status_code == 202
    assert len(executor.commands) == 1
    assert executor.commands[0].correlation_id == response.headers["x-correlation-id"]
    assert CORRELATION_PATTERN.fullmatch(executor.commands[0].correlation_id)


@pytest.mark.asyncio
async def test_api_09_browser_policy_is_root_path_safe_when_mounted() -> None:
    child = create_app(_settings())
    calls: list[str] = []
    _add_control_route(child, calls)
    parent = FastAPI()
    parent.mount("/control", child)
    async with AsyncClient(
        transport=ASGITransport(app=parent),
        base_url="http://testserver",
    ) as client:
        session = await client.get("/control/api/v1/session")
        headers = {
            "Origin": "http://testserver",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": session.json()["csrfToken"],
        }
        rejected = await client.post(
            "/control/api/v1/api-09-control",
            json={"safe": True},
        )
        accepted = await client.post(
            "/control/api/v1/api-09-control",
            json={"safe": True},
            headers=headers,
        )

    _assert_problem(rejected, status_code=403, code="browser_request_forbidden")
    assert accepted.status_code == 200
    assert calls == [accepted.headers["x-correlation-id"]]


@pytest.mark.asyncio
async def test_api_09_timeout_cancels_once_without_retry() -> None:
    app = create_app(_settings(timeout=0.01))
    calls: list[int] = []

    @app.get("/api/v1/api-09-slow")
    async def slow() -> dict[str, bool]:
        calls.append(1)
        await asyncio.sleep(1)
        return {"completed": True}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/api-09-slow")

    _assert_problem(response, status_code=503, code="request_timeout")
    assert response.json()["title"] == "Request Timeout"
    assert calls == [1]


def _scope(path: str, *, method: str = "GET") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1),
        "scheme": "http",
        "method": method,
        "root_path": "",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": ((b"host", b"testserver"), (b"content-type", b"application/json")),
        "state": {},
    }


async def _empty_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_api_09_timeout_after_response_start_never_double_starts() -> None:
    async def partial_response(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(1)

    middleware = Api09TransportSecurityMiddleware(
        partial_response,
        settings=_settings(timeout=0.01),
        csrf_token=ProcessLocalCsrfToken(),
    )
    messages: list[Message] = []

    async def capture(message: Message) -> None:
        messages.append(message)

    with pytest.raises(TimeoutError):
        await middleware(_scope("/api/v1/api-09-partial"), _empty_receive, capture)

    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 200


@pytest.mark.asyncio
async def test_api_09_webhook_exemption_preserves_exact_receive_bytes() -> None:
    chunks = (b'{"payload":"', b"exact\\u0000bytes", b'"}')
    received: list[bytes] = []

    async def webhook_app(_scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            received.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    pending = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]

    async def receive() -> Message:
        return pending.pop(0)

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    middleware = Api09TransportSecurityMiddleware(
        webhook_app,
        settings=_settings(),
        csrf_token=ProcessLocalCsrfToken(),
    )
    await middleware(
        _scope("/api/v1/webhooks/source.test/trigger.test", method="POST"),
        receive,
        send,
    )

    assert tuple(received) == chunks
    assert b"".join(received) == b"".join(chunks)
    assert messages[0]["status"] == 202
