"""RUN-10 real approve/reject routes enforce identity and safe command projection."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
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
from marketing_agents.config import Settings
from marketing_agents.domain.enums import ApprovalDecisionKind
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.infrastructure.db.models import (
    ApprovalDecisionRecord,
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
from tests.support.api import assert_problem, browser_request
from tests.support.identity import (
    FalseyStaticIdentityProvider,
    StaticIdentityProvider,
    human_principal,
    service_principal,
)


class RejectingDecisionExecutor:
    def __init__(self, code: str = "approval_decision_conflict") -> None:
        self.code = code
        self.calls: list[tuple[ApprovalDecisionCommand, AuthenticatedPrincipal]] = []

    async def decide(
        self,
        command: ApprovalDecisionCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> AuthorizedApprovalDecision:
        self.calls.append((command, principal))
        raise ApprovalDecisionServiceError(
            self.code,
            "sensitive-service-error-canary",
        )


class FalseyRejectingDecisionExecutor(RejectingDecisionExecutor):
    def __bool__(self) -> bool:
        return False


class PathMismatchedDecisionExecutor:
    def __init__(self, result: AuthorizedApprovalDecision) -> None:
        self.result = result
        self.calls: list[ApprovalDecisionCommand] = []

    async def decide(
        self,
        command: ApprovalDecisionCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> AuthorizedApprovalDecision:
        del principal
        self.calls.append(command)
        rebound_decision = replace(
            self.result.decision,
            correlation_id=command.correlation_id,
        )
        return replace(
            self.result,
            decision=rebound_decision,
            request=replace(self.result.request, decision=rebound_decision),
        )


class DenyingIdentityProvider:
    async def authenticate(
        self,
        evidence: AuthenticationEvidence,
    ) -> AuthenticatedPrincipal:
        del evidence
        raise IdentityAuthenticationError("identity_missing")


def _body(action_hash: str = "0" * 64) -> dict[str, object]:
    return {
        "expected_generation": 1,
        "expected_payload_hash": action_hash,
    }


def _route(
    request_id: str = "approval-request.run-10.route",
    decision: str = "approve",
) -> str:
    return f"/api/v1/approvals/{request_id}/{decision}"


@pytest.mark.parametrize(
    ("decision_path", "decision_kind", "expected_status"),
    [
        ("approve", ApprovalDecisionKind.APPROVE, "approved"),
        ("reject", ApprovalDecisionKind.REJECT, "rejected"),
    ],
)
@pytest.mark.asyncio
async def test_run_10_real_route_uses_local_actor_and_returns_only_resource_state(
    tmp_path: Path,
    decision_path: str,
    decision_kind: ApprovalDecisionKind,
    expected_status: str,
) -> None:
    runtime = await _runtime(tmp_path / f"run-10-route-{decision_path}.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1_200))
    plan = await _seed_run_and_plan(
        dependencies,
        event_id=f"event.run-10.route-{decision_path}",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context(f"run-10.route-{decision_path}.register"),
    )
    request = registered.requests[0].request
    clock.current = request.requested_at + timedelta(seconds=1)
    application = create_app(
        Settings(_env_file=None),
        approval_decision_service=ApprovalDecisionService(dependencies),
    )
    reason_canary = f"private-route-reason-{decision_path}"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await browser_request(
                client,
                "POST",
                _route(request.id, decision_path),
                csrf_app=application,
                json={
                    **_body(request.action_hash),
                    "reason": reason_canary,
                },
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload == {
            "approval_id": request.id,
            "decision_id": payload["decision_id"],
            "action_id": request.action_id,
            "run_id": request.run_id,
            "status": expected_status,
        }
        assert payload["decision_id"].startswith("approval-decision.")
        rendered = json.dumps(payload, sort_keys=True)
        assert reason_canary not in rendered
        assert "connector" not in rendered
        assert "executed" not in rendered
        assert "published" not in rendered

        async with runtime.session_factory() as session:
            decision = (await session.execute(select(ApprovalDecisionRecord))).scalar_one()
            attempts = int(
                (
                    await session.execute(
                        select(func.count(ExternalActionDispatchAttemptRecord.external_action_id))
                    )
                ).scalar_one()
            )
            receipts = int(
                (
                    await session.execute(
                        select(func.count(ConnectorActionReceiptRecord.receipt_id))
                    )
                ).scalar_one()
            )
        assert decision.decision == decision_kind.value
        assert decision.actor_id == "local-operator"
        assert decision.authentication_method == "local_fixed"
        assert decision.correlation_id.startswith("correlation.api.")
        assert (attempts, receipts) == (0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "principal",
    [
        human_principal(
            actor_id="principal.viewer",
            roles=frozenset({"viewer"}),
            scopes=frozenset({"approvals:read"}),
        ),
        human_principal(
            actor_id="principal.operator",
            roles=frozenset({"operator"}),
            scopes=frozenset({"approvals:read", "approvals:decide"}),
        ),
        human_principal(
            actor_id="principal.admin",
            roles=frozenset({"local_admin"}),
            scopes=frozenset({"approvals:read", "approvals:decide"}),
        ),
        service_principal(
            actor_id="principal.scheduler",
            roles=frozenset({"approver"}),
            scopes=frozenset({"approvals:decide", "scope.external-write"}),
        ),
    ],
    ids=["viewer", "operator", "admin-without-approver", "service"],
)
@pytest.mark.parametrize("decision_path", ["approve", "reject"])
@pytest.mark.asyncio
async def test_run_10_route_role_matrix_denies_before_executor(
    principal: AuthenticatedPrincipal,
    decision_path: str,
) -> None:
    executor = RejectingDecisionExecutor()
    application = create_app(
        Settings(_env_file=None),
        identity_provider=StaticIdentityProvider(principal),
        approval_decision_service=executor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await browser_request(
            client,
            "POST",
            _route(decision=decision_path),
            csrf_app=application,
            json=_body(),
        )
    assert_problem(response, status_code=403, code="request_forbidden")
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("decision_path", ["approve", "reject"])
async def test_run_10_route_missing_identity_is_unauthorized_before_executor(
    decision_path: str,
) -> None:
    executor = RejectingDecisionExecutor()
    application = create_app(
        Settings(_env_file=None),
        identity_provider=DenyingIdentityProvider(),
        approval_decision_service=executor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await browser_request(
            client,
            "POST",
            _route(decision=decision_path),
            csrf_app=application,
            json=_body(),
        )
    assert_problem(response, status_code=401, code="authentication_required")
    assert executor.calls == []


@pytest.mark.parametrize(
    "forged_field",
    [
        "actor_id",
        "roles",
        "scopes",
        "correlation_id",
        "decision",
        "expected_action_hash",
    ],
)
@pytest.mark.asyncio
async def test_run_10_forged_body_authority_and_decision_never_reach_executor(
    forged_field: str,
) -> None:
    executor = RejectingDecisionExecutor()
    provider = FalseyStaticIdentityProvider(
        human_principal(
            roles=frozenset({"approver"}),
            scopes=frozenset({"approvals:decide", "scope.external-write"}),
        )
    )
    application = create_app(
        Settings(_env_file=None),
        identity_provider=provider,
        approval_decision_service=executor,
    )
    forged = {
        **_body(),
        forged_field: (["approver"] if forged_field in {"roles", "scopes"} else "forged-value"),
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await browser_request(
            client,
            "POST",
            _route(),
            csrf_app=application,
            json=forged,
        )
    assert response.status_code == 422
    assert "forged-value" not in response.text
    assert executor.calls == []


@pytest.mark.asyncio
async def test_run_10_spoofed_header_and_oversized_reason_fail_without_secret_echo() -> None:
    executor = RejectingDecisionExecutor()
    provider = StaticIdentityProvider(
        human_principal(
            roles=frozenset({"approver"}),
            scopes=frozenset({"approvals:decide", "scope.external-write"}),
        )
    )
    application = create_app(
        Settings(_env_file=None),
        identity_provider=provider,
        approval_decision_service=executor,
    )
    reason_canary = "oversized-secret-reason-canary-" + "x" * 600
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        spoofed = await browser_request(
            client,
            "POST",
            _route(),
            csrf_app=application,
            json=_body(),
            headers={"X-Actor-ID": "principal.forged"},
        )
        invalid_reason = await browser_request(
            client,
            "POST",
            _route(),
            csrf_app=application,
            json={**_body(), "reason": reason_canary},
        )
        field_name_canary = "secret-field-name-canary"
        many_extras = await browser_request(
            client,
            "POST",
            _route(),
            csrf_app=application,
            json={
                **_body(),
                field_name_canary: "secret-field-value-canary",
                **{f"untrusted-extra-{index}": index for index in range(100)},
            },
        )
    assert_problem(spoofed, status_code=400, code="request_invalid")
    assert_problem(
        invalid_reason,
        status_code=422,
        code="request_validation_failed",
    )
    assert reason_canary not in invalid_reason.text
    assert '"input"' not in invalid_reason.text
    assert '"ctx"' not in invalid_reason.text
    extras_payload = assert_problem(
        many_extras,
        status_code=422,
        code="request_validation_failed",
    )
    assert field_name_canary not in many_extras.text
    assert "secret-field-value-canary" not in many_extras.text
    field_errors = extras_payload["field_errors"]
    assert len(field_errors) == 32
    assert {item["pointer"] for item in field_errors} == {"/body"}
    assert executor.calls == []


@pytest.mark.parametrize(
    ("service_code", "expected_status", "safe_code"),
    [
        ("approval_scope_missing", 403, "approval_scope_missing"),
        ("approval_request_missing", 404, "approval_not_found"),
        ("approval_decision_conflict", 409, "approval_decision_conflict"),
        ("approval_command_invalid", 422, "approval_input_invalid"),
        ("approval_record_corrupt", 409, "approval_conflict"),
    ],
)
@pytest.mark.asyncio
async def test_run_10_route_maps_service_errors_without_sensitive_detail(
    service_code: str,
    expected_status: int,
    safe_code: str,
) -> None:
    executor = FalseyRejectingDecisionExecutor(service_code)
    application = create_app(
        Settings(_env_file=None),
        identity_provider=StaticIdentityProvider(
            human_principal(
                roles=frozenset({"approver"}),
                scopes=frozenset({"approvals:decide", "scope.external-write"}),
            )
        ),
        approval_decision_service=executor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await browser_request(
            client,
            "POST",
            _route(),
            csrf_app=application,
            json=_body(),
        )
    assert_problem(response, status_code=expected_status, code=safe_code)
    assert "sensitive-service-error-canary" not in response.text
    assert len(executor.calls) == 1
    assert executor.calls[0][0].decision is ApprovalDecisionKind.APPROVE
    assert executor.calls[0][0].correlation_id.startswith("correlation.api.")


@pytest.mark.asyncio
async def test_run_10_real_service_rechecks_stored_policy_after_route_guard(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-10-route-service-defense.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-10.route-defense")
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("run-10.route-defense.register"),
    )
    request = registered.requests[0].request
    principal = human_principal(
        actor_id="principal.route-baseline-only",
        roles=frozenset({"approver"}),
        scopes=frozenset({"approvals:decide"}),
    )
    application = create_app(
        Settings(_env_file=None),
        identity_provider=StaticIdentityProvider(principal),
        approval_decision_service=ApprovalDecisionService(dependencies),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await browser_request(
                client,
                "POST",
                _route(request.id),
                csrf_app=application,
                json=_body(request.action_hash),
            )
        assert_problem(response, status_code=403, code="approval_scope_missing")
        async with runtime.session_factory() as session:
            decisions = int(
                (await session.execute(select(func.count(ApprovalDecisionRecord.id)))).scalar_one()
            )
        assert decisions == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_10_route_rejects_executor_result_that_disagrees_with_fixed_path(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-10-route-mismatched-result.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1_400))
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-10.route-mismatch")
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("run-10.route-mismatch.register"),
    )
    request = registered.requests[0].request
    principal = human_principal(
        actor_id="principal.route-mismatch",
        roles=frozenset({"approver"}),
        scopes=frozenset({"approvals:decide", "scope.external-write"}),
    )
    clock.current = request.requested_at + timedelta(seconds=1)
    approved = await ApprovalDecisionService(dependencies).decide(
        ApprovalDecisionCommand(
            request_id=request.id,
            expected_generation=request.generation,
            expected_action_hash=request.action_hash,
            decision=ApprovalDecisionKind.APPROVE,
            correlation_id="correlation.route-mismatch.seed",
        ),
        principal=principal,
    )
    executor = PathMismatchedDecisionExecutor(approved)
    application = create_app(
        Settings(_env_file=None),
        identity_provider=StaticIdentityProvider(principal),
        approval_decision_service=executor,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await browser_request(
                client,
                "POST",
                _route(request.id, "reject"),
                csrf_app=application,
                json=_body(request.action_hash),
            )
        assert_problem(response, status_code=409, code="approval_conflict")
        assert len(executor.calls) == 1
        assert executor.calls[0].decision is ApprovalDecisionKind.REJECT
    finally:
        await runtime.dispose()
