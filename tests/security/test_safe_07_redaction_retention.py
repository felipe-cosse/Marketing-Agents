"""SAFE-07: PII is centrally redacted and each persisted detail class has its own TTL."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.config import Settings
from marketing_agents.domain.data_classification import (
    DataClassification,
    highest_classification,
)
from marketing_agents.domain.retention import RetentionCategory, RetentionPolicy
from marketing_agents.security.redaction import REDACTED, SecretValue, redact
from pydantic import ValidationError


def test_safe_07_nested_schema_and_defense_in_depth_remove_pii_canaries() -> None:
    payload = {
        "contact": {"email": "person@example.invalid", "display": "Useful label"},
        "members": [{"name": "Canary Person", "status": "active"}],
        "webhook_signature": "signature-canary",
        "nested": {"credential_token": "token-canary", "count": 2},
    }
    original = copy.deepcopy(payload)
    schema = {
        "type": "object",
        "properties": {
            "contact": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "x-data-classification": "personal"},
                    "display": {"type": "string"},
                },
            },
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "x-sensitive": True},
                        "status": {"type": "string"},
                    },
                },
            },
        },
    }

    result = redact(payload, schema=schema)
    rendered = json.dumps(result)
    assert payload == original
    assert result["contact"] == {"email": REDACTED, "display": "Useful label"}
    assert result["members"] == [{"name": REDACTED, "status": "active"}]
    assert "signature-canary" not in rendered
    assert "token-canary" not in rendered
    assert result["nested"]["count"] == 2


def test_safe_07_secret_wrappers_and_settings_snapshots_never_render_values() -> None:
    canary = "runtime-secret-canary"
    wrapped = SecretValue(canary)
    assert str(wrapped) == REDACTED
    assert canary not in repr(wrapped)

    settings = Settings(
        _env_file=None,
        llm_provider="real",
        allow_external_network=True,
        real_llm_opt_in=True,
        real_llm_api_key=canary,
    )
    assert canary not in json.dumps(settings.safe_snapshot())


def test_safe_07_retention_defaults_and_independent_overrides_are_exact() -> None:
    defaults = Settings(_env_file=None).retention_policy
    assert defaults.ttl_for(RetentionCategory.ADMITTED_PAYLOAD) == timedelta(days=7)
    assert defaults.ttl_for(RetentionCategory.EXTERNAL_ACTION_PAYLOAD) == timedelta(days=7)
    assert defaults.ttl_for(RetentionCategory.APPROVAL_DETAIL) == timedelta(days=7)
    assert defaults.ttl_for(RetentionCategory.ARTIFACT_DETAIL) == timedelta(days=30)
    assert defaults.ttl_for(RetentionCategory.CONNECTOR_RECEIPT_DETAIL) == timedelta(days=30)
    assert defaults.ttl_for(RetentionCategory.AUDIT_METADATA) == timedelta(days=90)

    changed = Settings(_env_file=None, retention_approval_detail_days=11).retention_policy
    assert changed.ttl_for(RetentionCategory.APPROVAL_DETAIL) == timedelta(days=11)
    assert changed.ttl_for(RetentionCategory.ARTIFACT_DETAIL) == timedelta(days=30)


def test_safe_07_retention_rejects_unsafe_ttls_timestamps_and_secrets() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(admitted_payload_days=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retention_audit_metadata_days=3651)

    policy = RetentionPolicy()
    created = datetime(2026, 1, 1, tzinfo=UTC)
    assert policy.is_expired(
        RetentionCategory.ADMITTED_PAYLOAD,
        created,
        DataClassification.PERSONAL,
        created + timedelta(days=7),
    )
    with pytest.raises(ValueError, match="UTC"):
        policy.expires_at(
            RetentionCategory.ADMITTED_PAYLOAD,
            datetime(2026, 1, 1),
            DataClassification.INTERNAL,
        )
    with pytest.raises(ValueError, match="never retainable"):
        policy.expires_at(
            RetentionCategory.AUDIT_METADATA,
            created,
            DataClassification.SECRET,
        )
    assert (
        highest_classification(DataClassification.INTERNAL, DataClassification.SENSITIVE)
        is DataClassification.SENSITIVE
    )
