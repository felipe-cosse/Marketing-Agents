"""Trusted definition and deterministic renderer for the Email signup demo."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import JsonValue

from marketing_agents.application.ports.llm import LLMRequest
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.infrastructure.adapters.llm.deterministic import (
    DeterministicRenderContext,
    RendererKey,
    RendererRegistration,
)

from .contracts import DemoScenarioDefinition, DemoScenarioStep, DemoSelectedAgent

EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID = "demo.email.signup-onboarding.v1"
EMAIL_SIGNUP_ONBOARDING_WORKFLOW_ID = EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID
EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID = "tpl.email.newsletter.newsletter-subscriber"
EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID = "inst.email.newsletter.newsletter-subscriber.01"
EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID = "tpl.email.lifecycle-marketing.customer-onboarder"
EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID = (
    "inst.email.lifecycle-marketing.customer-onboarder.01"
)
EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA_ID = "schema.demo.email.signup-onboarding.input.v1"
EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID = "schema.demo.email.signup-onboarding.model-input.v1"
EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA_ID = (
    "schema.demo.email.signup-onboarding.model-output.v1"
)
EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA_ID = "schema.demo.email.signup-onboarding.output.v1"

EMAIL_SIGNUP_ONBOARDING_LIST_REF = "list.demo.email.signup-onboarding.v1"
EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID = "mock.newsletter.default"
EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID = "mock.crm.default"

_TIMESTAMP_SCHEMA = {
    "type": "string",
    "format": "date-time",
    "maxLength": 40,
}

EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contact_id",
        "name",
        "email",
        "newsletter_list_ref",
        "consent",
        "signup_at",
        "welcome_context",
    ],
    "properties": {
        "contact_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "pattern": "^demo-contact-[a-z0-9-]+$",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "x-sensitive": True,
        },
        "email": {
            "type": "string",
            "format": "email",
            "minLength": 3,
            "maxLength": 254,
            "x-sensitive": True,
        },
        "newsletter_list_ref": {
            "type": "string",
            "const": EMAIL_SIGNUP_ONBOARDING_LIST_REF,
        },
        "consent": {
            "type": "object",
            "additionalProperties": False,
            "required": ["granted", "source", "captured_at"],
            "properties": {
                "granted": {"type": "boolean", "const": True},
                "source": {"type": "string", "const": "demo_signup_form"},
                "captured_at": dict(_TIMESTAMP_SCHEMA),
            },
        },
        "signup_at": dict(_TIMESTAMP_SCHEMA),
        "welcome_context": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2_000,
        },
    },
}


def _mock_receipt_ref_schema(
    *,
    action_type: str,
    capability_id: str,
    binding_id: str,
) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "receipt_id",
            "action_id",
            "action_type",
            "capability_id",
            "binding_id",
            "status",
            "external_side_effect",
        ],
        "properties": {
            "receipt_id": {"type": "string", "minLength": 1, "maxLength": 240},
            "action_id": {"type": "string", "minLength": 1, "maxLength": 240},
            "action_type": {"const": action_type},
            "capability_id": {"const": capability_id},
            "binding_id": {"const": binding_id},
            "status": {"const": "mock_succeeded"},
            "external_side_effect": {"const": False},
        },
    }


_NEWSLETTER_RECEIPT_REF_SCHEMA = _mock_receipt_ref_schema(
    action_type="newsletter.subscribe",
    capability_id="cap.newsletter.subscribe",
    binding_id=EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID,
)
_CRM_RECEIPT_REF_SCHEMA = _mock_receipt_ref_schema(
    action_type="crm.upsert-contact",
    capability_id="cap.crm.upsert-contact",
    binding_id=EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID,
)
_ORDERED_RECEIPTS_SCHEMA = {
    "type": "array",
    "minItems": 2,
    "maxItems": 2,
    "uniqueItems": True,
    "prefixItems": [
        _NEWSLETTER_RECEIPT_REF_SCHEMA,
        _CRM_RECEIPT_REF_SCHEMA,
    ],
    "items": False,
}

EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contact_id",
        "name",
        "welcome_context",
        "mock_receipt_refs",
    ],
    "properties": {
        "contact_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "pattern": "^demo-contact-[a-z0-9-]+$",
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "x-sensitive": True,
        },
        "welcome_context": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2_000,
        },
        "mock_receipt_refs": _ORDERED_RECEIPTS_SCHEMA,
    },
}

_WELCOME_ARTIFACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "artifact_type",
        "recipient_contact_id",
        "subject",
        "body_text",
        "delivery_status",
        "send_status",
        "safety_notes",
    ],
    "properties": {
        "artifact_type": {"const": "welcome_message_draft"},
        "recipient_contact_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
        },
        "subject": {"type": "string", "minLength": 1, "maxLength": 120},
        "body_text": {"type": "string", "minLength": 1, "maxLength": 2_400},
        "delivery_status": {"const": "draft_only"},
        "send_status": {"const": "not_sent"},
        "safety_notes": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
    },
}

EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": ["contact_id", "mock_receipt_refs", "welcome_artifact"],
    "properties": {
        "contact_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
        },
        "mock_receipt_refs": _ORDERED_RECEIPTS_SCHEMA,
        "welcome_artifact": _WELCOME_ARTIFACT_SCHEMA,
    },
}

EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "scenario_id",
        "scenario_version",
        "artifact_type",
        "contact_id",
        "mock_receipt_refs",
        "welcome_artifact",
        "onboarding_status",
        "evidence_status",
        "external_side_effect",
        "email_send_status",
    ],
    "properties": {
        "scenario_id": {"const": EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID},
        "scenario_version": {"const": 1},
        "artifact_type": {"const": "email_onboarding_summary"},
        "contact_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
        },
        "mock_receipt_refs": _ORDERED_RECEIPTS_SCHEMA,
        "welcome_artifact": _WELCOME_ARTIFACT_SCHEMA,
        "onboarding_status": {"const": "mock_actions_succeeded"},
        "evidence_status": {"const": "mock_only"},
        "external_side_effect": {"const": False},
        "email_send_status": {"const": "not_sent"},
    },
}

EMAIL_SIGNUP_ONBOARDING_FIXTURE = {
    "contact_id": "demo-contact-0001",
    "name": "Avery Demo",
    "email": "avery.demo@example.test",
    "newsletter_list_ref": EMAIL_SIGNUP_ONBOARDING_LIST_REF,
    "consent": {
        "granted": True,
        "source": "demo_signup_form",
        "captured_at": "2026-08-31T16:00:00Z",
    },
    "signup_at": "2026-08-31T16:05:00Z",
    "welcome_context": ("Welcome the subscriber to governed AI updates for marketing teams."),
}

EMAIL_SIGNUP_ONBOARDING_SCENARIO = DemoScenarioDefinition(
    id=EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
    version=1,
    display_name="Email signup onboarding",
    description=(
        "Prepare approved mock newsletter and CRM onboarding actions, then create a "
        "welcome-message draft that is never sent."
    ),
    selected_agents=(
        DemoSelectedAgent(
            instance_id=EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
            template_id=EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,
        ),
        DemoSelectedAgent(
            instance_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
            template_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
        ),
    ),
    primary_instance_id=EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
    steps=(
        DemoScenarioStep(
            key="subscribe-newsletter",
            source_order=10,
            dependency_keys=(),
            terminal_result=False,
            kind="connector.write",
            selected_instance_id=EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
            capability_id="cap.newsletter.subscribe",
            effect="write",
        ),
        DemoScenarioStep(
            key="upsert-crm-contact",
            source_order=20,
            dependency_keys=(),
            terminal_result=False,
            kind="connector.write",
            selected_instance_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
            capability_id="cap.crm.upsert-contact",
            effect="write",
        ),
        DemoScenarioStep(
            key="create-welcome-draft",
            source_order=30,
            dependency_keys=("subscribe-newsletter", "upsert-crm-contact"),
            terminal_result=True,
            kind="model.generate-structured",
            selected_instance_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
            capability_id="cap.model.generate-structured",
            effect="read",
        ),
    ),
    workflow_id=EMAIL_SIGNUP_ONBOARDING_WORKFLOW_ID,
    effect="mutating",
    input_schema_id=EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA_ID,
    input_schema=EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA,
    output_schema_id=EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA_ID,
    output_schema=EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA,
    fixture=EMAIL_SIGNUP_ONBOARDING_FIXTURE,
    expected_state_path=(
        "received",
        "validated",
        "planned",
        "awaiting_approval",
        "executing",
        "completed",
    ),
    expected_model_calls=1,
    expected_connector_calls=2,
    expected_external_actions=2,
    expected_approvals=2,
    safe_submit_verb="Propose onboarding actions",
)


def build_email_signup_onboarding_model_input(
    signup_payload: Mapping[str, Any],
    mock_receipt_refs: Sequence[Mapping[str, Any]],
) -> dict[str, JsonValue]:
    """Build the server-owned model input after both exact mock writes succeed."""

    normalized_signup = json.loads(canonical_json_bytes(signup_payload))
    normalized_receipts = json.loads(canonical_json_bytes(mock_receipt_refs))
    if type(normalized_signup) is not dict or type(normalized_receipts) is not list:
        raise ValueError("Email onboarding model input requires canonical objects")
    if len(normalized_receipts) != 2:
        raise ValueError("Email onboarding requires exactly two mock receipt references")
    try:
        return {
            "contact_id": cast(JsonValue, normalized_signup["contact_id"]),
            "name": cast(JsonValue, normalized_signup["name"]),
            "welcome_context": cast(JsonValue, normalized_signup["welcome_context"]),
            "mock_receipt_refs": cast(JsonValue, normalized_receipts),
        }
    except KeyError:
        raise ValueError("Email onboarding signup payload is incomplete") from None


def _input_from_request(request: LLMRequest) -> dict[str, JsonValue]:
    if len(request.retrieved_content) != 1 or request.tool_results:
        raise ValueError("Email onboarding renderer requires one untrusted input and no tools")
    payload = json.loads(request.retrieved_content[0].content)
    if type(payload) is not dict:
        raise ValueError("Email onboarding model input must be an object")
    return cast(dict[str, JsonValue], payload)


def _render_email_signup_onboarding_payload(
    payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    contact_id = cast(str, payload["contact_id"])
    name = cast(str, payload["name"])
    welcome_context = cast(str, payload["welcome_context"])
    mock_receipt_refs = cast(list[JsonValue], payload["mock_receipt_refs"])
    body_text = (
        f"Hello {name},\n\n{welcome_context}\n\n"
        "This message is a reviewable draft and has not been sent."
    )
    return {
        "contact_id": contact_id,
        "mock_receipt_refs": mock_receipt_refs,
        "welcome_artifact": {
            "artifact_type": "welcome_message_draft",
            "recipient_contact_id": contact_id,
            "subject": "Welcome to governed AI updates",
            "body_text": body_text,
            "delivery_status": "draft_only",
            "send_status": "not_sent",
            "safety_notes": [
                "The signup and welcome context were treated as untrusted data.",
                "The newsletter and CRM receipts are mock-only evidence.",
                "No email-send capability was invoked or authorized.",
            ],
        },
    }


def render_email_signup_onboarding(
    request: LLMRequest,
    context: DeterministicRenderContext,
) -> dict[str, JsonValue]:
    """Render the draft and preserve the two ordered, server-supplied mock receipts."""

    del context
    return _render_email_signup_onboarding_payload(_input_from_request(request))


EMAIL_SIGNUP_ONBOARDING_RENDERER = RendererRegistration(
    key=RendererKey(
        template_id=EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
        output_schema_id=EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA_ID,
    ),
    version="demo-email-signup-onboarding-v1",
    output_schema_hash=canonical_schema_hash(EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA),
    renderer=render_email_signup_onboarding,
)


def finalize_email_signup_onboarding(
    model_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Add the trusted mock-only and never-sent summary envelope."""

    return {
        "scenario_id": EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
        "scenario_version": 1,
        "artifact_type": "email_onboarding_summary",
        "contact_id": cast(JsonValue, model_payload.get("contact_id")),
        "mock_receipt_refs": cast(JsonValue, model_payload.get("mock_receipt_refs")),
        "welcome_artifact": cast(JsonValue, model_payload.get("welcome_artifact")),
        "onboarding_status": "mock_actions_succeeded",
        "evidence_status": "mock_only",
        "external_side_effect": False,
        "email_send_status": "not_sent",
    }


def expected_email_signup_onboarding_artifact(
    model_input_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Recompute deterministic business fields for persisted-evidence validation."""

    normalized = json.loads(canonical_json_bytes(model_input_payload))
    if type(normalized) is not dict:
        raise ValueError("Email onboarding admitted model input must be an object")
    return finalize_email_signup_onboarding(_render_email_signup_onboarding_payload(normalized))


__all__ = [
    "EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID",
    "EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID",
    "EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID",
    "EMAIL_SIGNUP_ONBOARDING_FIXTURE",
    "EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA",
    "EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA_ID",
    "EMAIL_SIGNUP_ONBOARDING_LIST_REF",
    "EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA",
    "EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID",
    "EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA",
    "EMAIL_SIGNUP_ONBOARDING_MODEL_OUTPUT_SCHEMA_ID",
    "EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID",
    "EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID",
    "EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID",
    "EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA",
    "EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA_ID",
    "EMAIL_SIGNUP_ONBOARDING_RENDERER",
    "EMAIL_SIGNUP_ONBOARDING_SCENARIO",
    "EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID",
    "EMAIL_SIGNUP_ONBOARDING_WORKFLOW_ID",
    "build_email_signup_onboarding_model_input",
    "expected_email_signup_onboarding_artifact",
    "finalize_email_signup_onboarding",
    "render_email_signup_onboarding",
]
