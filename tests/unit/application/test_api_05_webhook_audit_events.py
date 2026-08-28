"""API-05: webhook audit witnesses retain only bounded server-owned linkage."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditActorSource, AuditContext, AuditOutcome
from marketing_agents.infrastructure.db import AuditEventRecord, Base
from marketing_agents.infrastructure.db.repositories.audit import (
    _draft_to_record,
    _record_to_domain,
)
from marketing_agents.security.audit_metadata import AuditMetadataError, seal_audit_metadata
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
SOURCE = "provider.api-05"
TRIGGER_ID = "trigger.api-05.webhook"
ATTEMPT_ID = "webhook-attempt.api-05.001"
RECEIPT_ID = "webhook-receipt.api-05.001"


def _verified_factory() -> AuditEventFactory:
    return AuditEventFactory(
        AuditContext.verified_webhook(
            "service.webhook.provider-api-05",
            correlation_id="correlation.api-05.webhook",
        )
    )


def _system_factory() -> AuditEventFactory:
    return AuditEventFactory(
        AuditContext.system(
            "service.webhook-gateway",
            correlation_id="correlation.api-05.signature-rejected",
        )
    )


def test_api_05_signature_audits_preserve_the_pre_and_post_verification_boundary() -> None:
    validated = _verified_factory().webhook_signature_validated(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        webhook_attempt_id=ATTEMPT_ID,
        occurred_at=NOW,
    )
    rejected = _system_factory().webhook_signature_rejected(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        webhook_attempt_id=ATTEMPT_ID,
        occurred_at=NOW,
    )

    assert validated.event_type == "webhook.signature_validated"
    assert validated.actor_source is AuditActorSource.SERVICE
    assert validated.auth_method == "verified_webhook"
    assert validated.outcome is AuditOutcome.ACCEPTED
    assert validated.mutation_version == 1
    assert validated.reason_code is None
    assert validated.run_id is None
    assert validated.safe_metadata.values == {
        "source": SOURCE,
        "trigger_id": TRIGGER_ID,
        "webhook_attempt_id": ATTEMPT_ID,
    }

    assert rejected.event_type == "webhook.signature_rejected"
    assert rejected.actor_source is AuditActorSource.SYSTEM
    assert rejected.auth_method == "internal"
    assert rejected.outcome is AuditOutcome.REJECTED
    assert rejected.mutation_version is None
    assert rejected.reason_code == "webhook_authentication_failed"
    assert rejected.run_id is None
    assert rejected.safe_metadata.values == validated.safe_metadata.values
    assert validated.aggregate_id != rejected.aggregate_id
    assert validated.aggregate_id.startswith("webhook-audit-v1:")
    validated.verify_integrity()
    rejected.verify_integrity()


@pytest.mark.parametrize(
    ("method_name", "event_type", "disposition", "outcome", "mutation", "reason"),
    (
        (
            "webhook_received",
            "webhook.received",
            "created",
            AuditOutcome.ACCEPTED,
            1,
            None,
        ),
        (
            "webhook_duplicate_suppressed",
            "webhook.duplicate_suppressed",
            "replayed",
            AuditOutcome.ACCEPTED,
            1,
            None,
        ),
        (
            "webhook_idempotency_collision",
            "webhook.idempotency_collision",
            "collision",
            AuditOutcome.REJECTED,
            None,
            "idempotency_conflict",
        ),
    ),
)
def test_api_05_receipt_events_have_exact_bounded_dispositions(
    method_name: str,
    event_type: str,
    disposition: str,
    outcome: AuditOutcome,
    mutation: int | None,
    reason: str | None,
) -> None:
    method = getattr(_verified_factory(), method_name)
    event = method(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        webhook_attempt_id=ATTEMPT_ID,
        webhook_receipt_id=RECEIPT_ID,
        target_count=3,
        occurred_at=NOW,
    )

    assert event.event_type == event_type
    assert event.aggregate_type == "webhook_ingress"
    assert event.run_id is None
    assert event.outcome is outcome
    assert event.mutation_version == mutation
    assert event.reason_code == reason
    assert event.safe_metadata.values == {
        "source": SOURCE,
        "trigger_id": TRIGGER_ID,
        "webhook_attempt_id": ATTEMPT_ID,
        "webhook_receipt_id": RECEIPT_ID,
        "receipt_disposition": disposition,
        "target_count": 3,
    }
    event.verify_integrity()


def test_api_05_schema_rejection_retains_only_complete_safe_configuration_linkage() -> None:
    event = _verified_factory().webhook_schema_rejected(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        webhook_attempt_id=ATTEMPT_ID,
        instance_id="instance.api-05.target",
        configuration_revision=7,
        workflow_id="workflow.api-05.target",
        occurred_at=NOW,
    )

    assert event.outcome is AuditOutcome.REJECTED
    assert event.reason_code == "schema_rejected"
    assert event.mutation_version is None
    assert event.safe_metadata.values == {
        "source": SOURCE,
        "trigger_id": TRIGGER_ID,
        "webhook_attempt_id": ATTEMPT_ID,
        "instance_id": "instance.api-05.target",
        "configuration_revision": 7,
        "workflow_id": "workflow.api-05.target",
        "rejection_code": "schema_rejected",
    }
    event.verify_integrity()

    with pytest.raises(ValueError, match="linkage must be complete"):
        _verified_factory().webhook_schema_rejected(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id="webhook-attempt.api-05.incomplete",
            instance_id="instance.api-05.target",
            occurred_at=NOW,
        )


def test_api_05_factories_fail_closed_on_wrong_contexts_and_unbounded_targets() -> None:
    with pytest.raises(ValueError, match="exact trusted actor context"):
        _system_factory().webhook_received(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id=ATTEMPT_ID,
            webhook_receipt_id=RECEIPT_ID,
            target_count=1,
            occurred_at=NOW,
        )
    with pytest.raises(ValueError, match="exact trusted actor context"):
        _verified_factory().webhook_signature_rejected(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id=ATTEMPT_ID,
            occurred_at=NOW,
        )
    for target_count in (0, True, 65):
        with pytest.raises(ValueError, match="bounded positive integer"):
            _verified_factory().webhook_received(
                source=SOURCE,
                trigger_id=TRIGGER_ID,
                webhook_attempt_id=f"webhook-attempt.api-05.target-{target_count}",
                webhook_receipt_id=RECEIPT_ID,
                target_count=target_count,
                occurred_at=NOW,
            )


@pytest.mark.parametrize(
    "forbidden_field",
    (
        "event_id",
        "raw_event_id",
        "raw_body",
        "signature",
        "secret_ref",
        "body_digest",
        "admission_digest",
    ),
)
def test_api_05_webhook_metadata_rejects_all_raw_or_secret_material(
    forbidden_field: str,
) -> None:
    metadata: dict[str, object] = {
        "source": SOURCE,
        "trigger_id": TRIGGER_ID,
        "webhook_attempt_id": ATTEMPT_ID,
        "webhook_receipt_id": RECEIPT_ID,
        "receipt_disposition": "created",
        "target_count": 1,
        forbidden_field: "api-05-forbidden-canary",
    }
    with pytest.raises(AuditMetadataError) as rejected:
        seal_audit_metadata("webhook.received", metadata, occurred_at=NOW)
    assert rejected.value.code == "metadata_field_forbidden"


def test_api_05_public_audit_methods_expose_no_raw_or_secret_parameter_seams() -> None:
    forbidden_parameters = {
        "event_id",
        "raw_event_id",
        "raw_body",
        "signature",
        "secret_ref",
        "body_digest",
        "admission_digest",
    }
    method_names = (
        "webhook_signature_validated",
        "webhook_signature_rejected",
        "webhook_received",
        "webhook_duplicate_suppressed",
        "webhook_idempotency_collision",
        "webhook_schema_rejected",
    )
    for method_name in method_names:
        parameters = set(inspect.signature(getattr(AuditEventFactory, method_name)).parameters)
        assert parameters.isdisjoint(forbidden_parameters)

    event = _verified_factory().webhook_received(
        source=SOURCE,
        trigger_id=TRIGGER_ID,
        webhook_attempt_id=ATTEMPT_ID,
        webhook_receipt_id=RECEIPT_ID,
        target_count=1,
        occurred_at=NOW,
    )
    rendered = repr((event.aggregate_id, event.safe_metadata.values, event))
    for canary in (
        "provider-event.api-05.secret",
        '{"raw":"api-05-body-canary"}',
        "api-05-signature-canary",
        "env/API_05_WEBHOOK_SECRET",
        "api-05-body-digest-canary",
        "api-05-admission-digest-canary",
    ):
        assert canary not in rendered


def test_api_05_webhook_events_satisfy_the_portable_record_constraints() -> None:
    verified = _verified_factory()
    events = (
        verified.webhook_signature_validated(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id="webhook-attempt.api-05.record.valid",
            occurred_at=NOW,
        ),
        _system_factory().webhook_signature_rejected(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id="webhook-attempt.api-05.record.rejected",
            occurred_at=NOW,
        ),
        verified.webhook_received(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id="webhook-attempt.api-05.record.created",
            webhook_receipt_id=RECEIPT_ID,
            target_count=2,
            occurred_at=NOW,
        ),
        verified.webhook_duplicate_suppressed(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id="webhook-attempt.api-05.record.replayed",
            webhook_receipt_id=RECEIPT_ID,
            target_count=2,
            occurred_at=NOW,
        ),
        verified.webhook_idempotency_collision(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id="webhook-attempt.api-05.record.collision",
            webhook_receipt_id=RECEIPT_ID,
            target_count=2,
            occurred_at=NOW,
        ),
        verified.webhook_schema_rejected(
            source=SOURCE,
            trigger_id=TRIGGER_ID,
            webhook_attempt_id="webhook-attempt.api-05.record.schema",
            occurred_at=NOW,
        ),
    )
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session, session.begin():
            session.add_all(
                _draft_to_record(event, None, feed_sequence)
                for feed_sequence, event in enumerate(events, start=1)
            )
        with Session(engine) as session:
            stored = session.scalars(
                select(AuditEventRecord).order_by(AuditEventRecord.global_sequence)
            ).all()
            hydrated = tuple(_record_to_domain(record) for record in stored)
        assert tuple(item.draft.event_type for item in hydrated) == tuple(
            event.event_type for event in events
        )
        assert all(item.run_sequence is None for item in hydrated)
    finally:
        engine.dispose()
