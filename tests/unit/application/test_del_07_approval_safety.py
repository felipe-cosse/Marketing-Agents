"""DEL-07: fail-closed approval identity, command, and renewal boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta, timezone

import pytest
from marketing_agents.application.policies.approval_authorization import (
    ApprovalAuthorizationError,
    authorize_approval_decision,
    authorize_approval_principal,
)
from marketing_agents.application.policies.runtime_guard import (
    AttemptContext,
    RuntimePolicyGuard,
    RuntimePolicyViolation,
)
from marketing_agents.application.policies.write_authorization import ApprovalReservation
from marketing_agents.application.services.approval_integrity import (
    ApprovalIntegrityError,
    invalidate_and_replace,
    renew_expired_request,
)
from marketing_agents.domain.approval import StoredActionApprovalRequest
from marketing_agents.domain.enums import ApprovalStatus

from tests.security.test_safe_02_external_write_authorization import (
    _action as _write_action,
)
from tests.security.test_safe_02_external_write_authorization import (
    _reservation,
)
from tests.security.test_safe_06_runtime_policy import _policy
from tests.support.identity import human_principal
from tests.unit.application.test_run_09_approval_invalidation import (
    NOW,
    _action,
    _proposal,
    _request,
)
from tests.unit.application.test_run_10_authorized_approval_actor import (
    _command,
    _full_principal,
)


def test_del_07_approval_rejects_non_principal_and_tampered_sealed_principal() -> None:
    principal = _full_principal()
    object.__setattr__(principal, "actor_id", "principal.changed.after.authentication")
    for candidate in (object(), principal):
        with pytest.raises(ApprovalAuthorizationError) as failure:
            authorize_approval_principal(candidate)  # type: ignore[arg-type]
        assert failure.value.code == "human_approval_required"


def test_del_07_approval_baseline_scope_and_exact_request_are_mandatory() -> None:
    with pytest.raises(ApprovalAuthorizationError) as missing_scope:
        authorize_approval_principal(human_principal(scopes=frozenset()))
    assert missing_scope.value.code == "approval_scope_missing"
    with pytest.raises(ApprovalAuthorizationError) as wrong_request:
        authorize_approval_decision(_full_principal(), object())  # type: ignore[arg-type]
    assert wrong_request.value.code == "human_approval_required"


@pytest.mark.parametrize("generation", (True, 0, -1, 1.0, "1"))
def test_del_07_decision_generation_is_an_exact_positive_integer(generation: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        replace(_command(), expected_generation=generation)  # type: ignore[arg-type]


def test_del_07_decision_string_is_not_the_trusted_enum() -> None:
    with pytest.raises(ValueError, match="exact decision enum"):
        replace(_command(), decision="approve")  # type: ignore[arg-type]


@pytest.mark.parametrize("control", ("\x00", "\n", "\x7f", "\x85", "\ud800"))
def test_del_07_reason_control_characters_fail_before_persistence(control: str) -> None:
    with pytest.raises(ValueError, match="unsupported characters"):
        replace(_command(), reason=f"Reviewed{control}action")


def test_del_07_supersession_rejects_stale_expected_hash_before_semantic_change() -> None:
    with pytest.raises(ApprovalIntegrityError) as failure:
        invalidate_and_replace(
            current_request=_request(),  # type: ignore[arg-type]
            replacement_request_id="approval-request.replacement",
            replacement_action=_proposal(_action(destination="contact:changed")),
            requested_by="principal.local.operator",
            now=NOW,
            expected_client_hash="0" * 64,
        )
    assert failure.value.code == "expected_hash_mismatch"


@pytest.mark.parametrize(
    ("change", "expected_code"),
    (
        ("hash", "expected_hash_mismatch"),
        ("not_expired", "approval_not_expired"),
        ("closed", "approval_not_renewable"),
        ("already_renewed", "approval_already_renewed"),
        ("action", "full_set_epoch_required"),
        ("projection", "approval_projection_changed"),
        ("missing_expired_at", "approval_state_corrupt"),
    ),
)
def test_del_07_renewal_rejects_conflicts_without_creating_a_replacement(
    change: str, expected_code: str
) -> None:
    request = _request()
    current = StoredActionApprovalRequest.created(request)  # type: ignore[arg-type]
    proposal = _proposal(_action())
    now = request.expires_at  # type: ignore[attr-defined]
    expected_hash = request.action_hash  # type: ignore[attr-defined]
    if change == "hash":
        expected_hash = "0" * 64
    elif change == "not_expired":
        now -= timedelta(microseconds=1)
    elif change == "closed":
        current = replace(
            current,
            status=ApprovalStatus.SUPERSEDED,
            version=2,
            updated_at=now,
            superseded_at=now,
            superseded_reason_code="approval_set_rejected",
        )
    elif change == "already_renewed":
        current = replace(
            current,
            status=ApprovalStatus.EXPIRED,
            version=3,
            updated_at=now,
            expired_at=now,
            replacement_request_id="approval-request.existing-replacement",
            renewed_at=now,
        )
    elif change == "action":
        proposal = _proposal(_action(destination="contact:changed"))
    elif change == "projection":
        # A separately valid safe projection must not replace what the reviewer saw.
        projection = dict(proposal.redacted_projection)
        projection["destination"] = "another configured contact"
        proposal = replace(proposal, redacted_projection=projection)
    else:
        current = replace(
            current,
            status=ApprovalStatus.EXPIRED,
            version=2,
            updated_at=now,
            expired_at=now,
        )
        # Exercise the defensive check if a corrupt repository bypasses hydration.
        object.__setattr__(current, "expired_at", None)
    with pytest.raises(ApprovalIntegrityError) as failure:
        renew_expired_request(
            current=current,
            replacement_request_id="approval-request.replacement",
            exact_action=proposal,
            now=now,
            expected_client_hash=expected_hash,
        )
    assert failure.value.code == expected_code
    assert current.replacement_request_id is None or change == "already_renewed"


def test_del_07_runtime_snapshot_rejects_duplicate_capabilities_and_inverted_timeouts() -> None:
    policy = _policy()
    with pytest.raises(ValueError, match="unique"):
        _policy(allowed_capabilities=policy.allowed_capabilities * 2)
    with pytest.raises(ValueError, match="cannot exceed"):
        _policy(step_timeout_seconds=61)


def test_del_07_input_field_limit_is_independent_of_total_payload_and_output_limits() -> None:
    guard = RuntimePolicyGuard(_policy(max_input_field_bytes=2))
    guard.validate_input({"a": "é"}, {})
    with pytest.raises(RuntimePolicyViolation) as failure:
        guard.validate_input({"a": "éx"}, {})
    assert failure.value.code == "input_field_too_large"
    assert failure.value.pointer == "/input"
    guard.validate_output({"a": "éx"}, {})


def test_del_07_expired_request_renewal_preserves_expiry_time_and_increments_once() -> None:
    request = _request()
    expired_at = request.expires_at  # type: ignore[attr-defined]
    current = replace(
        StoredActionApprovalRequest.created(request),  # type: ignore[arg-type]
        status=ApprovalStatus.EXPIRED,
        version=2,
        updated_at=expired_at,
        expired_at=expired_at,
    )
    renewed_at = expired_at + timedelta(seconds=1)
    renewal = renew_expired_request(
        current=current,
        replacement_request_id="approval-request.renewed",
        exact_action=_proposal(_action()),
        now=renewed_at,
        expected_client_hash=request.action_hash,  # type: ignore[attr-defined]
        requested_by="principal.renewing.operator",
    )
    assert renewal.expired.version == 3
    assert renewal.expired.expired_at == expired_at
    assert renewal.expired.renewed_at == renewed_at
    assert renewal.replacement.requested_by == "principal.renewing.operator"
    assert renewal.replacement.action_hash == request.action_hash  # type: ignore[attr-defined]


@pytest.mark.parametrize("field", ("now", "deadline"))
def test_del_07_attempt_clock_and_deadline_must_both_use_utc(field: str) -> None:
    arguments = {"now": NOW, "deadline": NOW + timedelta(seconds=10)}
    arguments[field] = arguments[field].astimezone(timezone(timedelta(hours=1)))
    with pytest.raises(ValueError, match=f"{field} must be UTC"):
        AttemptContext(**arguments, requested_timeout_seconds=1)


def test_del_07_write_reservation_requires_utc_and_distinct_approval_fact_ids() -> None:
    values = _reservation(_write_action()).model_dump()
    with pytest.raises(ValueError, match="reservation time must be UTC"):
        ApprovalReservation.model_validate(
            {**values, "reserved_at": NOW.astimezone(timezone(timedelta(hours=1)))}
        )
    with pytest.raises(ValueError, match="request and decision IDs must be distinct"):
        ApprovalReservation.model_validate(
            {**values, "approval_decision_id": values["approval_request_id"]}
        )
