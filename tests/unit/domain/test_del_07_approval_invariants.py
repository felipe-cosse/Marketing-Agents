"""DEL-07: complete approval-set, projection, lifecycle and renewal contracts."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from marketing_agents.domain.approval import (
    ApprovalBindingError,
    ApprovalRenewal,
    ApprovalUse,
    AuthorizationSet,
    AuthorizationSetHead,
    AuthorizationSetMember,
    AuthorizationSetStatus,
    ProposedExternalAction,
    StoredActionApprovalRequest,
    approval_redaction_schema,
    assert_decision_binds_request,
    assert_use_binds_request,
    authorization_set_membership_hash,
    authorization_set_release_hash,
    expected_approval_projection,
)
from marketing_agents.domain.enums import ApprovalStatus

from tests.unit.domain.test_run_08_approval_records import (
    NOW,
    _action,
    _decision,
    _policy,
    _request,
)


def _member(**changes: Any) -> AuthorizationSetMember:
    return AuthorizationSetMember(
        **{
            **dict(
                authorization_set_id="set.1",
                ordinal=1,
                run_id="run.1",
                plan_hash="a" * 64,
                proposal_revision=1,
                action_id="action.1",
                action_hash="b" * 64,
                step_id="step.1",
                step_key="step-one",
            ),
            **changes,
        }
    )


def _set() -> AuthorizationSet:
    return AuthorizationSet.open(authorization_set_id="set.1", members=(_member(),), opened_at=NOW)


@pytest.mark.parametrize("field", ["ordinal", "proposal_revision"])
@pytest.mark.parametrize("value", [0, True, 1.5])
def test_del_07_set_member_requires_positive_integer_identity(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match="positive"):
        _member(**{field: value})


def test_del_07_set_hashes_bind_membership_release_and_run_version() -> None:
    member = _member()
    original = authorization_set_membership_hash("set.1", (member,))
    assert original == authorization_set_membership_hash("set.1", (member,))
    assert original != authorization_set_membership_hash(
        "set.1", (replace(member, action_hash="c" * 64),)
    )
    arguments = dict(
        authorization_set_id="set.1",
        membership_hash=original,
        released_run_version=4,
        released_at=NOW,
        members=(member.hash_material(),),
    )
    release = authorization_set_release_hash(**arguments)
    assert release != original
    assert release == authorization_set_release_hash(**arguments)
    assert release != authorization_set_release_hash(**{**arguments, "released_run_version": 5})


@pytest.mark.parametrize("members", [(), []])
def test_del_07_membership_hash_and_release_require_nonempty_tuples(members: Any) -> None:
    with pytest.raises(ValueError, match="nonempty immutable tuple"):
        authorization_set_membership_hash("set.1", members)
    with pytest.raises(ValueError, match="every member"):
        authorization_set_release_hash(
            authorization_set_id="set.1",
            membership_hash="a" * 64,
            released_run_version=1,
            released_at=NOW,
            members=members,
        )


@pytest.mark.parametrize("version", [0, True, 1.0])
def test_del_07_release_hash_requires_positive_integer_run_version(version: Any) -> None:
    with pytest.raises(ValueError, match="positive"):
        authorization_set_release_hash(
            authorization_set_id="set.1",
            membership_hash="a" * 64,
            released_run_version=version,
            released_at=NOW,
            members=({},),
        )


def test_del_07_set_cannot_open_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        AuthorizationSet.open(authorization_set_id="set.1", members=(), opened_at=NOW)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"proposal_revision": True}, "proposal revision"),
        ({"members": []}, "nonempty tuple"),
        ({"status": "open"}, "exact enum"),
        ({"version": 0}, "positive"),
        ({"updated_at": NOW - timedelta(seconds=1)}, "before it opens"),
        ({"members": (_member(ordinal=2),)}, "contiguous"),
        ({"members": (_member(), _member(ordinal=2))}, "unique"),
        ({"members": (_member(run_id="run.other"),)}, "exact epoch"),
        ({"membership_hash": "c" * 64}, "hash is not current"),
        ({"superseded_by_set_id": "set.1"}, "supersede itself"),
        ({"release_hash": "d" * 64}, "pristine"),
        ({"status": AuthorizationSetStatus.CANCELLED}, "version two"),
    ],
)
def test_del_07_set_rejects_invalid_epoch_or_creation_facts(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_set(), **changes)


def test_del_07_set_release_requires_complete_barrier_evidence() -> None:
    opened = _set()
    released = replace(
        opened,
        status=AuthorizationSetStatus.RELEASED,
        version=2,
        release_hash="c" * 64,
        released_at=NOW,
        released_run_version=4,
        terminal_reason_code="approval_barrier_satisfied",
    )
    assert released.released_run_version == 4
    for changes in (
        {"release_hash": None},
        {"released_at": None},
        {"released_run_version": 0},
        {"released_run_version": True},
        {"terminal_reason_code": "wrong.reason"},
    ):
        with pytest.raises(ValueError, match="incomplete"):
            replace(released, **changes)


@pytest.mark.parametrize(
    "status, reason",
    [
        (AuthorizationSetStatus.REJECTED, "approval_rejected"),
        (AuthorizationSetStatus.CANCELLED, "operator_cancelled"),
        (AuthorizationSetStatus.SUPERSEDED, "approval_set_superseded"),
    ],
)
def test_del_07_closed_set_has_only_its_own_terminal_evidence(
    status: AuthorizationSetStatus, reason: str
) -> None:
    evidence = (
        dict(superseded_by_set_id="set.2", superseded_at=NOW)
        if status is AuthorizationSetStatus.SUPERSEDED
        else {}
    )
    closed = replace(_set(), status=status, version=2, terminal_reason_code=reason, **evidence)
    assert closed.status is status
    with pytest.raises(ValueError, match="release evidence"):
        replace(closed, release_hash="d" * 64)
    with pytest.raises(ValueError, match="terminal reason"):
        replace(closed, terminal_reason_code="wrong.reason")
    if status is AuthorizationSetStatus.SUPERSEDED:
        with pytest.raises(ValueError, match="replacement evidence"):
            replace(closed, superseded_by_set_id=None)
    else:
        with pytest.raises(ValueError, match="supersession evidence"):
            replace(closed, superseded_by_set_id="set.2")


def test_del_07_set_head_selects_one_exact_epoch() -> None:
    opened = _set()
    head = AuthorizationSetHead(
        opened.run_id,
        opened.id,
        opened.plan_hash,
        opened.proposal_revision,
        opened.membership_hash,
        1,
        NOW,
    )
    head.assert_selects(opened)
    for field in ("proposal_revision", "version"):
        with pytest.raises(ValueError, match="positive"):
            replace(head, **{field: True})
    with pytest.raises(ApprovalBindingError) as rejected:
        replace(head, current_set_id="set.other").assert_selects(opened)
    assert rejected.value.code == "authorization_set_head_mismatch"


def _proposal() -> ProposedExternalAction:
    return ProposedExternalAction.create(
        _action(),
        redacted_destination="configured destination",
        payload_schema=approval_redaction_schema(("/recipient",)),
    )


def test_del_07_nested_redaction_and_sequence_projection_remain_immutable() -> None:
    schema = approval_redaction_schema(("/contact/email", "/contact/phone"))
    assert schema["properties"]["contact"]["properties"]["email"]["x-sensitive"] is True
    proposal = _proposal()
    projected = replace(
        proposal,
        redacted_projection={
            **proposal.redacted_projection,
            "payload": {"items": ["safe", {"count": 1}]},
        },
    )
    assert projected.redacted_projection["payload"]["items"][0] == "safe"
    with pytest.raises(TypeError):
        projected.redacted_projection["payload"]["items"][1]["count"] = 2
    assert (
        expected_approval_projection(_action(), ("/recipient",))["payload"]["recipient"]
        == "[REDACTED]"
    )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"required_roles": {"role.approver"}}, "immutable sets"),
        ({"required_roles": frozenset()}, "roles and scopes"),
        ({"required_scopes": frozenset({1})}, "string identifiers"),
    ],
)
def test_del_07_policy_rejects_unsealed_or_empty_authority(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_policy(), **changes)


@pytest.mark.parametrize("summary", ["", " untrimmed ", "x" * 301])
def test_del_07_proposal_requires_bounded_destination_summary(summary: str) -> None:
    with pytest.raises(ValueError, match="destination"):
        ProposedExternalAction.create(_action(), redacted_destination=summary, payload_schema={})


@pytest.mark.parametrize(
    "changes, code",
    [
        ({"action_hash": "f" * 64}, "proposal_hash_mismatch"),
        ({"redacted_projection": []}, "proposal_projection_shape"),
        ({"redacted_projection": {}}, "proposal_projection_shape"),
    ],
)
def test_del_07_proposal_rejects_bad_hash_or_projection_shape(
    changes: dict[str, Any], code: str
) -> None:
    with pytest.raises(ApprovalBindingError) as rejected:
        replace(_proposal(), **changes)
    assert rejected.value.code == code


@pytest.mark.parametrize(
    "field, value, code",
    [
        ("action_type", "other.action", "proposal_projection_mismatch"),
        ("destination", 7, "proposal_destination_invalid"),
        ("payload", [], "proposal_payload_projection_invalid"),
    ],
)
def test_del_07_proposal_fields_cannot_lie_about_the_action(
    field: str, value: Any, code: str
) -> None:
    proposal = _proposal()
    with pytest.raises(ApprovalBindingError) as rejected:
        replace(proposal, redacted_projection={**proposal.redacted_projection, field: value})
    assert rejected.value.code == code


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"policy": object()}, "exact snapshot"),
        ({"generation": True}, "generation"),
        ({"proposal_revision": 0}, "proposal revision"),
        ({"expires_at": NOW}, "follow request"),
        ({"redacted_projection": []}, "invalid shape"),
        ({"redacted_projection": {}}, "invalid shape"),
    ],
)
def test_del_07_request_rejects_invalid_immutable_snapshot(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_request(), **changes)


@pytest.mark.parametrize(
    "field, value",
    [
        ("action_type", "other.action"),
        ("capability_id", "cap.other"),
        ("connector_family", "other.connector"),
        ("binding_id", "binding.other"),
        ("destination", "other safe summary"),
        ("payload", []),
    ],
)
def test_del_07_request_projection_must_match_every_snapshot_field(field: str, value: Any) -> None:
    request = _request()
    with pytest.raises(ApprovalBindingError) as rejected:
        replace(request, redacted_projection={**request.redacted_projection, field: value})
    assert rejected.value.code == "approval_destination_projection_mismatch"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"proposal_revision": True}, "positive"),
        ({"decision": "approve"}, "exact decision enum"),
        ({"authority_roles": {"role.approver"}}, "immutable string set"),
        ({"authority_scopes": frozenset({1})}, "immutable string set"),
        ({"reason_code": "approval_rejected"}, "match the decision"),
        ({"reason": "line\nbreak"}, "unsupported characters"),
        ({"reason": "bad\x7f"}, "unsupported characters"),
        ({"reason": "bad\ud800"}, "unsupported characters"),
    ],
)
def test_del_07_decision_rejects_invalid_authority_kind_or_reason(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_decision(), **changes)


def test_del_07_decision_accepts_bounded_printable_reason() -> None:
    assert (
        replace(_decision(), reason="Reviewed synthetic input.").reason
        == "Reviewed synthetic input."
    )


def _approved() -> StoredActionApprovalRequest:
    decision = _decision()
    return StoredActionApprovalRequest(
        _request(), ApprovalStatus.APPROVED, 2, decision.decided_at, decision=decision
    )


def _use() -> ApprovalUse:
    request = _request()
    decision = _decision()
    return ApprovalUse(
        id="use.1",
        request_id=request.id,
        decision_id=decision.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        step_id=request.step_id,
        step_key=request.step_key,
        reservation_id="reservation.1",
        used_at=NOW + timedelta(minutes=2),
    )


def test_del_07_decision_and_use_reject_different_exact_request() -> None:
    with pytest.raises(ApprovalBindingError) as decision:
        assert_decision_binds_request(replace(_decision(), request_id="request.other"), _request())
    assert decision.value.code == "approval_decision_request_mismatch"
    assert_use_binds_request(_use(), _approved())
    with pytest.raises(ApprovalBindingError) as use:
        assert_use_binds_request(replace(_use(), request_id="request.other"), _approved())
    assert use.value.code == "approval_use_request_mismatch"
    with pytest.raises(ValueError, match="positive"):
        replace(_use(), proposal_revision=True)


def test_del_07_pending_request_is_created_only_from_exact_request_type() -> None:
    stored = StoredActionApprovalRequest.created(_request())
    assert stored.status is ApprovalStatus.PENDING
    with pytest.raises(ValueError, match="exact immutable"):
        StoredActionApprovalRequest.created(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"request": object()}, "exact request contract"),
        ({"status": "approved"}, "exact status enum"),
        ({"version": True}, "positive"),
        ({"updated_at": NOW - timedelta(seconds=1)}, "before it was requested"),
        ({"decision": object()}, "exact contract"),
        ({"replacement_request_id": "approval-request.1"}, "replace itself"),
        ({"replacement_request_id": "request.2"}, "present together"),
        ({"superseded_at": NOW}, "present together"),
        (
            {
                "superseded_at": NOW - timedelta(seconds=1),
                "superseded_reason_code": "run_cancelled",
            },
            "before it was requested",
        ),
        ({"superseded_at": NOW, "superseded_reason_code": "unsupported.reason"}, "not supported"),
        ({"use": object()}, "exact immutable contract"),
        ({"status": ApprovalStatus.PENDING}, "pristine creation"),
        ({"version": 3}, "incomplete or contradictory"),
        ({"status": ApprovalStatus.CONSUMED}, "one unexpired approval use"),
    ],
)
def test_del_07_stored_approval_rejects_inconsistent_lifecycle_facts(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_approved(), **changes)


def _renewal() -> ApprovalRenewal:
    old = _request()
    renewed_at = old.expires_at + timedelta(seconds=1)
    replacement = replace(
        old,
        id="approval-request.2",
        generation=2,
        requested_at=renewed_at,
        expires_at=renewed_at + timedelta(seconds=old.policy.expires_after_seconds),
    )
    expired = StoredActionApprovalRequest(
        old,
        ApprovalStatus.EXPIRED,
        3,
        renewed_at,
        expired_at=old.expires_at,
        replacement_request_id=replacement.id,
        renewed_at=renewed_at,
    )
    return ApprovalRenewal(expired, replacement)


def test_del_07_renewal_keeps_one_unchanged_action_and_set_epoch() -> None:
    renewal = _renewal()
    assert renewal.replacement.generation == renewal.expired.request.generation + 1
    for field in ("expired", "replacement"):
        with pytest.raises(ValueError, match="exact"):
            replace(renewal, **{field: object()})
    with pytest.raises(ValueError, match="one exact action leaf"):
        replace(renewal, replacement=replace(renewal.replacement, generation=3))
