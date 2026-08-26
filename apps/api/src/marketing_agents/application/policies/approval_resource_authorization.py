"""Authorization for reading and explicitly requesting approval resources."""

from __future__ import annotations

from marketing_agents.domain.identity import AuthenticatedPrincipal, PrincipalKind

APPROVAL_READ_ROLES = frozenset({"viewer", "operator", "approver", "local_admin"})
APPROVAL_READ_SCOPE = "approvals:read"
APPROVAL_REQUEST_ROLE = "operator"
APPROVAL_REQUEST_SCOPE = "approvals:request"


class ApprovalResourceAuthorizationError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_human(principal: AuthenticatedPrincipal) -> None:
    if type(principal) is not AuthenticatedPrincipal:
        raise ApprovalResourceAuthorizationError(
            "approval_human_required",
            "approval resources require a server-authenticated human",
        )
    try:
        principal.verify_integrity()
    except (TypeError, ValueError):
        raise ApprovalResourceAuthorizationError(
            "approval_human_required",
            "approval resources require a server-authenticated human",
        ) from None
    if principal.kind is not PrincipalKind.HUMAN:
        raise ApprovalResourceAuthorizationError(
            "approval_human_required",
            "service principals cannot use the human approval control plane",
        )


def authorize_approval_reader(principal: AuthenticatedPrincipal) -> None:
    """Require an interactive read role and the dedicated approval-read scope."""

    _require_human(principal)
    if principal.roles.isdisjoint(APPROVAL_READ_ROLES):
        raise ApprovalResourceAuthorizationError(
            "approval_read_role_missing",
            "the authenticated principal lacks an approval-read role",
        )
    if APPROVAL_READ_SCOPE not in principal.scopes:
        raise ApprovalResourceAuthorizationError(
            "approval_read_scope_missing",
            "the authenticated principal cannot read approval resources",
        )


def authorize_approval_requester(principal: AuthenticatedPrincipal) -> None:
    """Require explicit operator authority for first or replacement requests."""

    authorize_approval_reader(principal)
    if APPROVAL_REQUEST_ROLE not in principal.roles:
        raise ApprovalResourceAuthorizationError(
            "approval_request_role_missing",
            "the authenticated principal lacks the approval-request role",
        )
    if APPROVAL_REQUEST_SCOPE not in principal.scopes:
        raise ApprovalResourceAuthorizationError(
            "approval_request_scope_missing",
            "the authenticated principal cannot request approval resources",
        )


__all__ = [
    "APPROVAL_READ_ROLES",
    "APPROVAL_READ_SCOPE",
    "APPROVAL_REQUEST_ROLE",
    "APPROVAL_REQUEST_SCOPE",
    "ApprovalResourceAuthorizationError",
    "authorize_approval_reader",
    "authorize_approval_requester",
]
