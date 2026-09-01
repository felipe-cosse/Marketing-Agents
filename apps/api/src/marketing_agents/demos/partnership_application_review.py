"""Trusted definition and deterministic renderer for the Partnership review demo."""

from __future__ import annotations

import json
from collections.abc import Mapping
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

PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID = "demo.partnerships.application-review.v1"
PARTNERSHIP_APPLICATION_REVIEW_TEMPLATE_ID = (
    "tpl.partnerships.implementation-partners.partner-application-reviewer"
)
PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID = (
    "inst.partnerships.implementation-partners.partner-application-reviewer.01"
)
PARTNERSHIP_APPLICATION_REVIEW_WORKFLOW_ID = PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID
PARTNERSHIP_APPLICATION_REVIEW_INPUT_SCHEMA_ID = (
    "schema.demo.partnerships.application-review.input.v1"
)
PARTNERSHIP_APPLICATION_REVIEW_MODEL_OUTPUT_SCHEMA_ID = (
    "schema.demo.partnerships.application-review.model-output.v1"
)
PARTNERSHIP_APPLICATION_REVIEW_OUTPUT_SCHEMA_ID = (
    "schema.demo.partnerships.application-review.output.v1"
)

PARTNERSHIP_NO_AUTOMATIC_DECISION_NOTE = (
    "This recommendation is advisory only and does not automatically accept or reject the "
    "applicant."
)

_STABLE_ID_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 80,
    "pattern": "^[a-z0-9][a-z0-9._-]{0,79}$",
}
_APPLICANT_ID_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 120,
    "pattern": "^[a-z0-9][a-z0-9._-]{0,119}$",
    "x-sensitive": True,
}
_DERIVED_ID_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 120,
    "pattern": "^[a-z0-9][a-z0-9._-]{0,119}$",
}
_BOUNDED_TEXT_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 500}
_RISK_FLAGS = [
    "compliance_gap",
    "delivery_capacity_gap",
    "security_gap",
    "unverified_claim",
]

_ORGANIZATION_METADATA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "x-sensitive": True,
    "required": [
        "organization_name",
        "organization_type",
        "organization_summary",
        "website_reference",
    ],
    "properties": {
        "organization_name": {"type": "string", "minLength": 1, "maxLength": 160},
        "organization_type": {
            "type": "string",
            "enum": [
                "systems_integrator",
                "consultancy",
                "technology_provider",
                "training_provider",
                "other",
            ],
        },
        "organization_summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2_000,
        },
        "website_reference": {
            "type": "string",
            "format": "uri",
            "minLength": 1,
            "maxLength": 2_048,
        },
    },
}
_DECLARED_CAPABILITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["capability_id", "label"],
    "properties": {
        "capability_id": dict(_STABLE_ID_SCHEMA),
        "label": {"type": "string", "minLength": 1, "maxLength": 160},
    },
}
_EVIDENCE_RECORD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "evidence_id",
        "evidence_type",
        "summary",
        "supports_criterion_ids",
        "risk_flags",
    ],
    "properties": {
        "evidence_id": dict(_STABLE_ID_SCHEMA),
        "evidence_type": {
            "type": "string",
            "enum": [
                "case_study",
                "certification",
                "customer_reference",
                "program_history",
                "security_attestation",
                "other",
            ],
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": 2_000},
        "supports_criterion_ids": {
            "type": "array",
            "maxItems": 24,
            "items": dict(_STABLE_ID_SCHEMA),
        },
        "risk_flags": {
            "type": "array",
            "maxItems": len(_RISK_FLAGS),
            "items": {"type": "string", "enum": _RISK_FLAGS},
        },
    },
}
_PROGRAM_CRITERION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criterion_id", "description", "required"],
    "properties": {
        "criterion_id": dict(_STABLE_ID_SCHEMA),
        "description": dict(_BOUNDED_TEXT_SCHEMA),
        "required": {"type": "boolean"},
    },
}
_MISSING_INFORMATION_INDICATOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["indicator_id", "description", "related_criterion_ids"],
    "properties": {
        "indicator_id": dict(_STABLE_ID_SCHEMA),
        "description": dict(_BOUNDED_TEXT_SCHEMA),
        "related_criterion_ids": {
            "type": "array",
            "maxItems": 24,
            "items": dict(_STABLE_ID_SCHEMA),
        },
    },
}

PARTNERSHIP_APPLICATION_REVIEW_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": PARTNERSHIP_APPLICATION_REVIEW_INPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "applicant_id",
        "organization_metadata",
        "declared_capabilities",
        "declared_regions",
        "evidence_records",
        "program_criteria",
        "program_constraints",
        "missing_information_indicators",
    ],
    "properties": {
        "applicant_id": dict(_APPLICANT_ID_SCHEMA),
        "organization_metadata": _ORGANIZATION_METADATA_SCHEMA,
        "declared_capabilities": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": _DECLARED_CAPABILITY_SCHEMA,
        },
        "declared_regions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": dict(_STABLE_ID_SCHEMA),
        },
        "evidence_records": {
            "type": "array",
            "maxItems": 24,
            "x-sensitive": True,
            "items": _EVIDENCE_RECORD_SCHEMA,
        },
        "program_criteria": {
            "type": "array",
            "minItems": 1,
            "maxItems": 24,
            "items": _PROGRAM_CRITERION_SCHEMA,
        },
        "program_constraints": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "eligible_regions",
                "required_capability_ids",
                "minimum_evidence_records",
                "disqualifying_risk_flags",
            ],
            "properties": {
                "eligible_regions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": dict(_STABLE_ID_SCHEMA),
                },
                "required_capability_ids": {
                    "type": "array",
                    "maxItems": 16,
                    "items": dict(_STABLE_ID_SCHEMA),
                },
                "minimum_evidence_records": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 24,
                },
                "disqualifying_risk_flags": {
                    "type": "array",
                    "maxItems": len(_RISK_FLAGS),
                    "items": {"type": "string", "enum": _RISK_FLAGS},
                },
            },
        },
        "missing_information_indicators": {
            "type": "array",
            "maxItems": 24,
            "items": _MISSING_INFORMATION_INDICATOR_SCHEMA,
        },
    },
}

_EVIDENCE_RATIONALE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criterion_id", "assessment", "evidence_ids", "rationale"],
    "properties": {
        "criterion_id": dict(_STABLE_ID_SCHEMA),
        "assessment": {
            "type": "string",
            "enum": ["met", "insufficient_information", "not_evidenced"],
        },
        "evidence_ids": {
            "type": "array",
            "maxItems": 24,
            "items": dict(_STABLE_ID_SCHEMA),
        },
        "rationale": dict(_BOUNDED_TEXT_SCHEMA),
    },
}
_CONFIDENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["level", "basis"],
    "properties": {
        "level": {"type": "string", "enum": ["low", "medium", "high"]},
        "basis": dict(_BOUNDED_TEXT_SCHEMA),
    },
}
_UNCERTAINTY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["uncertainty_id", "description", "related_criterion_ids"],
    "properties": {
        "uncertainty_id": dict(_DERIVED_ID_SCHEMA),
        "description": dict(_BOUNDED_TEXT_SCHEMA),
        "related_criterion_ids": {
            "type": "array",
            "maxItems": 24,
            "items": dict(_STABLE_ID_SCHEMA),
        },
    },
}
_RISK_CONCERN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["risk_code", "severity", "description", "evidence_ids"],
    "properties": {
        "risk_code": dict(_DERIVED_ID_SCHEMA),
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "description": dict(_BOUNDED_TEXT_SCHEMA),
        "evidence_ids": {
            "type": "array",
            "maxItems": 24,
            "items": dict(_STABLE_ID_SCHEMA),
        },
    },
}
_FOLLOW_UP_QUESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["indicator_id", "question"],
    "properties": {
        "indicator_id": dict(_DERIVED_ID_SCHEMA),
        "question": {"type": "string", "minLength": 1, "maxLength": 700},
    },
}
_OUTPUT_MISSING_INFORMATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["indicator_id", "description", "related_criterion_ids"],
    "properties": {
        "indicator_id": dict(_DERIVED_ID_SCHEMA),
        "description": dict(_BOUNDED_TEXT_SCHEMA),
        "related_criterion_ids": {
            "type": "array",
            "maxItems": 24,
            "items": dict(_STABLE_ID_SCHEMA),
        },
    },
}
_SOURCE_REFERENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reference_type", "url", "usage"],
    "properties": {
        "reference_type": {"const": "applicant_website"},
        "url": {"type": "string", "format": "uri", "minLength": 1, "maxLength": 2_048},
        "usage": {"const": "supplied_reference_not_fetched"},
    },
}
_ADVISORY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "automated_decision", "external_action"],
    "properties": {
        "status": {"const": "advisory_only"},
        "automated_decision": {"const": False},
        "external_action": {"const": "none"},
    },
}

_MODEL_REQUIRED = [
    "applicant_id",
    "recommendation",
    "evidence_linked_rationale",
    "confidence",
    "uncertainty",
    "risks_concerns",
    "missing_information",
    "follow_up_questions",
    "source_references",
]
_MODEL_PROPERTIES = {
    "applicant_id": dict(_APPLICANT_ID_SCHEMA),
    "recommendation": {"type": "string", "enum": ["accept", "reject", "needs_information"]},
    "evidence_linked_rationale": {
        "type": "array",
        "minItems": 1,
        "maxItems": 24,
        "items": _EVIDENCE_RATIONALE_SCHEMA,
    },
    "confidence": _CONFIDENCE_SCHEMA,
    "uncertainty": {"type": "array", "maxItems": 64, "items": _UNCERTAINTY_SCHEMA},
    "risks_concerns": {"type": "array", "maxItems": 64, "items": _RISK_CONCERN_SCHEMA},
    "missing_information": {
        "type": "array",
        "maxItems": 64,
        "items": _OUTPUT_MISSING_INFORMATION_SCHEMA,
    },
    "follow_up_questions": {
        "type": "array",
        "maxItems": 64,
        "items": _FOLLOW_UP_QUESTION_SCHEMA,
    },
    "source_references": {
        "type": "array",
        "minItems": 1,
        "maxItems": 1,
        "items": _SOURCE_REFERENCE_SCHEMA,
    },
}

PARTNERSHIP_APPLICATION_REVIEW_MODEL_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": PARTNERSHIP_APPLICATION_REVIEW_MODEL_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": _MODEL_REQUIRED,
    "properties": _MODEL_PROPERTIES,
}

PARTNERSHIP_APPLICATION_REVIEW_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": PARTNERSHIP_APPLICATION_REVIEW_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "scenario_id",
        "scenario_version",
        "artifact_type",
        *_MODEL_REQUIRED,
        "advisory_only",
        "advisory",
        "research_status",
        "partner_record_status",
        "notification_status",
        "no_automatic_decision_note",
        "proposed_actions",
    ],
    "properties": {
        "scenario_id": {"const": PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID},
        "scenario_version": {"const": 1},
        "artifact_type": {"const": "partner_review_recommendation"},
        **_MODEL_PROPERTIES,
        "advisory_only": {"const": True},
        "advisory": _ADVISORY_SCHEMA,
        "research_status": {"const": "supplied_evidence_only"},
        "partner_record_status": {"const": "not_mutated"},
        "notification_status": {"const": "not_sent"},
        "no_automatic_decision_note": {"const": PARTNERSHIP_NO_AUTOMATIC_DECISION_NOTE},
        "proposed_actions": {"type": "array", "maxItems": 0},
    },
}

PARTNERSHIP_APPLICATION_REVIEW_FIXTURE = {
    "applicant_id": "applicant.partnership-demo-0001",
    "organization_metadata": {
        "organization_name": "Northstar Systems Demo",
        "organization_type": "systems_integrator",
        "organization_summary": (
            "Synthetic implementation partner specializing in governed automation."
        ),
        "website_reference": "https://example.com/partners/northstar-systems",
    },
    "declared_capabilities": [
        {
            "capability_id": "implementation.governance",
            "label": "AI governance implementation",
        },
        {
            "capability_id": "integration.delivery",
            "label": "Integration delivery",
        },
    ],
    "declared_regions": ["europe", "north_america"],
    "evidence_records": [
        {
            "evidence_id": "evidence.partner-demo-001",
            "evidence_type": "case_study",
            "summary": "Synthetic case study documents a governed deployment.",
            "supports_criterion_ids": ["criterion.governed-delivery"],
            "risk_flags": [],
        },
        {
            "evidence_id": "evidence.partner-demo-002",
            "evidence_type": "certification",
            "summary": "Synthetic training record documents integration delivery readiness.",
            "supports_criterion_ids": ["criterion.integration-readiness"],
            "risk_flags": [],
        },
    ],
    "program_criteria": [
        {
            "criterion_id": "criterion.governed-delivery",
            "description": "Evidence of governed delivery.",
            "required": True,
        },
        {
            "criterion_id": "criterion.integration-readiness",
            "description": "Evidence of integration delivery readiness.",
            "required": True,
        },
        {
            "criterion_id": "criterion.security-attestation",
            "description": "Current security attestation.",
            "required": True,
        },
    ],
    "program_constraints": {
        "eligible_regions": ["europe", "north_america"],
        "required_capability_ids": ["implementation.governance", "integration.delivery"],
        "minimum_evidence_records": 3,
        "disqualifying_risk_flags": ["compliance_gap", "security_gap"],
    },
    "missing_information_indicators": [
        {
            "indicator_id": "missing.security-attestation",
            "description": "A current security attestation was not supplied.",
            "related_criterion_ids": ["criterion.security-attestation"],
        }
    ],
}

PARTNERSHIP_APPLICATION_REVIEW_SCENARIO = DemoScenarioDefinition(
    id=PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID,
    version=1,
    display_name="Partnership application review",
    description=(
        "Create a deterministic advisory recommendation from supplied partner application "
        "evidence without external research, applicant notification, record mutation, or an "
        "automated decision."
    ),
    selected_agents=(
        DemoSelectedAgent(
            instance_id=PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID,
            template_id=PARTNERSHIP_APPLICATION_REVIEW_TEMPLATE_ID,
        ),
    ),
    primary_instance_id=PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID,
    steps=(
        DemoScenarioStep(
            key="create-advisory-review",
            source_order=10,
            dependency_keys=(),
            terminal_result=True,
            kind="model.generate-structured",
            selected_instance_id=PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID,
            capability_id="cap.model.generate-structured",
            effect="read",
        ),
    ),
    workflow_id=PARTNERSHIP_APPLICATION_REVIEW_WORKFLOW_ID,
    effect="read_only",
    input_schema_id=PARTNERSHIP_APPLICATION_REVIEW_INPUT_SCHEMA_ID,
    input_schema=PARTNERSHIP_APPLICATION_REVIEW_INPUT_SCHEMA,
    output_schema_id=PARTNERSHIP_APPLICATION_REVIEW_OUTPUT_SCHEMA_ID,
    output_schema=PARTNERSHIP_APPLICATION_REVIEW_OUTPUT_SCHEMA,
    fixture=PARTNERSHIP_APPLICATION_REVIEW_FIXTURE,
    expected_state_path=("received", "validated", "planned", "executing", "completed"),
    expected_model_calls=1,
    expected_connector_calls=0,
    expected_external_actions=0,
    expected_approvals=0,
    safe_submit_verb="Create advisory review",
)

_RISK_DESCRIPTIONS = {
    "compliance_gap": "The supplied evidence identifies a compliance gap.",
    "delivery_capacity_gap": "The supplied evidence identifies a delivery-capacity gap.",
    "security_gap": "The supplied evidence identifies a security gap.",
    "unverified_claim": (
        "The supplied evidence contains a claim that is not independently verified."
    ),
}


def _input_from_request(request: LLMRequest) -> dict[str, JsonValue]:
    if len(request.retrieved_content) != 1 or request.tool_results:
        raise ValueError("Partnership review renderer requires one untrusted input and no tools")
    payload = json.loads(request.retrieved_content[0].content)
    if type(payload) is not dict:
        raise ValueError("Partnership review input must be an object")
    return cast(dict[str, JsonValue], payload)


def _render_partnership_application_review_payload(
    payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    applicant_id = cast(str, payload["applicant_id"])
    organization = cast(dict[str, Any], payload["organization_metadata"])
    capabilities = cast(list[dict[str, Any]], payload["declared_capabilities"])
    declared_regions = cast(list[str], payload["declared_regions"])
    evidence_records = cast(list[dict[str, Any]], payload["evidence_records"])
    criteria = cast(list[dict[str, Any]], payload["program_criteria"])
    constraints = cast(dict[str, Any], payload["program_constraints"])
    supplied_indicators = cast(list[dict[str, Any]], payload["missing_information_indicators"])

    declared_capability_ids = {cast(str, item["capability_id"]) for item in capabilities}
    required_capability_ids = cast(list[str], constraints["required_capability_ids"])
    missing_required_capabilities = sorted(set(required_capability_ids) - declared_capability_ids)
    eligible_regions = cast(list[str], constraints["eligible_regions"])
    matching_regions = sorted(set(declared_regions).intersection(eligible_regions))

    evidence_ids_by_criterion: dict[str, list[str]] = {}
    evidence_ids_by_risk: dict[str, list[str]] = {}
    for record in evidence_records:
        evidence_id = cast(str, record["evidence_id"])
        for criterion_id in cast(list[str], record["supports_criterion_ids"]):
            evidence_ids_by_criterion.setdefault(criterion_id, []).append(evidence_id)
        for risk_flag in cast(list[str], record["risk_flags"]):
            evidence_ids_by_risk.setdefault(risk_flag, []).append(evidence_id)
    for values in evidence_ids_by_criterion.values():
        values.sort()
    for values in evidence_ids_by_risk.values():
        values.sort()

    evidence_linked_rationale: list[dict[str, JsonValue]] = []
    required_criteria_without_evidence: list[str] = []
    for criterion in criteria:
        criterion_id = cast(str, criterion["criterion_id"])
        required = cast(bool, criterion["required"])
        linked_evidence = evidence_ids_by_criterion.get(criterion_id, [])
        if linked_evidence:
            assessment = "met"
            rationale = (
                f"{len(linked_evidence)} supplied evidence record(s) reference this criterion."
            )
        elif required:
            assessment = "insufficient_information"
            rationale = "No supplied evidence record references this required criterion."
            required_criteria_without_evidence.append(criterion_id)
        else:
            assessment = "not_evidenced"
            rationale = "No supplied evidence record references this optional criterion."
        evidence_linked_rationale.append(
            {
                "criterion_id": criterion_id,
                "assessment": assessment,
                "evidence_ids": cast(list[JsonValue], linked_evidence),
                "rationale": rationale,
            }
        )

    missing_information: list[dict[str, JsonValue]] = [
        {
            "indicator_id": cast(str, item["indicator_id"]),
            "description": cast(str, item["description"]),
            "related_criterion_ids": cast(list[JsonValue], item["related_criterion_ids"]),
        }
        for item in supplied_indicators
    ]
    covered_missing_criteria = {
        criterion_id
        for item in missing_information
        for criterion_id in cast(list[str], item["related_criterion_ids"])
    }
    for criterion_id in required_criteria_without_evidence:
        if criterion_id not in covered_missing_criteria:
            missing_information.append(
                {
                    "indicator_id": f"system.missing.criterion.{criterion_id}",
                    "description": (
                        f"Evidence for required criterion {criterion_id} was not supplied."
                    ),
                    "related_criterion_ids": [criterion_id],
                }
            )
    minimum_evidence_records = cast(int, constraints["minimum_evidence_records"])
    if len(evidence_records) < minimum_evidence_records:
        missing_information.append(
            {
                "indicator_id": "system.missing.minimum-evidence-records",
                "description": (
                    f"The program requires at least {minimum_evidence_records} evidence record(s); "
                    f"{len(evidence_records)} were supplied."
                ),
                "related_criterion_ids": [],
            }
        )
    missing_information.sort(key=lambda item: cast(str, item["indicator_id"]))

    disqualifying_flags = set(cast(list[str], constraints["disqualifying_risk_flags"]))
    supplied_disqualifying_risks = sorted(disqualifying_flags.intersection(evidence_ids_by_risk))
    reject = bool(
        supplied_disqualifying_risks or missing_required_capabilities or not matching_regions
    )
    recommendation = (
        "reject" if reject else "needs_information" if missing_information else "accept"
    )

    risks_concerns: list[dict[str, JsonValue]] = []
    for risk_flag in sorted(evidence_ids_by_risk):
        risks_concerns.append(
            {
                "risk_code": risk_flag,
                "severity": "high" if risk_flag in disqualifying_flags else "medium",
                "description": _RISK_DESCRIPTIONS[risk_flag],
                "evidence_ids": cast(list[JsonValue], evidence_ids_by_risk[risk_flag]),
            }
        )
    for capability_id in missing_required_capabilities:
        risks_concerns.append(
            {
                "risk_code": f"missing-required-capability.{capability_id}",
                "severity": "high",
                "description": f"Required capability {capability_id} was not declared.",
                "evidence_ids": [],
            }
        )
    if not matching_regions:
        risks_concerns.append(
            {
                "risk_code": "no-eligible-region",
                "severity": "high",
                "description": "No declared region matches the supplied eligible regions.",
                "evidence_ids": [],
            }
        )
    if len(evidence_records) < minimum_evidence_records:
        risks_concerns.append(
            {
                "risk_code": "insufficient-evidence-records",
                "severity": "medium",
                "description": "The supplied evidence count is below the program minimum.",
                "evidence_ids": [cast(str, record["evidence_id"]) for record in evidence_records],
            }
        )
    for criterion_id in required_criteria_without_evidence:
        risks_concerns.append(
            {
                "risk_code": f"required-criterion-unverified.{criterion_id}",
                "severity": "medium",
                "description": f"Required criterion {criterion_id} has no linked evidence.",
                "evidence_ids": [],
            }
        )
    risks_concerns.sort(key=lambda item: cast(str, item["risk_code"]))

    uncertainty = [
        {
            "uncertainty_id": f"uncertainty.{cast(str, item['indicator_id'])}",
            "description": cast(str, item["description"]),
            "related_criterion_ids": cast(list[str], item["related_criterion_ids"]),
        }
        for item in missing_information
    ]
    follow_up_questions = [
        {
            "indicator_id": cast(str, item["indicator_id"]),
            "question": f"Please provide the missing information: {cast(str, item['description'])}",
        }
        for item in missing_information
    ]

    confidence: dict[str, JsonValue]
    if recommendation == "reject":
        confidence = {
            "level": "high",
            "basis": (
                "The advisory rejection follows only explicit supplied program constraints and "
                "risk flags."
            ),
        }
    elif recommendation == "needs_information":
        confidence = {
            "level": "low",
            "basis": (
                f"The supplied application has {len(missing_information)} unresolved evidence or "
                "information gap(s)."
            ),
        }
    else:
        confidence = {
            "level": "medium" if risks_concerns else "high",
            "basis": (
                "All supplied hard constraints and required evidence checks passed; this remains "
                "an advisory assessment of supplied data only."
            ),
        }

    return {
        "applicant_id": applicant_id,
        "recommendation": recommendation,
        "evidence_linked_rationale": cast(list[JsonValue], evidence_linked_rationale),
        "confidence": confidence,
        "uncertainty": cast(list[JsonValue], uncertainty),
        "risks_concerns": cast(list[JsonValue], risks_concerns),
        "missing_information": cast(list[JsonValue], missing_information),
        "follow_up_questions": cast(list[JsonValue], follow_up_questions),
        "source_references": [
            {
                "reference_type": "applicant_website",
                "url": cast(str, organization["website_reference"]),
                "usage": "supplied_reference_not_fetched",
            }
        ],
    }


def render_partnership_application_review(
    request: LLMRequest,
    context: DeterministicRenderContext,
) -> dict[str, JsonValue]:
    """Render one supplied-evidence-only recommendation with no action authority."""

    del context
    return _render_partnership_application_review_payload(_input_from_request(request))


PARTNERSHIP_APPLICATION_REVIEW_RENDERER = RendererRegistration(
    key=RendererKey(
        template_id=PARTNERSHIP_APPLICATION_REVIEW_TEMPLATE_ID,
        output_schema_id=PARTNERSHIP_APPLICATION_REVIEW_MODEL_OUTPUT_SCHEMA_ID,
    ),
    version="demo-partnership-application-review-v1",
    output_schema_hash=canonical_schema_hash(PARTNERSHIP_APPLICATION_REVIEW_MODEL_OUTPUT_SCHEMA),
    renderer=render_partnership_application_review,
)


def finalize_partnership_application_review(
    model_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Add trusted advisory and zero-action status after deterministic generation."""

    return {
        **cast(dict[str, JsonValue], dict(model_payload)),
        "scenario_id": PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID,
        "scenario_version": 1,
        "artifact_type": "partner_review_recommendation",
        "advisory_only": True,
        "advisory": {
            "status": "advisory_only",
            "automated_decision": False,
            "external_action": "none",
        },
        "research_status": "supplied_evidence_only",
        "partner_record_status": "not_mutated",
        "notification_status": "not_sent",
        "no_automatic_decision_note": PARTNERSHIP_NO_AUTOMATIC_DECISION_NOTE,
        "proposed_actions": [],
    }


def expected_partnership_application_review_artifact(
    input_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Recompute every business field for persisted-artifact equivalence checks."""

    normalized = json.loads(canonical_json_bytes(input_payload))
    if type(normalized) is not dict:
        raise ValueError("Partnership review admitted input must be an object")
    return finalize_partnership_application_review(
        _render_partnership_application_review_payload(normalized)
    )


__all__ = [
    "PARTNERSHIP_APPLICATION_REVIEW_FIXTURE",
    "PARTNERSHIP_APPLICATION_REVIEW_INPUT_SCHEMA",
    "PARTNERSHIP_APPLICATION_REVIEW_INPUT_SCHEMA_ID",
    "PARTNERSHIP_APPLICATION_REVIEW_INSTANCE_ID",
    "PARTNERSHIP_APPLICATION_REVIEW_MODEL_OUTPUT_SCHEMA",
    "PARTNERSHIP_APPLICATION_REVIEW_MODEL_OUTPUT_SCHEMA_ID",
    "PARTNERSHIP_APPLICATION_REVIEW_OUTPUT_SCHEMA",
    "PARTNERSHIP_APPLICATION_REVIEW_OUTPUT_SCHEMA_ID",
    "PARTNERSHIP_APPLICATION_REVIEW_RENDERER",
    "PARTNERSHIP_APPLICATION_REVIEW_SCENARIO",
    "PARTNERSHIP_APPLICATION_REVIEW_SCENARIO_ID",
    "PARTNERSHIP_APPLICATION_REVIEW_TEMPLATE_ID",
    "PARTNERSHIP_APPLICATION_REVIEW_WORKFLOW_ID",
    "PARTNERSHIP_NO_AUTOMATIC_DECISION_NOTE",
    "expected_partnership_application_review_artifact",
    "finalize_partnership_application_review",
    "render_partnership_application_review",
]
