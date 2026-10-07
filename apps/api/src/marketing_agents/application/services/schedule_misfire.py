"""Bounded, deterministic misfire planning for an exact persisted claim."""

from __future__ import annotations

from datetime import datetime, timedelta

from marketing_agents.application.ports.recurrence import (
    RecurrenceCalculationError,
    RecurrenceCalculator,
)
from marketing_agents.domain.entities import Schedule, ScheduleClaim
from marketing_agents.domain.enums import MisfirePolicy
from marketing_agents.domain.recurrence_resolution import RecurrenceResolution, RecurrenceResult
from marketing_agents.domain.schedule_misfire import (
    MAX_COALESCED_MISSED_OCCURRENCES as MAX_COALESCED_MISSED_OCCURRENCES,
)
from marketing_agents.domain.schedule_misfire import ScheduleDisposition, ScheduleOccurrencePlan
from marketing_agents.domain.validation import require_utc


class ScheduleMisfireError(RuntimeError):
    """Stable fail-closed policy or recurrence contract failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScheduleMisfirePlanner:
    """Produce one immutable intent without reading a clock or owning a transaction."""

    def __init__(
        self,
        recurrence: RecurrenceCalculator,
        *,
        max_coalesced_occurrences: int = MAX_COALESCED_MISSED_OCCURRENCES,
    ) -> None:
        if (
            type(max_coalesced_occurrences) is not int
            or not 1 <= max_coalesced_occurrences <= MAX_COALESCED_MISSED_OCCURRENCES
        ):
            raise ValueError("coalesced occurrence limit must be from one through the safe maximum")
        self._recurrence = recurrence
        self._max_coalesced_occurrences = max_coalesced_occurrences

    def resolve(
        self,
        *,
        schedule: Schedule,
        claim: ScheduleClaim,
        record_resolution: bool = True,
    ) -> ScheduleOccurrencePlan:
        if type(schedule) is not Schedule or type(claim) is not ScheduleClaim:
            raise ScheduleMisfireError(
                "misfire_input_invalid",
                "misfire planning requires one exact Schedule and ScheduleClaim",
            )
        if type(record_resolution) is not bool:
            raise ValueError("recording recurrence resolution must be boolean")
        self._validate_claim_snapshot(schedule, claim)
        scheduled_recurrence = schedule.next_recurrence if record_resolution else None
        resolutions: list[RecurrenceResolution] = []
        if scheduled_recurrence is not None and scheduled_recurrence.resolution is not None:
            resolutions.append(scheduled_recurrence.resolution)

        try:
            lateness = claim.claimed_at_utc - claim.scheduled_for_utc
        except (OverflowError, TypeError, ValueError) as exc:
            raise ScheduleMisfireError(
                "misfire_time_invalid",
                "misfire lateness cannot be derived from the persisted claim",
            ) from exc

        if lateness <= timedelta(seconds=schedule.misfire_grace_seconds):
            next_at, next_recurrence = self._next_selection(
                schedule=schedule,
                after_utc=claim.scheduled_for_utc,
                record_resolution=record_resolution,
            )
            return ScheduleOccurrencePlan(
                schedule_id=schedule.id,
                scheduled_for_utc=claim.scheduled_for_utc,
                recurrence_version=schedule.recurrence_version,
                disposition=ScheduleDisposition.ON_TIME,
                next_run_at_utc=next_at,
                scheduled_recurrence=scheduled_recurrence,
                next_recurrence=next_recurrence,
                recurrence_resolutions=tuple(resolutions) if record_resolution else None,
            )

        first_missed_at_utc = claim.scheduled_for_utc
        last_missed_at_utc = first_missed_at_utc
        missed_count = 1
        for _ in range(self._max_coalesced_occurrences):
            candidate, next_recurrence = self._next_selection(
                schedule=schedule,
                after_utc=last_missed_at_utc,
                record_resolution=record_resolution,
            )
            if candidate > claim.claimed_at_utc:
                disposition = (
                    ScheduleDisposition.SKIP
                    if schedule.misfire_policy is MisfirePolicy.SKIP
                    else ScheduleDisposition.RUN_ONCE
                )
                return ScheduleOccurrencePlan(
                    schedule_id=schedule.id,
                    scheduled_for_utc=claim.scheduled_for_utc,
                    recurrence_version=schedule.recurrence_version,
                    disposition=disposition,
                    next_run_at_utc=candidate,
                    first_missed_at_utc=first_missed_at_utc,
                    last_missed_at_utc=last_missed_at_utc,
                    missed_count=missed_count,
                    scheduled_recurrence=scheduled_recurrence,
                    next_recurrence=next_recurrence,
                    recurrence_resolutions=tuple(resolutions) if record_resolution else None,
                )
            if missed_count == self._max_coalesced_occurrences:
                raise ScheduleMisfireError(
                    "misfire_range_exhausted",
                    "missed schedule range exceeds the safe coalescing limit",
                )
            last_missed_at_utc = candidate
            missed_count += 1
            if next_recurrence is not None and next_recurrence.resolution is not None:
                resolutions.append(next_recurrence.resolution)

        raise ScheduleMisfireError(
            "misfire_range_exhausted",
            "missed schedule range exceeds the safe coalescing limit",
        )

    @staticmethod
    def _validate_claim_snapshot(schedule: Schedule, claim: ScheduleClaim) -> None:
        comparisons = (
            (
                schedule.id == claim.schedule_id,
                "claim_schedule_mismatch",
                "schedule claim identifies another schedule",
            ),
            (
                schedule.next_run_at_utc == claim.scheduled_for_utc,
                "claim_due_mismatch",
                "schedule claim identifies another persisted due instant",
            ),
            (
                schedule.recurrence_version == claim.recurrence_version,
                "claim_recurrence_mismatch",
                "schedule claim identifies another recurrence version",
            ),
            (
                schedule.version == claim.version,
                "claim_version_mismatch",
                "schedule claim identifies another fencing version",
            ),
        )
        for matches, code, message in comparisons:
            if not matches:
                raise ScheduleMisfireError(code, message)

    def _next_selection(
        self,
        *,
        schedule: Schedule,
        after_utc: datetime,
        record_resolution: bool,
    ) -> tuple[datetime, RecurrenceResult | None]:
        try:
            result = None
            if record_resolution:
                result = self._recurrence.next_occurrence_after(
                    cron=schedule.cron, timezone=schedule.timezone, after_utc=after_utc
                )
                if type(result) is not RecurrenceResult:
                    raise ValueError("recurrence result must be typed")
                if (
                    result.resolution is not None
                    and result.resolution.timezone != schedule.timezone
                ):
                    raise ValueError("recurrence result changed its original timezone")
                candidate = result.scheduled_for_utc
            else:
                candidate = self._recurrence.next_after(
                    cron=schedule.cron,
                    timezone=schedule.timezone,
                    after_utc=after_utc,
                )
        except RecurrenceCalculationError as exc:
            raise ScheduleMisfireError(
                "recurrence_calculation_failed",
                "schedule recurrence could not produce a bounded future instant",
            ) from exc
        except (AttributeError, OverflowError, TypeError, ValueError) as exc:
            raise ScheduleMisfireError(
                "recurrence_contract_error",
                "schedule recurrence failed its bounded calculation contract",
            ) from exc
        try:
            require_utc(candidate, "calculated next scheduled time")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScheduleMisfireError(
                "recurrence_contract_error",
                "schedule recurrence returned a non-UTC instant",
            ) from exc
        if candidate <= after_utc:
            raise ScheduleMisfireError(
                "recurrence_contract_error",
                "schedule recurrence must advance strictly beyond its boundary",
            )
        return candidate, result
