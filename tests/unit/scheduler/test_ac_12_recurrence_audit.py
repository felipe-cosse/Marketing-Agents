"""AC-12: bounded safe audit summaries bind complete calendar-only evidence."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.recurrence_audit import (
    recurrence_audit_summary,
    validate_recurrence_audit_context,
    validate_recurrence_audit_summary,
)
from marketing_agents.domain.recurrence_resolution import (
    RecurrenceResolution,
    RecurrenceResult,
    recurrence_resolution_to_dict,
    recurrence_resolutions_to_list,
)
from marketing_agents.security.audit_metadata import AuditMetadataError, seal_audit_metadata

DUE = datetime(2026, 3, 8, 10, tzinfo=UTC)
NEXT = datetime(2026, 3, 9, 9, 30, tzinfo=UTC)
GAP = RecurrenceResolution(
    reason="nonexistent_local_time",
    nominal_local="2026-03-08T02:30:00.000000",
    timezone="America/Los_Angeles",
    resolved_at_utc=DUE,
)


def summary() -> dict[str, object]:
    result = recurrence_audit_summary(
        scheduled_recurrence=RecurrenceResult(scheduled_for_utc=DUE, resolution=GAP),
        next_recurrence=RecurrenceResult(scheduled_for_utc=NEXT),
        recurrence_resolutions=(GAP,),
    )
    assert result is not None
    return result


def metadata(value: object) -> dict[str, object]:
    return {
        "claim_fingerprint": "a" * 64,
        "scheduled_for_utc": DUE.isoformat(timespec="microseconds"),
        "next_run_at_utc": NEXT.isoformat(timespec="microseconds"),
        "recurrence_version": "cron-v1",
        "work_admitted": True,
        "recurrence_resolution": value,
    }


def test_gap_summary_survives_sealing_and_frozen_mapping_validation() -> None:
    value = summary()
    assert value["scheduled_resolution"] == recurrence_resolution_to_dict(GAP)
    assert value["adjustment_count"] == 1
    assert (
        value["adjustments_sha256"]
        == hashlib.sha256(canonical_json_bytes(recurrence_resolutions_to_list((GAP,)))).hexdigest()
    )
    sealed = seal_audit_metadata("schedule.occurrence_created", metadata(value), occurred_at=DUE)
    validate_recurrence_audit_context(
        sealed.values["recurrence_resolution"],
        scheduled_for_utc=DUE,
        next_run_at_utc=NEXT,
        last_missed_at_utc=DUE,
        missed_count=1,
    )


def test_ordinary_and_legacy_selections_do_not_change_audit_fingerprints() -> None:
    for scheduled, upcoming, values in (
        (None, None, None),
        (RecurrenceResult(scheduled_for_utc=DUE), RecurrenceResult(scheduled_for_utc=NEXT), ()),
    ):
        assert (
            recurrence_audit_summary(
                scheduled_recurrence=scheduled,
                next_recurrence=upcoming,
                recurrence_resolutions=values,
            )
            is None
        )


def test_next_gap_is_distinct_from_processed_range_evidence() -> None:
    value = recurrence_audit_summary(
        scheduled_recurrence=RecurrenceResult(scheduled_for_utc=DUE - timedelta(days=1)),
        next_recurrence=RecurrenceResult(scheduled_for_utc=DUE, resolution=GAP),
        recurrence_resolutions=(),
    )
    assert value is not None
    assert value["adjustment_count"] == 0
    assert value["next_resolution"] == recurrence_resolution_to_dict(GAP)
    assert value["scheduled_resolution"] is None
    validate_recurrence_audit_context(
        value, scheduled_for_utc=DUE - timedelta(days=1), next_run_at_utc=DUE, missed_count=1
    )


def test_summary_preserves_full_ten_thousand_item_digest_within_audit_bounds() -> None:
    # Synthetic historical calendar facts test structural storage/audit limits,
    # not a claim that a real timezone has daily DST gaps.
    values = tuple(
        RecurrenceResolution(
            reason="nonexistent_local_time",
            nominal_local=(datetime(2026, 3, 8, 2, 30) + timedelta(days=index)).isoformat(
                timespec="microseconds"
            ),
            timezone=GAP.timezone,
            resolved_at_utc=DUE + timedelta(days=index),
        )
        for index in range(10_000)
    )
    next_time = values[-1].resolved_at_utc + timedelta(days=1)
    value = recurrence_audit_summary(
        scheduled_recurrence=RecurrenceResult(scheduled_for_utc=DUE, resolution=GAP),
        next_recurrence=RecurrenceResult(scheduled_for_utc=next_time),
        recurrence_resolutions=values,
    )
    assert value is not None
    assert value["adjustment_count"] == len(values)
    assert value["first_adjustment"] == recurrence_resolution_to_dict(values[0])
    assert value["last_adjustment"] == recurrence_resolution_to_dict(values[-1])
    assert (
        value["adjustments_sha256"]
        == hashlib.sha256(canonical_json_bytes(recurrence_resolutions_to_list(values))).hexdigest()
    )
    fields = metadata(value)
    fields.update(
        next_run_at_utc=next_time.isoformat(timespec="microseconds"),
        first_missed_at_utc=DUE.isoformat(timespec="microseconds"),
        last_missed_at_utc=values[-1].resolved_at_utc.isoformat(timespec="microseconds"),
        missed_count=len(values),
    )
    sealed = seal_audit_metadata("schedule.misfire_run_once", fields, occurred_at=next_time)
    assert len(canonical_json_bytes(sealed.values)) < 8192
    validate_recurrence_audit_context(
        sealed.values["recurrence_resolution"],
        scheduled_for_utc=DUE,
        next_run_at_utc=next_time,
        last_missed_at_utc=values[-1].resolved_at_utc,
        missed_count=len(values),
    )


@pytest.mark.parametrize(
    "key,replacement",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("scheduled_calculation_known", 1),
        ("scheduled_calculation_known", False),
        ("range_observed", False),
        ("adjustment_count", True),
        ("adjustment_count", 0),
        ("adjustment_count", 10_001),
        ("first_adjustment", None),
        ("last_adjustment", None),
        ("adjustments_sha256", "b" * 64),
        ("business_input", "never reflect this"),
    ],
)
def test_malformed_summary_is_rejected_before_sealing(key: str, replacement: object) -> None:
    value = summary()
    value[key] = replacement
    with pytest.raises(ValueError):
        validate_recurrence_audit_summary(value)
    with pytest.raises(AuditMetadataError):
        seal_audit_metadata("schedule.occurrence_created", metadata(value), occurred_at=DUE)


def test_invalid_reason_and_cross_event_time_are_rejected() -> None:
    value = deepcopy(summary())
    value["scheduled_resolution"]["reason"] = "raw business error"  # type: ignore[index]
    with pytest.raises(AuditMetadataError):
        seal_audit_metadata("schedule.occurrence_created", metadata(value), occurred_at=DUE)
    with pytest.raises(ValueError, match="event UTC time"):
        validate_recurrence_audit_context(
            summary(), scheduled_for_utc=DUE - timedelta(minutes=1), next_run_at_utc=NEXT
        )
