"""Five-field cron recurrence with explicit IANA/DST semantics."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import (  # type: ignore[import-untyped]
    CroniterBadCronError,
    CroniterBadDateError,
    croniter,
)

from marketing_agents.application.ports.recurrence import RecurrenceCalculationError
from marketing_agents.domain.validation import require_iana_timezone, require_text, require_utc

MAX_WALL_CANDIDATES = 512
MAX_YEARS_BETWEEN_MATCHES = 50
MAX_NONEXISTENT_ADVANCE_MINUTES = 2_880
CRON_FIELD_PATTERN = re.compile(r"^[0-9*/,\-]+$")


def _validate_cron(expression: str) -> None:
    try:
        require_text(expression, "schedule cron", maximum=100)
    except ValueError as exc:
        raise RecurrenceCalculationError("invalid_cron", str(exc)) from exc
    fields = expression.split(" ")
    if len(fields) != 5 or any(CRON_FIELD_PATTERN.fullmatch(field) is None for field in fields):
        raise RecurrenceCalculationError(
            "invalid_cron",
            "schedule cron must contain exactly five fields",
        )
    try:
        valid = croniter.is_valid(expression, second_at_beginning=False, strict=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecurrenceCalculationError("invalid_cron", "schedule cron is invalid") from exc
    if not valid:
        raise RecurrenceCalculationError("invalid_cron", "schedule cron is invalid")


def _resolve_wall_time(wall_time: datetime, timezone: ZoneInfo) -> datetime | None:
    """Resolve one naive wall time, choosing fold=0 and rejecting nonexistent times."""

    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = wall_time.replace(tzinfo=timezone, fold=fold)
        utc_value = aware.astimezone(UTC)
        round_trip = utc_value.astimezone(timezone)
        if round_trip.replace(tzinfo=None) == wall_time and round_trip.fold == fold:
            candidates.append(utc_value)
    return min(candidates) if candidates else None


def _resolve_or_advance_nonexistent(
    wall_time: datetime,
    timezone: ZoneInfo,
) -> datetime | None:
    scheduled_at_utc = _resolve_wall_time(wall_time, timezone)
    if scheduled_at_utc is not None:
        return scheduled_at_utc
    for minutes in range(1, MAX_NONEXISTENT_ADVANCE_MINUTES + 1):
        scheduled_at_utc = _resolve_wall_time(
            wall_time + timedelta(minutes=minutes),
            timezone,
        )
        if scheduled_at_utc is not None:
            return scheduled_at_utc
    return None


class CroniterRecurrenceCalculator:
    """Calculate from original wall-clock cron and persist only the chosen UTC instant."""

    def next_after(
        self,
        *,
        cron: str,
        timezone: str,
        after_utc: datetime,
    ) -> datetime:
        try:
            require_utc(after_utc, "schedule calculation boundary")
        except (AttributeError, ValueError) as exc:
            raise RecurrenceCalculationError(
                "invalid_boundary",
                "schedule calculation boundary must be timezone-aware UTC",
            ) from exc
        _validate_cron(cron)
        try:
            zone = require_iana_timezone(timezone, "schedule timezone")
        except ValueError as exc:
            raise RecurrenceCalculationError("invalid_timezone", str(exc)) from exc

        local_boundary = after_utc.astimezone(zone).replace(tzinfo=None)
        try:
            iterator = croniter(
                cron,
                local_boundary,
                ret_type=datetime,
                max_years_between_matches=MAX_YEARS_BETWEEN_MATCHES,
                second_at_beginning=False,
            )
            for _ in range(MAX_WALL_CANDIDATES):
                wall_time = iterator.get_next(datetime)
                if not isinstance(wall_time, datetime):
                    raise RecurrenceCalculationError(
                        "recurrence_contract_error",
                        "cron recurrence returned an invalid wall time",
                    )
                scheduled_at_utc = _resolve_or_advance_nonexistent(wall_time, zone)
                if scheduled_at_utc is not None and scheduled_at_utc > after_utc:
                    return scheduled_at_utc
        except RecurrenceCalculationError:
            raise
        except (CroniterBadCronError, CroniterBadDateError, OverflowError, ValueError) as exc:
            raise RecurrenceCalculationError(
                "recurrence_exhausted",
                "schedule has no bounded future occurrence",
            ) from exc
        raise RecurrenceCalculationError(
            "recurrence_exhausted",
            "schedule has no bounded future occurrence",
        )
