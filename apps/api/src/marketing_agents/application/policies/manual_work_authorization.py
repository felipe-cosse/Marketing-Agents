"""Defense-in-depth authorization for human-triggered manual work."""

from __future__ import annotations

from marketing_agents.domain.identity import AuthenticatedPrincipal, PrincipalKind

MANUAL_WORK_OPERATOR_ROLE = "operator"


class ManualWorkAuthorizationError(PermissionError):
    """An authenticated principal cannot submit manual work."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def authorize_manual_work_operator(principal: AuthenticatedPrincipal) -> None:
    """Require one intact adapter-issued human operator before any state lookup."""

    if type(principal) is not AuthenticatedPrincipal:
        raise ManualWorkAuthorizationError(
            "manual_work_human_required",
            "manual work requires a server-authenticated human",
        )
    try:
        principal.verify_integrity()
    except (TypeError, ValueError):
        raise ManualWorkAuthorizationError(
            "manual_work_human_required",
            "manual work requires a server-authenticated human",
        ) from None
    if principal.kind is not PrincipalKind.HUMAN:
        raise ManualWorkAuthorizationError(
            "manual_work_human_required",
            "service principals cannot submit manual work",
        )
    if MANUAL_WORK_OPERATOR_ROLE not in principal.roles:
        raise ManualWorkAuthorizationError(
            "manual_work_operator_role_missing",
            "the authenticated principal lacks the operator role",
        )
