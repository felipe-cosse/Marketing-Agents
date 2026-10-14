"""EXEC-06: expiry retains conditional audit facts without accepting malformed events."""

from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext, AuditOutcome, _issue_audit_event_draft
from marketing_agents.infrastructure.db.repositories.audit import (
    AuditPersistenceInvariantError,
    _draft_to_record,
    _record_to_domain,
    expire_optional_metadata,
)
from marketing_agents.security.audit_metadata import seal_audit_metadata

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _completion(outcome, *, include_error):
    context = AuditContext.worker("worker.retention", correlation_id="request.retention")
    metadata = {
        "attempt_kind": "tool",
        "attempt_number": 1,
        "attempt_outcome": outcome,
        "operation_key": "operation.retention",
    }
    if include_error:
        metadata["safe_error_code"] = "temporary_failure"
    return _issue_audit_event_draft(
        id="audit.retention",
        run_id="run.retention",
        event_type="attempt.completed",
        aggregate_type="execution_attempt",
        aggregate_id="attempt.retention",
        outcome=AuditOutcome.ACCEPTED,
        actor_id=context.actor_id,
        actor_source=context.actor_source,
        auth_method=context.auth_method,
        correlation_id=context.correlation_id,
        safe_metadata=seal_audit_metadata("attempt.completed", metadata, occurred_at=NOW),
        occurred_at=NOW,
        step_id="step.retention",
        attempt_id="attempt.retention",
        artifact_id="artifact.retention" if outcome == "succeeded" else None,
        mutation_version=2,
        previous_state="reserved",
        new_state=outcome,
    )


@pytest.mark.parametrize(
    "outcome", ["succeeded", "transient_failure", "permanent_failure", "cancelled"]
)
def test_retention_preserves_conditional_completion_semantics(outcome):
    draft = _completion(outcome, include_error=outcome != "succeeded")
    record = _draft_to_record(draft, run_sequence=1, feed_sequence=1)
    record.global_sequence = 1
    expire_optional_metadata(record, now=NOW + timedelta(days=91))
    historical = _record_to_domain(record)
    assert historical.draft.new_state == outcome
    assert ("safe_error_code" in historical.safe_metadata.values) == (outcome != "succeeded")
    assert historical.safe_metadata.expires_at == draft.safe_metadata.expires_at


@pytest.mark.parametrize(
    "outcome", ["succeeded", "transient_failure", "permanent_failure", "cancelled"]
)
def test_retention_does_not_relax_live_conditional_completion_validation(outcome):
    with pytest.raises(ValueError, match="invalid terminal state"):
        _completion(outcome, include_error=outcome == "succeeded")


def test_retention_refuses_tampered_missing_error_before_mutating_audit():
    record = _draft_to_record(
        _completion("cancelled", include_error=True), run_sequence=1, feed_sequence=1
    )
    record.global_sequence = 1
    original_fingerprint = record.event_fingerprint
    record.safe_metadata = {
        key: value for key, value in record.safe_metadata.items() if key != "safe_error_code"
    }
    with pytest.raises(AuditPersistenceInvariantError):
        expire_optional_metadata(record, now=NOW + timedelta(days=91))
    assert record.event_fingerprint == original_fingerprint


def test_retention_still_erases_truly_optional_audit_metadata():
    draft = AuditEventFactory(
        AuditContext.worker("worker.retention", correlation_id="request.retention")
    ).runtime_control_denied(
        run_id="run.retention",
        step_id="step.retention",
        operation_key="operation.retention",
        denial_code="rate_limit_exhausted",
        retry_after_seconds=27,
        occurred_at=NOW,
    )
    record = _draft_to_record(draft, run_sequence=1, feed_sequence=1)
    record.global_sequence = 1
    expire_optional_metadata(record, now=NOW + timedelta(days=91))
    historical = _record_to_domain(record)
    assert dict(historical.safe_metadata.values) == {
        "denial_code": "rate_limit_exhausted",
        "operation_key": "operation.retention",
    }
    assert historical.id == draft.id
