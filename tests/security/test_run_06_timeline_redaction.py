"""RUN-06: attempt and artifact timelines retain codes, never payload/error detail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.security.audit_metadata import (
    AuditMetadataError,
    hydrate_audit_metadata,
    seal_audit_metadata,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
EXPIRES = NOW + timedelta(days=90)


def test_run_06_attempt_metadata_retains_only_bounded_typed_codes() -> None:
    reserved = seal_audit_metadata(
        "attempt.reserved",
        {
            "attempt_kind": "tool",
            "attempt_number": 2,
            "input_classification": "sensitive",
            "input_schema_id": "schema.run-06.input.v1",
            "operation_key": "operation.run-06.read",
        },
        occurred_at=NOW,
    )
    completed = seal_audit_metadata(
        "attempt.completed",
        {
            "attempt_kind": "tool",
            "attempt_number": 2,
            "attempt_outcome": "permanent_failure",
            "operation_key": "operation.run-06.read",
            "safe_error_code": "output_schema_invalid",
        },
        occurred_at=NOW,
    )

    assert set(reserved.values) == {
        "attempt_kind",
        "attempt_number",
        "input_classification",
        "input_schema_id",
        "operation_key",
    }
    assert completed.values["safe_error_code"] == "output_schema_invalid"
    assert "payload" not in reserved.values
    assert "error" not in completed.values


@pytest.mark.parametrize(
    ("event_type", "metadata"),
    (
        (
            "attempt.reserved",
            {
                "attempt_kind": "model",
                "attempt_number": 1,
                "input_classification": "personal",
                "input_schema_id": "schema.run-06.input.v1",
                "operation_key": "operation.run-06.read",
                "redacted_input": {"email": "person@example.invalid"},
            },
        ),
        (
            "attempt.completed",
            {
                "attempt_kind": "model",
                "attempt_number": 1,
                "attempt_outcome": "permanent_failure",
                "operation_key": "operation.run-06.read",
                "provider_error": "Bearer secret-token-canary",
            },
        ),
        (
            "artifact.persisted",
            {
                "data_classification": "personal",
                "output_schema_id": "schema.run-06.output.v1",
                "output_schema_version": "v1",
                "payload": {"email": "person@example.invalid"},
            },
        ),
    ),
)
def test_run_06_payload_and_provider_detail_fields_are_never_allowlisted(
    event_type: str,
    metadata: dict[str, object],
) -> None:
    with pytest.raises(AuditMetadataError) as rejected:
        seal_audit_metadata(event_type, metadata, occurred_at=NOW)
    assert rejected.value.code == "metadata_field_forbidden"


def test_run_06_safe_error_hydration_rejects_secret_canary_and_unknown_code() -> None:
    base = {
        "attempt_kind": "model",
        "attempt_number": 1,
        "attempt_outcome": "permanent_failure",
        "operation_key": "operation.run-06.read",
    }
    for canary in ("person@example.invalid", "Bearer-secret-token-canary"):
        with pytest.raises(AuditMetadataError) as rejected:
            hydrate_audit_metadata(
                "attempt.completed",
                {**base, "safe_error_code": canary},
                classification=DataClassification.INTERNAL,
                occurred_at=NOW,
                expires_at=EXPIRES,
            )
        assert rejected.value.code == "metadata_value_invalid"


def test_run_06_artifact_hydration_accepts_only_safe_identity_metadata() -> None:
    hydrated = hydrate_audit_metadata(
        "artifact.persisted",
        {
            "data_classification": "sensitive",
            "output_schema_hash": "schema-sha256-v1:" + ("e" * 64),
            "output_schema_id": "schema.run-06.output.v1",
            "output_schema_version": "v1",
        },
        classification=DataClassification.INTERNAL,
        occurred_at=NOW,
        expires_at=EXPIRES,
    )
    assert dict(hydrated.values) == {
        "data_classification": "sensitive",
        "output_schema_hash": "schema-sha256-v1:" + ("e" * 64),
        "output_schema_id": "schema.run-06.output.v1",
        "output_schema_version": "v1",
    }
