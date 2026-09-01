"""DEMO-03 API binds mutating admission to server-owned mock execution."""

from __future__ import annotations

from dataclasses import replace

import pytest
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunResult,
)
from marketing_agents.demos import (
    DEMO_SCENARIOS,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
)
from marketing_agents.demos.email_signup_onboarding import (
    EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
    EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,
)
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal

from tests.integration.api.test_demo_01_scenarios import _app, _plain, _result
from tests.support.api import api_request

DEFINITION = DEMO_SCENARIOS.get(EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID)
PATH = f"/api/v1/demo-scenarios/{EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID}/runs"


class FakeEmailAdmissionExecutor:
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
        return replace(base, work_item=work, mode=WorkMode.MOCK_EXECUTION)


@pytest.mark.asyncio
async def test_demo_03_discovery_freezes_two_agents_approval_path_and_counts() -> None:
    response = await api_request(
        _app(FakeEmailAdmissionExecutor()),
        "GET",
        "/api/v1/demo-scenarios",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    item = next(
        candidate
        for candidate in response.json()["items"]
        if candidate["id"] == EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID
    )
    assert item == {
        "id": EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
        "version": 1,
        "displayName": DEFINITION.display_name,
        "description": DEFINITION.description,
        "workflowId": EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
        "effect": "mutating",
        "mode": "deterministic_mock",
        "selectedAgents": [
            {
                "templateId": EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,
                "instanceId": EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
            },
            {
                "templateId": EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
                "instanceId": EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
            },
        ],
        "inputSchema": _plain(DEFINITION.input_schema),
        "preset": _plain(DEFINITION.fixture),
        "safeSubmitVerb": "Propose onboarding actions",
        "expected": {
            "statePath": [
                "received",
                "validated",
                "planned",
                "awaiting_approval",
                "executing",
                "completed",
            ],
            "modelCalls": 1,
            "connectorCalls": 2,
            "externalActions": 2,
            "approvals": 2,
            "externalWrites": 2,
        },
    }


@pytest.mark.asyncio
async def test_demo_03_run_uses_trusted_mock_execution_without_approvals_url() -> None:
    executor = FakeEmailAdmissionExecutor()
    response = await api_request(
        _app(executor),
        "POST",
        PATH,
        json={},
        headers={"Idempotency-Key": "demo-03-api-idempotency-0001"},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "status": "accepted",
        "disposition": "created",
        "scenarioId": EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
        "eventId": "manual-event-hmac-sha256-v1:" + ("a" * 64),
        "workId": "work.demo01.api.01",
        "runId": "run.demo01.api.01",
        "executionMode": "mock_execute",
        "instanceUrl": ("/api/v1/agent-instances/inst.email.newsletter.newsletter-subscriber.01"),
        "runUrl": "/api/v1/runs/run.demo01.api.01",
        "timelineUrl": "/api/v1/runs/run.demo01.api.01/timeline",
        "artifactsUrl": "/api/v1/runs/run.demo01.api.01/artifacts",
    }
    assert len(executor.commands) == 1
    command = executor.commands[0]
    assert command.mode is WorkMode.MOCK_EXECUTION
    assert command.demo_scenario_id == EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID
    assert command.instance_id == DEFINITION.instance_id
