"""Trusted definition and deterministic renderer for the Community reminder demo."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import JsonValue

from marketing_agents.application.ports.llm import LLMRequest
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.domain.validation import require_iana_timezone
from marketing_agents.infrastructure.adapters.llm.deterministic import (
    DeterministicRenderContext,
    RendererKey,
    RendererRegistration,
)

from .contracts import DemoScenarioDefinition, DemoScenarioStep, DemoSelectedAgent

COMMUNITY_REMINDER_DRAFT_SCENARIO_ID = "demo.community.reminder-draft.v1"
COMMUNITY_REMINDER_DRAFT_TEMPLATE_ID = "tpl.community.events.live-session-reminder"
COMMUNITY_REMINDER_DRAFT_INSTANCE_ID = "inst.community.events.live-session-reminder.01"
COMMUNITY_REMINDER_DRAFT_WORKFLOW_ID = COMMUNITY_REMINDER_DRAFT_SCENARIO_ID
COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA_ID = "schema.demo.community.reminder-draft.input.v1"
COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA_ID = (
    "schema.demo.community.reminder-draft.model-output.v1"
)
COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA_ID = "schema.demo.community.reminder-draft.output.v1"

_STABLE_REFERENCE_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 120,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$",
}
_LOCAL_TIMESTAMP_PATTERN = "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$"
_UTC_TIMESTAMP_SCHEMA = {
    "type": "string",
    "format": "date-time",
    "maxLength": 40,
}

COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "event_id",
        "event_name",
        "signup_event_id",
        "admitted_source",
        "signup_at",
        "session_local_start",
        "session_timezone",
        "reminder_offset_minutes",
        "attendee_display_name",
        "channel_label",
        "event_details",
    ],
    "properties": {
        "event_id": dict(_STABLE_REFERENCE_SCHEMA),
        "event_name": {"type": "string", "minLength": 1, "maxLength": 160},
        "signup_event_id": dict(_STABLE_REFERENCE_SCHEMA),
        "admitted_source": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
        },
        "signup_at": dict(_UTC_TIMESTAMP_SCHEMA),
        "session_local_start": {
            "type": "string",
            "pattern": _LOCAL_TIMESTAMP_PATTERN,
            "maxLength": 19,
        },
        "session_timezone": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": "^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$",
        },
        "reminder_offset_minutes": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10_080,
        },
        "attendee_display_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "x-sensitive": True,
        },
        "channel_label": {
            "type": "string",
            "enum": ["email", "community", "in_app"],
        },
        "event_details": {"type": "string", "minLength": 1, "maxLength": 2_000},
    },
}

_SOURCE_REFERENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reference_type", "reference_id", "usage"],
    "properties": {
        "reference_type": {"type": "string", "enum": ["event", "signup_event"]},
        "reference_id": dict(_STABLE_REFERENCE_SCHEMA),
        "usage": {"const": "supplied_input"},
    },
}
_SIGNUP_PROVENANCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["signup_event_id", "admitted_source", "signup_at"],
    "properties": {
        "signup_event_id": dict(_STABLE_REFERENCE_SCHEMA),
        "admitted_source": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
        },
        "signup_at": dict(_UTC_TIMESTAMP_SCHEMA),
    },
}
_MODEL_REQUIRED = [
    "event_id",
    "event_name",
    "attendee_reference",
    "draft_subject",
    "draft_message",
    "session_local_start",
    "session_timezone",
    "session_start_at_utc",
    "reminder_offset_minutes",
    "recommended_send_at_utc",
    "channel_label",
    "source_references",
    "signup_provenance",
    "safety_notes",
]
_MODEL_PROPERTIES = {
    "event_id": dict(_STABLE_REFERENCE_SCHEMA),
    "event_name": {"type": "string", "minLength": 1, "maxLength": 160},
    "attendee_reference": {"type": "string", "minLength": 1, "maxLength": 120},
    "draft_subject": {"type": "string", "minLength": 1, "maxLength": 240},
    "draft_message": {"type": "string", "minLength": 1, "maxLength": 4_000},
    "session_local_start": {
        "type": "string",
        "pattern": _LOCAL_TIMESTAMP_PATTERN,
        "maxLength": 19,
    },
    "session_timezone": {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
        "pattern": "^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$",
    },
    "session_start_at_utc": dict(_UTC_TIMESTAMP_SCHEMA),
    "reminder_offset_minutes": {
        "type": "integer",
        "minimum": 1,
        "maximum": 10_080,
    },
    "recommended_send_at_utc": dict(_UTC_TIMESTAMP_SCHEMA),
    "channel_label": {"type": "string", "enum": ["email", "community", "in_app"]},
    "source_references": {
        "type": "array",
        "minItems": 2,
        "maxItems": 2,
        "items": _SOURCE_REFERENCE_SCHEMA,
    },
    "signup_provenance": _SIGNUP_PROVENANCE_SCHEMA,
    "safety_notes": {
        "type": "array",
        "minItems": 3,
        "maxItems": 3,
        "items": {"type": "string", "minLength": 1, "maxLength": 300},
    },
}

COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": _MODEL_REQUIRED,
    "properties": _MODEL_PROPERTIES,
}

COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA_ID,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "scenario_id",
        "scenario_version",
        "artifact_type",
        *_MODEL_REQUIRED,
        "delivery_status",
        "external_schedule_status",
        "proposed_actions",
    ],
    "properties": {
        "scenario_id": {"const": COMMUNITY_REMINDER_DRAFT_SCENARIO_ID},
        "scenario_version": {"const": 1},
        "artifact_type": {"const": "scheduled_reminder_draft"},
        **_MODEL_PROPERTIES,
        "delivery_status": {"const": "not_sent"},
        "external_schedule_status": {"const": "not_externally_scheduled"},
        "proposed_actions": {"type": "array", "maxItems": 0},
    },
}

COMMUNITY_REMINDER_DRAFT_FIXTURE = {
    "event_id": "event.community-live-session.2026-09-17",
    "event_name": "Marketing operators live session",
    "signup_event_id": "signup.community-demo-0001",
    "admitted_source": "fixture.community-signup",
    "signup_at": "2026-09-01T16:30:00Z",
    "session_local_start": "2026-09-17T09:00:00",
    "session_timezone": "America/Los_Angeles",
    "reminder_offset_minutes": 1_440,
    "attendee_display_name": "Demo Attendee",
    "channel_label": "email",
    "event_details": (
        "A live session on governed marketing automation and approval-safe workflows."
    ),
}

COMMUNITY_REMINDER_DRAFT_SCENARIO = DemoScenarioDefinition(
    id=COMMUNITY_REMINDER_DRAFT_SCENARIO_ID,
    version=1,
    display_name="Community reminder draft",
    description=(
        "Create a deterministic reminder draft and recommended UTC time from supplied event "
        "signup details without scheduling or sending."
    ),
    selected_agents=(
        DemoSelectedAgent(
            instance_id=COMMUNITY_REMINDER_DRAFT_INSTANCE_ID,
            template_id=COMMUNITY_REMINDER_DRAFT_TEMPLATE_ID,
        ),
    ),
    primary_instance_id=COMMUNITY_REMINDER_DRAFT_INSTANCE_ID,
    steps=(
        DemoScenarioStep(
            key="create-reminder-draft",
            source_order=10,
            dependency_keys=(),
            terminal_result=True,
            kind="model.generate-structured",
            selected_instance_id=COMMUNITY_REMINDER_DRAFT_INSTANCE_ID,
            capability_id="cap.model.generate-structured",
            effect="read",
        ),
    ),
    workflow_id=COMMUNITY_REMINDER_DRAFT_WORKFLOW_ID,
    effect="read_only",
    input_schema_id=COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA_ID,
    input_schema=COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA,
    output_schema_id=COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA_ID,
    output_schema=COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA,
    fixture=COMMUNITY_REMINDER_DRAFT_FIXTURE,
    expected_state_path=("received", "validated", "planned", "executing", "completed"),
    expected_model_calls=1,
    expected_connector_calls=0,
    expected_external_actions=0,
    expected_approvals=0,
    safe_submit_verb="Create reminder draft",
)


class CommunityReminderTimeError(ValueError):
    """One safe semantic input failure with an exact scenario-input pointer."""

    def __init__(self, message: str, *, pointer: str) -> None:
        super().__init__(message)
        self.pointer = pointer


def calculate_community_reminder_times(
    session_local_start: str,
    session_timezone: str,
    reminder_offset_minutes: int,
) -> tuple[str, str]:
    """Resolve one unambiguous IANA wall time and subtract the bounded reminder offset."""

    try:
        zone = require_iana_timezone(session_timezone, "Community session timezone")
    except ValueError as exc:
        raise CommunityReminderTimeError(str(exc), pointer="/session_timezone") from None
    try:
        local_start = datetime.strptime(session_local_start, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        raise CommunityReminderTimeError(
            "Community session local start must be a valid local date-time",
            pointer="/session_local_start",
        ) from None
    if type(reminder_offset_minutes) is not int or not 1 <= reminder_offset_minutes <= 10_080:
        raise CommunityReminderTimeError(
            "Community reminder offset must be between 1 and 10080 minutes",
            pointer="/reminder_offset_minutes",
        )

    candidates: set[datetime] = set()
    for fold in (0, 1):
        aware = local_start.replace(tzinfo=zone, fold=fold)
        utc_value = aware.astimezone(UTC)
        if utc_value.astimezone(zone).replace(tzinfo=None) == local_start:
            candidates.add(utc_value)
    if not candidates:
        raise CommunityReminderTimeError(
            "Community session local start does not exist in the selected timezone",
            pointer="/session_local_start",
        )
    if len(candidates) != 1:
        raise CommunityReminderTimeError(
            "Community session local start is ambiguous in the selected timezone",
            pointer="/session_local_start",
        )

    session_start_at_utc = candidates.pop()
    try:
        recommended_send_at_utc = session_start_at_utc - timedelta(minutes=reminder_offset_minutes)
    except OverflowError:
        raise CommunityReminderTimeError(
            "Community recommended reminder time cannot be represented in UTC",
            pointer="/reminder_offset_minutes",
        ) from None
    return (
        session_start_at_utc.isoformat().replace("+00:00", "Z"),
        recommended_send_at_utc.isoformat().replace("+00:00", "Z"),
    )


def validate_community_reminder_temporal_order(
    signup_at: str,
    recommended_send_at_utc: str,
) -> None:
    """Require the recommendation to remain actionable after the admitted signup."""

    try:
        signup = datetime.fromisoformat(signup_at.removesuffix("Z") + "+00:00")
        recommended = datetime.fromisoformat(recommended_send_at_utc.removesuffix("Z") + "+00:00")
    except (AttributeError, ValueError):
        raise CommunityReminderTimeError(
            "Community signup timestamp must be a canonical UTC date-time",
            pointer="/signup_at",
        ) from None
    if signup.utcoffset() != timedelta(0) or recommended.utcoffset() != timedelta(0):
        raise CommunityReminderTimeError(
            "Community reminder timestamps must use UTC",
            pointer="/signup_at",
        )
    if recommended <= signup:
        raise CommunityReminderTimeError(
            "Community recommended reminder time must be after the admitted signup",
            pointer="/reminder_offset_minutes",
        )


def _input_from_request(request: LLMRequest) -> dict[str, JsonValue]:
    if len(request.retrieved_content) != 1 or request.tool_results:
        raise ValueError("Community reminder renderer requires one untrusted input and no tools")
    payload = json.loads(request.retrieved_content[0].content)
    if type(payload) is not dict:
        raise ValueError("Community reminder input must be an object")
    return cast(dict[str, JsonValue], payload)


def _render_community_reminder_payload(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    event_id = cast(str, payload["event_id"])
    event_name = cast(str, payload["event_name"])
    signup_event_id = cast(str, payload["signup_event_id"])
    admitted_source = cast(str, payload["admitted_source"])
    signup_at = cast(str, payload["signup_at"])
    session_local_start = cast(str, payload["session_local_start"])
    session_timezone = cast(str, payload["session_timezone"])
    reminder_offset_minutes = cast(int, payload["reminder_offset_minutes"])
    attendee_display_name = cast(str, payload["attendee_display_name"])
    channel_label = cast(str, payload["channel_label"])
    event_details = cast(str, payload["event_details"])
    session_start_at_utc, recommended_send_at_utc = calculate_community_reminder_times(
        session_local_start,
        session_timezone,
        reminder_offset_minutes,
    )
    validate_community_reminder_temporal_order(signup_at, recommended_send_at_utc)

    return {
        "event_id": event_id,
        "event_name": event_name,
        "attendee_reference": attendee_display_name,
        "draft_subject": f"Reminder: {event_name}",
        "draft_message": (
            f"Hello {attendee_display_name},\n\n"
            f"Reminder: {event_name} starts at {session_local_start} ({session_timezone}).\n\n"
            f"{event_details}\n\n"
            "This is a draft only; it has not been sent or externally scheduled."
        ),
        "session_local_start": session_local_start,
        "session_timezone": session_timezone,
        "session_start_at_utc": session_start_at_utc,
        "reminder_offset_minutes": reminder_offset_minutes,
        "recommended_send_at_utc": recommended_send_at_utc,
        "channel_label": channel_label,
        "source_references": [
            {
                "reference_type": "event",
                "reference_id": event_id,
                "usage": "supplied_input",
            },
            {
                "reference_type": "signup_event",
                "reference_id": signup_event_id,
                "usage": "supplied_input",
            },
        ],
        "signup_provenance": {
            "signup_event_id": signup_event_id,
            "admitted_source": admitted_source,
            "signup_at": signup_at,
        },
        "safety_notes": [
            "All event and signup details were treated as supplied untrusted data.",
            "The UTC time is a deterministic recommendation, not an external schedule.",
            "No attendee enrollment, calendar mutation, provider schedule, or send occurred.",
        ],
    }


def render_community_reminder_draft(
    request: LLMRequest,
    context: DeterministicRenderContext,
) -> dict[str, JsonValue]:
    """Render the inert reminder artifact from admitted input only."""

    del context
    return _render_community_reminder_payload(_input_from_request(request))


COMMUNITY_REMINDER_DRAFT_RENDERER = RendererRegistration(
    key=RendererKey(
        template_id=COMMUNITY_REMINDER_DRAFT_TEMPLATE_ID,
        output_schema_id=COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA_ID,
    ),
    version="demo-community-reminder-draft-v1",
    output_schema_hash=canonical_schema_hash(COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA),
    renderer=render_community_reminder_draft,
)


def finalize_community_reminder_draft(
    model_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Add trusted no-send/no-schedule status after the deterministic model call."""

    return {
        "scenario_id": COMMUNITY_REMINDER_DRAFT_SCENARIO_ID,
        "scenario_version": 1,
        "artifact_type": "scheduled_reminder_draft",
        **cast(dict[str, JsonValue], dict(model_payload)),
        "delivery_status": "not_sent",
        "external_schedule_status": "not_externally_scheduled",
        "proposed_actions": [],
    }


def expected_community_reminder_draft_artifact(
    input_payload: Mapping[str, Any],
) -> dict[str, JsonValue]:
    """Recompute every business field for persisted-artifact equivalence checks."""

    normalized = json.loads(canonical_json_bytes(input_payload))
    if type(normalized) is not dict:
        raise ValueError("Community reminder admitted input must be an object")
    return finalize_community_reminder_draft(_render_community_reminder_payload(normalized))


__all__ = [
    "COMMUNITY_REMINDER_DRAFT_FIXTURE",
    "COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA",
    "COMMUNITY_REMINDER_DRAFT_INPUT_SCHEMA_ID",
    "COMMUNITY_REMINDER_DRAFT_INSTANCE_ID",
    "COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA",
    "COMMUNITY_REMINDER_DRAFT_MODEL_OUTPUT_SCHEMA_ID",
    "COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA",
    "COMMUNITY_REMINDER_DRAFT_OUTPUT_SCHEMA_ID",
    "COMMUNITY_REMINDER_DRAFT_RENDERER",
    "COMMUNITY_REMINDER_DRAFT_SCENARIO",
    "COMMUNITY_REMINDER_DRAFT_SCENARIO_ID",
    "COMMUNITY_REMINDER_DRAFT_TEMPLATE_ID",
    "COMMUNITY_REMINDER_DRAFT_WORKFLOW_ID",
    "CommunityReminderTimeError",
    "calculate_community_reminder_times",
    "expected_community_reminder_draft_artifact",
    "finalize_community_reminder_draft",
    "render_community_reminder_draft",
    "validate_community_reminder_temporal_order",
]
