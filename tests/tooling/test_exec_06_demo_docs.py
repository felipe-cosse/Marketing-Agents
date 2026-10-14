"""EXEC-06: documented single-preset commands exercise real API contracts without sockets."""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import re
import shlex
import subprocess
import sys
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.application.ports.readiness import (
    CatalogReadinessMetadata,
    ReadinessCheck,
    ReadinessCheckName,
    ReadinessCheckStatus,
    ReadinessCode,
    ReadinessReport,
)
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.config import Settings
from marketing_agents.demos import DEMO_SCENARIOS
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import WorkMode

from scripts import demo_local
from scripts.del_05_runtime_smoke import LocalClient, SmokeFailure
from tests.integration.api.test_demo_01_scenarios import _result

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://127.0.0.1:8080"
DOC = (ROOT / "docs/demos.md").read_text()
COMMANDS = [
    shlex.split(block.replace("\\\n", ""))
    for block in re.findall(r"```sh\n(.*?)\n```", DOC, re.DOTALL)
    if block.startswith("python3 -m scripts.demo_local ")
]


class ReadyProbe:
    async def check(self):
        return ReadinessReport(
            checks=tuple(
                ReadinessCheck(name, ReadinessCheckStatus.READY, ReadinessCode.READY)
                for name in ReadinessCheckName
            ),
            catalog=CatalogReadinessMetadata(
                "1.0.0", "catalog-sha256-v1:" + "a" * 64, 5, 12, 36, 43
            ),
        )


class Admission:
    """Replace persistence only; transport still validates the exact domain result."""

    def __init__(self):
        self.commands = []
        self.principals = []

    async def submit(self, command, *, principal):
        self.commands.append(command)
        self.principals.append(principal)
        definition = DEMO_SCENARIOS.get(command.demo_scenario_id)
        result = _result(command, disposition=WorkRunReceiptDisposition.CREATED)
        work = replace(
            result.work_item,
            instance_id=definition.instance_id,
            workflow_id=definition.workflow_id,
            input_schema_id=definition.input_schema_id,
        )
        return replace(result, work_item=work, mode=command.mode)


class ApiOpener:
    """Route urllib's unchanged method/headers/body through the real ASGI app."""

    def __init__(self, application):
        self.application = application
        self.requests = []
        self.remove_csrf = False
        self.rewrite_response = lambda path, value: value

    def open(self, request, *, timeout):
        self.requests.append(request)
        headers = dict(request.header_items())
        if self.remove_csrf:
            headers.pop("X-csrf-token", None)

        async def call():
            async with AsyncClient(
                transport=ASGITransport(app=self.application, client=("127.0.0.1", 1234)),
                base_url=ORIGIN,
            ) as client:
                return await client.request(
                    request.get_method(),
                    request.full_url,
                    headers=headers,
                    content=request.data,
                    timeout=timeout,
                )

        response = asyncio.run(call())
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "", {}, None)
        payload = self.rewrite_response(response.request.url.path, response.json())
        return io.BytesIO(json.dumps(payload).encode())


@pytest.fixture
def local_api():
    admission = Admission()
    app = create_app(
        Settings(_env_file=None, catalog_root=ROOT / "catalog/v1", api_trusted_origins=(ORIGIN,)),
        readiness_probe=ReadyProbe(),
        manual_dry_run_service=admission,
    )
    client = LocalClient(ORIGIN)
    opener = ApiOpener(app)
    client.opener = opener
    return client, opener, admission


def test_documented_commands_cover_exactly_the_registered_five_presets():
    assert len(COMMANDS) == 5
    assert {command[command.index("--scenario") + 1] for command in COMMANDS} == {
        item.id for item in DEMO_SCENARIOS.list()
    }
    assert len({command[command.index("--idempotency-key") + 1] for command in COMMANDS}) == 5
    for scenario_id, filename in demo_local.SCENARIOS.items():
        fixture = json.loads((ROOT / "tests/fixtures/demos" / filename).read_text())
        assert canonical_json_bytes(fixture) == canonical_json_bytes(
            DEMO_SCENARIOS.get(scenario_id).fixture
        )


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: command[6])
def test_documented_command_uses_actual_route_csrf_and_exact_fixture(
    command, local_api, monkeypatch, capsys
):
    client, opener, admission = local_api
    monkeypatch.setattr(
        demo_local, "LocalClient", lambda origin: client if origin == ORIGIN else None
    )
    assert demo_local.main(command[3:]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    scenario_id = command[command.index("--scenario") + 1]
    key = command[command.index("--idempotency-key") + 1]
    email = scenario_id == demo_local.EMAIL_SCENARIO
    assert report == {
        "ok": True,
        "status": "accepted_only",
        "scenario": scenario_id,
        "disposition": "created",
        "run_id": "run.demo01.api.01",
        "work_id": "work.demo01.api.01",
        "run_url": "/api/v1/runs/run.demo01.api.01",
        "approval_policy": "manual_review_required" if email else "no_external_actions",
    }
    assert len(admission.commands) == len(admission.principals) == 1
    admitted = admission.commands[0]
    assert admitted.demo_scenario_id == scenario_id
    assert admitted.campaign_brief_id is None
    assert admitted.mode is (WorkMode.MOCK_EXECUTION if email else WorkMode.DRY_RUN)
    assert canonical_json_bytes(admitted.input_payload) == canonical_json_bytes(
        DEMO_SCENARIOS.get(scenario_id).fixture
    )
    assert admission.principals[0].actor_id == "local-operator"
    writes = [request for request in opener.requests if request.get_method() != "GET"]
    assert len(writes) == 1
    request = writes[0]
    assert request.full_url == ORIGIN + f"/api/v1/demo-scenarios/{scenario_id}/runs"
    headers = {name.casefold(): value for name, value in request.header_items()}
    assert headers["origin"] == ORIGIN
    assert headers["sec-fetch-site"] == "same-origin"
    assert headers["idempotency-key"] == key
    assert len(headers["x-csrf-token"]) >= 32
    assert not any("/approve" in request.full_url for request in opener.requests)
    assert "Avery Demo" not in output and "example.test" not in output
    assert headers["x-csrf-token"] not in output and key not in output


def test_command_cannot_bypass_actual_csrf_gate(local_api):
    client, opener, admission = local_api
    opener.remove_csrf = True
    with pytest.raises(SmokeFailure, match="http_status_403"):
        demo_local.submit_preset(client, demo_local.EMAIL_SCENARIO, "csrf-negative-0001")
    assert admission.commands == []


def test_command_cannot_bypass_actual_operator_authorization(local_api):
    client, opener, admission = local_api
    settings = opener.application.state.settings
    from marketing_agents.infrastructure.adapters.identity import LocalIdentityProvider

    viewer = settings.model_copy(update={"local_identity_roles": ("viewer",)})
    opener.application.state.identity_provider = LocalIdentityProvider(viewer)
    with pytest.raises(SmokeFailure, match="http_status_403"):
        demo_local.submit_preset(client, demo_local.EMAIL_SCENARIO, "viewer-negative-0001")
    assert admission.commands == []


@pytest.mark.parametrize(
    "key", ["", "short", "a" * 241, "contains space", "line\nbreak", "\u00e9" * 8]
)
def test_retry_keys_fail_before_any_request(local_api, key):
    client, opener, admission = local_api
    with pytest.raises(SmokeFailure, match="demo_retry_key_invalid"):
        demo_local.submit_preset(client, demo_local.EMAIL_SCENARIO, key)
    assert opener.requests == admission.commands == []


@pytest.mark.parametrize("key", ["a" * 8, "z" * 240, "visible-!~:key"])
def test_retry_key_bounds_match_actual_api(local_api, key):
    client, _, admission = local_api
    assert demo_local.submit_preset(client, demo_local.EMAIL_SCENARIO, key)["ok"]
    assert len(admission.commands) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authMode", "remote"),
        ("environment", "production"),
        ("modelMode", "real"),
        ("connectorMode", "real"),
        ("networkPermission", True),
    ],
)
def test_unsafe_session_is_rejected_before_admission(local_api, field, value):
    client, opener, admission = local_api

    def rewrite(path, payload):
        if path == "/api/v1/session":
            payload[field] = value
        return payload

    opener.rewrite_response = rewrite
    with pytest.raises(SmokeFailure, match="unsafe_session"):
        demo_local.submit_preset(client, demo_local.EMAIL_SCENARIO, "unsafe-negative-0001")
    assert admission.commands == []
    assert all(request.get_method() == "GET" for request in opener.requests)


@pytest.mark.parametrize("change", ["fixture", "mode", "effect", "approvals", "writes", "registry"])
def test_discovery_drift_fails_before_admission(local_api, change):
    client, opener, admission = local_api

    def rewrite(path, payload):
        if path != "/api/v1/demo-scenarios":
            return payload
        item = next(item for item in payload["items"] if item["id"] == demo_local.EMAIL_SCENARIO)
        if change == "fixture":
            item["preset"]["email"] = "changed.person@example.test"
        elif change == "mode":
            item["mode"] = "real"
        elif change == "effect":
            item["effect"] = "read_only"
        elif change == "approvals":
            item["expected"]["approvals"] = 0
        elif change == "writes":
            item["expected"]["externalWrites"] = 0
        else:
            payload["items"].pop()
        return payload

    opener.rewrite_response = rewrite
    with pytest.raises(SmokeFailure, match=r"demo_(preset|registry)_drift"):
        demo_local.submit_preset(client, demo_local.EMAIL_SCENARIO, "drift-negative-0001")
    assert admission.commands == []
    assert all(request.get_method() == "GET" for request in opener.requests)


@pytest.mark.parametrize("change", ["scenario", "mode", "run_id", "work_id", "run_url", "status"])
def test_receipt_drift_never_prints_untrusted_server_content(
    local_api, monkeypatch, capsys, change
):
    client, opener, _ = local_api
    canary = "private-contact@example.test"

    def rewrite(path, payload):
        if path.endswith("/runs"):
            field = {
                "scenario": "scenarioId",
                "mode": "executionMode",
                "run_id": "runId",
                "work_id": "workId",
                "run_url": "runUrl",
                "status": "status",
            }[change]
            payload[field] = canary
        return payload

    opener.rewrite_response = rewrite
    monkeypatch.setattr(demo_local, "LocalClient", lambda origin: client)
    assert demo_local.main(COMMANDS[0][3:]) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"ok": False, "code": "demo_submission_failed"}
    assert canary not in output


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8080",
        "http://example.test:8080",
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://user@127.0.0.1:8080",
        "http://127.0.0.1:8080/private",
        "http://127.0.0.1:8080/?next=external",
        "http://127.0.0.1:8080/#token",
    ],
)
def test_noncanonical_origin_fails_without_connecting(origin, capsys):
    assert (
        demo_local.main(
            [
                "--origin",
                origin,
                "--scenario",
                demo_local.EMAIL_SCENARIO,
                "--idempotency-key",
                "origin-negative-0001",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {"ok": False, "code": "demo_submission_failed"}


def test_client_help_requires_only_standard_library():
    result = subprocess.run(
        [sys.executable, "-S", "-m", "scripts.demo_local", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "never approve or dispatch" in result.stdout


@pytest.mark.parametrize(
    "error_type", [ValueError, TypeError, KeyError, OSError, http.client.BadStatusLine]
)
def test_client_sanitizes_transport_and_contract_failure_text(monkeypatch, capsys, error_type):
    canary = "private-contact@example.test"

    def fail(*args):
        raise error_type(canary)

    monkeypatch.setattr(demo_local, "submit_preset", fail)
    assert demo_local.main(COMMANDS[0][3:]) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"ok": False, "code": "demo_submission_failed"}
    assert canary not in output


def test_documented_smoke_explicitly_discloses_test_authority_and_evidence_limits():
    assert "automatically records both approvals as the local" in DOC
    assert "owned fresh synthetic test installation" in DOC
    assert "does **not** independently measure model" in DOC
    assert "does not enable live execution or send mail" in DOC
    assert "do not claim a fresh Docker acceptance result" in DOC
    assert "single-preset client" in DOC and "**never approves**" in DOC
