"""Atomic occurrence outcome, recurrence advancement, audit, and lease release."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from enum import StrEnum

from marketing_agents.application.orchestration.dependencies import (
    OrchestrationDependencies,
)
from marketing_agents.application.ports.recurrence import RecurrenceCalculator
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import (
    Run,
    Schedule,
    ScheduleClaim,
    ScheduleOccurrence,
    WorkItem,
)
from marketing_agents.domain.enums import MisfirePolicy, OccurrenceState, RunState
from marketing_agents.domain.run_lifecycle import initial_received_transition
from marketing_agents.domain.schedule_misfire import (
    ScheduleDisposition,
    ScheduleOccurrencePlan,
)
from marketing_agents.domain.schedule_occurrence_identity import (
    schedule_local_snapshot,
    schedule_occurrence_id,
)
from marketing_agents.security.admission_digest import derive_admission_digests
from marketing_agents.security.digest_key import DigestKey

from .audit_events import AuditEventFactory
from .incoming_work_validation import (
    IncomingWorkValidationError,
    IncomingWorkValidator,
    _validated_parts,
)
from .schedule_misfire import ScheduleMisfireError, ScheduleMisfirePlanner
from .schedule_occurrence_ingress import (
    ScheduleOccurrenceCommand,
    ScheduleOccurrenceIngressDisposition,
    ScheduleOccurrenceIngressService,
)
from .work_admission import _input_projection_integrity_digest


class ScheduleClaimProcessingError(RuntimeError):
    """Stable fail-closed processing error for one already-committed claim."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScheduleClaimProcessingDisposition(StrEnum):
    """Whether this call committed the outcome or suppressed an exact retry."""

    PROCESSED = "processed"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"


@dataclass(frozen=True, slots=True)
class ScheduleClaimProcessingResult:
    occurrence: ScheduleOccurrence
    plan: ScheduleOccurrencePlan
    resulting_schedule: Schedule
    work_item: WorkItem | None
    run: Run | None
    audit_events: tuple[AuditEvent, AuditEvent]
    disposition: ScheduleClaimProcessingDisposition


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
        self._digest_key = digest_key
        self._validator = validator
        self._current_catalog_hash = current_catalog_hash
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

        failure: ScheduleMisfireError | ScheduleClaimProcessingError
        try:
            return await self._process_active_claim(command)
        except ScheduleMisfireError as exc:
            if exc.code not in {
                "claim_schedule_mismatch",
                "claim_due_mismatch",
                "claim_recurrence_mismatch",
                "claim_version_mismatch",
            }:
                raise
            failure = exc
        except ScheduleClaimProcessingError as exc:
            if exc.code not in {
                "claim_fence_lost",
                "occurrence_partial_processing",
                "schedule_changed_during_processing",
            }:
                raise
            failure = exc

        replay = await self._load_committed_result(command)
        if replay is not None:
            return replay
        if isinstance(failure, ScheduleMisfireError):
            raise ScheduleClaimProcessingError(
                "claim_fence_lost",
                "schedule claim is stale, replaced, or already consumed",
            ) from failure
        raise failure

    async def _process_active_claim(
        self,
        command: ScheduleOccurrenceCommand,
    ) -> ScheduleClaimProcessingResult:
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
                claim,
                next_run_at_utc=resulting_schedule.next_run_at_utc,
                work_admitted=work_item is not None,
                occurred_at=completed_at,
            )
            advancement_event = audit_factory.schedule_next_occurrence_persisted(
                fenced_schedule,
                resulting_schedule,
                occurrence,
                plan,
                claim,
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
                disposition=ScheduleClaimProcessingDisposition.PROCESSED,
            )

    async def _load_committed_result(
        self,
        command: ScheduleOccurrenceCommand,
    ) -> ScheduleClaimProcessingResult | None:
        """Return one fully witnessed committed retry without performing a write."""

        claim = command.claim
        occurrence_id = schedule_occurrence_id(
            claim.schedule_id,
            claim.scheduled_for_utc,
            recurrence_version=claim.recurrence_version,
        )
        async with self._dependencies.unit_of_work() as unit_of_work:
            schedule = await unit_of_work.schedules.get(claim.schedule_id)
            if schedule is None:
                return None
            active_claim = await unit_of_work.schedules.get_claim(claim.schedule_id)
            advancement_event = await unit_of_work.audits.get_mutation_event(
                "schedule",
                claim.schedule_id,
                claim.version + 1,
            )
            exact_projection = (
                schedule.enabled
                and schedule.id == claim.schedule_id
                and schedule.recurrence_version == claim.recurrence_version
                and schedule.version == claim.version + 1
                and schedule.last_scheduled_at_utc == claim.scheduled_for_utc
                and schedule.next_run_at_utc > claim.scheduled_for_utc
                and active_claim is None
            )
            if not exact_projection:
                if advancement_event is not None:
                    raise ScheduleClaimProcessingError(
                        "committed_outcome_incomplete",
                        "committed schedule witnesses no longer describe one exact outcome",
                    )
                return None

            occurrence = await unit_of_work.schedules.get_occurrence(occurrence_id)
            occurrence_event = await unit_of_work.audits.get_mutation_event(
                "schedule_occurrence",
                occurrence_id,
                1,
            )
            if occurrence is None or occurrence_event is None or advancement_event is None:
                raise ScheduleClaimProcessingError(
                    "committed_outcome_incomplete",
                    "committed schedule outcome is missing an authoritative durable witness",
                )

            plan = self._require_committed_plan(
                schedule=schedule,
                claim=claim,
                occurrence=occurrence,
            )
            self._require_committed_scheduler_audits(
                schedule=schedule,
                claim=claim,
                occurrence=occurrence,
                plan=plan,
                occurrence_event=occurrence_event,
                advancement_event=advancement_event,
            )

            if plan.disposition is ScheduleDisposition.SKIP:
                if occurrence.work_item_id is not None or occurrence.run_id is not None:
                    raise ScheduleClaimProcessingError(
                        "committed_outcome_incomplete",
                        "committed skipped occurrence unexpectedly links admitted work",
                    )
                work_item = None
                run = None
            else:
                work_item, run = await self._require_committed_receipt(
                    unit_of_work,
                    schedule=schedule,
                    claim=claim,
                    occurrence=occurrence,
                    command=command,
                )

            return ScheduleClaimProcessingResult(
                occurrence=occurrence,
                plan=plan,
                resulting_schedule=schedule,
                work_item=work_item,
                run=run,
                audit_events=(occurrence_event, advancement_event),
                disposition=ScheduleClaimProcessingDisposition.DUPLICATE_SUPPRESSED,
            )

    def _require_committed_plan(
        self,
        *,
        schedule: Schedule,
        claim: ScheduleClaim,
        occurrence: ScheduleOccurrence,
    ) -> ScheduleOccurrencePlan:
        if (
            occurrence.schedule_id != claim.schedule_id
            or occurrence.scheduled_for_utc != claim.scheduled_for_utc
            or occurrence.recurrence_version != claim.recurrence_version
            or occurrence.timezone != schedule.timezone
            or occurrence.state
            not in {OccurrenceState.ENQUEUED, OccurrenceState.SKIPPED, OccurrenceState.COMPLETED}
        ):
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed occurrence does not match the exact processed claim",
            )

        if occurrence.misfire_policy_applied is None:
            disposition = ScheduleDisposition.ON_TIME
        elif occurrence.misfire_policy_applied is MisfirePolicy.SKIP:
            disposition = ScheduleDisposition.SKIP
        elif occurrence.misfire_policy_applied is MisfirePolicy.RUN_ONCE:
            disposition = ScheduleDisposition.RUN_ONCE
        else:  # pragma: no cover - ScheduleOccurrence rejects this shape
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed occurrence has an unsupported policy outcome",
            )
        if (disposition is ScheduleDisposition.SKIP) != (
            occurrence.state is OccurrenceState.SKIPPED
        ):
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed occurrence state disagrees with its policy outcome",
            )
        plan = ScheduleOccurrencePlan(
            schedule_id=claim.schedule_id,
            scheduled_for_utc=claim.scheduled_for_utc,
            recurrence_version=claim.recurrence_version,
            disposition=disposition,
            next_run_at_utc=schedule.next_run_at_utc,
            first_missed_at_utc=occurrence.first_missed_at_utc,
            last_missed_at_utc=occurrence.last_missed_at_utc,
            missed_count=occurrence.missed_count,
        )
        prior_schedule = replace(
            schedule,
            next_run_at_utc=claim.scheduled_for_utc,
            last_scheduled_at_utc=None,
            version=claim.version,
        )
        try:
            expected_plan = self._planner.resolve(schedule=prior_schedule, claim=claim)
        except ScheduleMisfireError as exc:
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed occurrence policy can no longer be revalidated",
            ) from exc
        if plan != expected_plan or (
            disposition is not ScheduleDisposition.ON_TIME
            and occurrence.misfire_evaluated_at_utc != claim.claimed_at_utc
        ):
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed occurrence does not match its deterministic claim-time plan",
            )
        return plan

    @staticmethod
    def _audit_occurrence_snapshot(occurrence: ScheduleOccurrence) -> ScheduleOccurrence:
        if occurrence.state is OccurrenceState.COMPLETED:
            return replace(occurrence, state=OccurrenceState.ENQUEUED)
        return occurrence

    def _require_committed_scheduler_audits(
        self,
        *,
        schedule: Schedule,
        claim: ScheduleClaim,
        occurrence: ScheduleOccurrence,
        plan: ScheduleOccurrencePlan,
        occurrence_event: AuditEvent,
        advancement_event: AuditEvent,
    ) -> None:
        if (
            occurrence_event.run_sequence is not None
            or advancement_event.run_sequence is not None
            or occurrence_event.occurred_at != advancement_event.occurred_at
        ):
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed scheduler audit witnesses have inconsistent timeline coordinates",
            )
        context = AuditContext.worker(
            claim.lease_owner,
            correlation_id=occurrence.id,
        )
        factory = AuditEventFactory(context)
        audit_occurrence = self._audit_occurrence_snapshot(occurrence)
        try:
            expected_occurrence = factory.schedule_occurrence(
                audit_occurrence,
                plan,
                claim,
                next_run_at_utc=schedule.next_run_at_utc,
                work_admitted=plan.admits_work,
                occurred_at=occurrence_event.occurred_at,
            )
            prior_schedule = replace(
                schedule,
                next_run_at_utc=claim.scheduled_for_utc,
                last_scheduled_at_utc=None,
                version=claim.version,
            )
            expected_advancement = factory.schedule_next_occurrence_persisted(
                prior_schedule,
                schedule,
                audit_occurrence,
                plan,
                claim,
                occurred_at=advancement_event.occurred_at,
            )
        except ValueError as exc:
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed scheduler audit witnesses cannot be reconstructed",
            ) from exc
        if (
            occurrence_event.draft != expected_occurrence
            or advancement_event.draft != expected_advancement
        ):
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed scheduler audit witnesses do not match the authoritative outcome",
            )

    async def _require_committed_receipt(
        self,
        unit_of_work: UnitOfWork,
        *,
        schedule: Schedule,
        claim: ScheduleClaim,
        occurrence: ScheduleOccurrence,
        command: ScheduleOccurrenceCommand,
    ) -> tuple[WorkItem, Run]:
        if occurrence.work_item_id is None or occurrence.run_id is None:
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed work-producing occurrence lacks its complete receipt links",
            )
        work_item = await unit_of_work.works.get(occurrence.work_item_id)
        run = await unit_of_work.runs.get(occurrence.run_id)
        if work_item is None or run is None:
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed occurrence links a missing WorkItem or Run",
            )
        if (
            work_item.source != "schedule"
            or work_item.event_id != occurrence.id
            or work_item.instance_id != schedule.instance_id
            or work_item.trigger_id != schedule.trigger_id
            or work_item.workflow_id != schedule.workflow_id
            or run.work_item_id != work_item.id
            or run.configuration_revision != work_item.configuration_revision
        ):
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed WorkItem and Run do not bind the recovered schedule occurrence",
            )
        try:
            expected_projection_digest = _input_projection_integrity_digest(
                work_item,
                self._digest_key,
            )
        except (TypeError, ValueError) as exc:
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed WorkItem input projection cannot be revalidated",
            ) from exc
        if not hmac.compare_digest(
            work_item.input_projection_integrity_digest,
            expected_projection_digest,
        ):
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed WorkItem input projection integrity does not match",
            )

        initial_run = replace(
            run,
            state=RunState.RECEIVED,
            updated_at=run.created_at,
            approval_required=None,
            terminal_reason_code=None,
            version=1,
        )
        initial_transition = initial_received_transition(initial_run)
        history = await unit_of_work.runs.list_transitions(run.id)
        initial_event = await unit_of_work.audits.get_mutation_event("run", run.id, 1)
        context = AuditContext.worker(claim.lease_owner, correlation_id=occurrence.id)
        expected_initial_event = AuditEventFactory(context).run_transition(
            initial_run,
            initial_transition,
        )
        if (
            not history
            or history[0] != initial_transition
            or initial_event is None
            or initial_event.run_sequence != 1
            or initial_event.draft != expected_initial_event
        ):
            raise ScheduleClaimProcessingError(
                "committed_outcome_incomplete",
                "committed primary Run lacks its exact initial receipt witnesses",
            )

        try:
            incoming = self._validator.validate(
                AdmissionEnvelope(
                    source="schedule",
                    event_id=occurrence.id,
                    instance_id=schedule.instance_id,
                    trigger_id=schedule.trigger_id,
                    workflow_id=schedule.workflow_id,
                    mode=command.mode,
                    brief_id=command.brief_id,
                    brief_revision=command.brief_revision,
                    configuration_revision=command.configuration_revision,
                    admitted_payload=command.admitted_payload,
                )
            )
            envelope, snapshot, redacted_projection, classification = _validated_parts(incoming)
            digests = derive_admission_digests(envelope, self._digest_key)
        except (IncomingWorkValidationError, TypeError, ValueError) as exc:
            raise ScheduleClaimProcessingError(
                "committed_replay_conflict",
                "retry input no longer matches the admitted schedule occurrence",
            ) from exc

        same_projection = hmac.compare_digest(
            canonical_json_bytes(work_item.redacted_input_projection),
            canonical_json_bytes(redacted_projection),
        )
        same_payload = hmac.compare_digest(
            canonical_json_bytes(work_item.admitted_payload),
            canonical_json_bytes(envelope.admitted_payload),
        )
        if (
            work_item.mode is not envelope.mode
            or work_item.brief_id != envelope.brief_id
            or work_item.brief_revision != envelope.brief_revision
            or work_item.configuration_revision != envelope.configuration_revision
            or not same_payload
            or not hmac.compare_digest(work_item.input_digest, digests.input_digest)
            or not hmac.compare_digest(work_item.admission_digest, digests.admission_digest)
            or work_item.digest_key_version != digests.digest_key_version
            or work_item.input_schema_id != snapshot.input_schema_id
            or work_item.input_schema_hash != snapshot.input_schema_hash
            or work_item.input_classification is not classification
            or not same_projection
            or snapshot.catalog_hash != self._current_catalog_hash
        ):
            raise ScheduleClaimProcessingError(
                "committed_replay_conflict",
                "retry input identifies different already-admitted schedule work",
            )
        return work_item, run

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
