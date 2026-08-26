"""FastAPI identity, catalog-query, and approval-decision dependency seams."""

from __future__ import annotations

import inspect
from typing import Annotated, Protocol, cast

from fastapi import Depends, HTTPException, Request, status
from pydantic import SecretStr

from marketing_agents.api.catalog_queries import CatalogQueryExecutor
from marketing_agents.application.policies.approval_authorization import (
    ApprovalAuthorizationError,
    authorize_approval_principal,
)
from marketing_agents.application.policies.catalog_authorization import (
    CatalogAuthorizationError,
    authorize_catalog_reader,
)
from marketing_agents.application.policies.instance_configuration_authorization import (
    InstanceConfigurationAuthorizationError,
    authorize_instance_configuration_admin,
)
from marketing_agents.application.ports.identity import (
    AuthenticationEvidence,
    IdentityAuthenticationError,
    IdentityProvider,
)
from marketing_agents.application.ports.readiness import ReadinessProbe
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    AuthorizedApprovalDecision,
)
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationSchema,
    InstanceConfigurationSnapshot,
    InstanceConfigurationUpdateResult,
    UpdateInstanceConfigurationCommand,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.instance_configuration import InstanceConfiguration

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


class InstanceConfigurationExecutor(Protocol):
    async def read(
        self,
        instance_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfiguration: ...

    async def read_all(
        self,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationSnapshot: ...

    async def schema(
        self,
        instance_id: str,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationSchema: ...

    async def update(
        self,
        command: UpdateInstanceConfigurationCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> InstanceConfigurationUpdateResult: ...


def get_readiness_probe(request: Request) -> ReadinessProbe | None:
    """Resolve the optional probe without trusting falsey or malformed objects."""

    try:
        probe = getattr(request.app.state, "readiness_probe", None)
        check = getattr(probe, "check", None)
    except Exception:
        return None
    if probe is None or not callable(check):
        return None
    return cast(ReadinessProbe, probe)


def get_catalog_query_executor(request: Request) -> CatalogQueryExecutor | None:
    """Resolve the optional catalog query seam without trusting truthiness."""

    try:
        executor = getattr(request.app.state, "catalog_query_service", None)
        read = getattr(executor, "read", None)
    except Exception:
        return None
    if executor is None or not callable(read) or not inspect.iscoroutinefunction(read):
        return None
    return cast(CatalogQueryExecutor, executor)


def get_instance_configuration_executor(request: Request) -> InstanceConfigurationExecutor:
    """Resolve only a complete asynchronous configuration application seam."""

    try:
        executor = getattr(request.app.state, "instance_configuration_service", None)
        methods = tuple(
            getattr(executor, name, None) for name in ("read", "read_all", "schema", "update")
        )
    except Exception:
        executor = None
        methods = ()
    if (
        executor is None
        or len(methods) != 4
        or any(
            not callable(method) or not inspect.iscoroutinefunction(method) for method in methods
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="instance configuration service unavailable",
        )
    return cast(InstanceConfigurationExecutor, executor)


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


async def require_catalog_principal(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_authenticated_principal),
    ],
) -> AuthenticatedPrincipal:
    """Apply the authenticated human viewer-equivalent boundary before loading."""

    try:
        authorize_catalog_reader(principal)
    except CatalogAuthorizationError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="catalog read is forbidden",
        ) from None
    return principal


async def require_instance_configuration_admin_principal(
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_authenticated_principal),
    ],
) -> AuthenticatedPrincipal:
    """Apply the local-admin human mutation boundary before service resolution."""

    try:
        authorize_instance_configuration_admin(principal)
    except InstanceConfigurationAuthorizationError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="instance configuration mutation is forbidden",
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
