"""Trusted test-only identity adapter fixtures for RUN-10 matrices."""

from __future__ import annotations

from marketing_agents.application.ports.identity import AuthenticationEvidence
from marketing_agents.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    PrincipalKind,
    _issue_authenticated_principal,
)


def human_principal(
    *,
    actor_id: str = "principal.test.approver",
    roles: frozenset[str] = frozenset({"approver"}),
    scopes: frozenset[str] = frozenset({"approvals:decide"}),
    method: AuthenticationMethod = AuthenticationMethod.BEARER,
) -> AuthenticatedPrincipal:
    return _issue_authenticated_principal(
        actor_id=actor_id,
        kind=PrincipalKind.HUMAN,
        authentication_method=method,
        roles=roles,
        scopes=scopes,
    )


def service_principal(
    *,
    actor_id: str = "principal.test.service",
    roles: frozenset[str] = frozenset({"approver"}),
    scopes: frozenset[str] = frozenset({"approvals:decide"}),
) -> AuthenticatedPrincipal:
    return _issue_authenticated_principal(
        actor_id=actor_id,
        kind=PrincipalKind.SERVICE,
        authentication_method=AuthenticationMethod.INTERNAL_SCHEDULER,
        roles=roles,
        scopes=scopes,
    )


class StaticIdentityProvider:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal
        self.evidence: list[AuthenticationEvidence] = []

    async def authenticate(
        self,
        evidence: AuthenticationEvidence,
    ) -> AuthenticatedPrincipal:
        self.evidence.append(evidence)
        return self.principal


class FalseyStaticIdentityProvider(StaticIdentityProvider):
    def __bool__(self) -> bool:
        return False
