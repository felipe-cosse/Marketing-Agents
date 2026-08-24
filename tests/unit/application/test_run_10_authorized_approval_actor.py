"""RUN-10: server-issued human authority is mandatory for approval decisions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.approval_authorization import (
    ApprovalAuthorizationError,
    authorize_approval_decision,
)
from marketing_agents.application.ports.identity import (
    AuthenticationEvidence,
    IdentityAuthenticationError,
)
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionService,
    ApprovalDecisionServiceError,
)
from marketing_agents.config import Settings
from marketing_agents.domain.enums import ApprovalDecisionKind
from marketing_agents.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    PrincipalKind,
)
from marketing_agents.infrastructure.adapters.identity import LocalIdentityProvider
from pydantic import SecretStr, TypeAdapter, ValidationError

from tests.support.identity import human_principal, service_principal
from tests.unit.domain.test_run_08_approval_records import _request


def _full_principal(*, actor_id: str = "principal.test.approver") -> AuthenticatedPrincipal:
    return human_principal(
        actor_id=actor_id,
        roles=frozenset({"approver", "role.approver", "local_admin"}),
        scopes=frozenset({"approvals:decide", "scope.external-write", "approvals:read"}),
    )


def _command() -> ApprovalDecisionCommand:
    request = _request()
    return ApprovalDecisionCommand(
        request_id=request.id,
        expected_generation=request.generation,
        expected_action_hash=request.action_hash,
        decision=ApprovalDecisionKind.APPROVE,
        correlation_id="correlation.run-10.unit",
    )


def test_run_10_principal_is_adapter_issued_immutable_and_secret_evidence_is_hidden() -> None:
    with pytest.raises(ValueError, match="identity adapter"):
        AuthenticatedPrincipal(
            actor_id="principal.forged",
            kind=PrincipalKind.HUMAN,
            authentication_method=AuthenticationMethod.BEARER,
            roles=frozenset({"approver"}),
            scopes=frozenset({"approvals:decide"}),
            _seal=object(),
        )
    principal = _full_principal()
    object.__setattr__(principal, "roles", frozenset({"approver"}))
    with pytest.raises(ValueError, match="changed after"):
        principal.verify_integrity()

    canary = "bearer-secret-canary"
    evidence = AuthenticationEvidence(bearer_token=SecretStr(canary))
    assert canary not in repr(evidence)
    assert canary not in str(evidence.safe_snapshot())
    assert canary.encode() not in TypeAdapter(AuthenticationEvidence).dump_json(evidence)


def test_run_10_principal_issuer_stays_behind_the_infrastructure_boundary() -> None:
    source_root = Path(__file__).resolve().parents[3] / "apps/api/src/marketing_agents"
    issuer_mentions = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if "_issue_authenticated_principal" in path.read_text(encoding="utf-8")
    }
    assert issuer_mentions == {
        "domain/identity.py",
        "infrastructure/adapters/identity.py",
    }


def test_run_10_authority_requires_human_and_all_baseline_and_policy_grants() -> None:
    request = _request()
    cases = (
        (
            service_principal(
                roles=frozenset({"approver", "role.approver"}),
                scopes=frozenset({"approvals:decide", "scope.external-write"}),
            ),
            "human_approval_required",
        ),
        (
            human_principal(
                roles=frozenset({"operator", "role.approver"}),
                scopes=frozenset({"approvals:decide", "scope.external-write"}),
            ),
            "approval_role_missing",
        ),
        (
            human_principal(
                roles=frozenset({"local_admin"}),
                scopes=frozenset({"approvals:decide", "scope.external-write", "approvals:read"}),
            ),
            "approval_role_missing",
        ),
        (
            human_principal(
                roles=frozenset({"approver"}),
                scopes=frozenset({"approvals:decide", "scope.external-write"}),
            ),
            "approval_role_missing",
        ),
        (
            human_principal(
                roles=frozenset({"approver", "role.approver"}),
                scopes=frozenset({"approvals:decide"}),
            ),
            "approval_scope_missing",
        ),
        (
            _full_principal(actor_id=request.requested_by),
            "self_approval_forbidden",
        ),
    )
    for principal, code in cases:
        with pytest.raises(ApprovalAuthorizationError) as captured:
            authorize_approval_decision(principal, request)
        assert captured.value.code == code

    authority = authorize_approval_decision(_full_principal(), request)
    assert authority.matched_roles == frozenset({"approver", "role.approver"})
    assert authority.matched_scopes == frozenset({"approvals:decide", "scope.external-write"})
    assert "local_admin" not in authority.matched_roles
    assert "approvals:read" not in authority.matched_scopes

    self_allowed = replace(
        request,
        policy=replace(request.policy, allow_self_approval=True),
    )
    self_authority = authorize_approval_decision(
        _full_principal(actor_id=request.requested_by),
        self_allowed,
    )
    assert self_authority.actor_id == request.requested_by


@pytest.mark.asyncio
async def test_run_10_baseline_denial_happens_before_clock_id_or_repository_access() -> None:
    class ExplodingClock:
        def now(self):  # type: ignore[no-untyped-def]
            raise AssertionError("clock must not run")

    class ExplodingIds:
        def new(self, namespace: str) -> str:
            raise AssertionError(f"ID generator must not run: {namespace}")

    class ExplodingUnitOfWork:
        def __call__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("repository must not run")

    service = ApprovalDecisionService(
        OrchestrationDependencies(ExplodingClock(), ExplodingIds(), ExplodingUnitOfWork())
    )
    command = _command()
    with pytest.raises(ApprovalDecisionServiceError) as captured:
        await service.decide(
            command,
            principal=human_principal(
                roles=frozenset({"operator"}),
                scopes=frozenset({"approvals:read"}),
            ),
        )
    assert captured.value.code == "approval_role_missing"


@pytest.mark.asyncio
async def test_run_10_local_provider_snapshots_settings_and_refuses_bearer() -> None:
    settings = Settings(
        _env_file=None,
        local_identity_actor_id="local-operator",
        local_identity_roles=("viewer", "operator", "approver", "local_admin"),
        local_identity_scopes=(
            "approvals:read",
            "approvals:decide",
            "scope.external-write",
        ),
    )
    provider = LocalIdentityProvider(settings)
    principal = await provider.authenticate(AuthenticationEvidence())
    assert principal.actor_id == "local-operator"
    assert principal.authentication_method is AuthenticationMethod.LOCAL_FIXED
    assert principal.roles == frozenset(settings.local_identity_roles)
    assert principal.scopes == frozenset(settings.local_identity_scopes)
    with pytest.raises(IdentityAuthenticationError) as captured:
        await provider.authenticate(AuthenticationEvidence(bearer_token=SecretStr("opaque")))
    assert captured.value.code == "local_bearer_forbidden"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, local_identity_roles=("approver", "approver"))


def test_run_10_command_has_no_authority_fields_and_bounds_untrusted_reason() -> None:
    request = _request()
    command = ApprovalDecisionCommand(
        request_id=request.id,
        expected_generation=1,
        expected_action_hash=request.action_hash,
        decision=ApprovalDecisionKind.APPROVE,
        reason="operator reviewed the safe projection",
        correlation_id="correlation.run-10.reason",
    )
    representation = repr(command)
    assert "operator reviewed" not in representation
    for forbidden in (
        "actor_id",
        "authentication_method",
        "roles",
        "scopes",
        "action_version",
        "decision_id",
        "decided_at",
    ):
        assert forbidden not in command.__dataclass_fields__
    with pytest.raises(ValueError, match="bounded"):
        replace(command, reason="x" * 501)
