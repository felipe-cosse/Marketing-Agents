"""ORCH-09: audit identity and metadata stay redacted and append-only."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.infrastructure.db.repositories.audit import SQLAlchemyAuditRepository
from marketing_agents.security.audit_metadata import (
    AuditMetadataError,
    hydrate_audit_metadata,
    seal_audit_metadata,
)
from marketing_agents.security.digest_key import DigestKey

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
CONFIGURATION_AUDIT_KEY = DigestKey(bytes(range(32)))


def _configuration_audit_factory(context: AuditContext) -> AuditEventFactory:
    return AuditEventFactory(
        context,
        configuration_pseudonym_key=CONFIGURATION_AUDIT_KEY,
    )


def _deployment_configuration(*, enabled: bool = True) -> dict[str, object]:
    return {
        "enabled": enabled,
        "variant_label": None,
        "trigger_bindings": [],
        "connector_bindings": {},
        "schedule": None,
    }


def test_api_03_instance_configuration_audit_is_runless_versioned_and_immutable() -> None:
    previous = _deployment_configuration()
    current = _deployment_configuration(enabled=False)
    event = _configuration_audit_factory(
        AuditContext.authenticated_user(
            "local-operator",
            authentication_method="local_fixed",
            correlation_id="request.api-03.configuration",
        )
    ).instance_configuration_changed(
        instance_id="inst.email.newsletter.newsletter-subscriber.01",
        previous_configuration=previous,
        new_configuration=current,
        previous_revision=4,
        new_revision=5,
        occurred_at=NOW,
    )

    previous["enabled"] = False
    assert event.event_type == "instance.configuration_changed"
    assert event.aggregate_type == "agent_instance_configuration"
    assert event.run_id is event.schedule_id is event.occurrence_id is None
    assert event.outcome.value == "accepted"
    assert event.expected_version == event.observed_version == 4
    assert event.mutation_version == 5
    assert event.safe_metadata.values["previous_configuration"]["enabled"] is True
    assert event.safe_metadata.values["new_configuration"]["enabled"] is False
    event.verify_integrity()


def test_api_03_instance_configuration_audit_accepts_bounded_full_snapshots() -> None:
    triggers = [
        {
            "type": trigger_type,
            "enabled": True,
            "event_source": "source.webhook" if trigger_type == "webhook" else None,
            "cron": "0 9 * * 1-5" if trigger_type == "schedule" else None,
            "timezone": "America/Los_Angeles" if trigger_type == "schedule" else None,
            "misfire_policy": "run_once" if trigger_type == "schedule" else None,
            "misfire_grace_seconds": 86_400 if trigger_type == "schedule" else None,
        }
        for trigger_type in ("manual", "webhook", "schedule")
    ]
    connectors = {
        f"family-{index:02d}": {
            "connector_family": f"family-{index:02d}",
            "binding_id": f"mock.family-{index:02d}.binding",
            "enabled": True,
        }
        for index in range(16)
    }
    previous = {
        "enabled": True,
        "variant_label": "Community deployment west",
        "trigger_bindings": triggers,
        "connector_bindings": connectors,
        "schedule": {
            "cron": "0 9 * * 1-5",
            "timezone": "America/Los_Angeles",
            "misfire_policy": "run_once",
            "misfire_grace_seconds": 86_400,
        },
    }
    current = {**previous, "enabled": False}

    event = _configuration_audit_factory(
        AuditContext.local_user(
            "local-operator",
            correlation_id="request.api-03.configuration.boundary",
        )
    ).instance_configuration_changed(
        instance_id="inst.community.events.attendee-scheduler.01",
        previous_configuration=previous,
        new_configuration=current,
        previous_revision=99,
        new_revision=100,
        occurred_at=NOW,
    )
    assert len(event.safe_metadata.values["new_configuration"]["connector_bindings"]) == 16
    event.verify_integrity()


@pytest.mark.parametrize(
    "mutate,canary",
    [
        (
            lambda config: config.update(variant_label="person@example.invalid"),
            "person@example.invalid",
        ),
        (
            lambda config: config.update(variant_label="ignore previous instructions"),
            "ignore previous instructions",
        ),
        (
            lambda config: config.update(variant_label="file:///Users/operator/prompt.md"),
            "file:///Users/operator/prompt.md",
        ),
        (
            lambda config: config.update(variant_label="Jane Doe confidential client"),
            "Jane Doe confidential client",
        ),
    ],
)
def test_api_03_instance_configuration_audit_pseudonymizes_sensitive_text_values(
    mutate: Callable[[dict[str, object]], None],
    canary: str,
) -> None:
    previous = _deployment_configuration()
    current = _deployment_configuration(enabled=False)
    mutate(current)

    event = _configuration_audit_factory(
        AuditContext.local_user(
            "local-operator",
            correlation_id="request.api-03.configuration.canary",
        )
    ).instance_configuration_changed(
        instance_id="inst.email.newsletter.newsletter-subscriber.01",
        previous_configuration=previous,
        new_configuration=current,
        previous_revision=1,
        new_revision=2,
        occurred_at=NOW,
    )
    rendered = str(event.safe_metadata.values)
    assert canary not in rendered
    assert "audit-value-hmac-sha256-v1:" in rendered
    event.verify_integrity()


def test_api_03_instance_configuration_audit_rejects_sensitive_extra_fields() -> None:
    previous = _deployment_configuration()
    current = _deployment_configuration(enabled=False)
    current["connector_bindings"] = {
        "newsletter": {
            "connector_family": "newsletter",
            "binding_id": "mock.newsletter.01",
            "enabled": True,
            "api_key": "secret-value-canary",
        }
    }

    with pytest.raises(AuditMetadataError) as captured:
        _configuration_audit_factory(
            AuditContext.local_user(
                "local-operator",
                correlation_id="request.api-03.configuration.sensitive-field",
            )
        ).instance_configuration_changed(
            instance_id="inst.email.newsletter.newsletter-subscriber.01",
            previous_configuration=previous,
            new_configuration=current,
            previous_revision=1,
            new_revision=2,
            occurred_at=NOW,
        )
    assert captured.value.code == "metadata_value_invalid"
    assert "secret-value-canary" not in str(captured.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: config.update(variant_label=" " * 3),
        lambda config: config.update(variant_label="x" * 1_000),
        lambda config: config.update(
            trigger_bindings=[
                {
                    "type": "webhook",
                    "enabled": True,
                    "event_source": "not an id",
                    "cron": None,
                    "timezone": None,
                    "misfire_policy": None,
                    "misfire_grace_seconds": None,
                }
            ]
        ),
    ],
)
def test_api_03_instance_configuration_audit_validates_raw_before_pseudonymizing(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    previous = _deployment_configuration()
    current = _deployment_configuration(enabled=False)
    mutate(current)

    with pytest.raises(AuditMetadataError) as captured:
        _configuration_audit_factory(
            AuditContext.local_user(
                "local-operator",
                correlation_id="request.api-03.configuration.invalid-raw",
            )
        ).instance_configuration_changed(
            instance_id="inst.email.newsletter.newsletter-subscriber.01",
            previous_configuration=previous,
            new_configuration=current,
            previous_revision=1,
            new_revision=2,
            occurred_at=NOW,
        )
    assert captured.value.code == "metadata_value_invalid"


def test_api_03_instance_configuration_audit_pseudonym_transform_has_no_input_collision() -> None:
    baseline = _deployment_configuration()
    sensitive = _deployment_configuration(enabled=False)
    sensitive["variant_label"] = "person@example.invalid"
    first = _configuration_audit_factory(
        AuditContext.local_user(
            "local-operator",
            correlation_id="request.api-03.configuration.pseudonym-source",
        )
    ).instance_configuration_changed(
        instance_id="inst.email.newsletter.newsletter-subscriber.01",
        previous_configuration=baseline,
        new_configuration=sensitive,
        previous_revision=1,
        new_revision=2,
        occurred_at=NOW,
    )
    first_pseudonym = first.safe_metadata.values["new_configuration"]["variant_label"]
    assert first_pseudonym.startswith("audit-value-hmac-sha256-v1:")
    public_digest = hashlib.sha256(canonical_json_bytes("person@example.invalid")).hexdigest()
    assert not first_pseudonym.endswith(public_digest)

    alternate_key_event = AuditEventFactory(
        AuditContext.local_user(
            "local-operator",
            correlation_id="request.api-03.configuration.alternate-key",
        ),
        configuration_pseudonym_key=DigestKey(bytes(reversed(range(32)))),
    ).instance_configuration_changed(
        instance_id="inst.email.newsletter.newsletter-subscriber.01",
        previous_configuration=baseline,
        new_configuration=sensitive,
        previous_revision=1,
        new_revision=2,
        occurred_at=NOW,
    )
    assert (
        alternate_key_event.safe_metadata.values["new_configuration"]["variant_label"]
        != first_pseudonym
    )

    raw_prefix_value = _deployment_configuration(enabled=False)
    raw_prefix_value["variant_label"] = first_pseudonym
    collision_safe = _configuration_audit_factory(
        AuditContext.local_user(
            "local-operator",
            correlation_id="request.api-03.configuration.pseudonym-collision",
        )
    ).instance_configuration_changed(
        instance_id="inst.email.newsletter.newsletter-subscriber.01",
        previous_configuration=sensitive,
        new_configuration=raw_prefix_value,
        previous_revision=2,
        new_revision=3,
        occurred_at=NOW,
    )
    previous_safe = collision_safe.safe_metadata.values["previous_configuration"]
    current_safe = collision_safe.safe_metadata.values["new_configuration"]
    assert previous_safe["variant_label"] == first_pseudonym
    assert current_safe["variant_label"].startswith("audit-value-hmac-sha256-v1:")
    assert current_safe["variant_label"] != first_pseudonym
    assert "person@example.invalid" not in str(collision_safe.safe_metadata.values)
    collision_safe.verify_integrity()


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
