"""DEMO-01: safe Social discovery and durable asynchronous demo admission."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import create_app
from marketing_agents.api.dependencies import (
    DemoScenarioRegistryExecutor,
    ManualDryRunExecutor,
)
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunResult,
    ManualDryRunServiceError,
)
from marketing_agents.config import Settings
from marketing_agents.demos import (
    DEMO_SCENARIOS,
    SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
    DemoScenarioDefinition,
    DemoScenarioRegistry,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.security.redaction import SecretValue

from tests.support.api import api_request, assert_problem
from tests.support.identity import StaticIdentityProvider, human_principal, service_principal

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
PATH = f"/api/v1/demo-scenarios/{SOCIAL_CONTENT_DRAFT_SCENARIO_ID}/runs"
NOW = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)
RAW_KEY = "demo-01-retry-key-0001"
DEFINITION = DEMO_SCENARIOS.get(SOCIAL_CONTENT_DRAFT_SCENARIO_ID)
SOCIAL_ONLY_SCENARIOS = DemoScenarioRegistry((DEFINITION,))


def _plain(value: Mapping[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(canonical_json_bytes(value)))


def _operator() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.test.demo-operator",
        roles=frozenset({"operator"}),
        scopes=frozenset(),
    )


def _viewer() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.test.demo-viewer",
        roles=frozenset({"viewer"}),
        scopes=frozenset(),
    )


def _work(command: ManualDryRunCommand) -> WorkItem:
    return WorkItem(
        id="work.demo01.api.01",
        source="manual",
        event_id="manual-event-hmac-sha256-v1:" + ("a" * 64),
        instance_id=command.instance_id,
        trigger_id="trigger.demo01.api.01",
        workflow_id=DEFINITION.workflow_id,
        mode=command.mode,
        brief_id=None,
        configuration_revision=2,
        input_digest="b" * 64,
        admission_digest="c" * 64,
        created_at=NOW,
        brief_revision=None,
        digest_key_version="admission-hmac-sha256-v1:" + ("d" * 64),
        admitted_payload=command.input_payload,
        redacted_input_projection={"idea": "[REDACTED]"},
        input_schema_id=DEFINITION.input_schema_id,
        input_schema_hash="schema-sha256-v1:" + ("e" * 64),
        input_projection_created_at=NOW,
        input_projection_expires_at=NOW + timedelta(days=7),
        input_projection_integrity_digest="f" * 64,
    )


def _result(
    command: ManualDryRunCommand,
    *,
    disposition: WorkRunReceiptDisposition,
) -> ManualDryRunResult:
    work = _work(command)
    run = Run(
        id="run.demo01.api.01",
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


class FakeDemoAdmissionExecutor:
    def __init__(self) -> None:
        self.commands: list[ManualDryRunCommand] = []
        self.principals: list[AuthenticatedPrincipal] = []
        self.disposition = WorkRunReceiptDisposition.CREATED
        self.error: Exception | None = None
        self.result_mutator: Callable[[ManualDryRunResult], object] | None = None

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
            return cast(ManualDryRunResult, self.result_mutator(result))
        return result


class SynchronousDemoAdmissionExecutor:
    def __init__(self) -> None:
        self.called = False

    def submit(self, *_args: object, **_kwargs: object) -> object:
        self.called = True
        return object()


class IncompleteDemoRegistry:
    def list(self) -> tuple[DemoScenarioDefinition, ...]:
        return (DEFINITION,)

    def get(self, _scenario_id: str) -> DemoScenarioDefinition:
        return DEFINITION


class InvalidResolvedDemoRegistry:
    def list(self) -> tuple[DemoScenarioDefinition, ...]:
        return (DEFINITION,)

    def get(self, _scenario_id: str) -> DemoScenarioDefinition:
        return DEFINITION

    def resolve_input(
        self,
        _scenario_id: str,
        _overrides: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return {"idea": "schema-invalid incomplete input"}


def _settings() -> Settings:
    return Settings(_env_file=None, catalog_root=CATALOG_ROOT)


def _app(
    executor: object | None,
    *,
    principal: AuthenticatedPrincipal | None = None,
    registry: object = DEMO_SCENARIOS,
) -> FastAPI:
    return create_app(
        _settings(),
        identity_provider=StaticIdentityProvider(principal or _operator()),
        manual_dry_run_service=cast(ManualDryRunExecutor | None, executor),
        demo_scenario_registry=cast(DemoScenarioRegistryExecutor, registry),
    )


async def _post(
    app: FastAPI,
    *,
    json_body: object | None = None,
    content: bytes | None = None,
    headers: Any = None,
    path: str = PATH,
) -> Response:
    return await api_request(
        app,
        "POST",
        path,
        json={} if json_body is None and content is None else json_body,
        content=content,
        headers=headers,
    )


@pytest.mark.asyncio
async def test_demo_01_discovery_is_exact_safe_and_private() -> None:
    response = await api_request(
        _app(FakeDemoAdmissionExecutor(), registry=SOCIAL_ONLY_SCENARIOS),
        "GET",
        "/api/v1/demo-scenarios",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert response.json() == {
        "items": [
            {
                "id": SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
                "version": 1,
                "displayName": DEFINITION.display_name,
                "description": DEFINITION.description,
                "workflowId": SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
                "effect": "read_only",
                "mode": "deterministic_mock",
                "selectedAgents": [
                    {
                        "templateId": "tpl.social-media.new-content.linkedin-post-drafter",
                        "instanceId": ("inst.social-media.new-content.linkedin-post-drafter.01"),
                    }
                ],
                "inputSchema": _plain(DEFINITION.input_schema),
                "preset": _plain(DEFINITION.fixture),
                "safeSubmitVerb": "Create draft",
                "expected": {
                    "statePath": [
                        "received",
                        "validated",
                        "planned",
                        "executing",
                        "completed",
                    ],
                    "modelCalls": 1,
                    "connectorCalls": 0,
                    "externalActions": 0,
                    "approvals": 0,
                    "externalWrites": 0,
                },
            }
        ]
    }
    payload_text = response.text.casefold()
    assert '"publish"' not in payload_text
    assert '"send"' not in payload_text


@pytest.mark.asyncio
async def test_demo_01_accepts_resolved_preset_as_durable_dry_run_only() -> None:
    executor = FakeDemoAdmissionExecutor()
    response = await _post(
        _app(executor),
        json_body={"overrides": {"tone": "educational"}},
        headers={"Idempotency-Key": RAW_KEY},
    )

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert response.json() == {
        "status": "accepted",
        "disposition": "created",
        "scenarioId": SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
        "eventId": "manual-event-hmac-sha256-v1:" + ("a" * 64),
        "workId": "work.demo01.api.01",
        "runId": "run.demo01.api.01",
        "executionMode": "dry_run",
        "instanceUrl": (
            "/api/v1/agent-instances/inst.social-media.new-content.linkedin-post-drafter.01"
        ),
        "runUrl": "/api/v1/runs/run.demo01.api.01",
        "timelineUrl": "/api/v1/runs/run.demo01.api.01/timeline",
        "artifactsUrl": "/api/v1/runs/run.demo01.api.01/artifacts",
    }
    command = executor.commands[0]
    assert command.instance_id == DEFINITION.instance_id
    assert command.demo_scenario_id == SOCIAL_CONTENT_DRAFT_SCENARIO_ID
    assert command.campaign_brief_id is None
    assert command.mode is WorkMode.DRY_RUN
    assert _plain(command.input_payload) == {
        **_plain(DEFINITION.fixture),
        "tone": "educational",
    }
    assert type(command.idempotency_key) is SecretValue
    assert command.idempotency_key.reveal() == RAW_KEY
    assert command.correlation_id.startswith("correlation.api.")
    assert executor.principals == [_operator()]
    assert RAW_KEY not in repr(command)
    assert all(word not in response.text for word in ("published", "sent", "completed"))


@pytest.mark.asyncio
async def test_demo_01_replays_the_current_completed_run_as_an_async_receipt() -> None:
    executor = FakeDemoAdmissionExecutor()
    executor.disposition = WorkRunReceiptDisposition.REPLAYED
    executor.result_mutator = lambda result: replace(
        result,
        run=replace(
            result.run,
            state=RunState.COMPLETED,
            version=5,
            updated_at=NOW + timedelta(seconds=4),
            approval_required=False,
            terminal_reason_code="execution_completed",
        ),
    )

    response = await _post(
        _app(executor),
        headers={"Idempotency-Key": RAW_KEY},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["disposition"] == "replayed"
    assert response.json()["workId"] == "work.demo01.api.01"
    assert response.json()["runId"] == "run.demo01.api.01"
    assert response.json()["runUrl"] == "/api/v1/runs/run.demo01.api.01"
    assert executor.commands[0].demo_scenario_id == SOCIAL_CONTENT_DRAFT_SCENARIO_ID
    assert "completed" not in response.text


@pytest.mark.asyncio
async def test_demo_01_get_allows_viewer_but_post_requires_human_operator() -> None:
    viewer_executor = FakeDemoAdmissionExecutor()
    viewer_app = _app(viewer_executor, principal=_viewer())
    discovery = await api_request(viewer_app, "GET", "/api/v1/demo-scenarios")
    denied = await _post(
        viewer_app,
        headers={"Idempotency-Key": RAW_KEY},
    )

    assert discovery.status_code == 200
    assert_problem(denied, status_code=403, code="request_forbidden")
    assert viewer_executor.commands == []

    service_executor = FakeDemoAdmissionExecutor()
    service_app = _app(
        service_executor,
        principal=service_principal(
            actor_id="principal.test.demo-service",
            roles=frozenset({"operator"}),
            scopes=frozenset(),
        ),
    )
    service_denied = await _post(
        service_app,
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(service_denied, status_code=403, code="request_forbidden")
    assert service_executor.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        None,
        {"Idempotency-Key": "short"},
        {"Idempotency-Key": "contains whitespace"},
        [("Idempotency-Key", RAW_KEY), ("Idempotency-Key", RAW_KEY)],
    ],
)
async def test_demo_01_requires_one_bounded_idempotency_key(headers: Any) -> None:
    executor = FakeDemoAdmissionExecutor()
    response = await _post(_app(executor), headers=headers)

    assert_problem(response, status_code=400, code="idempotency_key_invalid")
    assert executor.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"overrides": {"unknown": "authority"}},
        {"overrides": {"tone": "publish_now"}},
        {"overrides": {"source_urls": ["http://example.com/untrusted"]}},
        {"overrides": [], "actorId": "caller-authority"},
        {"input": _plain(DEFINITION.fixture)},
    ],
)
async def test_demo_01_rejects_invalid_overrides_and_caller_authority(body: object) -> None:
    executor = FakeDemoAdmissionExecutor()
    response = await _post(
        _app(executor),
        json_body=body,
        headers={"Idempotency-Key": RAW_KEY},
    )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert executor.commands == []


@pytest.mark.asyncio
async def test_demo_01_maps_scenario_and_service_errors_without_reflection() -> None:
    executor = FakeDemoAdmissionExecutor()
    unknown = await _post(
        _app(executor),
        path="/api/v1/demo-scenarios/demo.unknown.v1/runs",
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(unknown, status_code=404, code="demo_scenario_not_found")
    assert executor.commands == []

    executor.error = ManualDryRunServiceError(
        "input_schema_invalid",
        "SECRET-SERVICE-CANARY",
        pointer="/input/source_urls/0",
    )
    invalid = await _post(
        _app(executor),
        headers={"Idempotency-Key": RAW_KEY},
    )
    payload = assert_problem(
        invalid,
        status_code=422,
        code="demo_scenario_input_invalid",
    )
    assert payload["field_errors"][0]["pointer"] == "/input/source_urls/0"
    assert "SECRET-SERVICE-CANARY" not in invalid.text

    executor.error = ManualDryRunServiceError(
        "manual_idempotency_conflict",
        "SECRET-CONFLICT-CANARY",
    )
    conflict = await _post(
        _app(executor),
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(conflict, status_code=409, code="idempotency_conflict")
    assert "SECRET-CONFLICT-CANARY" not in conflict.text


@pytest.mark.asyncio
async def test_demo_01_fails_closed_for_incomplete_seams_and_unbound_results() -> None:
    incomplete_registry = await api_request(
        _app(FakeDemoAdmissionExecutor(), registry=IncompleteDemoRegistry()),
        "GET",
        "/api/v1/demo-scenarios",
    )
    assert_problem(
        incomplete_registry,
        status_code=503,
        code="demo_scenario_registry_unavailable",
    )

    invalid_executor = FakeDemoAdmissionExecutor()
    invalid_resolved_input = await _post(
        _app(invalid_executor, registry=InvalidResolvedDemoRegistry()),
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(
        invalid_resolved_input,
        status_code=503,
        code="demo_scenario_registry_unavailable",
    )
    assert invalid_executor.commands == []

    synchronous = SynchronousDemoAdmissionExecutor()
    unavailable = await _post(
        _app(synchronous),
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(unavailable, status_code=503, code="service_unavailable")
    assert not synchronous.called

    executor = FakeDemoAdmissionExecutor()
    executor.result_mutator = lambda result: object()
    unbound = await _post(
        _app(executor),
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(unbound, status_code=503, code="demo_run_unavailable")

    executor.result_mutator = lambda result: replace(
        result,
        run=replace(
            result.run,
            state=RunState.COMPLETED,
            version=5,
            updated_at=NOW + timedelta(seconds=4),
            approval_required=False,
            terminal_reason_code="execution_completed",
        ),
    )
    created_but_completed = await _post(
        _app(executor),
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(created_but_completed, status_code=503, code="demo_run_unavailable")

    executor.disposition = WorkRunReceiptDisposition.REPLAYED
    executor.result_mutator = lambda result: replace(
        result,
        run=replace(
            result.run,
            state=RunState.COMPLETED,
            version=2,
            approval_required=False,
            terminal_reason_code="execution_completed",
        ),
    )
    incoherent_completed = await _post(
        _app(executor),
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(incoherent_completed, status_code=503, code="demo_run_unavailable")

    executor.result_mutator = lambda result: replace(result, run=replace(result.run, version=2))
    incoherent_received = await _post(
        _app(executor),
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(incoherent_received, status_code=503, code="demo_run_unavailable")


@pytest.mark.asyncio
async def test_demo_01_enforces_csrf_strict_json_size_and_depth_before_submit() -> None:
    executor = FakeDemoAdmissionExecutor()
    app = _app(executor)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        csrf_denied = await client.post(
            PATH,
            json={},
            headers={"Idempotency-Key": RAW_KEY},
        )
    assert_problem(csrf_denied, status_code=403, code="browser_request_forbidden")

    duplicate = await _post(
        app,
        content=b'{"overrides":{},"overrides":{}}',
        headers={"Content-Type": "application/json", "Idempotency-Key": RAW_KEY},
    )
    assert_problem(duplicate, status_code=422, code="request_validation_failed")

    oversized = await _post(
        app,
        content=b'{"overrides":{"idea":"' + (b"x" * 1_048_576),
        headers={"Content-Type": "application/json", "Idempotency-Key": RAW_KEY},
    )
    assert_problem(oversized, status_code=413, code="payload_too_large")

    nested: object = "leaf"
    for _index in range(70):
        nested = [nested]
    too_deep = await _post(
        app,
        json_body={"overrides": {"key_points": nested}},
        headers={"Idempotency-Key": RAW_KEY},
    )
    assert_problem(too_deep, status_code=422, code="request_validation_failed")
    assert executor.commands == []


def test_demo_01_openapi_freezes_discovery_and_intake_contracts() -> None:
    schema = _app(FakeDemoAdmissionExecutor()).openapi()
    discovery = schema["paths"]["/api/v1/demo-scenarios"]["get"]
    creation = schema["paths"]["/api/v1/demo-scenarios/{scenario_id}/runs"]["post"]

    assert discovery["operationId"] == "listDemoScenarios"
    assert creation["operationId"] == "createDemoScenarioRun"
    idempotency = next(
        parameter for parameter in creation["parameters"] if parameter["name"] == "Idempotency-Key"
    )
    assert idempotency["in"] == "header"
    assert idempotency["required"] is True
    assert idempotency["schema"]["minLength"] == 8
    assert idempotency["schema"]["maxLength"] == 240
    request_schema = creation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/DemoScenarioRunInput")
    assert creation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/DemoScenarioRunResponse"
    )

    scenario_properties = schema["components"]["schemas"]["DemoScenarioView"]["properties"]
    assert "selectedAgents" in scenario_properties
    assert "selectedAgent" not in scenario_properties
    assert "approvalRequired" not in scenario_properties
    receipt_properties = schema["components"]["schemas"]["DemoScenarioRunResponse"]["properties"]
    assert set(receipt_properties) == {
        "status",
        "disposition",
        "scenarioId",
        "eventId",
        "workId",
        "runId",
        "executionMode",
        "instanceUrl",
        "runUrl",
        "timelineUrl",
        "artifactsUrl",
    }
