"""Approval invalidation and immutable replacement-generation semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from marketing_agents.domain.action_hash import CanonicalExternalAction, canonical_action_hash
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalBindingError,
    ProposedExternalAction,
    assert_request_binds_action,
    request_approval,
)
from marketing_agents.domain.entities._validation import require_digest, require_id, require_utc


class ApprovalIntegrityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SupersededApprovalRequest:
    request: ActionApprovalRequest
    superseded_at: datetime
    replacement_request_id: str
    replacement_action_hash: str
    replacement_generation: int

    def __post_init__(self) -> None:
        require_utc(self.superseded_at, "approval supersession time")
        require_id(self.replacement_request_id, "replacement approval request ID")
        require_digest(self.replacement_action_hash, "replacement action hash")
        if self.replacement_generation != self.request.generation + 1:
            raise ValueError("replacement generation must immediately follow the old request")

    @property
    def authorizable(self) -> bool:
        return False

    def reject_authorization(self) -> None:
        raise ApprovalIntegrityError(
            "approval_superseded", "a superseded approval generation cannot be reactivated"
        )


@dataclass(frozen=True, slots=True)
class ApprovalReplacement:
    superseded: SupersededApprovalRequest
    replacement: ActionApprovalRequest


def validate_current_action(
    request: ActionApprovalRequest,
    current_action: CanonicalExternalAction,
    *,
    expected_client_hash: str | None = None,
) -> None:
    if expected_client_hash is not None and expected_client_hash != request.action_hash:
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
) -> ApprovalReplacement:
    require_utc(now, "approval supersession time")
    if expected_client_hash != current_request.action_hash:
        raise ApprovalIntegrityError(
            "expected_hash_mismatch", "client expected hash is not the current approval hash"
        )
    if replacement_action.action_hash == current_request.action_hash:
        raise ApprovalIntegrityError(
            "approval_action_unchanged", "an unchanged action does not need a replacement request"
        )
    envelope = replacement_action.envelope
    if (
        envelope.action_id != current_request.action_id
        or envelope.authorization_set_id != current_request.authorization_set_id
        or envelope.run_id != current_request.run_id
        or envelope.step_id != current_request.step_id
    ):
        raise ApprovalIntegrityError(
            "replacement_scope_mismatch", "replacement must remain in the same logical action scope"
        )
    replacement = request_approval(
        request_id=replacement_request_id,
        proposed_action=replacement_action,
        policy=current_request.policy,
        requested_by=requested_by,
        requested_at=now,
        generation=current_request.generation + 1,
    )
    superseded = SupersededApprovalRequest(
        request=current_request,
        superseded_at=now,
        replacement_request_id=replacement.id,
        replacement_action_hash=canonical_action_hash(replacement_action.envelope),
        replacement_generation=replacement.generation,
    )
    return ApprovalReplacement(superseded=superseded, replacement=replacement)
