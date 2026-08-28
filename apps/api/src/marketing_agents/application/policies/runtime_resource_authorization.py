"""Defense-in-depth authorization for local runtime inspection resources."""

from __future__ import annotations

from marketing_agents.domain.identity import AuthenticatedPrincipal, PrincipalKind

RUNTIME_RESOURCE_READ_ROLES = frozenset({"viewer", "operator", "approver", "local_admin"})
RUNTIME_RESOURCE_INSTALLATION_SCOPE = "single-local-installation"


class RuntimeResourceAuthorizationError(PermissionError):
    """Stable non-sensitive denial raised by runtime query services."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def authorize_runtime_resource_reader(principal: AuthenticatedPrincipal) -> None:
    """Authorize one server-issued human for installation-wide runtime reads.

    The local deployment has no tenant boundary or tenant claim. Authorization is
    consequently bound to the authenticated human's role, not to request-provided
    ownership or tenant identifiers.
    """

    if type(principal) is not AuthenticatedPrincipal:
        raise RuntimeResourceAuthorizationError(
            "runtime_human_required",
            "runtime resources require a server-authenticated human",
        )
    try:
        principal.verify_integrity()
    except (TypeError, ValueError):
        raise RuntimeResourceAuthorizationError(
            "runtime_human_required",
            "runtime resources require a server-authenticated human",
        ) from None
    if principal.kind is not PrincipalKind.HUMAN:
        raise RuntimeResourceAuthorizationError(
            "runtime_human_required",
            "service principals cannot inspect human runtime resources",
        )
    if principal.roles.isdisjoint(RUNTIME_RESOURCE_READ_ROLES):
        raise RuntimeResourceAuthorizationError(
            "runtime_read_role_missing",
            "the authenticated principal lacks a runtime-read role",
        )


__all__ = [
    "RUNTIME_RESOURCE_INSTALLATION_SCOPE",
    "RUNTIME_RESOURCE_READ_ROLES",
    "RuntimeResourceAuthorizationError",
    "authorize_runtime_resource_reader",
]
