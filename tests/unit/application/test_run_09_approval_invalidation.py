"""RUN-09: any action-payload change invalidates and supersedes approval."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from marketing_agents.application.services.approval_integrity import (
    ApprovalIntegrityError,
    invalidate_and_replace,
    validate_current_action,
)
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    semantic_action_hash,
)
from marketing_agents.domain.approval import (
    ApprovalPolicySnapshot,
    ProposedExternalAction,
    request_approval,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "display_name": {"type": "string"},
        "details": {"type": "object"},
    },
}


def _action(**updates: object) -> CanonicalExternalAction:
    values: dict[str, object] = {
        "action_id": "action.1",
        "authorization_set_id": "authorization-set.1",
        "run_id": "run.1",
        "plan_hash": "a" * 64,
        "proposal_revision": 1,
        "step_id": "step.send",
        "step_key": "send",
        "template_id": "tpl.email.newsletter.newsletter-subscriber",
        "instance_id": "inst.email.newsletter.newsletter-subscriber.01",
        "action_type": "email.send",
        "capability_id": "cap.email.send",
        "connector_family": "newsletter-email",
        "binding_id": "binding.mock.email",
        "destination": "contact:1",
        "payload_schema_id": "schema.email.send.v1",
        "minimized_payload": {"display_name": "Café", "details": {"b": 2, "a": 1}},
    }
    values.update(updates)
    semantic = SemanticExternalAction.model_validate(
        {
            key: values[key]
            for key in (
                "template_id",
                "instance_id",
                "action_type",
                "capability_id",
                "connector_family",
                "binding_id",
                "destination",
                "payload_schema_id",
                "minimized_payload",
            )
        }
    )
    values["semantic_action_hash"] = semantic_action_hash(semantic)
    return CanonicalExternalAction.model_validate(values)


def _proposal(action: CanonicalExternalAction) -> ProposedExternalAction:
    return ProposedExternalAction.create(
        action,
        redacted_destination="configured contact",
        payload_schema=PAYLOAD_SCHEMA,
    )


def _request() -> object:
    proposal = _proposal(_action())
    return request_approval(
        request_id="approval-request.1",
        proposed_action=proposal,
        policy=ApprovalPolicySnapshot(
            "policy.human-write.v1",
            frozenset({"role.approver"}),
            frozenset({"scope.external-write"}),
            900,
            False,
        ),
        requested_by="principal.local.operator",
        requested_at=NOW,
    )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("action_type", "email.unsubscribe"),
        ("destination", "contact:2"),
        ("capability_id", "cap.email.unsubscribe"),
        ("binding_id", "binding.mock.other"),
        ("minimized_payload", {"display_name": "Changed", "details": {"a": 1}}),
    ],
)
def test_run_09_action_mutation_invalidates_and_creates_next_generation(
    field: str, changed: object
) -> None:
    current = _request()
    action = _action(**{field: changed})
    with pytest.raises(ApprovalIntegrityError) as captured:
        validate_current_action(current, action)  # type: ignore[arg-type]
    assert captured.value.code == "approval_invalidated"

    replacement = invalidate_and_replace(
        current_request=current,  # type: ignore[arg-type]
        replacement_request_id="approval-request.2",
        replacement_action=_proposal(action),
        requested_by="principal.local.operator",
        now=NOW,
        expected_client_hash=current.action_hash,  # type: ignore[attr-defined]
    )
    assert replacement.replacement.generation == 2
    assert replacement.replacement.action_hash != current.action_hash  # type: ignore[attr-defined]
    assert not replacement.superseded.authorizable
    with pytest.raises(ApprovalIntegrityError) as superseded:
        replacement.superseded.reject_authorization()
    assert superseded.value.code == "approval_superseded"


def test_run_09_canonical_key_order_and_unicode_equivalence_do_not_invalidate() -> None:
    current = _request()
    equivalent = _action(
        minimized_payload={
            "details": {"a": 1, "b": 2},
            "display_name": "Cafe\u0301",
        }
    )
    validate_current_action(current, equivalent)  # type: ignore[arg-type]
    with pytest.raises(ApprovalIntegrityError) as captured:
        invalidate_and_replace(
            current_request=current,  # type: ignore[arg-type]
            replacement_request_id="approval-request.2",
            replacement_action=_proposal(equivalent),
            requested_by="principal.local.operator",
            now=NOW,
            expected_client_hash=current.action_hash,  # type: ignore[attr-defined]
        )
    assert captured.value.code == "approval_action_unchanged"


def test_run_09_stale_client_hash_and_replacement_scope_fail_closed() -> None:
    current = _request()
    with pytest.raises(ApprovalIntegrityError) as stale:
        validate_current_action(current, _action(), expected_client_hash="0" * 64)  # type: ignore[arg-type]
    assert stale.value.code == "expected_hash_mismatch"

    changed_scope = _proposal(
        _action(action_id="action.2", minimized_payload={"display_name": "Changed"})
    )
    with pytest.raises(ApprovalIntegrityError) as mismatch:
        invalidate_and_replace(
            current_request=current,  # type: ignore[arg-type]
            replacement_request_id="approval-request.2",
            replacement_action=changed_scope,
            requested_by="principal.local.operator",
            now=NOW,
            expected_client_hash=current.action_hash,  # type: ignore[attr-defined]
        )
    assert mismatch.value.code == "replacement_scope_mismatch"
