"""API-06 authenticated approval resource and mutation HTTP contracts."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from marketing_agents.api import create_app
from marketing_agents.api.dependencies import (
    ApprovalDecisionExecutor,
    ApprovalResourceExecutor,
)
from marketing_agents.application.ports.identity import (
    AuthenticationEvidence,
    IdentityAuthenticationError,
)
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionService,
    ApprovalDecisionServiceError,
    AuthorizedApprovalDecision,
)
from marketing_agents.application.services.approval_records import ApprovalRecordService
from marketing_agents.application.services.approval_resources import (
    ApprovalListQuery,
    ApprovalPage,
    ApprovalRequestCommand,
    ApprovalRequestDisposition,
    ApprovalRequestResult,
    ApprovalResource,
    ApprovalResourceService,
    ApprovalResourceServiceError,
)
from marketing_agents.config import Settings
from marketing_agents.domain.enums import ApprovalStatus
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.infrastructure.db.models import (
    ConnectorActionReceiptRecord,
    ExternalActionDispatchAttemptRecord,
)
from sqlalchemy import func, select

from tests.integration.db.test_run_08_approval_persistence import (
    IncrementingIds,
    MutableClock,
    _context,
    _dependencies,
    _runtime,
    _seed_run_and_plan,
)
from tests.support.identity import StaticIdentityProvider, human_principal, service_principal

NOW = datetime(2026, 8, 26, 21, 0, tzinfo=UTC)
ACTION_HASH = "a" * 64
CANARY = "api-06-private-canary"
APPROVAL_ID = "approval.api-06.01"
ACTION_ID = "action.api-06.01"


class DenyingIdentityProvider:
    async def authenticate(
        self,
        evidence: AuthenticationEvidence,
    ) -> AuthenticatedPrincipal:
        del evidence
        raise IdentityAuthenticationError("missing")


class FakeApprovalResourceExecutor:
    def __init__(self, resource: ApprovalResource | None = None) -> None:
        self.resource = resource or _resource()
        self.page = ApprovalPage(
            items=(self.resource,),
            next_cursor="approval-page-v1.opaque-next-page",
        )
        self.request_result = ApprovalRequestResult(
            approval=self.resource,
            disposition=ApprovalRequestDisposition.EXISTING,
        )
        self.list_error: Exception | None = None
        self.read_error: Exception | None = None
        self.request_error: Exception | None = None
        self.list_calls: list[tuple[ApprovalListQuery, AuthenticatedPrincipal]] = []
        self.read_calls: list[tuple[str, AuthenticatedPrincipal]] = []
        self.request_calls: list[tuple[ApprovalRequestCommand, AuthenticatedPrincipal]] = []

    async def list(
        self,
        query: ApprovalListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalPage:
        self.list_calls.append((query, principal))
        if self.list_error is not None:
            raise self.list_error
        return self.page

    async def read(
        self,
        approval_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalResource:
        self.read_calls.append((approval_id, principal))
        if self.read_error is not None:
            raise self.read_error
        return self.resource

    async def request(
        self,
        command: ApprovalRequestCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalRequestResult:
        self.request_calls.append((command, principal))
        if self.request_error is not None:
            raise self.request_error
        return self.request_result


class RejectingDecisionExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[ApprovalDecisionCommand, AuthenticatedPrincipal]] = []

    async def decide(
        self,
        command: ApprovalDecisionCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> AuthorizedApprovalDecision:
        self.calls.append((command, principal))
        raise ApprovalDecisionServiceError(
            "approval_decision_conflict",
            f"{CANARY}-decision-service-detail",
        )


class ReasonMismatchedDecisionExecutor:
    """Return an internally coherent decision that does not bind the request body."""

    def __init__(self, delegate: ApprovalDecisionExecutor) -> None:
        self.delegate = delegate

    async def decide(
        self,
        command: ApprovalDecisionCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> AuthorizedApprovalDecision:
        result = await self.delegate.decide(command, principal=principal)
        mismatched = replace(
            result.decision,
            reason="different bounded executor reason",
        )
        return replace(
            result,
            decision=mismatched,
            request=replace(result.request, decision=mismatched),
        )


class MutatingApprovalResourceExecutor:
    """Delegate every operation while corrupting only the detail projection."""

    def __init__(self, delegate: ApprovalResourceExecutor, mutation: str) -> None:
        self.delegate = delegate
        self.mutation = mutation

    async def list(
        self,
        query: ApprovalListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalPage:
        return await self.delegate.list(query, principal=principal)

    async def read(
        self,
        approval_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalResource:
        resource = await self.delegate.read(approval_id, principal=principal)
        if self.mutation == "contradictory":
            return replace(resource, decision_actor_id="principal.api-06.contradictory")
        return replace(resource, approval_url="/api/v1/approvals/wrong-resource")

    async def request(
        self,
        command: ApprovalRequestCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> ApprovalRequestResult:
        return await self.delegate.request(command, principal=principal)


def _resource(
    *,
    approval_id: str = APPROVAL_ID,
    action_id: str = ACTION_ID,
    status: ApprovalStatus = ApprovalStatus.PENDING,
    generation: int = 1,
    requested_at: datetime = NOW,
) -> ApprovalResource:
    expires_at = requested_at + timedelta(minutes=30)
    is_expired = status is ApprovalStatus.EXPIRED
    is_actionable = status is ApprovalStatus.PENDING and not is_expired
    return ApprovalResource(
        approval_id=approval_id,
        status=status,
        resource_version=1,
        generation=generation,
        one_time_use_state="unused",
        action_id=action_id,
        action_type="email.send",
        capability_id="capability.email.send",
        connector_family="email",
        binding_id="binding.mock.email",
        destination_summary="redacted recipient",
        redacted_payload={
            "recipient": "[REDACTED]",
            "subject": "Safe approval summary",
        },
        payload_hash=ACTION_HASH,
        run_id="run.api-06.01",
        step_id="step.api-06.01",
        template_id="tpl.marketing.community.manager",
        instance_id="inst.marketing.community.manager.01",
        policy_id="policy.external-write.default",
        required_roles=("approver",),
        required_scopes=("scope.external-write",),
        allow_self_approval=False,
        requested_by="principal.api-06.operator",
        requested_at=requested_at,
        expires_at=expires_at,
        updated_at=requested_at,
        is_expired=is_expired,
        is_actionable=is_actionable,
        decision_id=None,
        decision_kind=None,
        decision_actor_id=None,
        decision_reason_code=None,
        decision_reason=None,
        decided_at=None,
        expired_at=requested_at if is_expired else None,
        replacement_approval_id=None,
        renewed_at=None,
        superseded_at=None,
        superseded_reason_code=None,
        consumed_at=None,
        approval_url=f"/api/v1/approvals/{approval_id}",
        action_url=f"/api/v1/external-actions/{action_id}",
        run_url="/api/v1/runs/run.api-06.01",
        step_url="/api/v1/runs/run.api-06.01/steps/step.api-06.01",
        template_url="/api/v1/agent-templates/tpl.marketing.community.manager",
        instance_url="/api/v1/agent-instances/inst.marketing.community.manager.01",
    )


def _reader(role: str = "viewer") -> AuthenticatedPrincipal:
    return human_principal(
        actor_id=f"principal.api-06.{role}",
        roles=frozenset({role}),
        scopes=frozenset({"approvals:read"}),
    )


def _requester() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.api-06.operator",
        roles=frozenset({"operator"}),
        scopes=frozenset({"approvals:read", "approvals:request"}),
    )


def _control_plane_principal() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.api-06.control-plane",
        roles=frozenset({"viewer", "operator", "approver"}),
        scopes=frozenset(
            {
                "approvals:read",
                "approvals:request",
                "approvals:decide",
                "scope.external-write",
            }
        ),
    )


def _app(
    resource_executor: object | None,
    *,
    principal: AuthenticatedPrincipal | None = None,
    identity_provider: object | None = None,
    decision_executor: object | None = None,
) -> FastAPI:
    provider = identity_provider or StaticIdentityProvider(principal or _reader())
    return create_app(
        Settings(_env_file=None),
        identity_provider=cast(Any, provider),
        approval_resource_service=cast(
            ApprovalResourceExecutor | None,
            resource_executor,
        ),
        approval_decision_service=cast(
            ApprovalDecisionExecutor | None,
            decision_executor,
        ),
    )


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    **kwargs: Any,
) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.request(method, path, **kwargs)


async def _stream_chunks(chunks: tuple[bytes, ...]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _assert_private(response: Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization"


@pytest.mark.asyncio
async def test_api_06_list_and_detail_are_bounded_safe_private_projections() -> None:
    executor = FakeApprovalResourceExecutor()
    principal = _reader()
    application = _app(executor, principal=principal)

    listed = await _request(
        application,
        "GET",
        "/api/v1/approvals",
        params={
            "status": "pending",
            "run_id": "run.api-06.01",
            "action_id": ACTION_ID,
            "limit": 1,
        },
    )
    detail = await _request(application, "GET", f"/api/v1/approvals/{APPROVAL_ID}")

    assert listed.status_code == 200
    assert detail.status_code == 200
    _assert_private(listed)
    _assert_private(detail)
    assert executor.list_calls == [
        (
            ApprovalListQuery(
                status=ApprovalStatus.PENDING,
                run_id="run.api-06.01",
                action_id=ACTION_ID,
                limit=1,
            ),
            principal,
        )
    ]
    assert executor.read_calls == [(APPROVAL_ID, principal)]

    listed_body = listed.json()
    assert listed_body["next_cursor"] == "approval-page-v1.opaque-next-page"
    assert len(listed_body["items"]) == 1
    summary = listed_body["items"][0]
    assert summary == {
        "id": APPROVAL_ID,
        "status": "pending",
        "resource_version": 1,
        "generation": 1,
        "action_id": ACTION_ID,
        "action_type": "email.send",
        "destination_summary": "redacted recipient",
        "run_id": "run.api-06.01",
        "template_id": "tpl.marketing.community.manager",
        "instance_id": "inst.marketing.community.manager.01",
        "requested_at": "2026-08-26T21:00:00Z",
        "expires_at": "2026-08-26T21:30:00Z",
        "is_expired": False,
        "is_actionable": True,
        "approval_url": f"/api/v1/approvals/{APPROVAL_ID}",
        "action_url": f"/api/v1/external-actions/{ACTION_ID}",
        "run_url": "/api/v1/runs/run.api-06.01",
    }
    assert "payload_hash" not in summary
    assert "redacted_payload" not in summary

    detail_body = detail.json()
    assert detail_body["redacted_payload"] == {
        "recipient": "[REDACTED]",
        "subject": "Safe approval summary",
    }
    assert detail_body["payload_hash"] == ACTION_HASH
    assert not {
        "action_envelope",
        "authorization_set_id",
        "correlation_id",
        "idempotency_key",
        "integrity_digest",
        "plan_hash",
        "raw_payload",
        "reason",
        "reservation_id",
        "semantic_action_hash",
    }.intersection(detail_body)
    rendered = json.dumps(detail_body, sort_keys=True)
    assert CANARY not in rendered
    assert "executed" not in rendered
    assert "published" not in rendered
    assert "sent" not in rendered


@pytest.mark.parametrize("role", ["viewer", "operator", "approver", "local_admin"])
@pytest.mark.parametrize(
    ("path", "call_attribute"),
    [
        ("/api/v1/approvals", "list_calls"),
        (f"/api/v1/approvals/{APPROVAL_ID}", "read_calls"),
    ],
)
@pytest.mark.asyncio
async def test_api_06_every_human_read_role_requires_the_read_scope(
    role: str,
    path: str,
    call_attribute: str,
) -> None:
    executor = FakeApprovalResourceExecutor()
    response = await _request(_app(executor, principal=_reader(role)), "GET", path)

    assert response.status_code == 200
    _assert_private(response)
    assert len(getattr(executor, call_attribute)) == 1


@pytest.mark.parametrize(
    "principal",
    [
        human_principal(
            actor_id="principal.api-06.no-role",
            roles=frozenset({"auditor"}),
            scopes=frozenset({"approvals:read"}),
        ),
        human_principal(
            actor_id="principal.api-06.no-scope",
            roles=frozenset({"viewer"}),
            scopes=frozenset(),
        ),
        service_principal(
            actor_id="principal.api-06.service-reader",
            roles=frozenset({"viewer"}),
            scopes=frozenset({"approvals:read"}),
        ),
    ],
    ids=["role", "scope", "service"],
)
@pytest.mark.parametrize(
    "path",
    ["/api/v1/approvals", f"/api/v1/approvals/{APPROVAL_ID}"],
)
@pytest.mark.asyncio
async def test_api_06_read_role_scope_matrix_denies_before_executor(
    principal: AuthenticatedPrincipal,
    path: str,
) -> None:
    executor = FakeApprovalResourceExecutor()
    response = await _request(_app(executor, principal=principal), "GET", path)

    assert response.status_code == 403
    assert response.json() == {"detail": "approval read is forbidden"}
    _assert_private(response)
    assert executor.list_calls == []
    assert executor.read_calls == []


@pytest.mark.asyncio
async def test_api_06_missing_identity_is_unauthorized_before_resource_lookup() -> None:
    executor = FakeApprovalResourceExecutor()
    response = await _request(
        _app(executor, identity_provider=DenyingIdentityProvider()),
        "GET",
        "/api/v1/approvals",
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    _assert_private(response)
    assert executor.list_calls == []


@pytest.mark.asyncio
async def test_api_06_untrusted_host_rejection_remains_private() -> None:
    executor = FakeApprovalResourceExecutor()

    response = await _request(
        _app(executor),
        "GET",
        "/api/v1/approvals",
        headers={"Host": "untrusted.example.invalid"},
    )

    assert response.status_code == 400
    assert response.text == "Invalid host header"
    _assert_private(response)
    assert response.headers["vary"].split(",").count("Authorization") == 1
    assert executor.list_calls == []


@pytest.mark.parametrize(
    "principal",
    [
        human_principal(
            actor_id="principal.api-06.viewer-request",
            roles=frozenset({"viewer"}),
            scopes=frozenset({"approvals:request"}),
        ),
        human_principal(
            actor_id="principal.api-06.operator-no-request",
            roles=frozenset({"operator"}),
            scopes=frozenset({"approvals:read"}),
        ),
        human_principal(
            actor_id="principal.api-06.operator-no-read",
            roles=frozenset({"operator"}),
            scopes=frozenset({"approvals:request"}),
        ),
        service_principal(
            actor_id="principal.api-06.service-request",
            roles=frozenset({"operator"}),
            scopes=frozenset({"approvals:request"}),
        ),
    ],
    ids=["role", "request-scope", "read-scope", "service"],
)
@pytest.mark.asyncio
async def test_api_06_request_role_scope_matrix_denies_before_executor(
    principal: AuthenticatedPrincipal,
) -> None:
    executor = FakeApprovalResourceExecutor()
    response = await _request(
        _app(executor, principal=principal),
        "POST",
        f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
        json={
            "expected_generation": 0,
            "expected_payload_hash": ACTION_HASH,
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "approval request creation is forbidden"}
    _assert_private(response)
    assert executor.request_calls == []


@pytest.mark.parametrize(
    ("disposition", "expected_generation", "resource", "expected_status"),
    [
        (
            ApprovalRequestDisposition.EXISTING,
            0,
            _resource(),
            200,
        ),
        (
            ApprovalRequestDisposition.RENEWED,
            1,
            _resource(
                approval_id="approval.api-06.02",
                generation=2,
                requested_at=NOW + timedelta(hours=1),
            ),
            201,
        ),
    ],
    ids=["existing", "renewed"],
)
@pytest.mark.asyncio
async def test_api_06_existing_and_renewed_requests_return_authoritative_location(
    disposition: ApprovalRequestDisposition,
    expected_generation: int,
    resource: ApprovalResource,
    expected_status: int,
) -> None:
    principal = _requester()
    executor = FakeApprovalResourceExecutor(resource)
    executor.request_result = ApprovalRequestResult(
        approval=resource,
        disposition=disposition,
    )

    response = await _request(
        _app(executor, principal=principal),
        "POST",
        f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
        json={
            "expected_generation": expected_generation,
            "expected_payload_hash": ACTION_HASH,
        },
    )

    assert response.status_code == expected_status
    _assert_private(response)
    assert response.headers["location"] == resource.approval_url
    assert response.json()["disposition"] == disposition.value
    assert response.json()["approval"]["id"] == resource.approval_id
    assert response.json()["approval"]["generation"] == resource.generation
    assert len(executor.request_calls) == 1
    command, called_principal = executor.request_calls[0]
    assert called_principal == principal
    assert command.action_id == ACTION_ID
    assert command.expected_generation == expected_generation
    assert command.expected_action_hash == ACTION_HASH
    assert command.correlation_id.startswith("correlation.approval-request-api.")


@pytest.mark.parametrize(
    ("expected_generation", "disposition", "resource"),
    [
        (
            0,
            ApprovalRequestDisposition.EXISTING,
            replace(
                _resource(status=ApprovalStatus.REJECTED),
                is_actionable=False,
            ),
        ),
        (
            1,
            ApprovalRequestDisposition.RENEWED,
            replace(
                _resource(
                    approval_id="approval.api-06.misattributed",
                    generation=2,
                    requested_at=NOW + timedelta(hours=1),
                ),
                requested_by="principal.api-06.someone-else",
            ),
        ),
        (
            1,
            ApprovalRequestDisposition.EXISTING,
            replace(
                _resource(
                    approval_id="approval.api-06.non-pristine",
                    generation=2,
                    requested_at=NOW + timedelta(hours=1),
                ),
                is_actionable=False,
            ),
        ),
    ],
    ids=["terminal-existing", "misattributed-renewal", "non-pristine-replay"],
)
@pytest.mark.asyncio
async def test_api_06_request_result_must_bind_reusable_lifecycle_and_requester(
    expected_generation: int,
    disposition: ApprovalRequestDisposition,
    resource: ApprovalResource,
) -> None:
    executor = FakeApprovalResourceExecutor(resource)
    executor.request_result = ApprovalRequestResult(
        approval=resource,
        disposition=disposition,
    )

    response = await _request(
        _app(executor, principal=_requester()),
        "POST",
        f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
        json={
            "expected_generation": expected_generation,
            "expected_payload_hash": ACTION_HASH,
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "approval_service_unavailable",
            "message": "approval service is unavailable",
        }
    }
    _assert_private(response)
    assert len(executor.request_calls) == 1


@pytest.mark.asyncio
async def test_api_06_mutations_require_one_application_json_media_type() -> None:
    principal = _control_plane_principal()
    resource_executor = FakeApprovalResourceExecutor()
    decision_executor = RejectingDecisionExecutor()
    application = _app(
        resource_executor,
        principal=principal,
        decision_executor=decision_executor,
    )
    requests = (
        (
            f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
            b'{"expected_generation":0,"expected_payload_hash":"' + ACTION_HASH.encode() + b'"}',
        ),
        (
            f"/api/v1/approvals/{APPROVAL_ID}/approve",
            b'{"expected_generation":1,"expected_payload_hash":"' + ACTION_HASH.encode() + b'"}',
        ),
        (
            f"/api/v1/approvals/{APPROVAL_ID}/reject",
            b'{"expected_generation":1,"expected_payload_hash":"' + ACTION_HASH.encode() + b'"}',
        ),
    )
    invalid_headers: tuple[list[tuple[str, str]], ...] = (
        [],
        [("Content-Type", "text/plain")],
        [("Content-Type", "application/merge-patch+json")],
        [
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
        ],
    )

    for path, body in requests:
        for headers in invalid_headers:
            response = await _request(
                application,
                "POST",
                path,
                content=body,
                headers=headers,
            )
            assert response.status_code == 415
            _assert_private(response)
            assert response.json() == {
                "detail": {
                    "code": "approval_json_required",
                    "message": "approval mutations require application/json",
                }
            }

    assert resource_executor.request_calls == []
    assert decision_executor.calls == []

    accepted = await _request(
        application,
        "POST",
        f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
        content=requests[0][1],
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert accepted.status_code == 200
    assert len(resource_executor.request_calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
        f"/api/v1/approvals/{APPROVAL_ID}/approve",
        f"/api/v1/approvals/{APPROVAL_ID}/reject",
    ],
)
@pytest.mark.asyncio
async def test_api_06_mutation_body_limit_rejects_declared_and_streamed_oversize(
    path: str,
) -> None:
    executor = FakeApprovalResourceExecutor()
    decision_executor = RejectingDecisionExecutor()
    application = _app(
        executor,
        principal=_control_plane_principal(),
        decision_executor=decision_executor,
    )

    declared = await _request(
        application,
        "POST",
        path,
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": "8193",
        },
    )
    streamed = await _request(
        application,
        "POST",
        path,
        content=_stream_chunks((b"{" + (b" " * 4_999), b" " * 4_000)),
        headers={"Content-Type": "application/json"},
    )

    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json() == {
            "detail": {
                "code": "approval_body_too_large",
                "message": "approval request body exceeds the allowed size",
            }
        }
        _assert_private(response)
    assert executor.request_calls == []
    assert decision_executor.calls == []


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
        f"/api/v1/approvals/{APPROVAL_ID}/approve",
        f"/api/v1/approvals/{APPROVAL_ID}/reject",
    ],
)
@pytest.mark.asyncio
async def test_api_06_excessively_deep_json_is_rejected_before_executor(path: str) -> None:
    executor = FakeApprovalResourceExecutor()
    decision_executor = RejectingDecisionExecutor()
    body = b'{"nested":' + (b"[" * 17) + b"0" + (b"]" * 17) + b"}"

    response = await _request(
        _app(
            executor,
            principal=_control_plane_principal(),
            decision_executor=decision_executor,
        ),
        "POST",
        path,
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "approval_input_invalid",
            "message": "approval request input is invalid",
        }
    }
    _assert_private(response)
    assert executor.request_calls == []
    assert decision_executor.calls == []


@pytest.mark.asyncio
async def test_api_06_mutation_authority_fields_are_rejected_without_reflection() -> None:
    principal = _control_plane_principal()
    resource_executor = FakeApprovalResourceExecutor()
    decision_executor = RejectingDecisionExecutor()
    application = _app(
        resource_executor,
        principal=principal,
        decision_executor=decision_executor,
    )
    cases = (
        (
            f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
            {
                "expected_generation": 0,
                "expected_payload_hash": ACTION_HASH,
                "payload": {"secret": CANARY},
                "requested_by": CANARY,
            },
        ),
        (
            f"/api/v1/approvals/{APPROVAL_ID}/approve",
            {
                "expected_generation": 1,
                "expected_payload_hash": ACTION_HASH,
                "actor_id": CANARY,
                "roles": ["approver", CANARY],
            },
        ),
    )

    for path, body in cases:
        response = await _request(application, "POST", path, json=body)
        assert response.status_code == 422
        _assert_private(response)
        assert response.json()["detail"]["code"] == "request_validation_failed"
        assert CANARY not in response.text
        assert '"input"' not in response.text
        assert '"ctx"' not in response.text
        assert {item["pointer"] for item in response.json()["detail"]["field_errors"]} == {"/body"}

    assert resource_executor.request_calls == []
    assert decision_executor.calls == []


@pytest.mark.parametrize(
    "unsupported",
    ["\x00", "\x1f", "\x7f", "\x85", "\ud800"],
    ids=["nul", "c0", "del", "c1", "lone-surrogate"],
)
@pytest.mark.asyncio
async def test_api_06_decision_reason_rejects_control_and_surrogate_text_before_executor(
    unsupported: str,
) -> None:
    resource_executor = FakeApprovalResourceExecutor()
    decision_executor = RejectingDecisionExecutor()
    reason = f"{CANARY}{unsupported}unsafe"

    response = await _request(
        _app(
            resource_executor,
            principal=_control_plane_principal(),
            decision_executor=decision_executor,
        ),
        "POST",
        f"/api/v1/approvals/{APPROVAL_ID}/approve",
        content=json.dumps(
            {
                "expected_generation": 1,
                "expected_payload_hash": ACTION_HASH,
                "reason": reason,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii"),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "request_validation_failed"
    assert CANARY not in response.text
    _assert_private(response)
    assert decision_executor.calls == []


@pytest.mark.parametrize(
    ("method", "path", "principal", "error_attribute", "error", "status_code", "body"),
    [
        (
            "GET",
            "/api/v1/approvals",
            _reader(),
            "list_error",
            ApprovalResourceServiceError("approval_cursor_invalid", CANARY),
            422,
            {
                "detail": {
                    "code": "approval_input_invalid",
                    "message": "approval request input is invalid",
                }
            },
        ),
        (
            "GET",
            f"/api/v1/approvals/{APPROVAL_ID}",
            _reader(),
            "read_error",
            ApprovalResourceServiceError("approval_request_missing", CANARY),
            404,
            {
                "detail": {
                    "code": "approval_not_found",
                    "message": "approval resource was not found",
                }
            },
        ),
        (
            "POST",
            f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
            _requester(),
            "request_error",
            ApprovalResourceServiceError("approval_record_corrupt", CANARY),
            503,
            {
                "detail": {
                    "code": "approval_service_unavailable",
                    "message": "approval service is unavailable",
                }
            },
        ),
        (
            "POST",
            f"/api/v1/external-actions/{ACTION_ID}/approval-requests",
            _requester(),
            "request_error",
            ApprovalResourceServiceError("private_unknown_code", CANARY),
            409,
            {
                "detail": {
                    "code": "approval_request_conflict",
                    "message": "approval request could not be created",
                }
            },
        ),
    ],
    ids=["query", "missing", "corrupt", "unknown"],
)
@pytest.mark.asyncio
async def test_api_06_service_errors_are_stable_private_and_non_reflective(
    method: str,
    path: str,
    principal: AuthenticatedPrincipal,
    error_attribute: str,
    error: Exception,
    status_code: int,
    body: dict[str, object],
) -> None:
    executor = FakeApprovalResourceExecutor()
    setattr(executor, error_attribute, error)
    kwargs: dict[str, object] = {}
    if method == "POST":
        kwargs["json"] = {
            "expected_generation": 0,
            "expected_payload_hash": ACTION_HASH,
        }

    response = await _request(_app(executor, principal=principal), method, path, **kwargs)

    assert response.status_code == status_code
    _assert_private(response)
    assert response.json() == body
    assert CANARY not in response.text
    assert len(response.content) < 256


@pytest.mark.asyncio
async def test_api_06_malformed_primary_resource_fails_closed() -> None:
    malformed = replace(
        _resource(),
        approval_url="/api/v1/approvals/contradictory-resource",
    )

    response = await _request(
        _app(FakeApprovalResourceExecutor(malformed)),
        "GET",
        f"/api/v1/approvals/{APPROVAL_ID}",
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "approval_service_unavailable",
            "message": "approval service is unavailable",
        }
    }
    _assert_private(response)


@pytest.mark.asyncio
async def test_api_06_decision_errors_remain_private_and_do_not_reflect_reason() -> None:
    resource_executor = FakeApprovalResourceExecutor()
    decision_executor = RejectingDecisionExecutor()
    response = await _request(
        _app(
            resource_executor,
            principal=_control_plane_principal(),
            decision_executor=decision_executor,
        ),
        "POST",
        f"/api/v1/approvals/{APPROVAL_ID}/approve",
        json={
            "expected_generation": 1,
            "expected_payload_hash": ACTION_HASH,
            "reason": f"{CANARY}-caller-reason",
        },
    )

    assert response.status_code == 409
    _assert_private(response)
    assert response.json() == {
        "detail": {
            "code": "approval_decision_conflict",
            "message": "approval request could not be decided",
            "current_status": "pending",
            "current_resource_version": 1,
        }
    }
    assert CANARY not in response.text
    assert len(decision_executor.calls) == 1
    assert len(resource_executor.read_calls) == 1


@pytest.mark.asyncio
async def test_api_06_ambiguous_and_invalid_queries_fail_without_lookup_or_reflection() -> None:
    executor = FakeApprovalResourceExecutor()
    application = _app(executor)

    duplicate = await _request(
        application,
        "GET",
        "/api/v1/approvals?limit=1&limit=2",
    )
    invalid = await _request(
        application,
        "GET",
        "/api/v1/approvals",
        params={"cursor": CANARY + ("x" * 1_100)},
    )

    assert duplicate.status_code == 400
    assert duplicate.json() == {
        "detail": {
            "code": "approval_query_ambiguous",
            "message": "approval query parameters must be unique",
        }
    }
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "request_validation_failed"
    assert CANARY not in invalid.text
    _assert_private(duplicate)
    _assert_private(invalid)
    assert executor.list_calls == []


@pytest.mark.asyncio
async def test_api_06_non_ascii_cursor_digest_is_a_safe_input_error(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "api-06-non-ascii-cursor.db")
    dependencies = _dependencies(runtime, clock=MutableClock(), ids=IncrementingIds(2_650))
    payload = json.dumps(
        {
            "filter": "é" * 64,
            "id": APPROVAL_ID,
            "requested_at": NOW.isoformat(timespec="microseconds"),
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    cursor = "approval-page-v1." + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    try:
        response = await _request(
            _app(ApprovalResourceService(dependencies), principal=_reader()),
            "GET",
            "/api/v1/approvals",
            params={"cursor": cursor},
        )

        assert response.status_code == 422
        assert response.json() == {
            "detail": {
                "code": "approval_input_invalid",
                "message": "approval request input is invalid",
            }
        }
        _assert_private(response)
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("decision_path", "expected_status", "inject_resource"),
    [
        ("approve", "approved", True),
        ("reject", "rejected", False),
    ],
)
@pytest.mark.asyncio
async def test_api_06_decisions_preserve_run_10_shape_and_optionally_embed_authoritative_state(
    tmp_path: Path,
    decision_path: str,
    expected_status: str,
    inject_resource: bool,
) -> None:
    runtime = await _runtime(tmp_path / f"api-06-decision-{decision_path}.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(2_600))
    plan = await _seed_run_and_plan(
        dependencies,
        event_id=f"event.api-06.decision-{decision_path}",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context(f"api-06.decision-{decision_path}.register"),
    )
    approval_request = registered.requests[0].request
    clock.current = approval_request.requested_at + timedelta(seconds=1)
    decision_reason = f"bounded API-06 operator reason for {decision_path}: {CANARY}"
    resource_service = ApprovalResourceService(dependencies) if inject_resource else None
    application = create_app(
        Settings(_env_file=None),
        approval_decision_service=ApprovalDecisionService(dependencies),
        approval_resource_service=resource_service,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/api/v1/approvals/{approval_request.id}/{decision_path}",
                json={
                    "expected_generation": approval_request.generation,
                    "expected_payload_hash": approval_request.action_hash,
                    "reason": decision_reason,
                },
            )
            detail = (
                await client.get(f"/api/v1/approvals/{approval_request.id}")
                if inject_resource
                else None
            )
        async with runtime.session_factory() as session:
            dispatch_attempts = int(
                (
                    await session.execute(
                        select(func.count(ExternalActionDispatchAttemptRecord.external_action_id))
                    )
                ).scalar_one()
            )
            connector_receipts = int(
                (
                    await session.execute(
                        select(func.count(ConnectorActionReceiptRecord.receipt_id))
                    )
                ).scalar_one()
            )

        assert response.status_code == 200
        assert (dispatch_attempts, connector_receipts) == (0, 0)
        _assert_private(response)
        assert response.headers["location"] == f"/api/v1/approvals/{approval_request.id}"
        body = response.json()
        assert body["approval_id"] == approval_request.id
        assert body["action_id"] == approval_request.action_id
        assert body["run_id"] == approval_request.run_id
        assert body["status"] == expected_status
        assert body["decision_id"].startswith("approval-decision.")
        rendered = json.dumps(body, sort_keys=True)
        assert "executed" not in rendered
        assert "published" not in rendered
        assert "sent" not in rendered

        if detail is None:
            assert set(body) == {
                "approval_id",
                "decision_id",
                "action_id",
                "run_id",
                "status",
            }
            assert decision_reason not in rendered
        else:
            assert detail.status_code == 200
            _assert_private(detail)
            authoritative = detail.json()
            nested = body["approval"]
            assert nested["id"] == approval_request.id
            assert nested["action_id"] == approval_request.action_id
            assert nested["run_id"] == approval_request.run_id
            assert nested["payload_hash"] == approval_request.action_hash
            assert nested["decision_id"] == body["decision_id"]
            assert nested["decision_reason"] == decision_reason
            assert authoritative["decision_reason"] == decision_reason
            assert all(authoritative[key] == value for key, value in nested.items())
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_06_coherent_executor_result_must_bind_the_client_reason(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "api-06-cross-bound-decision.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(2_700))
    plan = await _seed_run_and_plan(
        dependencies,
        event_id="event.api-06.cross-bound-decision",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("api-06.cross-bound-decision.register"),
    )
    approval_request = registered.requests[0].request
    clock.current = approval_request.requested_at + timedelta(seconds=1)
    application = create_app(
        Settings(_env_file=None),
        approval_decision_service=ReasonMismatchedDecisionExecutor(
            ApprovalDecisionService(dependencies)
        ),
    )

    try:
        response = await _request(
            application,
            "POST",
            f"/api/v1/approvals/{approval_request.id}/approve",
            json={
                "expected_generation": approval_request.generation,
                "expected_payload_hash": approval_request.action_hash,
                "reason": "caller-bound reason",
            },
        )

        assert response.status_code == 409
        assert response.json() == {
            "detail": {
                "code": "approval_conflict",
                "message": "approval request could not be decided",
            }
        }
        _assert_private(response)
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("mutation", ["contradictory", "malformed"])
@pytest.mark.asyncio
async def test_api_06_invalid_optional_authoritative_state_is_omitted(
    tmp_path: Path,
    mutation: str,
) -> None:
    runtime = await _runtime(tmp_path / f"api-06-authoritative-{mutation}.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(2_750))
    plan = await _seed_run_and_plan(
        dependencies,
        event_id=f"event.api-06.authoritative-{mutation}",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context(f"api-06.authoritative-{mutation}.register"),
    )
    approval_request = registered.requests[0].request
    clock.current = approval_request.requested_at + timedelta(seconds=1)
    resources = MutatingApprovalResourceExecutor(
        ApprovalResourceService(dependencies),
        mutation,
    )
    application = create_app(
        Settings(_env_file=None),
        approval_decision_service=ApprovalDecisionService(dependencies),
        approval_resource_service=resources,
    )

    try:
        response = await _request(
            application,
            "POST",
            f"/api/v1/approvals/{approval_request.id}/approve",
            json={
                "expected_generation": approval_request.generation,
                "expected_payload_hash": approval_request.action_hash,
                "reason": "valid authoritative projection reason",
            },
        )

        assert response.status_code == 200
        _assert_private(response)
        assert response.headers["location"] == (f"/api/v1/approvals/{approval_request.id}")
        assert set(response.json()) == {
            "approval_id",
            "decision_id",
            "action_id",
            "run_id",
            "status",
        }
    finally:
        await runtime.dispose()


def test_api_06_openapi_declares_all_typed_approval_routes() -> None:
    document = _app(FakeApprovalResourceExecutor()).openapi()
    paths = document["paths"]

    assert set(paths["/api/v1/approvals"]) == {"get"}
    assert set(paths["/api/v1/approvals/{approval_id}"]) == {"get"}
    assert set(paths["/api/v1/external-actions/{action_id}/approval-requests"]) == {"post"}
    assert set(paths["/api/v1/approvals/{approval_id}/approve"]) == {"post"}
    assert set(paths["/api/v1/approvals/{approval_id}/reject"]) == {"post"}
    assert {
        paths["/api/v1/approvals"]["get"]["operationId"],
        paths["/api/v1/approvals/{approval_id}"]["get"]["operationId"],
        paths["/api/v1/external-actions/{action_id}/approval-requests"]["post"]["operationId"],
        paths["/api/v1/approvals/{approval_id}/approve"]["post"]["operationId"],
        paths["/api/v1/approvals/{approval_id}/reject"]["post"]["operationId"],
    } == {
        "listApprovals",
        "getApproval",
        "createApprovalRequest",
        "approveApproval",
        "rejectApproval",
    }

    request_operation = paths["/api/v1/external-actions/{action_id}/approval-requests"]["post"]
    request_responses = request_operation["responses"]
    assert request_responses["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApprovalRequestResponse"
    }
    assert set(request_responses) == {
        "200",
        "201",
        "400",
        "401",
        "403",
        "404",
        "409",
        "413",
        "415",
        "422",
        "503",
    }

    approval_operations = (
        paths["/api/v1/approvals"]["get"],
        paths["/api/v1/approvals/{approval_id}"]["get"],
        request_operation,
        paths["/api/v1/approvals/{approval_id}/approve"]["post"],
        paths["/api/v1/approvals/{approval_id}/reject"]["post"],
    )
    error_refs = {
        "#/components/schemas/ApprovalHttpError",
        "#/components/schemas/ApprovalPlainHttpError",
        "#/components/schemas/ApprovalRequestValidationError",
    }
    for operation in approval_operations:
        for response in operation["responses"].values():
            headers = response["headers"]
            assert headers["Cache-Control"]["schema"]["const"] == "no-store"
            assert headers["Vary"]["schema"]["type"] == "string"
        for status_code, response in operation["responses"].items():
            if status_code.startswith("2"):
                continue
            alternatives = response["content"]["application/json"]["schema"]["anyOf"]
            assert {candidate["$ref"] for candidate in alternatives} == error_refs

    for operation in approval_operations[2:]:
        for status_code, response in operation["responses"].items():
            if status_code.startswith("2"):
                assert response["headers"]["Location"]["schema"]["type"] == "string"

    for operation in approval_operations[-2:]:
        alternatives = operation["responses"]["200"]["content"]["application/json"]["schema"][
            "anyOf"
        ]
        assert [candidate["$ref"] for candidate in alternatives] == [
            "#/components/schemas/ApprovalDecisionResourceResponse",
            "#/components/schemas/ApprovalDecisionResponse",
        ]

    schemas = document["components"]["schemas"]
    assert schemas["ApprovalRequestInput"]["additionalProperties"] is False
    assert schemas["ApprovalDecisionInput"]["additionalProperties"] is False
    assert schemas["ApprovalRequestResponse"]["additionalProperties"] is False
    assert schemas["ApprovalResourceView"]["additionalProperties"] is False
    assert "decision_reason" in schemas["ApprovalResourceView"]["required"]
    assert {
        "ApprovalFieldError",
        "ApprovalHttpError",
        "ApprovalPlainHttpError",
        "ApprovalProblem",
        "ApprovalRequestValidationError",
        "ApprovalValidationProblem",
    } <= set(schemas)
