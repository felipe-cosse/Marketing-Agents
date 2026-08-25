"""ORCH-09: audit identity and metadata stay redacted and append-only."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.infrastructure.db.repositories.audit import SQLAlchemyAuditRepository
from marketing_agents.security.audit_metadata import (
    AuditMetadataError,
    hydrate_audit_metadata,
    seal_audit_metadata,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def test_orch_09_audit_context_pseudonymizes_canaries_and_detects_mutation() -> None:
    actor_canary = "person@example.invalid/api_key.secret"
    correlation_canary = "Bearer-token-14155550123"
    context = AuditContext.local_user(
        actor_canary,
        correlation_id=correlation_canary,
    )

    rendered = repr(context) + context.actor_id + context.correlation_id
    assert actor_canary not in rendered
    assert correlation_canary not in rendered
    assert context.actor_id.startswith("audit-actor-v1:")
    assert context.correlation_id.startswith("audit-correlation-v1:")
    assert context.actor_id != context.correlation_id
    AuditEventFactory(context)

    replacement = AuditContext.local_user(
        "different@example.invalid",
        correlation_id=correlation_canary,
    )
    object.__setattr__(context, "actor_id", replacement.actor_id)
    with pytest.raises(ValueError, match="changed after trusted issuance"):
        AuditEventFactory(context)


def test_orch_09_persisted_metadata_is_revalidated_not_trusted_by_fingerprint() -> None:
    personal = seal_audit_metadata(
        "run.transitioned",
        {"command": "fail"},
        occurred_at=NOW,
        classification=DataClassification.PERSONAL,
    )
    assert dict(personal.values) == {}

    with pytest.raises(AuditMetadataError, match="wholly redacted"):
        hydrate_audit_metadata(
            "run.transitioned",
            {"command": "fail"},
            classification=DataClassification.PERSONAL,
            occurred_at=NOW,
            expires_at=NOW + timedelta(days=90),
        )
    with pytest.raises(AuditMetadataError, match="invalid safe value"):
        hydrate_audit_metadata(
            "run.transitioned",
            {"command": "person@example.invalid"},
            classification=DataClassification.INTERNAL,
            occurred_at=NOW,
            expires_at=NOW + timedelta(days=90),
        )
    with pytest.raises(AuditMetadataError, match="forbidden field"):
        hydrate_audit_metadata(
            "run.transitioned",
            {"command": "fail", "api_key": "secret-canary"},
            classification=DataClassification.INTERNAL,
            occurred_at=NOW,
            expires_at=NOW + timedelta(days=90),
        )


def test_orch_09_audit_repository_has_no_event_update_or_delete_surface() -> None:
    public_methods = {
        name
        for name, member in inspect.getmembers(
            SQLAlchemyAuditRepository,
            predicate=inspect.isfunction,
        )
        if not name.startswith("_")
    }
    assert public_methods == {
        "append",
        "append_global",
        "append_global_many",
        "append_many",
        "get",
        "get_attempt_event",
        "get_mutation_event",
        "list_run",
    }

    source = inspect.getsource(SQLAlchemyAuditRepository)
    assert "update(AuditEventRecord)" not in source
    assert "delete(AuditEventRecord)" not in source
    assert ".delete(" not in source
