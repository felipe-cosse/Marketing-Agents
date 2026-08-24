"""Defense-in-depth authorization for one human approval decision."""

from __future__ import annotations

from dataclasses import dataclass, field

from marketing_agents.domain.approval import ActionApprovalRequest
from marketing_agents.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    PrincipalKind,
)

APPROVER_ROLE = "approver"
APPROVAL_DECIDE_SCOPE = "approvals:decide"


class ApprovalAuthorizationError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApprovalAuthoritySnapshot:
    """Minimum grants retained by the append-only decision fact."""

    actor_id: str
    authentication_method: AuthenticationMethod
    matched_roles: frozenset[str] = field(repr=False)
    matched_scopes: frozenset[str] = field(repr=False)


def authorize_approval_principal(principal: AuthenticatedPrincipal) -> None:
    """Apply baseline human-approver permission before any request lookup."""

    if type(principal) is not AuthenticatedPrincipal:
        raise ApprovalAuthorizationError(
            "human_approval_required",
            "approval decisions require a server-authenticated human",
        )
    try:
        principal.verify_integrity()
    except (TypeError, ValueError):
        raise ApprovalAuthorizationError(
            "human_approval_required",
            "approval decisions require a server-authenticated human",
        ) from None
    if principal.kind is not PrincipalKind.HUMAN:
        raise ApprovalAuthorizationError(
            "human_approval_required",
            "service principals cannot grant human approval",
        )
    if APPROVER_ROLE not in principal.roles:
        raise ApprovalAuthorizationError(
            "approval_role_missing",
            "the authenticated principal lacks the approver role",
        )
    if APPROVAL_DECIDE_SCOPE not in principal.scopes:
        raise ApprovalAuthorizationError(
            "approval_scope_missing",
            "the authenticated principal cannot decide approvals",
        )


def authorize_approval_decision(
    principal: AuthenticatedPrincipal,
    request: ActionApprovalRequest,
) -> ApprovalAuthoritySnapshot:
    """Require a human plus every baseline/policy grant and the self-decision rule."""

    authorize_approval_principal(principal)
    if type(request) is not ActionApprovalRequest:
        raise ApprovalAuthorizationError(
            "human_approval_required",
            "approval decisions require an exact persisted request",
        )
    required_roles = request.policy.required_roles | frozenset({APPROVER_ROLE})
    required_scopes = request.policy.required_scopes | frozenset({APPROVAL_DECIDE_SCOPE})
    if not required_roles.issubset(principal.roles):
        raise ApprovalAuthorizationError(
            "approval_role_missing",
            "the authenticated principal lacks a required approval role",
        )
    if not required_scopes.issubset(principal.scopes):
        raise ApprovalAuthorizationError(
            "approval_scope_missing",
            "the authenticated principal lacks a required approval scope",
        )
    if not request.policy.allow_self_approval and principal.actor_id == request.requested_by:
        raise ApprovalAuthorizationError(
            "self_approval_forbidden",
            "the approval policy forbids requester self-approval",
        )
    return ApprovalAuthoritySnapshot(
        actor_id=principal.actor_id,
        authentication_method=principal.authentication_method,
        matched_roles=required_roles,
        matched_scopes=required_scopes,
    )
