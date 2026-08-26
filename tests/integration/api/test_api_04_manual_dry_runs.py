"""API-04: authorized manual work is accepted without executing inline."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import create_app
from marketing_agents.api.dependencies import ManualDryRunExecutor
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunResult,
    ManualDryRunServiceError,
)
from marketing_agents.config import Settings
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.security.redaction import SecretValue

from tests.support.identity import (
    StaticIdentityProvider,
    human_principal,
    service_principal,
)

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
INSTANCE_ID = "inst.social-media.content-ideation.content-idea-generator.01"
PATH = f"/api/v1/agent-instances/{INSTANCE_ID}/dry-runs"
CANARY = "manual-api-secret-canary"
NOW = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)


def _work(command: ManualDryRunCommand) -> WorkItem:
    event_id = "manual-event-hmac-sha256-v1:" + ("a" * 64)
    return WorkItem(
        id="work.manual.api04.01",
        source="manual",
        event_id=event_id,
        instance_id=command.instance_id,
        trigger_id="trigger.manual.api04",
        workflow_id="workflow.manual.api04",
        mode=command.mode,
        brief_id=command.campaign_brief_id,
        configuration_revision=3,
        input_digest="b" * 64,
        admission_digest="c" * 64,
        created_at=NOW,
        brief_revision=1 if command.campaign_brief_id is not None else None,
        digest_key_version="admission-hmac-sha256-v1:" + ("d" * 64),
        admitted_payload=command.input_payload,
        redacted_input_projection={"topic": "[REDACTED]"},
        input_schema_id="schema.manual.api04",
        input_schema_hash="schema-sha256-v1:" + ("e" * 64),
        input_projection_created_at=NOW,
        input_projection_expires_at=NOW + timedelta(days=7),
        input_projection_integrity_digest="f" * 64,
    )


def _result(
    command: ManualDryRunCommand,
    *,
    disposition: WorkRunReceiptDisposition = WorkRunReceiptDisposition.CREATED,
) -> ManualDryRunResult:
    work = _work(command)
    run = Run(
        id="run.manual.api04.01",
        work_item_id=work.id,
        state=RunState.RECEIVED,
        catalog_hash="catalog-sha256-v1:" + ("1" * 64),
        configuration_revision=work.configuration_revision,
        created_at=NOW,
        updated_at=NOW,
    )
    return ManualDryRunResult(
        work_item=work,
        run=run,
        disposition=disposition,
        event_id=work.event_id,
        mode=command.mode,
    )


class FakeManualDryRunExecutor:
    def __init__(self) -> None:
        self.commands: list[ManualDryRunCommand] = []
        self.principals: list[AuthenticatedPrincipal] = []
        self.error: Exception | None = None
        self.disposition = WorkRunReceiptDisposition.CREATED
        self.result_mutator: (
            Callable[
                [ManualDryRunResult, ManualDryRunCommand, AuthenticatedPrincipal],
                object,
            ]
            | None
        ) = None

    async def submit(
        self,
        command: ManualDryRunCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ManualDryRunResult:
        self.commands.append(command)
        self.principals.append(principal)
        if self.error is not None:
            raise self.error
        result = _result(command, disposition=self.disposition)
        if self.result_mutator is not None:
            return cast(
                ManualDryRunResult,
                self.result_mutator(result, command, principal),
            )
        return result


class SynchronousManualDryRunExecutor:
    def __init__(self) -> None:
        self.called = False

    def submit(self, *_args: object, **_kwargs: object) -> object:
        self.called = True
        return object()


def _settings() -> Settings:
    return Settings(_env_file=None, catalog_root=CATALOG_ROOT)


def _operator() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.test.operator",
        roles=frozenset({"operator"}),
        scopes=frozenset(),
    )


def _app(
    executor: object | None,
    *,
    principal: AuthenticatedPrincipal | None = None,
) -> FastAPI:
    return create_app(
        _settings(),
        identity_provider=StaticIdentityProvider(principal or _operator()),
        manual_dry_run_service=cast(ManualDryRunExecutor | None, executor),
    )


async def _request(
    app: FastAPI,
    *,
    json: object | None = None,
    content: bytes | None = None,
    headers: Any = None,
) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.post(
            PATH,
            json=json,
            content=content,
            headers=headers,
        )


def _mutate_and_return(
    result: ManualDryRunResult,
    target: object,
    field_name: str,
    value: object,
) -> ManualDryRunResult:
    object.__setattr__(target, field_name, value)
    return result


@pytest.mark.asyncio
async def test_api_04_accepts_default_dry_run_and_retains_only_server_authority() -> None:
    executor = FakeManualDryRunExecutor()
    raw_key = "retry-key-API04-0001"
    response = await _request(
        _app(executor),
        json={"input": {"topic": CANARY}},
        headers={"Idempotency-Key": raw_key},
    )

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert response.json() == {
        "status": "accepted",
        "disposition": "created",
        "eventId": "manual-event-hmac-sha256-v1:" + ("a" * 64),
        "workId": "work.manual.api04.01",
        "runId": "run.manual.api04.01",
        "executionMode": "dry_run",
        "instanceUrl": f"/api/v1/agent-instances/{INSTANCE_ID}",
        "runUrl": "/api/v1/runs/run.manual.api04.01",
    }
    command = executor.commands[0]
    assert command.instance_id == INSTANCE_ID
    assert command.input_payload == {"topic": CANARY}
    assert command.mode is WorkMode.DRY_RUN
    assert type(command.idempotency_key) is SecretValue
    assert command.idempotency_key.reveal() == raw_key
    assert command.campaign_brief_id is None
    assert command.demo_scenario_id is None
    assert command.correlation_id.startswith("correlation.manual-api.")
    assert executor.principals == [_operator()]
    assert raw_key not in repr(command)
    assert raw_key not in response.text
    assert all(word not in response.text for word in ("published", "sent", "completed"))


@pytest.mark.asyncio
async def test_api_04_maps_mock_execute_and_replay_without_claiming_execution() -> None:
    executor = FakeManualDryRunExecutor()
    executor.disposition = WorkRunReceiptDisposition.REPLAYED
    response = await _request(
        _app(executor),
        json={
            "input": {"topic": "safe"},
            "executionMode": "mock_execute",
            "campaignBriefId": "brief.api04.01",
            "demoScenarioId": "demo.api04.01",
        },
        headers={"Idempotency-Key": "retry-key-API04-replay"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["disposition"] == "replayed"
    assert response.json()["executionMode"] == "mock_execute"
    command = executor.commands[0]
    assert command.mode is WorkMode.MOCK_EXECUTION
    assert command.campaign_brief_id == "brief.api04.01"
    assert command.demo_scenario_id == "demo.api04.01"


@pytest.mark.asyncio
async def test_api_04_allows_an_ad_hoc_submission_without_a_retry_key() -> None:
    executor = FakeManualDryRunExecutor()
    response = await _request(_app(executor), json={"input": {"topic": "one-off"}})

    assert response.status_code == 202
    assert executor.commands[0].idempotency_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"input": []},
        {"input": {}, "execution_mode": "dry_run"},
        {"input": {}, "executionMode": "mock_execution"},
        {"input": {}, "actorId": CANARY},
        {"input": {}, "workflowId": CANARY},
        {"input": {}, "triggerId": CANARY},
        {"input": {}, "configurationRevision": 99},
        {"input": {}, "eventId": CANARY},
        {"input": {}, "runId": CANARY},
        {"input": {}, "workId": CANARY},
    ],
)
async def test_api_04_body_is_alias_only_and_cannot_supply_authority(
    body: object,
) -> None:
    executor = FakeManualDryRunExecutor()
    response = await _request(_app(executor), json=body)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation_failed"
    assert CANARY not in response.text
    assert executor.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal, expected",
    [
        (
            human_principal(roles=frozenset({"viewer"}), scopes=frozenset()),
            403,
        ),
        (service_principal(roles=frozenset({"operator"}), scopes=frozenset()), 403),
    ],
)
async def test_api_04_requires_a_human_operator_before_executor_resolution(
    principal: AuthenticatedPrincipal,
    expected: int,
) -> None:
    malformed = SynchronousManualDryRunExecutor()
    response = await _request(
        _app(malformed, principal=principal),
        json={"input": {}},
    )

    assert response.status_code == expected
    assert malformed.called is False
    assert "service unavailable" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [("Idempotency-Key", "duplicate-key-01"), ("Idempotency-Key", "duplicate-key-02")],
        [("Idempotency-Key", "")],
        [("Idempotency-Key", "short")],
        [("Idempotency-Key", " bad-key-01")],
        [("Idempotency-Key", "bad-key-01\x01")],
        [(b"Idempotency-Key", b"bad-key-01\xff")],
        [("Idempotency-Key", "x" * 241)],
    ],
)
async def test_api_04_rejects_duplicate_or_malformed_retry_keys_without_reflection(
    headers: list[tuple[str | bytes, str | bytes]],
) -> None:
    executor = FakeManualDryRunExecutor()
    response = await _request(
        _app(executor),
        content=b'{"input":{"topic":"safe"}}',
        headers=[("Content-Type", "application/json"), *headers],
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Idempotency-Key must contain one valid opaque retry key"}
    assert CANARY not in response.text
    assert executor.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Content-Type", "text/plain")],
        [("Content-Type", "application/json"), ("Content-Type", "application/json")],
    ],
)
async def test_api_04_requires_one_application_json_media_type(
    headers: list[tuple[str, str]],
) -> None:
    executor = FakeManualDryRunExecutor()
    response = await _request(
        _app(executor),
        content=b'{"input":{}}',
        headers=headers,
    )

    assert response.status_code == 415
    assert executor.commands == []

    accepted = await _request(
        _app(executor),
        content=b'{"input":{}}',
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert accepted.status_code == 202


@pytest.mark.asyncio
async def test_api_04_rejects_deep_json_before_recursive_command_normalization() -> None:
    executor = FakeManualDryRunExecutor()
    deep_body = b'{"input":' + (b'{"nested":' * 1_100) + b"{}" + (b"}" * 1_100) + b"}"
    response = await _request(
        _app(executor),
        content=deep_body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "dry_run_input_invalid",
        "message": "manual dry-run input is invalid",
    }
    assert len(response.content) < 256
    assert executor.commands == []

    boundary_body = b'{"input":' + (b'{"nested":' * 63) + b"{}" + (b"}" * 63) + b"}"
    boundary = await _request(
        _app(executor),
        content=boundary_body,
        headers={"Content-Type": "application/json"},
    )
    assert boundary.status_code == 202


@pytest.mark.asyncio
async def test_api_04_rejects_raw_body_over_common_limit_even_when_excess_is_whitespace() -> None:
    executor = FakeManualDryRunExecutor()
    oversized = b'{"input":{"topic":"' + CANARY.encode() + b'"}}' + (b" " * 1_048_576)
    response = await _request(
        _app(executor),
        content=oversized,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "dry_run_input_invalid",
        "message": "manual dry-run input is invalid",
    }
    assert len(response.content) < 256
    assert CANARY not in response.text
    assert executor.commands == []

    async def streamed_oversized_body() -> AsyncIterator[bytes]:
        yield b'{"input":{}}'
        yield b" " * 1_048_576

    async with AsyncClient(
        transport=ASGITransport(app=_app(executor)),
        base_url="http://testserver",
    ) as client:
        streamed = await client.post(
            PATH,
            content=streamed_oversized_body(),
            headers={"Content-Type": "application/json"},
        )
    assert streamed.status_code == 422
    assert streamed.json()["code"] == "dry_run_input_invalid"
    assert len(streamed.content) < 256
    assert executor.commands == []

    invalid_long_instance_path = "/api/v1/agent-instances/" + ("x" * 513) + "/dry-runs"
    async with AsyncClient(
        transport=ASGITransport(app=_app(executor)),
        base_url="http://testserver",
    ) as client:
        invalid_path = await client.post(
            invalid_long_instance_path,
            content=oversized,
            headers={"Content-Type": "application/json"},
        )
    assert invalid_path.status_code == 422
    assert invalid_path.json()["code"] == "dry_run_input_invalid"
    assert len(invalid_path.content) < 256
    assert CANARY not in invalid_path.text
    assert executor.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, expected_status, expected_code",
    [
        ("manual_work_operator_role_missing", 403, "dry_run_forbidden"),
        ("instance_not_found", 404, "manual_resource_not_found"),
        ("instance_unknown", 404, "manual_resource_not_found"),
        ("campaign_brief_unknown", 404, "manual_resource_not_found"),
        ("demo_scenario_unknown", 404, "manual_resource_not_found"),
        ("instance_disabled", 409, "dry_run_conflict"),
        ("campaign_brief_disabled", 409, "dry_run_conflict"),
        ("demo_scenario_disabled", 409, "dry_run_conflict"),
        ("manual_trigger_unavailable", 409, "dry_run_conflict"),
        ("idempotency_conflict", 409, "idempotency_conflict"),
        ("manual_work_command_invalid", 422, "dry_run_input_invalid"),
        ("work_mode_not_allowed", 422, "dry_run_input_invalid"),
        ("manual_binding_unavailable", 503, "manual_work_unavailable"),
        ("input_projection_integrity_mismatch", 503, "manual_work_unavailable"),
        ("internal_canary_failure", 503, "manual_work_unavailable"),
    ],
)
async def test_api_04_maps_service_failures_to_safe_stable_problems(
    code: str,
    expected_status: int,
    expected_code: str,
) -> None:
    executor = FakeManualDryRunExecutor()
    executor.error = ManualDryRunServiceError(code, CANARY)
    response = await _request(_app(executor), json={"input": {}})

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert CANARY not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"


@pytest.mark.asyncio
async def test_api_04_returns_only_service_sanitized_input_schema_pointers() -> None:
    executor = FakeManualDryRunExecutor()
    executor.error = ManualDryRunServiceError(
        "input_schema_invalid",
        CANARY,
        pointer="/input/topic",
    )
    response = await _request(_app(executor), json={"input": {}})
    assert response.status_code == 422
    assert response.json() == {
        "code": "dry_run_input_invalid",
        "message": "manual dry-run input is invalid",
        "pointer": "/input/topic",
    }
    assert CANARY not in response.text

    executor.error = ManualDryRunServiceError(
        "input_schema_invalid",
        CANARY,
        pointer=f"/input/{CANARY}/invalid~pointer",
    )
    rejected_pointer = await _request(_app(executor), json={"input": {}})
    assert rejected_pointer.status_code == 422
    assert "pointer" not in rejected_pointer.json()
    assert CANARY not in rejected_pointer.text


@pytest.mark.asyncio
async def test_api_04_missing_synchronous_failing_and_mismatched_executors_fail_closed() -> None:
    missing = await _request(_app(None), json={"input": {}})
    assert missing.status_code == 503

    synchronous = SynchronousManualDryRunExecutor()
    malformed = await _request(_app(synchronous), json={"input": {}})
    assert malformed.status_code == 503
    assert synchronous.called is False

    failing = FakeManualDryRunExecutor()
    failing.error = RuntimeError(CANARY)
    failure = await _request(_app(failing), json={"input": {}})
    assert failure.status_code == 503
    assert CANARY not in failure.text

    def wrong_path(
        result: ManualDryRunResult,
        _command: ManualDryRunCommand,
        _principal: AuthenticatedPrincipal,
    ) -> object:
        object.__setattr__(result.work_item, "instance_id", "inst.wrong.path.value.01")
        return result

    mismatched = FakeManualDryRunExecutor()
    mismatched.result_mutator = wrong_path
    mismatch = await _request(_app(mismatched), json={"input": {}})
    assert mismatch.status_code == 503
    assert mismatch.json()["code"] == "manual_work_unavailable"


@pytest.mark.asyncio
async def test_api_04_rejects_result_principal_mode_payload_and_resource_mismatches() -> None:
    mutators: tuple[
        Callable[
            [ManualDryRunResult, ManualDryRunCommand, AuthenticatedPrincipal],
            object,
        ],
        ...,
    ] = (
        lambda _result, _command, _principal: object(),
        lambda result, _command, principal: _mutate_and_return(
            result,
            principal,
            "actor_id",
            "principal.mutated",
        ),
        lambda result, _command, _principal: _mutate_and_return(
            result,
            result,
            "mode",
            WorkMode.MOCK_EXECUTION,
        ),
        lambda result, _command, _principal: _mutate_and_return(
            result,
            result.work_item,
            "admitted_payload",
            {"other": True},
        ),
        lambda result, _command, _principal: _mutate_and_return(
            result,
            result.run,
            "work_item_id",
            "work.other.api04",
        ),
    )
    for mutate in mutators:
        executor = FakeManualDryRunExecutor()
        executor.result_mutator = mutate
        response = await _request(_app(executor), json={"input": {"topic": "safe"}})
        assert response.status_code == 503
        assert response.json()["code"] == "manual_work_unavailable"


@pytest.mark.asyncio
async def test_api_04_rejects_python_equal_but_json_distinct_result_payload() -> None:
    executor = FakeManualDryRunExecutor()
    executor.result_mutator = lambda result, _command, _principal: _mutate_and_return(
        result,
        result.work_item,
        "admitted_payload",
        {"accepted": 1},
    )

    response = await _request(_app(executor), json={"input": {"accepted": True}})

    assert response.status_code == 503
    assert response.json()["code"] == "manual_work_unavailable"


def test_api_04_openapi_is_exact_typed_and_never_accepts_authority_fields() -> None:
    openapi = _app(FakeManualDryRunExecutor()).openapi()
    operation = openapi["paths"]["/api/v1/agent-instances/{instance_id}/dry-runs"]["post"]

    assert operation["operationId"] == "createAgentInstanceDryRun"
    idempotency_parameters = [
        parameter
        for parameter in operation["parameters"]
        if parameter.get("in") == "header" and parameter.get("name") == "Idempotency-Key"
    ]
    assert idempotency_parameters == [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": False,
            "description": (
                "An optional opaque retry key. Reuse is valid only for the exact same admission."
            ),
            "schema": {
                "type": "string",
                "pattern": r"^[\x21-\x7e]{8,240}$",
                "minLength": 8,
                "maxLength": 240,
            },
        }
    ]
    assert set(operation["requestBody"]["content"]) == {"application/json"}
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ManualDryRunInput"
    }
    assert set(operation["responses"]) == {
        "202",
        "400",
        "401",
        "403",
        "404",
        "409",
        "415",
        "422",
        "503",
    }
    assert operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ManualDryRunResponse"
    }
    assert operation["responses"]["202"]["headers"]["Cache-Control"]["schema"] == {
        "type": "string",
        "const": "no-store",
    }
    assert operation["responses"]["202"]["headers"]["Vary"]["schema"] == {
        "type": "string",
        "const": "Authorization",
    }
    input_schema = openapi["components"]["schemas"]["ManualDryRunInput"]
    assert input_schema["additionalProperties"] is False
    assert input_schema["required"] == ["input"]
    assert set(input_schema["properties"]) == {
        "input",
        "executionMode",
        "campaignBriefId",
        "demoScenarioId",
    }
    assert input_schema["properties"]["input"]["type"] == "object"
    assert input_schema["properties"]["executionMode"] == {
        "type": "string",
        "enum": ["dry_run", "mock_execute"],
        "default": "dry_run",
        "title": "Executionmode",
    }
    forbidden = {
        "actorId",
        "principalId",
        "workflowId",
        "triggerId",
        "configurationRevision",
        "eventId",
        "workId",
        "runId",
        "source",
    }
    assert not forbidden.intersection(input_schema["properties"])
    response_schema = openapi["components"]["schemas"]["ManualDryRunResponse"]
    assert response_schema["additionalProperties"] is False
    assert "status" in response_schema["required"]
    assert set(response_schema["properties"]) == {
        "status",
        "disposition",
        "eventId",
        "workId",
        "runId",
        "executionMode",
        "instanceUrl",
        "runUrl",
    }
    assert response_schema["properties"]["status"]["const"] == "accepted"
    assert response_schema["properties"]["disposition"]["enum"] == [
        "created",
        "replayed",
    ]
