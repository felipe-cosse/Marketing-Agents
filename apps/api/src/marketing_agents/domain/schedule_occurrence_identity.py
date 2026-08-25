"""Stable identity and local snapshot helpers for persisted schedule occurrences."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.validation import (
    require_iana_timezone,
    require_id,
    require_text,
    require_utc,
)

SCHEDULE_RECURRENCE_VERSION = "five-field-cron-adr0008-v1"
SCHEDULE_OCCURRENCE_ID_SCHEME = "schedule-occurrence-sha256-v1"

_OCCURRENCE_ID_DOMAIN = b"marketing-agents:schedule-occurrence-id:sha256:v1\x00"


def schedule_occurrence_id(
    schedule_id: str,
    scheduled_for_utc: datetime,
    *,
    recurrence_version: str,
) -> str:
    """Derive one replay-stable ID from immutable recurrence identity facts."""

    require_id(schedule_id, "occurrence schedule ID")
    require_utc(scheduled_for_utc, "scheduled occurrence time")
    require_id(recurrence_version, "schedule recurrence version")
    require_text(recurrence_version, "schedule recurrence version", maximum=64)
    material = {
        "recurrence_version": recurrence_version,
        "schedule_id": schedule_id,
        "scheduled_for_utc": scheduled_for_utc.astimezone(UTC).isoformat(timespec="microseconds"),
    }
    digest = hashlib.sha256(_OCCURRENCE_ID_DOMAIN + canonical_json_bytes(material)).hexdigest()
    return f"{SCHEDULE_OCCURRENCE_ID_SCHEME}:{digest}"


def schedule_local_snapshot(
    scheduled_for_utc: datetime,
    timezone_name: str,
) -> tuple[str, int]:
    """Capture the persisted wall-clock label and fold selected by the UTC instant."""

    require_utc(scheduled_for_utc, "scheduled occurrence time")
    timezone = require_iana_timezone(timezone_name, "occurrence timezone")
    local = scheduled_for_utc.astimezone(timezone)
    return local.replace(tzinfo=None).isoformat(timespec="microseconds"), local.fold
