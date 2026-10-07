"""Recorded recurrence selections, without reinterpreting historical timezone rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Literal

from marketing_agents.domain.validation import require_iana_timezone, require_utc

MAX_RECORDED_RECURRENCE_RESOLUTIONS = 10_000


def _nominal_local(value: object) -> str:
    if type(value) is not str:
        raise ValueError("nominal local time must be a canonical string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("nominal local time must be a canonical string") from exc
    if (
        parsed.tzinfo is not None
        or parsed.isoformat(timespec="microseconds") != value
        or parsed.second != 0
        or parsed.microsecond != 0
    ):
        raise ValueError("nominal local time must be canonical and minute-aligned")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class RecurrenceResolution:
    """Why a selected nonexistent wall-clock candidate advanced to a valid instant.

    The adapter establishes the timezone fact when selecting the occurrence. This
    immutable evidence is not re-resolved on hydration against a newer tzdb.
    """

    reason: Literal["nonexistent_local_time"]
    nominal_local: str
    timezone: str
    resolved_at_utc: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("recurrence resolution version must be supported")
        if type(self.reason) is not str or self.reason != "nonexistent_local_time":
            raise ValueError("recurrence resolution reason must be supported")
        _nominal_local(self.nominal_local)
        require_iana_timezone(self.timezone, "recurrence resolution timezone")
        require_utc(self.resolved_at_utc, "resolved recurrence UTC time")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecurrenceResult:
    """One calculated selection; a null resolution records an ordinary selection."""

    scheduled_for_utc: datetime
    resolution: RecurrenceResolution | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("recurrence result version must be supported")
        require_utc(self.scheduled_for_utc, "selected recurrence UTC time")
        if self.resolution is not None:
            if type(self.resolution) is not RecurrenceResolution:
                raise ValueError("recurrence result resolution must be typed")
            if self.resolution.resolved_at_utc != self.scheduled_for_utc:
                raise ValueError("recurrence resolution must identify the selected UTC time")


def _mapping(value: object, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("recurrence snapshot must contain its exact fields")
    return value


def _utc_from_json(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("recurrence UTC time must be a canonical string")
    try:
        parsed = datetime.fromisoformat(value)
        require_utc(parsed, "recurrence snapshot UTC time")
    except (TypeError, ValueError) as exc:
        raise ValueError("recurrence UTC time must be a canonical string") from exc
    if parsed.isoformat(timespec="microseconds") != value:
        raise ValueError("recurrence UTC time must be a canonical string")
    return parsed


def recurrence_resolution_to_dict(value: RecurrenceResolution) -> dict[str, object]:
    if type(value) is not RecurrenceResolution:
        raise ValueError("recurrence resolution must be typed")
    return {
        "schema_version": value.schema_version,
        "reason": value.reason,
        "nominal_local": value.nominal_local,
        "timezone": value.timezone,
        "resolved_at_utc": value.resolved_at_utc.isoformat(timespec="microseconds"),
    }


def recurrence_resolution_from_dict(value: object) -> RecurrenceResolution:
    data = _mapping(
        value, {"schema_version", "reason", "nominal_local", "timezone", "resolved_at_utc"}
    )
    return RecurrenceResolution(
        schema_version=data["schema_version"],  # type: ignore[arg-type]
        reason=data["reason"],  # type: ignore[arg-type]
        nominal_local=data["nominal_local"],  # type: ignore[arg-type]
        timezone=data["timezone"],  # type: ignore[arg-type]
        resolved_at_utc=_utc_from_json(data["resolved_at_utc"]),
    )


def recurrence_result_to_dict(value: RecurrenceResult) -> dict[str, object]:
    if type(value) is not RecurrenceResult:
        raise ValueError("recurrence result must be typed")
    return {
        "schema_version": value.schema_version,
        "scheduled_for_utc": value.scheduled_for_utc.isoformat(timespec="microseconds"),
        "resolution": None
        if value.resolution is None
        else recurrence_resolution_to_dict(value.resolution),
    }


def recurrence_result_from_dict(value: object) -> RecurrenceResult:
    data = _mapping(value, {"schema_version", "scheduled_for_utc", "resolution"})
    return RecurrenceResult(
        schema_version=data["schema_version"],  # type: ignore[arg-type]
        scheduled_for_utc=_utc_from_json(data["scheduled_for_utc"]),
        resolution=None
        if data["resolution"] is None
        else recurrence_resolution_from_dict(data["resolution"]),
    )


def validate_recurrence_resolutions(values: tuple[RecurrenceResolution, ...]) -> None:
    if type(values) is not tuple or len(values) > MAX_RECORDED_RECURRENCE_RESOLUTIONS:
        raise ValueError("recorded recurrence resolutions must be a bounded tuple")
    if any(type(item) is not RecurrenceResolution for item in values):
        raise ValueError("recorded recurrence resolutions must be typed")
    instants = tuple(item.resolved_at_utc for item in values)
    if any(left >= right for left, right in pairwise(instants)):
        raise ValueError("recorded recurrence resolutions must be ordered and unique")


def recurrence_resolutions_to_list(
    values: tuple[RecurrenceResolution, ...],
) -> list[dict[str, object]]:
    validate_recurrence_resolutions(values)
    return [recurrence_resolution_to_dict(item) for item in values]


def recurrence_resolutions_from_list(value: object) -> tuple[RecurrenceResolution, ...]:
    if type(value) is not list or len(value) > MAX_RECORDED_RECURRENCE_RESOLUTIONS:
        raise ValueError("recorded recurrence resolutions must be a bounded JSON list")
    result = tuple(recurrence_resolution_from_dict(item) for item in value)
    validate_recurrence_resolutions(result)
    return result


def validate_recurrence_range(
    *,
    scheduled_for_utc: datetime,
    scheduled_recurrence: RecurrenceResult | None,
    next_recurrence: RecurrenceResult | None,
    recurrence_resolutions: tuple[RecurrenceResolution, ...] | None,
    last_missed_at_utc: datetime | None = None,
    next_run_at_utc: datetime | None = None,
    timezone: str | None = None,
    allow_pending_only: bool = False,
) -> None:
    """Bind new evidence to a processed range while leaving old records unknown."""
    if (scheduled_recurrence, next_recurrence, recurrence_resolutions) == (None, None, None):
        return
    if scheduled_recurrence is not None:
        if type(scheduled_recurrence) is not RecurrenceResult:
            raise ValueError("scheduled recurrence selection must be typed")
        if scheduled_recurrence.scheduled_for_utc != scheduled_for_utc:
            raise ValueError("scheduled recurrence selection must match the original due time")
        if (
            timezone is not None
            and scheduled_recurrence.resolution is not None
            and scheduled_recurrence.resolution.timezone != timezone
        ):
            raise ValueError("scheduled recurrence selection must preserve its original timezone")
        if allow_pending_only and next_recurrence is None and recurrence_resolutions is None:
            return
    if type(next_recurrence) is not RecurrenceResult or recurrence_resolutions is None:
        raise ValueError("recorded recurrence range requires its next selection and resolutions")
    validate_recurrence_resolutions(recurrence_resolutions)
    end = last_missed_at_utc or scheduled_for_utc
    if next_recurrence.scheduled_for_utc <= end:
        raise ValueError("next recurrence selection must follow the processed range")
    if next_run_at_utc is not None and next_recurrence.scheduled_for_utc != next_run_at_utc:
        raise ValueError("next recurrence selection must match the next UTC projection")
    if any(not scheduled_for_utc <= item.resolved_at_utc <= end for item in recurrence_resolutions):
        raise ValueError("recorded recurrence resolution is outside the processed range")
    due_resolution = None if scheduled_recurrence is None else scheduled_recurrence.resolution
    actual_due = tuple(
        item for item in recurrence_resolutions if item.resolved_at_utc == scheduled_for_utc
    )
    expected_due = () if due_resolution is None else (due_resolution,)
    if actual_due != expected_due:
        raise ValueError("recorded due resolution must preserve the exact pending selection")
    resolutions = recurrence_resolutions + (
        () if next_recurrence.resolution is None else (next_recurrence.resolution,)
    )
    zones = {item.timezone for item in resolutions}
    if len(zones) > 1 or (timezone is not None and zones - {timezone}):
        raise ValueError("recorded recurrence resolutions must preserve their original timezone")
