"""ORCH-06: runtime-control denials are redacted replay-stable nonmutations."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import (
    AuditContext,
    AuditEventDraft,
    AuditOutcome,
    _issue_audit_event_draft,
)
from marketing_agents.security.audit_metadata import AuditMetadataError

NOW = datetime(2026, 8, 24, 18, tzinfo=UTC)


def _factory(correlation_id: str = "request.orch-06.runtime-denial") -> AuditEventFactory:
    return AuditEventFactory(
        AuditContext.worker(
            "worker.orch-06.runtime-control",
            correlation_id=correlation_id,
        )
    )


def _reissue(draft: AuditEventDraft, **changes: object) -> AuditEventDraft:
    values = {
        item.name: getattr(draft, item.name)
        for item in fields(AuditEventDraft)
        if item.name not in {"schema_version", "issuance_fingerprint"}
    }
    values.update(changes)
    return _issue_audit_event_draft(**values)


def _model_budget_denial(
    factory: AuditEventFactory,
    *,
    action_id: str | None = None,
) -> AuditEventDraft:
    return factory.runtime_control_denied(
        run_id="run.orch-06.runtime-denial",
        step_id="step.orch-06.runtime-denial",
        operation_key="operation.orch-06.model-read",
        denial_code="model_budget_exhausted",
        occurred_at=NOW,
        action_id=action_id,
    )


def test_orch_06_runtime_denial_is_exact_redacted_nonmutation() -> None:
    event = _factory().runtime_control_denied(
        run_id="run.orch-06.runtime-denial",
        step_id="step.orch-06.runtime-denial",
        action_id="action.orch-06.runtime-denial",
        operation_key="operation.orch-06.newsletter-write",
        denial_code="rate_limit_exhausted",
        retry_after_seconds=27,
        occurred_at=NOW,
    )

    assert event.event_type == "runtime.control_denied"
    assert event.aggregate_type == "runtime_control_denial"
    assert event.aggregate_id.startswith("runtime-control-denial-v1:")
    assert len(event.aggregate_id) == 90
    assert event.outcome is AuditOutcome.REJECTED
    assert event.run_id == "run.orch-06.runtime-denial"
    assert event.step_id == "step.orch-06.runtime-denial"
    assert event.action_id == "action.orch-06.runtime-denial"
    assert event.safe_metadata.values == {
        "denial_code": "rate_limit_exhausted",
        "operation_key": "operation.orch-06.newsletter-write",
        "retry_after_seconds": 27,
    }
    assert event.mutation_version is None
    assert event.transition_sequence is None
    assert event.previous_state is None
    assert event.new_state is None
    assert event.reason_code is None
    assert event.action_attempt_number is None
    assert event.receipt_id is None
    assert event.approval_request_id is None
    assert event.approval_decision_id is None
    assert "provider" not in repr(event)


def test_orch_06_runtime_denial_identity_is_deterministic_and_context_bound() -> None:
    first = _model_budget_denial(_factory())
    replay = _model_budget_denial(_factory())
    other_request = _model_budget_denial(_factory("request.orch-06.runtime-denial.other"))
    action_bound = _model_budget_denial(
        _factory(),
        action_id="action.orch-06.runtime-denial",
    )

    assert first == replay
    assert first.id == replay.id
    assert first.aggregate_id == replay.aggregate_id
    assert first.safe_metadata.values == {
        "denial_code": "model_budget_exhausted",
        "operation_key": "operation.orch-06.model-read",
    }
    assert first.action_id is None
    assert other_request.aggregate_id != first.aggregate_id
    assert other_request.id != first.id
    assert action_bound.aggregate_id != first.aggregate_id
    assert action_bound.id != first.id

    with pytest.raises(ValueError, match="aggregate identity"):
        _reissue(first, aggregate_id="runtime-control-denial-v1:" + ("0" * 64))


@pytest.mark.parametrize(
    ("denial_code", "retry_after_seconds", "expected"),
    (
        ("provider-secret-token", None, "not allowlisted"),
        ("rate_limit_exhausted", 0, "invalid safe value"),
        ("rate_limit_exhausted", 3_601, "safe bound"),
        ("rate_limit_exhausted", True, "invalid safe value"),
    ),
)
def test_orch_06_runtime_denial_rejects_untrusted_codes_and_retry_bounds(
    denial_code: str,
    retry_after_seconds: int | None,
    expected: str,
) -> None:
    exception = ValueError if retry_after_seconds is None else AuditMetadataError
    with pytest.raises(exception, match=expected):
        _factory().runtime_control_denied(
            run_id="run.orch-06.runtime-denial",
            step_id="step.orch-06.runtime-denial",
            operation_key="operation.orch-06.model-read",
            denial_code=denial_code,
            retry_after_seconds=retry_after_seconds,
            occurred_at=NOW,
        )


def test_orch_06_runtime_denial_factory_has_no_raw_error_or_provider_fields() -> None:
    with pytest.raises(TypeError):
        _factory().runtime_control_denied(
            run_id="run.orch-06.runtime-denial",
            step_id="step.orch-06.runtime-denial",
            operation_key="operation.orch-06.model-read",
            denial_code="deadline_exceeded",
            occurred_at=NOW,
            provider_text="secret-canary",  # type: ignore[call-arg]
        )
