"""DEMO-05 API binds Partnership discovery to safe dry-run admission."""

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
    DEMO_SCENARIOS,
    PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID,
)
from marketing_agents.demos.partnership_application_review import (
    PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID,
    PARTNERSHIP_APPLICATION_REVIEW_TEMPLATE_ID,
)
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal

from tests.integration.api.test_demo_01_scenarios import _app, _plain, _result
from tests.support.api import api_request, assert_problem
from tests.support.identity import human_principal, service_principal

DEFINITION = DEMO_SCENARIOS.get(PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID)
PATH = f"/api/v1/demo-scenarios/{PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID}/runs"
RAW_KEY = "demo-05-api-idempotency-0001"


class FakePartnershipAdmissionExecutor:
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
async def test_demo_05_discovery_freezes_advisory_read_only_zero_action_contract() -> None:
    response = await api_request(
        _app(FakePartnershipAdmissionExecutor()),
        "GET",
        "/api/v1/demo-scenarios",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    item = next(
        candidate
        for candidate in response.json()["items"]
        if candidate["id"] == PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID
    )
    assert item == {
        "id": PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID,
        "version": 1,
        "displayName": "Partnership application review",
        "description": (
            "Create a deterministic advisory recommendation from supplied partner application "
            "evidence without external research, applicant notification, record mutation, or an "
            "automated decision."
        ),
        "workflowId": PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID,
        "effect": "read_only",
        "mode": "deterministic_mock",
        "selectedAgents": [
            {
                "templateId": PARTNERSHIP_APPLICATION_REVIEW_TEMPLATE_ID,
                "instanceId": PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID,
            }
        ],
        "inputSchema": _plain(DEFINITION.input_schema),
        "preset": _plain(DEFINITION.fixture),
        "safeSubmitVerb": "Create advisory review",
        "expected": {
            "statePath": ["received", "validated", "planned", "executing", "completed"],
            "modelCalls": 1,
            "connectorCalls": 0,
            "externalActions": 0,
            "approvals": 0,
            "externalWrites": 0,
        },
    }
    assert item["selectedAgents"] == [
        {
            "templateId": PARTNERSHIP_APPLICATION_REVIEW_TEMPLATE_ID,
            "instanceId": PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID,
        }
    ]
    payload_text = response.text.casefold()
    assert '"advisoryonly"' not in payload_text
    assert '"publish"' not in payload_text
    assert '"send"' not in payload_text


@pytest.mark.asyncio
async def test_demo_05_run_uses_resolved_preset_and_returns_async_dry_run_receipt_only() -> None:
    executor = FakePartnershipAdmissionExecutor()
    response = await api_request(
        _app(executor),
        "POST",
        PATH,
        json={},
        headers={"Idempotency-Key": RAW_KEY},
    )

    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert response.json() == {
        "status": "accepted",
        "disposition": "created",
        "scenarioId": PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID,
        "eventId": "manual-event-hmac-sha256-v1:" + ("a" * 64),
        "workId": "work.demo01.api.01",
        "runId": "run.demo01.api.01",
        "executionMode": "dry_run",
        "instanceUrl": f"/api/v1/agent-instances/{PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID}",
        "runUrl": "/api/v1/runs/run.demo01.api.01",
        "timelineUrl": "/api/v1/runs/run.demo01.api.01/timeline",
        "artifactsUrl": "/api/v1/runs/run.demo01.api.01/artifacts",
    }
    assert len(executor.commands) == 1
    command = executor.commands[0]
    assert command.mode is WorkMode.DRY_RUN
    assert command.demo_scenario_id == PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID
    assert command.instance_id == PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID
    assert _plain(command.input_payload) == _plain(DEFINITION.fixture)
    assert executor.principals
    assert RAW_KEY not in repr(command)
    assert "completed" not in response.text
    assert "needs_information" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "pointer"),
    (
        ("unsafe_url", "/input/organization_metadata/website_reference"),
        ("duplicate_region", "/input/declared_regions/1"),
        (
            "dangling_evidence_reference",
            "/input/evidence_records/0/supports_criterion_ids/0",
        ),
        ("unknown_authority", "/input/automatic_decision"),
    ),
)
async def test_demo_05_rejects_invalid_overrides_before_admission(
    case: str,
    pointer: str,
) -> None:
    preset = _plain(DEFINITION.fixture)
    if case == "unsafe_url":
        organization = dict(preset["organization_metadata"])
        organization["website_reference"] = "http://example.com/partner"
        overrides: dict[str, Any] = {"organization_metadata": organization}
    elif case == "duplicate_region":
        overrides = {"declared_regions": ["europe", "EUROPE"]}
    elif case == "dangling_evidence_reference":
        evidence = [dict(item) for item in preset["evidence_records"]]
        evidence[0] = {
            **evidence[0],
            "supports_criterion_ids": ["criterion.unknown"],
        }
        overrides = {"evidence_records": evidence}
    else:
        overrides = {"automatic_decision": True}

    executor = FakePartnershipAdmissionExecutor()
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
async def test_demo_05_post_requires_a_human_operator_before_submit() -> None:
    for principal in (
        human_principal(
            actor_id="principal.test.demo-05-viewer",
            roles=frozenset({"viewer"}),
            scopes=frozenset(),
        ),
        service_principal(
            actor_id="principal.test.demo-05-service",
            roles=frozenset({"operator"}),
            scopes=frozenset(),
        ),
    ):
        executor = FakePartnershipAdmissionExecutor()
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
async def test_demo_05_enforces_csrf_strict_json_and_bounded_idempotency_before_submit() -> None:
    csrf_executor = FakePartnershipAdmissionExecutor()
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
        executor = FakePartnershipAdmissionExecutor()
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
