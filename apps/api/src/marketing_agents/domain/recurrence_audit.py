"""Bounded safe summaries of complete, durable recurrence adjustment evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.recurrence_resolution import (
    MAX_RECORDED_RECURRENCE_RESOLUTIONS,
    RecurrenceResolution,
    RecurrenceResult,
    recurrence_resolution_from_dict,
    recurrence_resolution_to_dict,
    recurrence_resolutions_to_list,
)
from marketing_agents.domain.validation import require_digest

_KEYS = frozenset(
    {
        "schema_version",
        "scheduled_calculation_known",
        "range_observed",
        "scheduled_resolution",
        "next_resolution",
        "adjustment_count",
        "first_adjustment",
        "last_adjustment",
        "adjustments_sha256",
    }
)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def recurrence_audit_summary(
    *,
    scheduled_recurrence: RecurrenceResult | None,
    next_recurrence: RecurrenceResult | None,
    recurrence_resolutions: tuple[RecurrenceResolution, ...] | None,
) -> dict[str, object] | None:
    """Keep full ranges in storage; expose only fixed-size, integrity-bound facts."""
    scheduled = None if scheduled_recurrence is None else scheduled_recurrence.resolution
    upcoming = None if next_recurrence is None else next_recurrence.resolution
    values = (
        None
        if recurrence_resolutions is None
        else recurrence_resolutions_to_list(recurrence_resolutions)
    )
    if scheduled is None and upcoming is None and not values:
        return None
    result: dict[str, object] = {
        "schema_version": 1,
        "scheduled_calculation_known": scheduled_recurrence is not None,
        "range_observed": values is not None,
        "scheduled_resolution": None
        if scheduled is None
        else recurrence_resolution_to_dict(scheduled),
        "next_resolution": None if upcoming is None else recurrence_resolution_to_dict(upcoming),
        "adjustment_count": len(values) if values is not None else 0,
        "first_adjustment": values[0] if values else None,
        "last_adjustment": values[-1] if values else None,
        "adjustments_sha256": _digest(values),
    }
    validate_recurrence_audit_summary(result)
    return result


def validate_recurrence_audit_summary(value: object) -> None:
    """Validate typed calendar facts without reinterpreting historical tzdb rules."""
    if not isinstance(value, Mapping) or set(value) != _KEYS:
        raise ValueError("recurrence audit summary requires its exact safe fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported recurrence audit summary version")
    if any(
        type(value[key]) is not bool for key in ("scheduled_calculation_known", "range_observed")
    ):
        raise ValueError("recurrence audit evidence flags must be boolean")
    count = value["adjustment_count"]
    if type(count) is not int or not 0 <= count <= MAX_RECORDED_RECURRENCE_RESOLUTIONS:
        raise ValueError("recurrence audit adjustment count must be bounded")
    require_digest(value["adjustments_sha256"], "recurrence audit adjustments digest")
    children = {
        key: None if value[key] is None else recurrence_resolution_from_dict(value[key])
        for key in (
            "scheduled_resolution",
            "next_resolution",
            "first_adjustment",
            "last_adjustment",
        )
    }
    scheduled = children["scheduled_resolution"]
    first, last = children["first_adjustment"], children["last_adjustment"]
    if not value["scheduled_calculation_known"] and scheduled is not None:
        raise ValueError("unknown scheduled calculation cannot claim resolution evidence")
    if not value["range_observed"] and count:
        raise ValueError("unobserved recurrence range cannot claim adjustments")
    if count == 0:
        if first is not None or last is not None:
            raise ValueError("empty recurrence range cannot retain adjustment endpoints")
        if value["adjustments_sha256"] != _digest([] if value["range_observed"] else None):
            raise ValueError("empty recurrence range digest does not match")
    else:
        if first is None or last is None:
            raise ValueError("nonempty recurrence range requires both endpoints")
        if (count == 1 and first != last) or (
            count > 1 and first.resolved_at_utc >= last.resolved_at_utc
        ):
            raise ValueError("recurrence range endpoints must match its count and order")
        if count <= 2:
            endpoints = [recurrence_resolution_to_dict(first)]
            if count == 2:
                endpoints.append(recurrence_resolution_to_dict(last))
            if value["adjustments_sha256"] != _digest(endpoints):
                raise ValueError("recurrence endpoint digest does not match")
    if value["range_observed"] and scheduled is not None and first != scheduled:
        raise ValueError("scheduled adjustment must be the first observed adjustment")
    if len({item.timezone for item in children.values() if item is not None}) > 1:
        raise ValueError("recurrence audit facts must preserve one original timezone")
    if not any(item is not None for item in children.values()):
        raise ValueError("recurrence audit summary requires an actual adjustment")


def validate_recurrence_audit_context(
    value: object,
    *,
    scheduled_for_utc: datetime,
    next_run_at_utc: datetime,
    last_missed_at_utc: datetime | None = None,
    missed_count: int | None = None,
) -> None:
    validate_recurrence_audit_summary(value)
    assert isinstance(value, Mapping)
    for key, expected in (
        ("scheduled_resolution", scheduled_for_utc),
        ("next_resolution", next_run_at_utc),
    ):
        if (
            value[key] is not None
            and recurrence_resolution_from_dict(value[key]).resolved_at_utc != expected
        ):
            raise ValueError("recurrence audit selection does not match the event UTC time")
    if missed_count is not None and value["adjustment_count"] > missed_count:
        raise ValueError("recurrence audit has more adjustments than processed occurrences")
    if value["first_adjustment"] is not None:
        first = recurrence_resolution_from_dict(value["first_adjustment"])
        if (
            first.resolved_at_utc == scheduled_for_utc
            and value["first_adjustment"] != value["scheduled_resolution"]
        ):
            raise ValueError("due adjustment must preserve the exact scheduled evidence")
    for key in ("first_adjustment", "last_adjustment"):
        if value[key] is not None:
            instant = recurrence_resolution_from_dict(value[key]).resolved_at_utc
            if not scheduled_for_utc <= instant < next_run_at_utc:
                raise ValueError("recurrence audit adjustment is outside the processed range")
            if last_missed_at_utc is not None and instant > last_missed_at_utc:
                raise ValueError("recurrence audit adjustment follows the last missed occurrence")
