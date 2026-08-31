"""Trusted definition and deterministic renderer for the Blog content-review demo."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
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

BLOG_CONTENT_REVIEW_SCENARIO_ID = "demo.blog-seo.content-review.v1"
BLOG_CONTENT_REVIEW_TEMPLATE_ID = "tpl.blog-seo.new-content.blog-post-updater"
BLOG_CONTENT_REVIEW_INSTANCE_ID = "inst.blog-seo.new-content.blog-post-updater.01"
BLOG_CONTENT_REVIEW_WORKFLOW_ID = BLOG_CONTENT_REVIEW_SCENARIO_ID
BLOG_CONTENT_REVIEW_INPUT_SCHEMA_ID = "schema.demo.blog-seo.content-review.input.v1"
BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA_ID = "schema.demo.blog-seo.content-review.model-output.v1"
BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA_ID = "schema.demo.blog-seo.content-review.output.v1"

_TIMESTAMP_SCHEMA = {
    "type": "string",
    "format": "date-time",
    "maxLength": 40,
}
_METADATA_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "summary"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "summary": {"type": "string", "minLength": 1, "maxLength": 500},
    },
}

BLOG_CONTENT_REVIEW_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": BLOG_CONTENT_REVIEW_INPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "article_title",
        "canonical_url",
        "supplied_excerpt",
        "last_updated_at",
        "assessment_at",
        "target_keywords",
        "current_product_metadata",
    ],
    "properties": {
        "article_title": {"type": "string", "minLength": 1, "maxLength": 240},
        "canonical_url": {
            "type": "string",
            "format": "uri",
            "minLength": 1,
            "maxLength": 2_048,
        },
        "supplied_excerpt": {"type": "string", "minLength": 1, "maxLength": 8_000},
        "last_updated_at": dict(_TIMESTAMP_SCHEMA),
        "assessment_at": dict(_TIMESTAMP_SCHEMA),
        "target_keywords": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
        },
        "current_product_metadata": {
            "type": "object",
            "additionalProperties": False,
            "required": ["features", "integrations"],
            "properties": {
                "features": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 6,
                    "items": _METADATA_ITEM_SCHEMA,
                },
                "integrations": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 6,
                    "items": _METADATA_ITEM_SCHEMA,
                },
            },
        },
    },
}

_STALENESS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["level", "age_days", "last_updated_at", "assessment_at", "basis"],
    "properties": {
        "level": {"type": "string", "enum": ["current", "review_due", "stale"]},
        "age_days": {"type": "integer", "minimum": 0, "maximum": 3_652_058},
        "last_updated_at": dict(_TIMESTAMP_SCHEMA),
        "assessment_at": dict(_TIMESTAMP_SCHEMA),
        "basis": {"const": "elapsed_utc_days"},
    },
}
_SEO_FINDING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["code", "severity", "finding", "evidence"],
    "properties": {
        "code": {
            "type": "string",
            "enum": ["content_age", "keyword_coverage", "metadata_alignment"],
        },
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "finding": {"type": "string", "minLength": 1, "maxLength": 500},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 500},
    },
}
_CONTENT_GAP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "label", "evidence"],
    "properties": {
        "kind": {"type": "string", "enum": ["keyword", "feature", "integration"]},
        "label": {"type": "string", "minLength": 1, "maxLength": 120},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 500},
    },
}
_RECOMMENDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["priority", "recommendation", "rationale"],
    "properties": {
        "priority": {"type": "integer", "minimum": 1, "maximum": 24},
        "recommendation": {"type": "string", "minLength": 1, "maxLength": 500},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 500},
    },
}
_KEYWORD_COVERAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["keyword", "covered", "occurrence_count"],
    "properties": {
        "keyword": {"type": "string", "minLength": 1, "maxLength": 80},
        "covered": {"type": "boolean"},
        "occurrence_count": {"type": "integer", "minimum": 0, "maximum": 10_000},
    },
}
_SOURCE_REFERENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url", "usage"],
    "properties": {
        "url": {"type": "string", "minLength": 1, "maxLength": 2_048},
        "usage": {"const": "supplied_reference_not_fetched"},
    },
}

_MODEL_REQUIRED = [
    "article_title",
    "staleness",
    "seo_findings",
    "content_gaps",
    "recommendations",
    "keyword_coverage",
    "source_references",
    "assumptions",
]
_MODEL_PROPERTIES = {
    "article_title": {"type": "string", "minLength": 1, "maxLength": 240},
    "staleness": _STALENESS_SCHEMA,
    "seo_findings": {
        "type": "array",
        "minItems": 3,
        "maxItems": 3,
        "items": _SEO_FINDING_SCHEMA,
    },
    "content_gaps": {"type": "array", "maxItems": 20, "items": _CONTENT_GAP_SCHEMA},
    "recommendations": {
        "type": "array",
        "minItems": 1,
        "maxItems": 24,
        "items": _RECOMMENDATION_SCHEMA,
    },
    "keyword_coverage": {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "items": _KEYWORD_COVERAGE_SCHEMA,
    },
    "source_references": {
        "type": "array",
        "minItems": 1,
        "maxItems": 1,
        "items": _SOURCE_REFERENCE_SCHEMA,
    },
    "assumptions": {
        "type": "array",
        "minItems": 4,
        "maxItems": 6,
        "items": {"type": "string", "minLength": 1, "maxLength": 300},
    },
}

BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": _MODEL_REQUIRED,
    "properties": _MODEL_PROPERTIES,
}

BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "scenario_id",
        "scenario_version",
        "artifact_type",
        *_MODEL_REQUIRED,
        "review_status",
        "cms_status",
        "proposed_actions",
    ],
    "properties": {
        "scenario_id": {"const": BLOG_CONTENT_REVIEW_SCENARIO_ID},
        "scenario_version": {"const": 1},
        "artifact_type": {"const": "content_review"},
        **_MODEL_PROPERTIES,
        "review_status": {"const": "advisory_only"},
        "cms_status": {"const": "not_updated"},
        "proposed_actions": {"type": "array", "maxItems": 0},
    },
}

BLOG_CONTENT_REVIEW_FIXTURE = {
    "article_title": "Governed AI workflows for marketing teams",
    "canonical_url": "https://example.com/blog/governed-ai-workflows",
    "supplied_excerpt": (
        "Governed AI helps marketing teams create reviewable drafts with artifact provenance."
    ),
    "last_updated_at": "2025-12-01T00:00:00Z",
    "assessment_at": "2026-08-31T00:00:00Z",
    "target_keywords": ["governed AI", "marketing teams", "approval workflows"],
    "current_product_metadata": {
        "features": [
            {
                "name": "Artifact provenance",
                "summary": "Generated artifacts retain source and provider provenance.",
            },
            {
                "name": "Exact approval gates",
                "summary": "External writes require approval of the exact payload.",
            },
        ],
        "integrations": [
            {
                "name": "CMS review export",
                "summary": (
                    "Review artifacts can be prepared for a later human-controlled CMS workflow."
                ),
            }
        ],
    },
}

BLOG_CONTENT_REVIEW_SCENARIO = DemoScenarioDefinition(
    id=BLOG_CONTENT_REVIEW_SCENARIO_ID,
    version=1,
    display_name="Blog & SEO content review",
    description=(
        "Review supplied article and product metadata for deterministic SEO and content gaps "
        "without fetching or updating a CMS."
    ),
    selected_agents=(
        DemoSelectedAgent(
            instance_id=BLOG_CONTENT_REVIEW_INSTANCE_ID,
            template_id=BLOG_CONTENT_REVIEW_TEMPLATE_ID,
        ),
    ),
    primary_instance_id=BLOG_CONTENT_REVIEW_INSTANCE_ID,
    steps=(
        DemoScenarioStep(
            key="create-review",
            source_order=10,
            dependency_keys=(),
            terminal_result=True,
            kind="model.generate-structured",
            selected_instance_id=BLOG_CONTENT_REVIEW_INSTANCE_ID,
            capability_id="cap.model.generate-structured",
            effect="read",
        ),
    ),
    workflow_id=BLOG_CONTENT_REVIEW_WORKFLOW_ID,
    effect="read_only",
    input_schema_id=BLOG_CONTENT_REVIEW_INPUT_SCHEMA_ID,
    input_schema=BLOG_CONTENT_REVIEW_INPUT_SCHEMA,
    output_schema_id=BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA_ID,
    output_schema=BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA,
    fixture=BLOG_CONTENT_REVIEW_FIXTURE,
    expected_state_path=("received", "validated", "planned", "executing", "completed"),
    expected_model_calls=1,
    expected_connector_calls=0,
    expected_external_actions=0,
    expected_approvals=0,
    safe_submit_verb="Create review",
)


def _parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("Blog review timestamps must use UTC")
    return parsed


def calculate_blog_staleness(
    last_updated_at: str,
    assessment_at: str,
) -> dict[str, JsonValue]:
    """Calculate stable whole-day age from two admitted UTC timestamps."""

    last_updated = _parse_utc_timestamp(last_updated_at)
    assessment = _parse_utc_timestamp(assessment_at)
    if last_updated > assessment:
        raise ValueError("Blog review last-updated timestamp cannot be in the future")
    age_days = int((assessment - last_updated).total_seconds()) // 86_400
    level = "current" if age_days < 90 else "review_due" if age_days < 180 else "stale"
    return {
        "level": level,
        "age_days": age_days,
        "last_updated_at": last_updated_at,
        "assessment_at": assessment_at,
        "basis": "elapsed_utc_days",
    }


def _normalized_phrase(value: str) -> str:
    return " ".join(value.casefold().split())


def _whole_phrase_count(content: str, phrase: str) -> int:
    normalized_content = _normalized_phrase(content)
    normalized_phrase = _normalized_phrase(phrase)
    pattern = re.compile(rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)")
    return len(pattern.findall(normalized_content))


def _input_from_request(request: LLMRequest) -> dict[str, JsonValue]:
    if len(request.retrieved_content) != 1 or request.tool_results:
        raise ValueError("Blog review renderer requires one untrusted input and no tool results")
    payload = json.loads(request.retrieved_content[0].content)
    if type(payload) is not dict:
        raise ValueError("Blog review input must be an object")
    return cast(dict[str, JsonValue], payload)


def _render_blog_content_review_payload(
    payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    title = cast(str, payload["article_title"])
    excerpt = cast(str, payload["supplied_excerpt"])
    canonical_url = cast(str, payload["canonical_url"])
    last_updated_at = cast(str, payload["last_updated_at"])
    assessment_at = cast(str, payload["assessment_at"])
    target_keywords = cast(list[str], payload["target_keywords"])
    metadata = cast(dict[str, list[dict[str, str]]], payload["current_product_metadata"])
    article_content = f"{title}\n{excerpt}"
    staleness = calculate_blog_staleness(last_updated_at, assessment_at)

    keyword_coverage: list[dict[str, JsonValue]] = []
    keyword_gaps: list[dict[str, JsonValue]] = []
    for keyword in target_keywords:
        count = _whole_phrase_count(article_content, keyword)
        keyword_coverage.append(
            {"keyword": keyword, "covered": count > 0, "occurrence_count": count}
        )
        if count == 0:
            keyword_gaps.append(
                {
                    "kind": "keyword",
                    "label": keyword,
                    "evidence": (
                        "The target keyword was not found as a whole phrase in the supplied "
                        "title or excerpt."
                    ),
                }
            )

    metadata_gaps: list[dict[str, JsonValue]] = []
    metadata_total = 0
    metadata_covered = 0
    for kind, records in (
        ("feature", metadata["features"]),
        ("integration", metadata["integrations"]),
    ):
        for record in records:
            metadata_total += 1
            name = record["name"]
            if _whole_phrase_count(article_content, name) > 0:
                metadata_covered += 1
            else:
                metadata_gaps.append(
                    {
                        "kind": kind,
                        "label": name,
                        "evidence": (
                            f"The supplied current {kind} name was not found as a whole phrase "
                            "in the supplied title or excerpt."
                        ),
                    }
                )

    covered_keywords = sum(1 for item in keyword_coverage if item["covered"] is True)
    keyword_severity = (
        "low"
        if covered_keywords == len(keyword_coverage)
        else "high"
        if covered_keywords == 0
        else "medium"
    )
    metadata_severity = (
        "low" if not metadata_gaps else "medium" if len(metadata_gaps) == 1 else "high"
    )
    age_level = cast(str, staleness["level"])
    age_severity = (
        "low" if age_level == "current" else "medium" if age_level == "review_due" else "high"
    )
    age_days = cast(int, staleness["age_days"])
    content_gaps = keyword_gaps + metadata_gaps

    recommendations: list[dict[str, JsonValue]] = []
    if age_level != "current":
        recommendations.append(
            {
                "priority": 1,
                "recommendation": "Review and refresh the supplied article content.",
                "rationale": (
                    f"The article is classified {age_level} at {age_days} elapsed UTC days."
                ),
            }
        )
    for gap in content_gaps:
        kind = cast(str, gap["kind"])
        label = cast(str, gap["label"])
        recommendations.append(
            {
                "priority": len(recommendations) + 1,
                "recommendation": f"Review the supplied article for missing {kind}: {label}.",
                "rationale": cast(str, gap["evidence"]),
            }
        )
    recommendations.append(
        {
            "priority": len(recommendations) + 1,
            "recommendation": "Have a human review this advisory artifact before any CMS change.",
            "rationale": "This deterministic review grants no CMS update or upload authority.",
        }
    )

    return {
        "article_title": title,
        "staleness": staleness,
        "seo_findings": [
            {
                "code": "content_age",
                "severity": age_severity,
                "finding": f"The supplied article is classified {age_level}.",
                "evidence": (
                    f"It is {age_days} elapsed UTC days old; current is 0-89, "
                    "review_due is 90-179, and stale is 180 or more."
                ),
            },
            {
                "code": "keyword_coverage",
                "severity": keyword_severity,
                "finding": (
                    f"{covered_keywords} of {len(keyword_coverage)} supplied target keywords "
                    "are covered."
                ),
                "evidence": (
                    "Coverage uses case-folded whole-phrase matching over only the supplied "
                    "title and excerpt."
                ),
            },
            {
                "code": "metadata_alignment",
                "severity": metadata_severity,
                "finding": (
                    f"{metadata_covered} of {metadata_total} supplied current feature and "
                    "integration names are covered."
                ),
                "evidence": (
                    "The metadata was supplied with the fixture and was not independently verified."
                ),
            },
        ],
        "content_gaps": cast(list[JsonValue], content_gaps),
        "recommendations": cast(list[JsonValue], recommendations),
        "keyword_coverage": cast(list[JsonValue], keyword_coverage),
        "source_references": [{"url": canonical_url, "usage": "supplied_reference_not_fetched"}],
        "assumptions": [
            "The review uses only the supplied article and product metadata.",
            "The canonical URL was retained as provenance and was not fetched.",
            "Keyword coverage is lexical evidence, not a search-ranking measurement.",
            "No CMS update, upload, search query, or external write was performed.",
        ],
    }


def render_blog_content_review(
    request: LLMRequest,
    context: DeterministicRenderContext,
) -> dict[str, JsonValue]:
    """Render one advisory review without interpreting untrusted text as authority."""

    del context
    return _render_blog_content_review_payload(_input_from_request(request))


BLOG_CONTENT_REVIEW_RENDERER = RendererRegistration(
    key=RendererKey(
        template_id=BLOG_CONTENT_REVIEW_TEMPLATE_ID,
        output_schema_id=BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA_ID,
    ),
    version="demo-blog-content-review-v1",
    output_schema_hash=canonical_schema_hash(BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA),
    renderer=render_blog_content_review,
)


def finalize_blog_content_review(
    model_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Add trusted advisory/no-write envelope fields after the model call."""

    return {
        "scenario_id": BLOG_CONTENT_REVIEW_SCENARIO_ID,
        "scenario_version": 1,
        "artifact_type": "content_review",
        **cast(dict[str, JsonValue], dict(model_payload)),
        "review_status": "advisory_only",
        "cms_status": "not_updated",
        "proposed_actions": [],
    }


def expected_blog_content_review_artifact(
    input_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Recompute every deterministic business field for persisted-evidence validation."""

    normalized = json.loads(canonical_json_bytes(input_payload))
    if type(normalized) is not dict:
        raise ValueError("Blog review admitted input must be an object")
    return finalize_blog_content_review(_render_blog_content_review_payload(normalized))


__all__ = [
    "BLOG_CONTENT_REVIEW_FIXTURE",
    "BLOG_CONTENT_REVIEW_INPUT_SCHEMA",
    "BLOG_CONTENT_REVIEW_INPUT_SCHEMA_ID",
    "BLOG_CONTENT_REVIEW_INSTANCE_ID",
    "BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA",
    "BLOG_CONTENT_REVIEW_MODEL_OUTPUT_SCHEMA_ID",
    "BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA",
    "BLOG_CONTENT_REVIEW_OUTPUT_SCHEMA_ID",
    "BLOG_CONTENT_REVIEW_RENDERER",
    "BLOG_CONTENT_REVIEW_SCENARIO",
    "BLOG_CONTENT_REVIEW_SCENARIO_ID",
    "BLOG_CONTENT_REVIEW_TEMPLATE_ID",
    "BLOG_CONTENT_REVIEW_WORKFLOW_ID",
    "calculate_blog_staleness",
    "expected_blog_content_review_artifact",
    "finalize_blog_content_review",
    "render_blog_content_review",
]
