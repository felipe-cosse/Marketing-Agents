"""DEMO-02: safe Blog discovery and durable asynchronous demo admission."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import Response
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
)
from marketing_agents.config import Settings
from marketing_agents.demos import (
    BLOG_CONTENT_REVIEW_SCENARIO_ID,
    DEMO_SCENARIOS,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
    SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.security.redaction import SecretValue

from tests.support.api import api_request, assert_problem
from tests.support.identity import StaticIdentityProvider, human_principal

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
PATH = f"/api/v1/demo-scenarios/{BLOG_CONTENT_REVIEW_SCENARIO_ID}/runs"
NOW = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)
RAW_KEY = "demo-02-retry-key-0001"
DEFINITION = DEMO_SCENARIOS.get(BLOG_CONTENT_REVIEW_SCENARIO_ID)
BLOG_INSTANCE_ID = "inst.blog-seo.new-content.blog-post-updater.01"
BLOG_TEMPLATE_ID = "tpl.blog-seo.new-content.blog-post-updater"


def _plain(value: Mapping[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(canonical_json_bytes(value)))


def _operator() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.test.demo02-operator",
        roles=frozenset({"operator"}),
        scopes=frozenset(),
    )


def _result(command: ManualDryRunCommand) -> ManualDryRunResult:
    work = WorkItem(
        id="work.demo02.api.01",
        source="manual",
        event_id="manual-event-hmac-sha256-v1:" + ("a" * 64),
        instance_id=command.instance_id,
        trigger_id="trigger.demo02.api.01",
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
        redacted_input_projection={"article_title": "[REDACTED]"},
        input_schema_id=DEFINITION.input_schema_id,
        input_schema_hash="schema-sha256-v1:" + ("e" * 64),
        input_projection_created_at=NOW,
        input_projection_expires_at=NOW + timedelta(days=7),
        input_projection_integrity_digest="f" * 64,
    )
    run = Run(
        id="run.demo02.api.01",
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
        disposition=WorkRunReceiptDisposition.CREATED,
        event_id=work.event_id,
        mode=command.mode,
    )


class FakeDemoAdmissionExecutor:
    def __init__(self) -> None:
        self.commands: list[ManualDryRunCommand] = []
        self.principals: list[AuthenticatedPrincipal] = []
        self.results: list[ManualDryRunResult] = []

    async def submit(
        self,
        command: ManualDryRunCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ManualDryRunResult:
        self.commands.append(command)
        self.principals.append(principal)
        result = _result(command)
        self.results.append(result)
        return result


def _app(executor: object) -> FastAPI:
    return create_app(
        Settings(_env_file=None, catalog_root=CATALOG_ROOT),
        identity_provider=StaticIdentityProvider(_operator()),
        manual_dry_run_service=cast(ManualDryRunExecutor, executor),
        demo_scenario_registry=cast(DemoScenarioRegistryExecutor, DEMO_SCENARIOS),
    )


async def _post(app: FastAPI, body: object) -> Response:
    return await api_request(
        app,
        "POST",
        PATH,
        json=body,
        headers={"Idempotency-Key": RAW_KEY},
    )


@pytest.mark.asyncio
async def test_demo_02_discovery_is_exact_safe_and_private() -> None:
    response = await api_request(_app(FakeDemoAdmissionExecutor()), "GET", "/api/v1/demo-scenarios")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    items = response.json()["items"]
    assert [item["id"] for item in items] == [
        BLOG_CONTENT_REVIEW_SCENARIO_ID,
        EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
        SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
    ]
    assert items[0] == {
        "id": BLOG_CONTENT_REVIEW_SCENARIO_ID,
        "version": 1,
        "displayName": DEFINITION.display_name,
        "description": DEFINITION.description,
        "workflowId": BLOG_CONTENT_REVIEW_SCENARIO_ID,
        "effect": "read_only",
        "mode": "deterministic_mock",
        "selectedAgents": [
            {
                "templateId": BLOG_TEMPLATE_ID,
                "instanceId": BLOG_INSTANCE_ID,
            }
        ],
        "inputSchema": _plain(DEFINITION.input_schema),
        "preset": _plain(DEFINITION.fixture),
        "safeSubmitVerb": "Create review",
        "expected": {
            "statePath": ["received", "validated", "planned", "executing", "completed"],
            "modelCalls": 1,
            "connectorCalls": 0,
            "externalActions": 0,
            "approvals": 0,
            "externalWrites": 0,
        },
    }
    payload_text = response.text.casefold()
    assert '"publish"' not in payload_text
    assert '"send"' not in payload_text


@pytest.mark.asyncio
async def test_demo_02_accepts_resolved_preset_as_durable_dry_run_only() -> None:
    executor = FakeDemoAdmissionExecutor()
    response = await _post(_app(executor), {})

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"
    assert response.json() == {
        "status": "accepted",
        "disposition": "created",
        "scenarioId": BLOG_CONTENT_REVIEW_SCENARIO_ID,
        "eventId": "manual-event-hmac-sha256-v1:" + ("a" * 64),
        "workId": "work.demo02.api.01",
        "runId": "run.demo02.api.01",
        "executionMode": "dry_run",
        "instanceUrl": f"/api/v1/agent-instances/{BLOG_INSTANCE_ID}",
        "runUrl": "/api/v1/runs/run.demo02.api.01",
        "timelineUrl": "/api/v1/runs/run.demo02.api.01/timeline",
        "artifactsUrl": "/api/v1/runs/run.demo02.api.01/artifacts",
    }
    command = executor.commands[0]
    result = executor.results[0]
    assert command.instance_id == BLOG_INSTANCE_ID
    assert command.demo_scenario_id == BLOG_CONTENT_REVIEW_SCENARIO_ID
    assert command.campaign_brief_id is None
    assert command.mode is WorkMode.DRY_RUN
    assert _plain(command.input_payload) == _plain(DEFINITION.fixture)
    assert type(command.idempotency_key) is SecretValue
    assert command.idempotency_key.reveal() == RAW_KEY
    assert result.work_item.instance_id == BLOG_INSTANCE_ID
    assert result.work_item.workflow_id == BLOG_CONTENT_REVIEW_SCENARIO_ID
    assert result.run.state is RunState.RECEIVED
    assert executor.principals == [_operator()]
    assert RAW_KEY not in repr(command)
    assert all(word not in response.text for word in ("published", "updated", "completed"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "pointer"),
    [
        ({"canonical_url": "http://example.com/blog/unsafe"}, "/input/canonical_url"),
        ({"assessment_at": "9999-12-31T23:59:59-14:00"}, "/input/assessment_at"),
        ({"last_updated_at": "not-a-timestamp"}, "/input/last_updated_at"),
        ({"last_updated_at": "2026-09-01T00:00:00Z"}, "/input/last_updated_at"),
        (
            {"target_keywords": ["Governed AI", "governed ai"]},
            "/input/target_keywords/1",
        ),
        ({"unknown_field": "caller-authority"}, "/input/unknown_field"),
    ],
)
async def test_demo_02_rejects_invalid_overrides_before_admission(
    overrides: Mapping[str, Any],
    pointer: str,
) -> None:
    executor = FakeDemoAdmissionExecutor()
    response = await _post(_app(executor), {"overrides": overrides})

    payload = assert_problem(response, status_code=422, code="demo_scenario_input_invalid")
    assert payload["field_errors"][0]["pointer"] == pointer
    assert response.headers["cache-control"] == "no-store"
    assert executor.commands == []
    assert executor.principals == []
    assert executor.results == []
