"""Exact-action validation and expiry-only request renewal semantics."""

from __future__ import annotations

import hmac
from datetime import datetime

from marketing_agents.domain.action_hash import CanonicalExternalAction
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalBindingError,
    ApprovalRenewal,
    ProposedExternalAction,
    StoredActionApprovalRequest,
    assert_request_binds_action,
    request_approval,
)
from marketing_agents.domain.enums import ApprovalStatus
from marketing_agents.domain.validation import require_utc


class ApprovalIntegrityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_current_action(
    request: ActionApprovalRequest,
    current_action: CanonicalExternalAction,
    *,
    expected_client_hash: str | None = None,
) -> None:
    if expected_client_hash is not None and not hmac.compare_digest(
        expected_client_hash,
        request.action_hash,
    ):
        raise ApprovalIntegrityError(
            "expected_hash_mismatch", "client expected hash is not the current approval hash"
        )
    try:
        assert_request_binds_action(request, current_action)
    except ApprovalBindingError as exc:
        raise ApprovalIntegrityError(
            "approval_invalidated", "the proposed action changed after approval was requested"
        ) from exc


def invalidate_and_replace(
    *,
    current_request: ActionApprovalRequest,
    replacement_request_id: str,
    replacement_action: ProposedExternalAction,
    requested_by: str,
    now: datetime,
    expected_client_hash: str,
) -> None:
    require_utc(now, "approval supersession time")
    if expected_client_hash != current_request.action_hash:
        raise ApprovalIntegrityError(
            "expected_hash_mismatch", "client expected hash is not the current approval hash"
        )
    if replacement_action.action_hash == current_request.action_hash:
        raise ApprovalIntegrityError(
            "approval_action_unchanged", "an unchanged action does not need a replacement request"
        )
    del replacement_request_id, requested_by
    raise ApprovalIntegrityError(
        "full_set_epoch_required",
        "semantic action changes require a wholly new authorization-set epoch",
    )


def renew_expired_request(
    *,
    current: StoredActionApprovalRequest,
    replacement_request_id: str,
    exact_action: ProposedExternalAction,
    now: datetime,
    expected_client_hash: str,
) -> ApprovalRenewal:
    """Replace only an expired request leaf for the unchanged action/set epoch."""

    require_utc(now, "approval renewal time")
    request = current.request
    if expected_client_hash != request.action_hash:
        raise ApprovalIntegrityError(
            "expected_hash_mismatch", "client expected hash is not the current approval hash"
        )
    if current.status not in {
        ApprovalStatus.PENDING,
        ApprovalStatus.APPROVED,
        ApprovalStatus.EXPIRED,
    }:
        raise ApprovalIntegrityError(
            "approval_not_renewable", "only pending or approved expiry may be renewed"
        )
    if now < request.expires_at:
        raise ApprovalIntegrityError("approval_not_expired", "approval is not yet expired")
    if current.replacement_request_id is not None:
        raise ApprovalIntegrityError(
            "approval_already_renewed", "expired approval already has a replacement"
        )
    try:
        assert_request_binds_action(request, exact_action.envelope)
    except ApprovalBindingError as exc:
        raise ApprovalIntegrityError(
            "full_set_epoch_required",
            "semantic action changes require a wholly new authorization-set epoch",
        ) from exc
    if exact_action.redacted_projection != request.redacted_projection:
        raise ApprovalIntegrityError(
            "approval_projection_changed",
            "expiry renewal must retain the original safe projection",
        )
    replacement = request_approval(
        request_id=replacement_request_id,
        proposed_action=exact_action,
        policy=request.policy,
        requested_by=request.requested_by,
        requested_at=now,
        generation=request.generation + 1,
    )
    if current.status is ApprovalStatus.EXPIRED:
        expired_at = current.expired_at
        if expired_at is None:
            raise ApprovalIntegrityError(
                "approval_state_corrupt", "expired approval is missing its expiration time"
            )
        version = current.version + 1
    else:
        expired_at = now
        # Expiration and renewal linkage are two lifecycle facts even when
        # persisted atomically in one outer transaction.
        version = current.version + 2
    expired = StoredActionApprovalRequest(
        request=request,
        status=ApprovalStatus.EXPIRED,
        version=version,
        updated_at=now,
        decision=current.decision,
        expired_at=expired_at,
        replacement_request_id=replacement.id,
        renewed_at=now,
    )
    return ApprovalRenewal(expired=expired, replacement=replacement)
