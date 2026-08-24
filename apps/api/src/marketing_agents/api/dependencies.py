"""FastAPI identity and narrow approval-decision dependency seams."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, HTTPException, Request, status
from pydantic import SecretStr

from marketing_agents.application.policies.approval_authorization import (
    ApprovalAuthorizationError,
    authorize_approval_principal,
)
from marketing_agents.application.ports.identity import (
    AuthenticationEvidence,
    IdentityAuthenticationError,
    IdentityProvider,
)
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    AuthorizedApprovalDecision,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal

_FORBIDDEN_IDENTITY_PREFIXES = (
    "x-actor",
    "x-user",
    "x-role",
    "x-scope",
    "x-principal",
    "x-auth-",
)
_FORBIDDEN_IDENTITY_HEADERS = frozenset(
    {
        "remote-user",
        "x-forwarded-user",
        "x-forwarded-email",
        "x-forwarded-actor",
        "x-forwarded-role",
        "x-forwarded-roles",
        "x-forwarded-scope",
        "x-forwarded-scopes",
    }
)
_MAX_BEARER_LENGTH = 4_096


class ApprovalDecisionExecutor(Protocol):
    async def decide(
        self,
        command: ApprovalDecisionCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> AuthorizedApprovalDecision: ...


def get_identity_provider(request: Request) -> IdentityProvider:
    provider = getattr(request.app.state, "identity_provider", None)
    if provider is None or not callable(getattr(provider, "authenticate", None)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )
    return cast(IdentityProvider, provider)


def get_approval_decision_executor(request: Request) -> ApprovalDecisionExecutor:
    executor = getattr(request.app.state, "approval_decision_service", None)
    if executor is None or not callable(getattr(executor, "decide", None)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="approval service unavailable",
        )
    return cast(ApprovalDecisionExecutor, executor)


def authentication_evidence(request: Request) -> AuthenticationEvidence:
    """Parse only standard Authorization and reject caller-supplied identity assertions."""

    for header_name in request.headers:
        normalized = header_name.casefold()
        if normalized in _FORBIDDEN_IDENTITY_HEADERS or normalized.startswith(
            _FORBIDDEN_IDENTITY_PREFIXES
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="caller identity assertions are forbidden",
            )
    authorization_values = request.headers.getlist("authorization")
    if not authorization_values:
        return AuthenticationEvidence()
    if len(authorization_values) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="authorization header is malformed",
        )
    raw = authorization_values[0]
    parts = raw.split(" ")
    if (
        raw != raw.strip()
        or len(parts) != 2
        or parts[0].casefold() != "bearer"
        or not parts[1]
        or len(parts[1]) > _MAX_BEARER_LENGTH
        or any(character.isspace() or ord(character) < 33 for character in parts[1])
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="authorization header is malformed",
        )
    return AuthenticationEvidence(bearer_token=SecretStr(parts[1]))


async def require_authenticated_principal(
    provider: Annotated[IdentityProvider, Depends(get_identity_provider)],
    evidence: Annotated[AuthenticationEvidence, Depends(authentication_evidence)],
) -> AuthenticatedPrincipal:
    try:
        principal = await provider.authenticate(evidence)
    except IdentityAuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        ) from None
    if type(principal) is not AuthenticatedPrincipal:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )
    try:
        principal.verify_integrity()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        ) from None
    return principal


async def require_approval_principal(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_authenticated_principal),
    ],
) -> AuthenticatedPrincipal:
    """Apply the non-enumerating approval baseline at the transport boundary too."""

    try:
        authorize_approval_principal(principal)
    except ApprovalAuthorizationError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="approval decision is forbidden",
        ) from None
    return principal
