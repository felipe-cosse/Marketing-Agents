"""RUN-07: one approval request authorizes one immutable proposed action only."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    semantic_action_hash,
)
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalBindingError,
    ApprovalPolicySnapshot,
    ProposedExternalAction,
    assert_request_binds_action,
    request_approval,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _action(**updates: object) -> CanonicalExternalAction:
    values: dict[str, object] = {
        "action_id": "action.1",
        "authorization_set_id": "authorization-set.1",
        "run_id": "run.1",
        "plan_hash": "a" * 64,
        "proposal_revision": 1,
        "step_id": "step.subscribe",
        "step_key": "subscribe",
        "template_id": "tpl.email.newsletter.newsletter-subscriber",
        "instance_id": "inst.email.newsletter.newsletter-subscriber.01",
        "action_type": "newsletter.subscribe",
        "capability_id": "cap.newsletter.subscribe",
        "connector_family": "newsletter-email",
        "binding_id": "binding.mock.newsletter",
        "destination": "list:newsletter",
        "payload_schema_id": "schema.newsletter.subscribe.v1",
        "minimized_payload": {"email": "person@example.invalid", "locale": "en"},
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


def _request(action: CanonicalExternalAction | None = None) -> ActionApprovalRequest:
    action = action or _action()
    proposed = ProposedExternalAction.create(
        action,
        redacted_destination="configured newsletter list",
        payload_schema={
            "type": "object",
            "properties": {
                "email": {"type": "string", "x-data-classification": "personal"},
                "locale": {"type": "string"},
            },
        },
    )
    return request_approval(
        request_id="approval-request.1",
        proposed_action=proposed,
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


def test_run_07_request_snapshots_one_exact_action_with_finite_expiry() -> None:
    action = _action()
    request = _request(action)
    assert_request_binds_action(request, action)
    assert request.action_id == action.action_id
    assert request.authorization_set_id == action.authorization_set_id
    assert (request.expires_at - request.requested_at).total_seconds() == 900
    assert request.generation == 1
    assert request.redacted_projection["payload"] == {
        "email": "[REDACTED]",
        "locale": "en",
    }
    assert "person@example.invalid" not in str(request.redacted_projection)
    with pytest.raises(FrozenInstanceError):
        request.action_hash = "0" * 64  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.redacted_projection["payload"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        request.redacted_projection["payload"]["locale"] = "pt"  # type: ignore[index]


def test_run_07_identical_payload_in_a_different_action_is_not_reusable() -> None:
    original = _action()
    request = _request(original)
    different = _action(action_id="action.2")
    with pytest.raises(ApprovalBindingError) as captured:
        assert_request_binds_action(request, different)
    assert captured.value.code == "approval_action_mismatch"


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("authorization_set_id", "authorization-set.2"),
        ("run_id", "run.2"),
        ("plan_hash", "b" * 64),
        ("proposal_revision", 2),
        ("step_id", "step.other"),
        ("step_key", "other"),
        ("template_id", "tpl.other"),
        ("instance_id", "inst.other.01"),
        ("action_type", "newsletter.unsubscribe"),
        ("capability_id", "cap.newsletter.unsubscribe"),
        ("connector_family", "crm"),
        ("binding_id", "binding.mock.other"),
        ("destination", "list:other"),
        ("payload_schema_id", "schema.other.v1"),
        ("minimized_payload", {"email": "changed@example.invalid"}),
    ],
)
def test_run_07_any_action_scope_or_payload_change_breaks_binding(
    field: str, changed: object
) -> None:
    request = _request()
    with pytest.raises(ApprovalBindingError):
        assert_request_binds_action(request, _action(**{field: changed}))


@pytest.mark.parametrize("seconds", [0, 59, 86_401])
def test_run_07_unbounded_or_nonfinite_approval_expiry_is_rejected(seconds: int) -> None:
    with pytest.raises(ValueError, match="expiry"):
        ApprovalPolicySnapshot(
            "policy.invalid",
            frozenset({"role.approver"}),
            frozenset({"scope.external-write"}),
            seconds,
            False,
        )
