"""DEMO-04 API binds Community discovery to safe dry-run admission."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunResult,
)
from marketing_agents.demos import (
    COMMUNITY_REMINDER_DRAFT_SCENARIO_ID,
    DEMO_SCENARIOS,
)
from marketing_agents.demos.community_reminder_draft import (
    COMMUNITY_REMINDER_DRAFT_INSTANCE_ID,
    COMMUNITY_REMINDER_DRAFT_TEMPLATE_ID,
)
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal

from tests.integration.api.test_demo_01_scenarios import _app, _plain, _result
from tests.support.api import api_request, assert_problem
from tests.support.identity import human_principal, service_principal

DEFINITION = DEMO_SCENARIOS.get(COMMUNITY_REMINDER_DRAFT_SCENARIO_ID)
PATH = f"/api/v1/demo-scenarios/{COMMUNITY_REMINDER_DRAFT_SCENARIO_ID}/runs"
RAW_KEY = "demo-04-api-idempotency-0001"


class FakeCommunityAdmissionExecutor:
    def __init__(self) -> None:
        self.commands: list[ManualDryRunCommand] = []
        self.principals: list[AuthenticatedPrincipal] = []

    async def submit(
        self,
        command: ManualDryRunCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ManualDryRunResult:
        self.commands.append(command)
        self.principals.append(principal)
        base = _result(command, disposition=WorkRunReceiptDisposition.CREATED)
        work = replace(
            base.work_item,
            instance_id=DEFINITION.instance_id,
            workflow_id=DEFINITION.workflow_id,
            input_schema_id=DEFINITION.input_schema_id,
        )
        return replace(base, work_item=work, mode=WorkMode.DRY_RUN)


@pytest.mark.asyncio
async def test_demo_04_discovery_freezes_read_only_agent_time_and_zero_call_contract() -> None:
    response = await api_request(
        _app(FakeCommunityAdmissionExecutor()),
        "GET",
        "/api/v1/demo-scenarios",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    item = next(
        candidate
        for candidate in response.json()["items"]
        if candidate["id"] == COMMUNITY_REMINDER_DRAFT_SCENARIO_ID
    )
    assert item == {
        "id": COMMUNITY_REMINDER_DRAFT_SCENARIO_ID,
        "version": 1,
        "displayName": "Community reminder draft",
        "description": (
            "Create a deterministic reminder draft and recommended UTC time from supplied event "
            "signup details without scheduling or sending."
        ),
        "workflowId": COMMUNITY_REMINDER_DRAFT_SCENARIO_ID,
        "effect": "read_only",
        "mode": "deterministic_mock",
        "selectedAgents": [
            {
                "templateId": COMMUNITY_REMINDER_DRAFT_TEMPLATE_ID,
                "instanceId": COMMUNITY_REMINDER_DRAFT_INSTANCE_ID,
            }
        ],
        "inputSchema": _plain(DEFINITION.input_schema),
        "preset": _plain(DEFINITION.fixture),
        "safeSubmitVerb": "Create reminder draft",
        "expected": {
            "statePath": ["received", "validated", "planned", "executing", "completed"],
            "modelCalls": 1,
            "connectorCalls": 0,
            "externalActions": 0,
            "approvals": 0,
            "externalWrites": 0,
        },
    }
    assert item["selectedAgents"] != [
        {
            "templateId": COMMUNITY_REMINDER_DRAFT_TEMPLATE_ID,
            "instanceId": "inst.community.events.live-session-reminder.02",
        }
    ]


@pytest.mark.asyncio
async def test_demo_04_run_uses_resolved_preset_and_dry_run_only() -> None:
    executor = FakeCommunityAdmissionExecutor()
    response = await api_request(
        _app(executor),
        "POST",
        PATH,
        json={"overrides": {"channel_label": "community"}},
        headers={"Idempotency-Key": RAW_KEY},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "status": "accepted",
        "disposition": "created",
        "scenarioId": COMMUNITY_REMINDER_DRAFT_SCENARIO_ID,
        "eventId": "manual-event-hmac-sha256-v1:" + ("a" * 64),
        "workId": "work.demo01.api.01",
        "runId": "run.demo01.api.01",
        "executionMode": "dry_run",
        "instanceUrl": f"/api/v1/agent-instances/{COMMUNITY_REMINDER_DRAFT_INSTANCE_ID}",
        "runUrl": "/api/v1/runs/run.demo01.api.01",
        "timelineUrl": "/api/v1/runs/run.demo01.api.01/timeline",
        "artifactsUrl": "/api/v1/runs/run.demo01.api.01/artifacts",
    }
    assert len(executor.commands) == 1
    command = executor.commands[0]
    assert command.mode is WorkMode.DRY_RUN
    assert command.demo_scenario_id == COMMUNITY_REMINDER_DRAFT_SCENARIO_ID
    assert command.instance_id == COMMUNITY_REMINDER_DRAFT_INSTANCE_ID
    assert _plain(command.input_payload) == {
        **_plain(DEFINITION.fixture),
        "channel_label": "community",
    }
    assert executor.principals
    assert RAW_KEY not in repr(command)
    assert "completed" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "pointer"),
    (
        ({"session_local_start": "2026-03-08T02:30:00"}, "/input/session_local_start"),
        ({"session_local_start": "2026-11-01T01:30:00"}, "/input/session_local_start"),
        ({"session_timezone": "Mars/Olympus_Mons"}, "/input/session_timezone"),
        (
            {
                "session_local_start": "2026-09-02T09:00:00",
                "reminder_offset_minutes": 1_440,
            },
            "/input/reminder_offset_minutes",
        ),
        ({"attendee_id": "caller-controlled-alias"}, "/input/attendee_id"),
    ),
)
async def test_demo_04_rejects_invalid_overrides_before_admission(
    overrides: dict[str, Any],
    pointer: str,
) -> None:
    executor = FakeCommunityAdmissionExecutor()
    response = await api_request(
        _app(executor),
        "POST",
        PATH,
        json={"overrides": overrides},
        headers={"Idempotency-Key": RAW_KEY},
    )

    problem = assert_problem(response, status_code=422, code="demo_scenario_input_invalid")
    assert problem["field_errors"][0]["pointer"] == pointer
    assert response.headers["cache-control"] == "no-store"
    assert executor.commands == []
    assert executor.principals == []


@pytest.mark.asyncio
async def test_demo_04_post_requires_a_human_operator_before_submit() -> None:
    for principal in (
        human_principal(
            actor_id="principal.test.demo-04-viewer",
            roles=frozenset({"viewer"}),
            scopes=frozenset(),
        ),
        service_principal(
            actor_id="principal.test.demo-04-service",
            roles=frozenset({"operator"}),
            scopes=frozenset(),
        ),
    ):
        executor = FakeCommunityAdmissionExecutor()
        response = await api_request(
            _app(executor, principal=principal),
            "POST",
            PATH,
            json={},
            headers={"Idempotency-Key": RAW_KEY},
        )

        assert_problem(response, status_code=403, code="request_forbidden")
        assert executor.commands == []
        assert executor.principals == []


@pytest.mark.asyncio
async def test_demo_04_enforces_csrf_strict_json_and_bounded_idempotency_before_submit() -> None:
    csrf_executor = FakeCommunityAdmissionExecutor()
    csrf_app = _app(csrf_executor)
    async with AsyncClient(
        transport=ASGITransport(app=csrf_app),
        base_url="http://testserver",
    ) as client:
        csrf_denied = await client.post(
            PATH,
            json={},
            headers={"Idempotency-Key": RAW_KEY},
        )
    assert_problem(csrf_denied, status_code=403, code="browser_request_forbidden")
    assert csrf_executor.commands == []

    request_cases = (
        (
            b"{}",
            {"Content-Type": "text/plain", "Idempotency-Key": RAW_KEY},
            403,
            "browser_request_forbidden",
        ),
        (
            b'{"overrides":',
            {"Content-Type": "application/json", "Idempotency-Key": RAW_KEY},
            422,
            "request_validation_failed",
        ),
        (b"{}", {"Content-Type": "application/json"}, 400, "idempotency_key_invalid"),
        (
            b"{}",
            {"Content-Type": "application/json", "Idempotency-Key": "short"},
            400,
            "idempotency_key_invalid",
        ),
    )
    for content, headers, status_code, code in request_cases:
        executor = FakeCommunityAdmissionExecutor()
        response = await api_request(
            _app(executor),
            "POST",
            PATH,
            content=content,
            headers=headers,
        )
        assert_problem(response, status_code=status_code, code=code)
        assert executor.commands == []
        assert executor.principals == []
