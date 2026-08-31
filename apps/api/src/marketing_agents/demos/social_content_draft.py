"""Trusted definition and deterministic renderer for the social draft demo."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from pydantic import JsonValue

from marketing_agents.application.ports.llm import LLMRequest
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.infrastructure.adapters.llm.deterministic import (
    DeterministicRenderContext,
    RendererKey,
    RendererRegistration,
)

from .contracts import DemoScenarioDefinition, DemoScenarioStep, DemoSelectedAgent

SOCIAL_CONTENT_DRAFT_SCENARIO_ID = "demo.social-media.content-draft.v1"
SOCIAL_CONTENT_DRAFT_TEMPLATE_ID = "tpl.social-media.new-content.linkedin-post-drafter"
SOCIAL_CONTENT_DRAFT_INSTANCE_ID = "inst.social-media.new-content.linkedin-post-drafter.01"
SOCIAL_CONTENT_DRAFT_WORKFLOW_ID = SOCIAL_CONTENT_DRAFT_SCENARIO_ID
SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA_ID = "schema.demo.social-media.content-draft.input.v1"
SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA_ID = (
    "schema.demo.social-media.content-draft.model-output.v1"
)
SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA_ID = "schema.demo.social-media.content-draft.output.v1"

SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": ["idea", "audience", "tone", "key_points"],
    "properties": {
        "idea": {"type": "string", "minLength": 1, "maxLength": 1_200},
        "audience": {"type": "string", "minLength": 1, "maxLength": 160},
        "tone": {
            "type": "string",
            "enum": ["professional", "conversational", "educational", "bold"],
        },
        "key_points": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {"type": "string", "minLength": 1, "maxLength": 250},
        },
        "call_to_action": {"type": "string", "minLength": 1, "maxLength": 250},
        "source_urls": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "minLength": 1, "maxLength": 2_048},
        },
    },
}

SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "draft_text",
        "hashtags",
        "cta_summary",
        "source_references",
        "safety_notes",
    ],
    "properties": {
        "draft_text": {"type": "string", "minLength": 1, "maxLength": 4_000},
        "hashtags": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^#[A-Za-z0-9]{1,40}$"},
        },
        "cta_summary": {"type": ["string", "null"], "maxLength": 300},
        "source_references": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "usage"],
                "properties": {
                    "url": {"type": "string", "maxLength": 2_048},
                    "usage": {"const": "supplied_reference_not_fetched"},
                },
            },
        },
        "safety_notes": {
            "type": "array",
            "minItems": 2,
            "maxItems": 6,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
    },
}

SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "scenario_id",
        "scenario_version",
        "artifact_type",
        "platform",
        "draft_text",
        "hashtags",
        "character_count",
        "cta_summary",
        "source_references",
        "safety_notes",
        "publication_status",
        "proposed_actions",
    ],
    "properties": {
        "scenario_id": {"const": SOCIAL_CONTENT_DRAFT_SCENARIO_ID},
        "scenario_version": {"const": 1},
        "artifact_type": {"const": "social_post_draft"},
        "platform": {"const": "LinkedIn"},
        "draft_text": {"type": "string", "minLength": 1, "maxLength": 4_000},
        "hashtags": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^#[A-Za-z0-9]{1,40}$"},
        },
        "character_count": {"type": "integer", "minimum": 1, "maximum": 4_000},
        "cta_summary": {"type": ["string", "null"], "maxLength": 300},
        "source_references": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "usage"],
                "properties": {
                    "url": {"type": "string", "maxLength": 2_048},
                    "usage": {"const": "supplied_reference_not_fetched"},
                },
            },
        },
        "safety_notes": {
            "type": "array",
            "minItems": 2,
            "maxItems": 6,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "publication_status": {"const": "not_published"},
        "proposed_actions": {"type": "array", "maxItems": 0},
    },
}

SOCIAL_CONTENT_DRAFT_FIXTURE = {
    "idea": "Share how governed AI workflows turn a raw marketing idea into a reviewable draft.",
    "audience": "Marketing and platform leaders",
    "tone": "professional",
    "key_points": [
        "Treat external content as untrusted data.",
        "Keep generation separate from publishing authority.",
        "Persist a traceable artifact for review.",
    ],
    "source_urls": ["https://example.com/governed-ai"],
}

SOCIAL_CONTENT_DRAFT_SCENARIO = DemoScenarioDefinition(
    id=SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
    version=1,
    display_name="Social content draft",
    description="Turn a supplied content idea into an inert, reviewable LinkedIn draft artifact.",
    selected_agents=(
        DemoSelectedAgent(
            instance_id=SOCIAL_CONTENT_DRAFT_INSTANCE_ID,
            template_id=SOCIAL_CONTENT_DRAFT_TEMPLATE_ID,
        ),
    ),
    primary_instance_id=SOCIAL_CONTENT_DRAFT_INSTANCE_ID,
    steps=(
        DemoScenarioStep(
            key="create-draft",
            source_order=10,
            dependency_keys=(),
            terminal_result=True,
            kind="model.generate-structured",
            selected_instance_id=SOCIAL_CONTENT_DRAFT_INSTANCE_ID,
            capability_id="cap.model.generate-structured",
            effect="read",
        ),
    ),
    workflow_id=SOCIAL_CONTENT_DRAFT_WORKFLOW_ID,
    effect="read_only",
    input_schema_id=SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA_ID,
    input_schema=SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA,
    output_schema_id=SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA_ID,
    output_schema=SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA,
    fixture=SOCIAL_CONTENT_DRAFT_FIXTURE,
    expected_state_path=("received", "validated", "planned", "executing", "completed"),
    expected_model_calls=1,
    expected_connector_calls=0,
    expected_external_actions=0,
    expected_approvals=0,
    safe_submit_verb="Create draft",
)


def _input_from_request(request: LLMRequest) -> dict[str, JsonValue]:
    if len(request.retrieved_content) != 1 or request.tool_results:
        raise ValueError("social draft renderer requires one untrusted input and no tool results")
    payload = json.loads(request.retrieved_content[0].content)
    if type(payload) is not dict:
        raise ValueError("social draft input must be an object")
    return cast(dict[str, JsonValue], payload)


def render_social_content_draft(
    request: LLMRequest,
    context: DeterministicRenderContext,
) -> dict[str, JsonValue]:
    """Render deterministic business fields; untrusted text is never interpreted as authority."""

    del context
    payload = _input_from_request(request)
    idea = cast(str, payload["idea"])
    audience = cast(str, payload["audience"])
    tone = cast(str, payload["tone"])
    points = cast(list[str], payload["key_points"])
    cta = cast(str | None, payload.get("call_to_action"))
    urls = cast(list[str], payload.get("source_urls", []))

    lines = [idea, "", f"For {audience}:"]
    lines.extend(f"• {point}" for point in points)
    if cta is not None:
        lines.extend(("", cta))
    draft_text = "\n".join(lines)
    hashtags = ["#GovernedAI", "#MarketingOperations", f"#{tone.title()}"]
    return {
        "draft_text": draft_text,
        "hashtags": cast(list[JsonValue], hashtags),
        "cta_summary": cta,
        "source_references": [
            {"url": url, "usage": "supplied_reference_not_fetched"} for url in urls
        ],
        "safety_notes": [
            "All supplied content was treated as untrusted data, not as instructions.",
            "This artifact is a draft and grants no publication authority.",
            "Any supplied URLs were retained as provenance and were not fetched.",
        ],
    }


SOCIAL_CONTENT_DRAFT_RENDERER = RendererRegistration(
    key=RendererKey(
        template_id=SOCIAL_CONTENT_DRAFT_TEMPLATE_ID,
        output_schema_id=SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
    ),
    version="demo-social-content-draft-v1",
    output_schema_hash=canonical_schema_hash(SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA),
    renderer=render_social_content_draft,
)


def finalize_social_content_draft(
    model_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Add trusted deterministic envelope fields after the one model call returns."""

    draft_text = model_payload.get("draft_text")
    if type(draft_text) is not str:
        raise ValueError("social draft model output lacks draft text")
    return {
        "scenario_id": SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
        "scenario_version": 1,
        "artifact_type": "social_post_draft",
        "platform": "LinkedIn",
        "draft_text": draft_text,
        "hashtags": cast(JsonValue, model_payload.get("hashtags")),
        "character_count": len(draft_text),
        "cta_summary": cast(JsonValue, model_payload.get("cta_summary")),
        "source_references": cast(JsonValue, model_payload.get("source_references")),
        "safety_notes": cast(JsonValue, model_payload.get("safety_notes")),
        "publication_status": "not_published",
        "proposed_actions": [],
    }


__all__ = [
    "SOCIAL_CONTENT_DRAFT_FIXTURE",
    "SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA",
    "SOCIAL_CONTENT_DRAFT_INPUT_SCHEMA_ID",
    "SOCIAL_CONTENT_DRAFT_INSTANCE_ID",
    "SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA",
    "SOCIAL_CONTENT_DRAFT_MODEL_OUTPUT_SCHEMA_ID",
    "SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA",
    "SOCIAL_CONTENT_DRAFT_OUTPUT_SCHEMA_ID",
    "SOCIAL_CONTENT_DRAFT_RENDERER",
    "SOCIAL_CONTENT_DRAFT_SCENARIO",
    "SOCIAL_CONTENT_DRAFT_SCENARIO_ID",
    "SOCIAL_CONTENT_DRAFT_TEMPLATE_ID",
    "SOCIAL_CONTENT_DRAFT_WORKFLOW_ID",
    "finalize_social_content_draft",
    "render_social_content_draft",
]
