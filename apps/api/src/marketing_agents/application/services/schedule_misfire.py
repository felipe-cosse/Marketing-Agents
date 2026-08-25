"""Bounded, deterministic misfire planning for an exact persisted claim."""

from __future__ import annotations

from datetime import datetime, timedelta

from marketing_agents.application.ports.recurrence import (
    RecurrenceCalculationError,
    RecurrenceCalculator,
)
from marketing_agents.domain.entities import Schedule, ScheduleClaim
from marketing_agents.domain.enums import MisfirePolicy
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
    ) -> ScheduleOccurrencePlan:
        if type(schedule) is not Schedule or type(claim) is not ScheduleClaim:
            raise ScheduleMisfireError(
                "misfire_input_invalid",
                "misfire planning requires one exact Schedule and ScheduleClaim",
            )
        self._validate_claim_snapshot(schedule, claim)

        try:
            lateness = claim.claimed_at_utc - claim.scheduled_for_utc
        except (OverflowError, TypeError, ValueError) as exc:
            raise ScheduleMisfireError(
                "misfire_time_invalid",
                "misfire lateness cannot be derived from the persisted claim",
            ) from exc

        if lateness <= timedelta(seconds=schedule.misfire_grace_seconds):
            return ScheduleOccurrencePlan(
                schedule_id=schedule.id,
                scheduled_for_utc=claim.scheduled_for_utc,
                recurrence_version=schedule.recurrence_version,
                disposition=ScheduleDisposition.ON_TIME,
                next_run_at_utc=self._next_after(
                    schedule=schedule,
                    after_utc=claim.scheduled_for_utc,
                ),
            )

        first_missed_at_utc = claim.scheduled_for_utc
        last_missed_at_utc = first_missed_at_utc
        missed_count = 1
        for _ in range(self._max_coalesced_occurrences):
            candidate = self._next_after(
                schedule=schedule,
                after_utc=last_missed_at_utc,
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
                )
            if missed_count == self._max_coalesced_occurrences:
                raise ScheduleMisfireError(
                    "misfire_range_exhausted",
                    "missed schedule range exceeds the safe coalescing limit",
                )
            last_missed_at_utc = candidate
            missed_count += 1

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

    def _next_after(
        self,
        *,
        schedule: Schedule,
        after_utc: datetime,
    ) -> datetime:
        try:
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
        return candidate
