"""Atomic occurrence outcome, recurrence advancement, audit, and lease release."""

from __future__ import annotations

from dataclasses import dataclass

from marketing_agents.application.orchestration.dependencies import (
    OrchestrationDependencies,
)
from marketing_agents.application.ports.recurrence import RecurrenceCalculator
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.entities import (
    Run,
    Schedule,
    ScheduleClaim,
    ScheduleOccurrence,
    WorkItem,
)
from marketing_agents.domain.enums import OccurrenceState
from marketing_agents.domain.schedule_misfire import (
    ScheduleDisposition,
    ScheduleOccurrencePlan,
)
from marketing_agents.domain.schedule_occurrence_identity import (
    schedule_local_snapshot,
    schedule_occurrence_id,
)
from marketing_agents.security.digest_key import DigestKey

from .audit_events import AuditEventFactory
from .incoming_work_validation import IncomingWorkValidator
from .schedule_misfire import ScheduleMisfirePlanner
from .schedule_occurrence_ingress import (
    ScheduleOccurrenceCommand,
    ScheduleOccurrenceIngressDisposition,
    ScheduleOccurrenceIngressService,
)


class ScheduleClaimProcessingError(RuntimeError):
    """Stable fail-closed processing error for one already-committed claim."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ScheduleClaimProcessingResult:
    occurrence: ScheduleOccurrence
    plan: ScheduleOccurrencePlan
    resulting_schedule: Schedule
    work_item: WorkItem | None
    run: Run | None
    audit_events: tuple[AuditEvent, AuditEvent]


class ScheduleClaimProcessingService:
    """Consume one exact live claim in one caller-owned write transaction."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        digest_key: DigestKey,
        validator: IncomingWorkValidator,
        recurrence: RecurrenceCalculator,
        *,
        current_catalog_hash: str,
    ) -> None:
        self._dependencies = dependencies
        self._planner = ScheduleMisfirePlanner(recurrence)
        self._ingress = ScheduleOccurrenceIngressService(
            dependencies,
            digest_key,
            validator,
            current_catalog_hash=current_catalog_hash,
        )

    async def process_claimed_once(
        self,
        command: ScheduleOccurrenceCommand,
    ) -> ScheduleClaimProcessingResult:
        if type(command) is not ScheduleOccurrenceCommand:
            raise ValueError("schedule processing requires a typed occurrence command")
        if type(command.claim) is not ScheduleClaim:
            raise ValueError("schedule processing requires an exact ScheduleClaim")
        claim = command.claim

        async with self._dependencies.unit_of_work() as unit_of_work:
            schedule = await unit_of_work.schedules.get(claim.schedule_id)
            if schedule is None:
                raise ScheduleClaimProcessingError(
                    "schedule_missing",
                    "claimed schedule no longer exists",
                )
            plan = self._planner.resolve(schedule=schedule, claim=claim)

            fence_attempted_at = self._dependencies.utc_now()
            if not await unit_of_work.schedules.fence_claim(
                claim,
                now=fence_attempted_at,
            ):
                raise ScheduleClaimProcessingError(
                    "claim_fence_lost",
                    "schedule claim is stale, replaced, expired, or no longer active",
                )
            fenced_at = self._dependencies.utc_now()
            if fenced_at < claim.claimed_at_utc or claim.lease_expires_at_utc < fenced_at:
                raise ScheduleClaimProcessingError(
                    "claim_fence_lost",
                    "schedule claim expired while its processing fence was acquired",
                )
            fenced_schedule = await unit_of_work.schedules.get(claim.schedule_id)
            if fenced_schedule != schedule:
                raise ScheduleClaimProcessingError(
                    "schedule_changed_during_processing",
                    "schedule changed before its processing fence was acquired",
                )

            work_item: WorkItem | None
            run: Run | None
            if plan.disposition is ScheduleDisposition.SKIP:
                occurrence = self._skipped_occurrence(fenced_schedule, claim, plan)
                inserted = await unit_of_work.schedules.add_occurrence_or_get(occurrence)
                if not inserted.inserted:
                    raise ScheduleClaimProcessingError(
                        "occurrence_partial_processing",
                        "a live claim cannot replay a previously committed skip outcome",
                    )
                occurrence = inserted.occurrence
                work_item = None
                run = None
            else:
                admitted = await self._ingress.admit_claimed_in_uow(
                    unit_of_work,
                    command,
                    schedule=fenced_schedule,
                    plan=plan,
                )
                if admitted.disposition is not ScheduleOccurrenceIngressDisposition.CREATED:
                    raise ScheduleClaimProcessingError(
                        "occurrence_partial_processing",
                        "a live claim cannot replay a previously committed Work and Run receipt",
                    )
                occurrence = admitted.occurrence
                work_item = admitted.work_item
                run = admitted.run

            completed_at = self._dependencies.utc_now()
            resulting_schedule = await unit_of_work.schedules.advance_and_release_claim(
                claim=claim,
                next_run_at_utc=plan.next_run_at_utc,
                completed_at_utc=completed_at,
            )
            if resulting_schedule is None:
                raise ScheduleClaimProcessingError(
                    "claim_fence_lost",
                    "schedule claim expired or changed before recurrence advancement",
                )
            if (
                resulting_schedule.version != claim.version + 1
                or resulting_schedule.last_scheduled_at_utc != claim.scheduled_for_utc
                or resulting_schedule.next_run_at_utc != plan.next_run_at_utc
            ):
                raise ScheduleClaimProcessingError(
                    "schedule_advance_invalid",
                    "persisted recurrence advancement does not match its exact claim plan",
                )

            audit_factory = AuditEventFactory(
                AuditContext.worker(
                    claim.lease_owner,
                    correlation_id=occurrence.id,
                )
            )
            occurrence_event = audit_factory.schedule_occurrence(
                occurrence,
                plan,
                next_run_at_utc=resulting_schedule.next_run_at_utc,
                work_admitted=work_item is not None,
                occurred_at=completed_at,
            )
            advancement_event = audit_factory.schedule_next_occurrence_persisted(
                fenced_schedule,
                resulting_schedule,
                occurrence,
                plan,
                occurred_at=completed_at,
            )
            audit_events = await unit_of_work.audits.append_global_many(
                (occurrence_event, advancement_event)
            )
            if len(audit_events) != 2:
                raise ScheduleClaimProcessingError(
                    "schedule_audit_incomplete",
                    "schedule processing must append both mutation witnesses",
                )
            await unit_of_work.commit()
            return ScheduleClaimProcessingResult(
                occurrence=occurrence,
                plan=plan,
                resulting_schedule=resulting_schedule,
                work_item=work_item,
                run=run,
                audit_events=(audit_events[0], audit_events[1]),
            )

    @staticmethod
    def _skipped_occurrence(
        schedule: Schedule,
        claim: ScheduleClaim,
        plan: ScheduleOccurrencePlan,
    ) -> ScheduleOccurrence:
        if (
            plan.disposition is not ScheduleDisposition.SKIP
            or plan.first_missed_at_utc is None
            or plan.last_missed_at_utc is None
            or plan.missed_count is None
        ):
            raise ScheduleClaimProcessingError(
                "skip_plan_invalid",
                "skip processing requires one complete bounded missed range",
            )
        scheduled_local, timezone_fold = schedule_local_snapshot(
            claim.scheduled_for_utc,
            schedule.timezone,
        )
        return ScheduleOccurrence(
            id=schedule_occurrence_id(
                claim.schedule_id,
                claim.scheduled_for_utc,
                recurrence_version=claim.recurrence_version,
            ),
            schedule_id=claim.schedule_id,
            scheduled_for_utc=claim.scheduled_for_utc,
            scheduled_local=scheduled_local,
            timezone=schedule.timezone,
            timezone_fold=timezone_fold,
            recurrence_version=claim.recurrence_version,
            state=OccurrenceState.SKIPPED,
            misfire_policy_applied=schedule.misfire_policy,
            misfire_grace_seconds=schedule.misfire_grace_seconds,
            misfire_evaluated_at_utc=claim.claimed_at_utc,
            first_missed_at_utc=plan.first_missed_at_utc,
            last_missed_at_utc=plan.last_missed_at_utc,
            missed_count=plan.missed_count,
        )
