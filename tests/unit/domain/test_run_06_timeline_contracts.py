"""RUN-06: attempts and artifacts have exact, immutable timeline witnesses."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import (
    AuditContext,
    AuditOutcome,
    _issue_audit_event_draft,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.execution_control import AttemptOutcome, ExecutionAttempt
from marketing_agents.domain.provenance import (
    ArtifactEnvelope,
    ProvenanceSource,
    ProviderVersion,
)
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.infrastructure.db.models import AuditEventRecord
from marketing_agents.security.audit_metadata import seal_audit_metadata
from sqlalchemy import ForeignKeyConstraint, Table, UniqueConstraint

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _factory() -> AuditEventFactory:
    return AuditEventFactory(
        AuditContext.worker(
            "worker.run-06.timeline",
            correlation_id="correlation.run-06.timeline",
        )
    )


def test_run_06_runtime_output_schema_hash_is_canonical_and_content_bound() -> None:
    common = {
        "schema_id": "schema.run-06.output.v1",
        "schema_version": "v1",
        "classification": DataClassification.INTERNAL,
        "provider_kind": "llm",
        "provider_mode": "mock",
        "provider_name": "deterministic",
        "provider_version": "v1",
    }
    left = RuntimeOutputContract(
        **common,
        schema={"type": "object", "properties": {"b": {"type": "string"}}},
    )
    equivalent = RuntimeOutputContract(
        **common,
        schema={"properties": {"b": {"type": "string"}}, "type": "object"},
    )
    changed = RuntimeOutputContract(
        **common,
        schema={"type": "object", "properties": {"b": {"type": "boolean"}}},
    )

    assert left.schema_hash == equivalent.schema_hash
    assert left.schema_hash != changed.schema_hash
    assert left.schema_hash.startswith("schema-sha256-v1:")


def _attempt(
    *,
    outcome: AttemptOutcome | None = None,
    safe_error_code: str | None = None,
    output_artifact_id: str | None = None,
) -> ExecutionAttempt:
    completed = outcome is not None
    return ExecutionAttempt(
        id="attempt.run-06.1",
        run_id="run.run-06.timeline",
        step_id="step.run-06.read",
        operation_key="operation.run-06.read",
        policy_hash="a" * 64,
        kind=AttemptKind.MODEL,
        attempt_number=1,
        source_control_version=2,
        source_step_version=3,
        eligible_at=NOW - timedelta(seconds=1),
        reserved_at=NOW,
        call_deadline_at=NOW + timedelta(seconds=30),
        input_schema_id="schema.run-06.input.v1",
        redacted_input={"prompt": "[REDACTED]"},
        input_classification=DataClassification.PERSONAL,
        outcome=outcome,
        completed_at=NOW + timedelta(seconds=5) if completed else None,
        retry_not_before=None,
        terminal_reason_code=(
            "permanent_failure" if outcome is AttemptOutcome.PERMANENT_FAILURE else None
        ),
        safe_error_code=safe_error_code,
        output_artifact_id=output_artifact_id,
        version=2 if completed else 1,
    )


def _artifact(
    *,
    created_at: datetime = NOW + timedelta(seconds=5),
) -> ArtifactEnvelope:
    return ArtifactEnvelope.create(
        payload={
            "draft": "person@example.invalid",
            "provider_detail": "Bearer secret-token-canary",
        },
        artifact_id="artifact.run-06.1",
        work_item_id="work-item.run-06.1",
        run_id="run.run-06.timeline",
        step_id="step.run-06.read",
        workflow_id="workflow.run-06",
        workflow_version="v1",
        template_id="template.run-06",
        instance_id="instance.run-06",
        admitted_input_digest="b" * 64,
        catalog_hash="catalog-sha256-v1:" + ("c" * 64),
        instance_config_revision=1,
        sources=(
            ProvenanceSource(
                kind="work_input",
                source_id="work-item.run-06.1",
                integrity_digest="d" * 64,
                classification=DataClassification.PERSONAL,
            ),
        ),
        parent_artifact_ids=(),
        providers=(
            ProviderVersion(
                provider_kind="llm",
                mode="mock",
                name="deterministic",
                version="v1",
            ),
        ),
        output_schema_id="schema.run-06.output.v1",
        output_schema_version="v1",
        output_schema_hash="schema-sha256-v1:" + ("e" * 64),
        created_at=created_at,
        classification=DataClassification.PERSONAL,
    )


def test_run_06_factory_issues_exact_reserved_completed_and_artifact_events() -> None:
    factory = _factory()
    reserved = factory.attempt_reserved(_attempt())
    succeeded_attempt = _attempt(
        outcome=AttemptOutcome.SUCCEEDED,
        output_artifact_id="artifact.run-06.1",
    )
    completed = factory.attempt_completed(succeeded_attempt)
    artifact = factory.artifact_persisted(_artifact(), succeeded_attempt)

    assert (
        reserved.event_type,
        reserved.aggregate_type,
        reserved.aggregate_id,
        reserved.attempt_id,
        reserved.mutation_version,
        reserved.previous_state,
        reserved.new_state,
        reserved.occurred_at,
    ) == (
        "attempt.reserved",
        "execution_attempt",
        succeeded_attempt.id,
        succeeded_attempt.id,
        1,
        None,
        "reserved",
        succeeded_attempt.reserved_at,
    )
    assert dict(reserved.safe_metadata.values) == {
        "attempt_kind": "model",
        "attempt_number": 1,
        "input_classification": "personal",
        "input_schema_id": "schema.run-06.input.v1",
        "operation_key": "operation.run-06.read",
    }
    assert (
        completed.event_type,
        completed.aggregate_type,
        completed.aggregate_id,
        completed.artifact_id,
        completed.mutation_version,
        completed.previous_state,
        completed.new_state,
        completed.occurred_at,
    ) == (
        "attempt.completed",
        "execution_attempt",
        succeeded_attempt.id,
        "artifact.run-06.1",
        2,
        "reserved",
        "succeeded",
        succeeded_attempt.completed_at,
    )
    assert "safe_error_code" not in completed.safe_metadata.values
    assert (
        artifact.event_type,
        artifact.aggregate_type,
        artifact.aggregate_id,
        artifact.artifact_id,
        artifact.attempt_id,
        artifact.mutation_version,
        artifact.previous_state,
        artifact.new_state,
    ) == (
        "artifact.persisted",
        "artifact",
        "artifact.run-06.1",
        "artifact.run-06.1",
        succeeded_attempt.id,
        1,
        None,
        "persisted",
    )
    assert dict(artifact.safe_metadata.values) == {
        "data_classification": "personal",
        "output_schema_hash": "schema-sha256-v1:" + ("e" * 64),
        "output_schema_id": "schema.run-06.output.v1",
        "output_schema_version": "v1",
    }
    assert len({reserved.id, completed.id, artifact.id}) == 3


def test_run_06_failure_completion_requires_one_allowlisted_safe_error() -> None:
    failed = _attempt(
        outcome=AttemptOutcome.PERMANENT_FAILURE,
        safe_error_code="output_schema_invalid",
    )
    event = _factory().attempt_completed(failed)

    assert event.artifact_id is None
    assert event.new_state == "permanent_failure"
    assert event.safe_metadata.values["safe_error_code"] == "output_schema_invalid"

    missing_error = replace(failed, safe_error_code=None)
    with pytest.raises(ValueError, match="allowlisted safe error"):
        _factory().attempt_completed(missing_error)


def test_run_06_artifact_audit_rejects_mismatched_time_identity_and_payload() -> None:
    attempt = _attempt(
        outcome=AttemptOutcome.SUCCEEDED,
        output_artifact_id="artifact.run-06.1",
    )
    factory = _factory()

    with pytest.raises(ValueError, match="succeeded attempt"):
        factory.artifact_persisted(
            _artifact(created_at=NOW + timedelta(seconds=6)),
            attempt,
        )
    with pytest.raises(ValueError, match="succeeded attempt"):
        factory.artifact_persisted(
            _artifact(),
            replace(attempt, output_artifact_id="artifact.run-06.other"),
        )
    tampered = _artifact().model_copy(update={"payload": {"draft": "changed"}})
    with pytest.raises(ValueError, match="payload hash"):
        factory.artifact_persisted(tampered, attempt)


def test_run_06_raw_hydration_rejects_impossible_attempt_and_artifact_links() -> None:
    context = AuditContext.worker(
        "worker.run-06.hydration",
        correlation_id="correlation.run-06.hydration",
    )
    completion_metadata = seal_audit_metadata(
        "attempt.completed",
        {
            "attempt_kind": "model",
            "attempt_number": 1,
            "attempt_outcome": "permanent_failure",
            "operation_key": "operation.run-06.read",
        },
        occurred_at=NOW,
    )
    with pytest.raises(ValueError, match="terminal state"):
        _issue_audit_event_draft(
            id="audit.run-06.missing-safe-error",
            run_id="run.run-06.timeline",
            event_type="attempt.completed",
            aggregate_type="execution_attempt",
            aggregate_id="attempt.run-06.1",
            outcome=AuditOutcome.ACCEPTED,
            actor_id=context.actor_id,
            actor_source=context.actor_source,
            auth_method=context.auth_method,
            correlation_id=context.correlation_id,
            safe_metadata=completion_metadata,
            occurred_at=NOW,
            step_id="step.run-06.read",
            attempt_id="attempt.run-06.1",
            mutation_version=2,
            previous_state="reserved",
            new_state="permanent_failure",
        )

    transition_metadata = seal_audit_metadata(
        "run.transitioned",
        {"command": "mark_validated"},
        occurred_at=NOW,
    )
    with pytest.raises(ValueError, match="attempt link"):
        _issue_audit_event_draft(
            id="audit.run-06.unrelated-attempt-link",
            run_id="run.run-06.timeline",
            event_type="run.transitioned",
            aggregate_type="run",
            aggregate_id="run.run-06.timeline",
            outcome=AuditOutcome.ACCEPTED,
            actor_id=context.actor_id,
            actor_source=context.actor_source,
            auth_method=context.auth_method,
            correlation_id=context.correlation_id,
            safe_metadata=transition_metadata,
            occurred_at=NOW,
            attempt_id="attempt.run-06.1",
            mutation_version=2,
            transition_sequence=2,
            previous_state="received",
            new_state="validated",
            reason_code="input_validated",
        )


def test_run_06_rejected_run_attempt_shape_and_replay_identity_remain_exact() -> None:
    context = AuditContext.worker(
        "worker.run-06.rejection",
        correlation_id="correlation.run-06.rejection",
    )
    metadata = seal_audit_metadata(
        "run.transition_rejected",
        {"command": "complete"},
        occurred_at=NOW,
    )
    event = _issue_audit_event_draft(
        id="audit.run-06.rejected",
        run_id="run.run-06.timeline",
        event_type="run.transition_rejected",
        aggregate_type="run_attempt",
        aggregate_id="run-attempt-v1:" + ("e" * 64),
        outcome=AuditOutcome.REJECTED,
        actor_id=context.actor_id,
        actor_source=context.actor_source,
        auth_method=context.auth_method,
        correlation_id=context.correlation_id,
        safe_metadata=metadata,
        occurred_at=NOW,
        attempt_id="run-attempt-v1:" + ("e" * 64),
        attempted_command="complete",
        expected_version=4,
        observed_version=5,
        observed_state="executing",
        requested_state="completed",
        mutation_version=None,
        reason_code="execution_incomplete",
    )

    event.verify_integrity()
    assert event.mutation_version is None
    assert event.aggregate_id == event.attempt_id


def test_run_06_database_contracts_bind_attempts_and_artifacts_without_collision() -> None:
    table = cast(Table, AuditEventRecord.__table__)
    unique = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_audit_events_run_event_attempt"
    )
    assert tuple(unique.columns.keys()) == ("run_id", "event_type", "attempt_id")

    foreign_keys = {
        (
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("attempt_id", "run_id", "step_id"),
        (
            "execution_attempts.id",
            "execution_attempts.run_id",
            "execution_attempts.step_id",
        ),
    ) in foreign_keys
    assert (
        ("artifact_id", "run_id", "step_id"),
        ("artifacts.id", "artifacts.run_id", "artifacts.step_id"),
    ) in foreign_keys
