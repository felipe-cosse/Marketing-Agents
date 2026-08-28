"""API-05: raw authenticated webhook transport remains a narrow async boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import create_app
from marketing_agents.api.dependencies import WebhookAdmissionExecutor
from marketing_agents.api.routes import webhooks as webhook_routes
from marketing_agents.application.ports.identity import AuthenticationEvidence
from marketing_agents.application.services.webhook_intake import (
    MAX_WEBHOOK_BODY_BYTES,
    WebhookAdmissionCommand,
    WebhookAdmissionDisposition,
    WebhookAdmissionResult,
    WebhookAdmissionServiceError,
)
from marketing_agents.config import Settings
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.webhook import WebhookReceipt, WebhookReceiptDelivery

from tests.support.api import assert_problem

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
SOURCE = "source.api05"
TRIGGER_ID = "trigger.webhook.api05"
PATH = f"/api/v1/webhooks/{SOURCE}/{TRIGGER_ID}"
NOW = datetime(2026, 8, 26, 20, 30, tzinfo=UTC)
CANARY = "webhook-http-secret-canary"
RAW_BODY = b'{ "eventId" : "event.api05.01", "input" : {"text":"exact\\nbytes"} }\n'


class NeverIdentityProvider:
    """Fail loudly if the webhook route inherits browser/local authentication."""

    def __init__(self) -> None:
        self.calls = 0

    async def authenticate(self, _evidence: AuthenticationEvidence) -> AuthenticatedPrincipal:
        self.calls += 1
        raise AssertionError("webhooks must not call the local identity provider")


class FakeWebhookAdmissionExecutor:
    def __init__(self) -> None:
        self.commands: list[WebhookAdmissionCommand] = []
        self.error: Exception | None = None
        self.disposition = WebhookAdmissionDisposition.CREATED
        self.result_mutator: (
            Callable[[WebhookAdmissionResult, WebhookAdmissionCommand], object] | None
        ) = None
        self.block = False
        self.cancelled = False

    async def submit(self, command: WebhookAdmissionCommand) -> WebhookAdmissionResult:
        self.commands.append(command)
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.error is not None:
            raise self.error
        result = _result(command, disposition=self.disposition)
        if self.result_mutator is not None:
            return cast(WebhookAdmissionResult, self.result_mutator(result, command))
        return result


class SynchronousWebhookAdmissionExecutor:
    def __init__(self) -> None:
        self.called = False

    def submit(self, _command: WebhookAdmissionCommand) -> object:
        self.called = True
        return object()


def _settings() -> Settings:
    return Settings(_env_file=None, catalog_root=CATALOG_ROOT)


def _app(
    executor: object | None,
    *,
    identity_provider: NeverIdentityProvider | None = None,
) -> FastAPI:
    return create_app(
        _settings(),
        identity_provider=identity_provider or NeverIdentityProvider(),
        webhook_admission_service=cast(WebhookAdmissionExecutor | None, executor),
    )


def _headers() -> list[tuple[str, str]]:
    return [
        ("Content-Type", "application/json"),
        ("X-Webhook-Timestamp", "1787776200"),
        ("X-Webhook-Signature", "v1=" + ("a" * 64)),
    ]


async def _request(
    app: FastAPI,
    *,
    content: Any = RAW_BODY,
    headers: Any = None,
) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.post(
            PATH,
            content=content,
            headers=_headers() if headers is None else headers,
        )


def _receipt(command: WebhookAdmissionCommand) -> WebhookReceipt:
    return WebhookReceipt(
        id="receipt.webhook.api05.01",
        source=command.source,
        event_id="event.api05.01",
        trigger_id=command.trigger_id,
        body_digest="b" * 64,
        digest_key_version="webhook-body-hmac-sha256-v1:" + ("c" * 64),
        mapper_version="webhook-envelope-input-v1",
        received_at=NOW,
        deliveries=(
            WebhookReceiptDelivery(
                instance_id="inst.webhook.target.02",
                work_item_id="work.webhook.api05.02",
                run_id="run.webhook.api05.02",
            ),
            WebhookReceiptDelivery(
                instance_id="inst.webhook.target.01",
                work_item_id="work.webhook.api05.01",
                run_id="run.webhook.api05.01",
            ),
        ),
    )


def _result(
    command: WebhookAdmissionCommand,
    *,
    disposition: WebhookAdmissionDisposition,
) -> WebhookAdmissionResult:
    return WebhookAdmissionResult(
        receipt=_receipt(command),
        disposition=disposition,
    )


@pytest.mark.asyncio
async def test_api_05_preserves_exact_body_and_headers_without_local_identity() -> None:
    identity = NeverIdentityProvider()
    executor = FakeWebhookAdmissionExecutor()
    headers = [
        *_headers(),
        ("Authorization", "Bearer browser-token-must-be-ignored"),
        ("X-Custom-Webhook-Metadata", "opaque-value"),
    ]

    response = await _request(
        _app(executor, identity_provider=identity),
        content=RAW_BODY,
        headers=headers,
    )

    assert response.status_code == 202
    assert identity.calls == 0
    assert len(executor.commands) == 1
    command = executor.commands[0]
    assert command.raw_body == RAW_BODY
    assert command.source == SOURCE
    assert command.trigger_id == TRIGGER_ID
    assert command.correlation_id == response.headers["x-correlation-id"]
    assert ("x-webhook-timestamp", "1787776200") in command.received_headers
    assert ("x-webhook-signature", "v1=" + ("a" * 64)) in command.received_headers
    assert (
        "authorization",
        "Bearer browser-token-must-be-ignored",
    ) in command.received_headers
    assert ("x-custom-webhook-metadata", "opaque-value") in command.received_headers
    assert RAW_BODY not in repr(command).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    (WebhookAdmissionDisposition.CREATED, WebhookAdmissionDisposition.REPLAYED),
)
async def test_api_05_returns_typed_202_fan_out_without_claiming_execution(
    disposition: WebhookAdmissionDisposition,
) -> None:
    executor = FakeWebhookAdmissionExecutor()
    executor.disposition = disposition

    response = await _request(_app(executor))

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "accepted",
        "disposition": disposition.value,
        "source": SOURCE,
        "eventId": "event.api05.01",
        "receiptId": "receipt.webhook.api05.01",
        "deliveries": [
            {
                "instanceId": "inst.webhook.target.01",
                "workId": "work.webhook.api05.01",
                "runId": "run.webhook.api05.01",
                "instanceUrl": "/api/v1/agent-instances/inst.webhook.target.01",
                "runUrl": "/api/v1/runs/run.webhook.api05.01",
            },
            {
                "instanceId": "inst.webhook.target.02",
                "workId": "work.webhook.api05.02",
                "runId": "run.webhook.api05.02",
                "instanceUrl": "/api/v1/agent-instances/inst.webhook.target.02",
                "runUrl": "/api/v1/runs/run.webhook.api05.02",
            },
        ],
    }
    assert all(word not in response.text for word in ("executed", "sent", "completed"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_code", "expected_status", "expected_code", "pointer"),
    (
        ("webhook_authentication_failed", 401, "webhook_authentication_failed", None),
        ("webhook_binding_forbidden", 403, "webhook_forbidden", None),
        ("webhook_idempotency_conflict", 409, "webhook_idempotency_conflict", None),
        ("idempotency_conflict", 409, "webhook_idempotency_conflict", None),
        ("webhook_envelope_invalid", 422, "webhook_input_invalid", None),
        ("input_schema_invalid", 422, "webhook_input_invalid", "/input/topic"),
        ("webhook_service_unavailable", 503, "webhook_unavailable", None),
        ("internal_canary_failure", 503, "webhook_unavailable", None),
    ),
)
async def test_api_05_maps_service_errors_to_non_reflective_stable_problems(
    service_code: str,
    expected_status: int,
    expected_code: str,
    pointer: str | None,
) -> None:
    executor = FakeWebhookAdmissionExecutor()
    executor.error = WebhookAdmissionServiceError(service_code, CANARY, pointer=pointer)

    response = await _request(_app(executor))

    payload = assert_problem(
        response,
        status_code=expected_status,
        code=expected_code,
    )
    expected_field_errors = (
        [
            {
                "pointer": pointer,
                "code": expected_code,
                "message": "invalid request field",
            }
        ]
        if pointer is not None
        else None
    )
    assert payload.get("field_errors") == expected_field_errors
    assert CANARY not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_api_09_rate_limit_problem_preserves_retry_contract() -> None:
    executor = FakeWebhookAdmissionExecutor()
    executor.error = WebhookAdmissionServiceError(
        "webhook_rate_limited",
        CANARY,
        retry_after_seconds=17,
    )

    response = await _request(_app(executor))

    payload = assert_problem(
        response,
        status_code=429,
        code="webhook_rate_limited",
    )
    assert response.headers.get_list("retry-after") == ["17"]
    assert payload["retry_after_seconds"] == 17
    assert CANARY not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    (
        [],
        [("Content-Type", "text/plain")],
        [("Content-Type", "application/problem+json")],
        [("Content-Type", "application/json; charset=iso-8859-1")],
        [("Content-Type", "application/json"), ("Content-Type", "application/json")],
        [("Content-Type", "application/json"), ("Content-Encoding", "gzip")],
        [("Content-Type", "application/json"), ("Content-Encoding", "identity")],
    ),
)
async def test_api_05_requires_one_unencoded_application_json_transport(
    headers: list[tuple[str, str]],
) -> None:
    executor = FakeWebhookAdmissionExecutor()

    response = await _request(_app(executor), headers=headers)

    assert_problem(response, status_code=415, code="media_type_unsupported")
    assert response.headers["cache-control"] == "no-store"
    assert executor.commands == []


@pytest.mark.asyncio
async def test_api_05_accepts_utf8_json_media_type_but_defers_json_parsing() -> None:
    executor = FakeWebhookAdmissionExecutor()
    invalid_json = b"not-json-but-still-signed-raw-bytes"

    response = await _request(
        _app(executor),
        content=invalid_json,
        headers=[
            ("Content-Type", "APPLICATION/JSON; CHARSET=UTF-8"),
            ("X-Webhook-Timestamp", "not-yet-validated"),
            ("X-Webhook-Signature", "not-yet-validated"),
        ],
    )

    assert response.status_code == 202
    assert executor.commands[0].raw_body == invalid_json


@pytest.mark.asyncio
async def test_api_05_raw_body_limit_accepts_boundary_and_rejects_buffered_and_chunked_excess() -> (
    None
):
    executor = FakeWebhookAdmissionExecutor()
    boundary = b"x" * MAX_WEBHOOK_BODY_BYTES
    accepted = await _request(_app(executor), content=boundary)

    assert accepted.status_code == 202
    assert executor.commands[0].raw_body == boundary

    oversized = b"x" * (MAX_WEBHOOK_BODY_BYTES + 1)
    rejected = await _request(_app(executor), content=oversized)
    assert_problem(rejected, status_code=413, code="webhook_body_too_large")
    assert rejected.headers["cache-control"] == "no-store"
    assert len(executor.commands) == 1

    async def streamed_oversized_body() -> AsyncIterator[bytes]:
        yield b"x" * (MAX_WEBHOOK_BODY_BYTES // 2)
        yield b"x" * (MAX_WEBHOOK_BODY_BYTES // 2 + 1)

    streamed = await _request(_app(executor), content=streamed_oversized_body())
    assert_problem(streamed, status_code=413, code="webhook_body_too_large")
    assert streamed.headers["cache-control"] == "no-store"
    assert len(executor.commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_length_headers",
    (
        [("Content-Length", "01")],
        [("Content-Length", "-1")],
        [("Content-Length", str(MAX_WEBHOOK_BODY_BYTES + 1))],
        [("Content-Length", "1"), ("Content-Length", "1")],
    ),
)
async def test_api_05_rejects_ambiguous_or_invalid_declared_lengths_before_service(
    content_length_headers: list[tuple[str, str]],
) -> None:
    executor = FakeWebhookAdmissionExecutor()

    response = await _request(
        _app(executor),
        content=b"x",
        headers=[*_headers(), *content_length_headers],
    )

    assert_problem(response, status_code=413, code="webhook_body_too_large")
    assert executor.commands == []


@pytest.mark.asyncio
async def test_api_05_passes_malformed_and_duplicate_signature_headers_to_service() -> None:
    executor = FakeWebhookAdmissionExecutor()
    headers = [
        ("Content-Type", "application/json"),
        ("X-Webhook-Timestamp", "not-a-timestamp"),
        ("x-webhook-timestamp", "1787776200"),
        ("X-Webhook-Signature", "malformed"),
        ("x-webhook-signature", "v1=" + ("d" * 64)),
    ]

    response = await _request(_app(executor), headers=headers)

    assert response.status_code == 202
    relevant = tuple(
        item
        for item in executor.commands[0].received_headers
        if item[0] in {"x-webhook-timestamp", "x-webhook-signature"}
    )
    assert relevant == (
        ("x-webhook-timestamp", "not-a-timestamp"),
        ("x-webhook-timestamp", "1787776200"),
        ("x-webhook-signature", "malformed"),
        ("x-webhook-signature", "v1=" + ("d" * 64)),
    )


@pytest.mark.asyncio
async def test_api_05_timeout_cancels_executor_and_returns_bounded_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhook_routes, "_SUBMIT_TIMEOUT_SECONDS", 0.01)
    executor = FakeWebhookAdmissionExecutor()
    executor.block = True

    response = await _request(_app(executor))

    assert_problem(response, status_code=503, code="webhook_unavailable")
    assert response.headers["cache-control"] == "no-store"
    assert executor.cancelled is True


@pytest.mark.asyncio
async def test_api_05_missing_sync_failing_and_invalid_results_fail_closed() -> None:
    missing = await _request(_app(None))
    assert_problem(missing, status_code=503, code="service_unavailable")

    synchronous = SynchronousWebhookAdmissionExecutor()
    sync_response = await _request(_app(synchronous))
    assert_problem(sync_response, status_code=503, code="service_unavailable")
    assert synchronous.called is False

    failing = FakeWebhookAdmissionExecutor()
    failing.error = RuntimeError(CANARY)
    failure = await _request(_app(failing))
    assert_problem(failure, status_code=503, code="webhook_unavailable")
    assert CANARY not in failure.text

    wrong_type = FakeWebhookAdmissionExecutor()
    wrong_type.result_mutator = lambda _result, _command: object()
    invalid = await _request(_app(wrong_type))
    assert_problem(invalid, status_code=503, code="webhook_unavailable")

    mismatched = FakeWebhookAdmissionExecutor()

    def change_source(result: WebhookAdmissionResult, _command: WebhookAdmissionCommand) -> object:
        object.__setattr__(result.receipt, "source", "source.other")
        return result

    mismatched.result_mutator = change_source
    mismatch = await _request(_app(mismatched))
    assert_problem(mismatch, status_code=503, code="webhook_unavailable")


def test_api_05_openapi_exposes_only_exact_webhook_transport_contract() -> None:
    openapi = _app(FakeWebhookAdmissionExecutor()).openapi()
    path = "/api/v1/webhooks/{source}/{trigger_id}"
    assert set(openapi["paths"][path]) == {"post"}
    operation = openapi["paths"][path]["post"]

    assert operation["operationId"] == "admitWebhookEvent"
    assert operation.get("security") in (None, [])
    assert operation["parameters"] == [
        {
            "name": "source",
            "in": "path",
            "required": True,
            "schema": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,99}$",
                "title": "Source",
            },
        },
        {
            "name": "trigger_id",
            "in": "path",
            "required": True,
            "schema": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,199}$",
                "title": "Trigger Id",
            },
        },
        {
            "name": "X-Webhook-Timestamp",
            "in": "header",
            "required": True,
            "schema": {
                "type": "string",
                "pattern": r"^(?:0|[1-9][0-9]{0,11})$",
            },
        },
        {
            "name": "X-Webhook-Signature",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "pattern": r"^v1=[0-9a-f]{64}$"},
        },
    ]
    assert operation["requestBody"] == {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["eventId", "input"],
                    "properties": {
                        "eventId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$",
                        },
                        "input": {"type": "object"},
                    },
                }
            }
        },
    }
    assert set(operation["responses"]) == {
        "202",
        "400",
        "401",
        "403",
        "409",
        "413",
        "415",
        "422",
        "429",
        "503",
        "default",
    }
    assert operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WebhookAdmissionResponse"
    }

    response_schema = openapi["components"]["schemas"]["WebhookAdmissionResponse"]
    assert response_schema["additionalProperties"] is False
    assert response_schema["required"] == [
        "status",
        "disposition",
        "source",
        "eventId",
        "receiptId",
        "deliveries",
    ]
    assert set(response_schema["properties"]) == {
        "status",
        "disposition",
        "source",
        "eventId",
        "receiptId",
        "deliveries",
    }
    assert response_schema["properties"]["status"]["const"] == "accepted"
    assert response_schema["properties"]["disposition"]["enum"] == [
        "created",
        "replayed",
    ]
    assert response_schema["properties"]["deliveries"]["minItems"] == 1
    assert response_schema["properties"]["deliveries"]["maxItems"] == 43
