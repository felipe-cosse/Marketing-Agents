"""Defense-in-depth authorization for static catalog reads."""

from __future__ import annotations

from marketing_agents.domain.identity import AuthenticatedPrincipal, PrincipalKind

CATALOG_READ_ROLES = frozenset({"viewer", "operator", "approver", "local_admin"})


class CatalogAuthorizationError(PermissionError):
    """Raised when a principal cannot read the human control-plane catalog."""


def authorize_catalog_reader(principal: AuthenticatedPrincipal) -> None:
    """Require an intact human principal with an interactive read-capable role."""

    if type(principal) is not AuthenticatedPrincipal:
        raise CatalogAuthorizationError("catalog reads require an authenticated human")
    try:
        principal.verify_integrity()
    except (TypeError, ValueError):
        raise CatalogAuthorizationError("catalog reads require an authenticated human") from None
    if principal.kind is not PrincipalKind.HUMAN:
        raise CatalogAuthorizationError("service principals cannot browse the catalog")
    if CATALOG_READ_ROLES.isdisjoint(principal.roles):
        raise CatalogAuthorizationError("the principal lacks catalog read permission")
