"""Focused DEMO-03 scenario, registry, renderer, and composition contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from marketing_agents.application.policies.json_schema import compile_json_schema
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterContract,
    ReadAdapterPermanentError,
    ReadAdapterRequest,
)
from marketing_agents.demos import (
    DEMO_SCENARIOS,
    DemoScenarioInputError,
    build_demo_deterministic_provider,
    build_demo_read_adapter,
)
from marketing_agents.demos.email_signup_onboarding import (
    EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID,
    EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
    EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
    EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
    EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA_ID,
    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID,
    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,
    EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO,
    build_email_signup_onboarding_model_input,
    expected_email_signup_onboarding_artifact,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.runtime_policy import AttemptKind, RateLimitScope, RetryBackoff
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.infrastructure.catalog import compile_catalog

ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = ROOT / "catalog" / "v1"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "demos" / "email-signup.json"


def _mock_receipt_refs() -> list[dict[str, object]]:
    return [
        {
            "receipt_id": "mock-receipt:newsletter-demo-03",
            "action_id": "external-action:newsletter-demo-03",
            "action_type": "newsletter.subscribe",
            "capability_id": "cap.newsletter.subscribe",
            "binding_id": EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID,
            "status": "mock_succeeded",
            "external_side_effect": False,
        },
        {
            "receipt_id": "mock-receipt:crm-demo-03",
            "action_id": "external-action:crm-demo-03",
            "action_type": "crm.upsert-contact",
            "capability_id": "cap.crm.upsert-contact",
            "binding_id": EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID,
            "status": "mock_succeeded",
            "external_side_effect": False,
        },
    ]


def _email_model_operation(*, instance_id: str) -> OperationExecutionPolicy:
    return OperationExecutionPolicy(
        run_id="run.demo-03.contract",
        step_id="step.demo-03.create-welcome-draft",
        operation_key="operation.demo-03.create-welcome-draft",
        kind=AttemptKind.MODEL,
        capability_id="cap.model.generate-structured",
        selected_instance_id=instance_id,
        configuration_revision=1,
        connector_family="model",
        binding_id=None,
        binding_configuration_revision=None,
        request_schema_id=EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
        result_schema_id=EMAIL_SIGNUP_ONBOARDING_SCENARIO.output_schema_id,
        result_schema_hash=canonical_schema_hash(EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA),
        request_redaction_fields=(),
        result_redaction_fields=(),
        data_classification=DataClassification.INTERNAL,
        connector_timeout_seconds=None,
        policy_hash="a" * 64,
        max_attempts=1,
        retry_backoff=RetryBackoff.NONE,
        step_timeout_seconds=30,
        max_input_bytes=65_536,
        max_input_field_bytes=16_384,
        max_output_bytes=262_144,
        max_model_output_tokens=4_096,
        rate_limit_scope=RateLimitScope.TEMPLATE,
        rate_limit_key="rate.demo-03.create-welcome-draft",
        rate_window_max_calls=20,
        rate_window_seconds=60,
    )


def test_demo_03_contract_and_fixture_are_exact_and_bounded() -> None:
    scenario = EMAIL_SIGNUP_ONBOARDING_SCENARIO
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert fixture == json.loads(canonical_json_bytes(scenario.fixture))
    assert (scenario.id, scenario.workflow_id, scenario.version) == (
        "demo.email.signup-onboarding.v1",
        "demo.email.signup-onboarding.v1",
        1,
    )
    assert scenario.primary_instance_id == EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID
    assert scenario.selected_agents == (
        scenario.selected_agents[0].__class__(
            instance_id=EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
            template_id=EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,
        ),
        scenario.selected_agents[1].__class__(
            instance_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
            template_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
        ),
    )
    assert [
        (
            step.key,
            step.source_order,
            step.dependency_keys,
            step.terminal_result,
            step.kind,
            step.selected_instance_id,
            step.capability_id,
            step.effect,
        )
        for step in scenario.steps
    ] == [
        (
            "subscribe-newsletter",
            10,
            (),
            False,
            "connector.write",
            EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
            "cap.newsletter.subscribe",
            "write",
        ),
        (
            "upsert-crm-contact",
            20,
            (),
            False,
            "connector.write",
            EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
            "cap.crm.upsert-contact",
            "write",
        ),
        (
            "create-welcome-draft",
            30,
            ("subscribe-newsletter", "upsert-crm-contact"),
            True,
            "model.generate-structured",
            EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
            "cap.model.generate-structured",
            "read",
        ),
    ]
    assert scenario.effect == "mutating"
    assert scenario.expected_state_path == (
        "received",
        "validated",
        "planned",
        "awaiting_approval",
        "executing",
        "completed",
    )
    assert (
        scenario.expected_model_calls,
        scenario.expected_connector_calls,
        scenario.expected_external_actions,
        scenario.expected_approvals,
    ) == (1, 2, 2, 2)
    assert scenario.safe_submit_verb == "Propose onboarding actions"
    assert all(step.capability_id != "cap.email.send-message" for step in scenario.steps)

    properties = EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA["properties"]
    assert properties["name"]["x-sensitive"] is True
    assert properties["email"]["x-sensitive"] is True
    assert "binding_id" not in properties
    for schema, schema_id in (
        (EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA, scenario.input_schema_id),
        (
            EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA,
            EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
        ),
        (
            EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA,
            EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA_ID,
        ),
        (EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA, scenario.output_schema_id),
    ):
        compile_json_schema(schema, expected_schema_id=schema_id)


def test_demo_03_registry_normalizes_reserved_email_and_utc_timestamps() -> None:
    resolved = DEMO_SCENARIOS.resolve_input(
        EMAIL_SIGNUP_ONBOARDING_SCENARIO.id,
        {
            "email": "Avery.Demo@EXAMPLE.TEST",
            "consent": {
                "granted": True,
                "source": "demo_signup_form",
                "captured_at": "2026-08-31T09:00:00-07:00",
            },
            "signup_at": "2026-08-31T09:05:00-07:00",
        },
    )

    assert resolved["email"] == "Avery.Demo@example.test"
    assert resolved["consent"]["captured_at"] == "2026-08-31T16:00:00Z"
    assert resolved["signup_at"] == "2026-08-31T16:05:00Z"


@pytest.mark.parametrize(
    ("overrides", "pointer"),
    (
        ({"email": "avery.demo@example.com"}, "/email"),
        (
            {
                "consent": {
                    "granted": True,
                    "source": "demo_signup_form",
                    "captured_at": "2026-08-31T16:06:00Z",
                }
            },
            "/consent/captured_at",
        ),
        ({"newsletter_list_ref": "list.caller-controlled"}, "/newsletter_list_ref"),
        ({"binding_id": "real.newsletter.default"}, "/binding_id"),
    ),
)
def test_demo_03_registry_rejects_unsafe_or_noncanonical_authority(
    overrides: dict[str, object],
    pointer: str,
) -> None:
    with pytest.raises(DemoScenarioInputError) as captured:
        DEMO_SCENARIOS.resolve_input(EMAIL_SIGNUP_ONBOARDING_SCENARIO.id, overrides)

    assert captured.value.pointer == pointer


def test_demo_03_expected_artifact_is_one_mock_only_never_sent_summary() -> None:
    model_input = build_email_signup_onboarding_model_input(
        EMAIL_SIGNUP_ONBOARDING_SCENARIO.fixture,
        _mock_receipt_refs(),
    )
    assert "email" not in model_input
    compile_json_schema(
        EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA,
        expected_schema_id=EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
    ).validate(model_input, pointer_root="/model-input", max_depth=16)

    artifact = expected_email_signup_onboarding_artifact(model_input)
    compile_json_schema(
        EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA,
        expected_schema_id=EMAIL_SIGNUP_ONBOARDING_SCENARIO.output_schema_id,
    ).validate(artifact, pointer_root="/artifact", max_depth=16)

    assert artifact["artifact_type"] == "email_onboarding_summary"
    assert [item["action_type"] for item in artifact["mock_receipt_refs"]] == [
        "newsletter.subscribe",
        "crm.upsert-contact",
    ]
    assert all(item["external_side_effect"] is False for item in artifact["mock_receipt_refs"])
    assert artifact["evidence_status"] == "mock_only"
    assert artifact["external_side_effect"] is False
    assert artifact["email_send_status"] == "not_sent"
    assert artifact["welcome_artifact"]["artifact_type"] == "welcome_message_draft"
    assert artifact["welcome_artifact"]["delivery_status"] == "draft_only"
    assert artifact["welcome_artifact"]["send_status"] == "not_sent"
    assert "avery.demo@example.test" not in canonical_json_bytes(artifact).decode("utf-8")


@pytest.mark.asyncio
async def test_demo_03_composition_binds_model_to_customer_onboarder_only() -> None:
    catalog = compile_catalog(CATALOG_ROOT)
    provider = build_demo_deterministic_provider(catalog)
    adapter = build_demo_read_adapter(catalog, provider)
    operation = _email_model_operation(instance_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID)
    model_input = build_email_signup_onboarding_model_input(
        EMAIL_SIGNUP_ONBOARDING_SCENARIO.fixture,
        _mock_receipt_refs(),
    )

    assert adapter.input_contract_for(operation).schema_id == (
        EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID
    )
    assert adapter.output_contract_for(operation).schema_id == (
        EMAIL_SIGNUP_ONBOARDING_SCENARIO.output_schema_id
    )
    request = ReadAdapterRequest(
        attempt_id="execution-attempt.demo-03.contract.1",
        run_id=operation.run_id,
        step_id=operation.step_id,
        operation_key=operation.operation_key,
        policy_hash=operation.policy_hash,
        attempt_number=1,
        call_deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        correlation_id="correlation.demo-03.contract",
        requested_timeout_seconds=30,
        provenance_ids=("work-item:demo-03-contract",),
        input_classification=DataClassification.INTERNAL,
        contract=ReadAdapterContract.from_operation(operation),
        input_payload=model_input,
    )
    result = await adapter.execute(request)

    assert canonical_json_bytes(result.output_payload) == canonical_json_bytes(
        expected_email_signup_onboarding_artifact(model_input)
    )

    newsletter_primary = _email_model_operation(
        instance_id=EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID
    )
    with pytest.raises(ReadAdapterPermanentError) as captured:
        adapter.input_contract_for(newsletter_primary)
    assert captured.value.code == "adapter_contract_unavailable"
