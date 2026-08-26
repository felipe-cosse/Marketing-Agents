"""Defense-in-depth authorization for deployment-configuration mutations."""

from __future__ import annotations

from marketing_agents.domain.identity import AuthenticatedPrincipal, PrincipalKind

INSTANCE_CONFIGURATION_ADMIN_ROLE = "local_admin"


class InstanceConfigurationAuthorizationError(PermissionError):
    """A principal is not an intact local deployment administrator."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def authorize_instance_configuration_admin(principal: AuthenticatedPrincipal) -> None:
    """Require an adapter-issued human local administrator before any state lookup."""

    if type(principal) is not AuthenticatedPrincipal:
        raise InstanceConfigurationAuthorizationError(
            "configuration_human_required",
            "instance configuration requires a server-authenticated human",
        )
    try:
        principal.verify_integrity()
    except (TypeError, ValueError):
        raise InstanceConfigurationAuthorizationError(
            "configuration_human_required",
            "instance configuration requires a server-authenticated human",
        ) from None
    if principal.kind is not PrincipalKind.HUMAN:
        raise InstanceConfigurationAuthorizationError(
            "configuration_human_required",
            "service principals cannot change instance configuration",
        )
    if INSTANCE_CONFIGURATION_ADMIN_ROLE not in principal.roles:
        raise InstanceConfigurationAuthorizationError(
            "configuration_admin_role_missing",
            "the authenticated principal lacks the local administrator role",
        )
