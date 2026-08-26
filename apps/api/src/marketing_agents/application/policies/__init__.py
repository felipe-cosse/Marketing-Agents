"""Bounded application policy services."""

from .action_recovery import (
    StaleActionRecoveryDecision,
    classify_stale_action_recovery,
)
from .approval_authorization import (
    APPROVAL_DECIDE_SCOPE,
    APPROVER_ROLE,
    ApprovalAuthoritySnapshot,
    ApprovalAuthorizationError,
    authorize_approval_decision,
    authorize_approval_principal,
)
from .approval_resource_authorization import (
    APPROVAL_READ_ROLES,
    APPROVAL_READ_SCOPE,
    APPROVAL_REQUEST_ROLE,
    APPROVAL_REQUEST_SCOPE,
    ApprovalResourceAuthorizationError,
    authorize_approval_reader,
    authorize_approval_requester,
)

__all__ = [
    "APPROVAL_DECIDE_SCOPE",
    "APPROVAL_READ_ROLES",
    "APPROVAL_READ_SCOPE",
    "APPROVAL_REQUEST_ROLE",
    "APPROVAL_REQUEST_SCOPE",
    "APPROVER_ROLE",
    "ApprovalAuthoritySnapshot",
    "ApprovalAuthorizationError",
    "ApprovalResourceAuthorizationError",
    "StaleActionRecoveryDecision",
    "authorize_approval_decision",
    "authorize_approval_principal",
    "authorize_approval_reader",
    "authorize_approval_requester",
    "classify_stale_action_recovery",
]
