"""Atomic schedule-occurrence admission through the normal WorkItem/Run receipt path."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from marketing_agents.application.orchestration.dependencies import (
    OrchestrationDependencies,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import (
    Run,
    Schedule,
    ScheduleClaim,
    ScheduleOccurrence,
    WorkItem,
)
from marketing_agents.domain.enums import OccurrenceState, WorkMode
from marketing_agents.domain.schedule_misfire import (
    ScheduleDisposition,
    ScheduleOccurrencePlan,
)
from marketing_agents.domain.schedule_occurrence_identity import (
    schedule_local_snapshot,
    schedule_occurrence_id,
)
from marketing_agents.security.digest_key import DigestKey

from .idempotent_work_receipt import (
    IdempotentWorkRunReceiptService,
    WorkRunReceiptDisposition,
)
from .incoming_work_validation import IncomingWorkValidator


class ScheduleOccurrenceIngressError(RuntimeError):
    """Stable fail-closed rejection at the claimed schedule ingress boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScheduleOccurrenceIngressDisposition(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleOccurrenceCommand:
    """Trusted schedule input fields that still require catalog validation."""

    claim: ScheduleClaim
    mode: WorkMode
    configuration_revision: int
    admitted_payload: Mapping[str, Any]
    brief_id: str | None = None
    brief_revision: int | None = None


@dataclass(frozen=True, slots=True)
class ScheduleOccurrenceIngressResult:
    occurrence: ScheduleOccurrence
    work_item: WorkItem
    run: Run
    disposition: ScheduleOccurrenceIngressDisposition


class ScheduleOccurrenceIngressService:
    """Create or replay one claimed occurrence and its idempotent Run receipt."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        digest_key: DigestKey,
        validator: IncomingWorkValidator,
        *,
        current_catalog_hash: str,
    ) -> None:
        if not isinstance(validator, IncomingWorkValidator):
            raise ValueError("schedule ingress requires the configured incoming-work validator")
        self._dependencies = dependencies
        self._validator = validator
        self._receipt = IdempotentWorkRunReceiptService(
            dependencies,
            digest_key,
            current_catalog_hash=current_catalog_hash,
        )

    async def admit_claimed_once(
        self,
        command: ScheduleOccurrenceCommand,
    ) -> ScheduleOccurrenceIngressResult:
        if type(command) is not ScheduleOccurrenceCommand:
            raise ValueError("schedule ingress requires a typed occurrence command")
        if type(command.claim) is not ScheduleClaim:
            raise ValueError("schedule ingress requires an exact ScheduleClaim")

        async with self._dependencies.unit_of_work() as unit_of_work:
            schedule = await unit_of_work.schedules.get(command.claim.schedule_id)
            if schedule is None:
                raise ScheduleOccurrenceIngressError(
                    "schedule_missing",
                    "claimed schedule no longer exists",
                )

            fence_attempted_at = self._dependencies.utc_now()
            if not await unit_of_work.schedules.fence_claim(
                command.claim,
                now=fence_attempted_at,
            ):
                raise ScheduleOccurrenceIngressError(
                    "claim_fence_lost",
                    "schedule claim is stale, replaced, expired, or no longer active",
                )
            fenced_at = self._dependencies.utc_now()
            if (
                fenced_at < command.claim.claimed_at_utc
                or command.claim.lease_expires_at_utc < fenced_at
            ):
                raise ScheduleOccurrenceIngressError(
                    "claim_fence_lost",
                    "schedule claim expired while its processing fence was acquired",
                )
            fenced_schedule = await unit_of_work.schedules.get(command.claim.schedule_id)
            if fenced_schedule != schedule:
                raise ScheduleOccurrenceIngressError(
                    "schedule_changed_during_ingress",
                    "schedule routing changed before its claim fence was acquired",
                )
            result = await self.admit_claimed_in_uow(
                unit_of_work,
                command,
                schedule=fenced_schedule,
            )
            await unit_of_work.commit()
            return result

    async def admit_claimed_in_uow(
        self,
        unit_of_work: UnitOfWork,
        command: ScheduleOccurrenceCommand,
        *,
        schedule: Schedule,
        plan: ScheduleOccurrencePlan | None = None,
    ) -> ScheduleOccurrenceIngressResult:
        """Admit one work-producing occurrence without fencing or committing."""

        if type(command) is not ScheduleOccurrenceCommand:
            raise ValueError("schedule ingress requires a typed occurrence command")
        if type(command.claim) is not ScheduleClaim:
            raise ValueError("schedule ingress requires an exact ScheduleClaim")
        if type(schedule) is not Schedule:
            raise ValueError("schedule ingress requires the exact fenced Schedule")
        claim = command.claim
        if (
            not schedule.enabled
            or schedule.id != claim.schedule_id
            or schedule.next_run_at_utc != claim.scheduled_for_utc
            or schedule.recurrence_version != claim.recurrence_version
            or schedule.version != claim.version
        ):
            raise ScheduleOccurrenceIngressError(
                "claim_schedule_mismatch",
                "schedule no longer matches the exact claimed due snapshot",
            )
        if plan is not None and (
            type(plan) is not ScheduleOccurrencePlan
            or plan.schedule_id != claim.schedule_id
            or plan.scheduled_for_utc != claim.scheduled_for_utc
            or plan.recurrence_version != claim.recurrence_version
            or plan.disposition is ScheduleDisposition.SKIP
        ):
            raise ScheduleOccurrenceIngressError(
                "occurrence_plan_mismatch",
                "work ingress requires an exact on-time or run-once occurrence plan",
            )

        occurrence_id = schedule_occurrence_id(
            claim.schedule_id,
            claim.scheduled_for_utc,
            recurrence_version=claim.recurrence_version,
        )
        scheduled_local, timezone_fold = schedule_local_snapshot(
            claim.scheduled_for_utc,
            schedule.timezone,
        )
        misfire_plan = (
            plan if plan is not None and plan.disposition is ScheduleDisposition.RUN_ONCE else None
        )
        pending = ScheduleOccurrence(
            id=occurrence_id,
            schedule_id=schedule.id,
            scheduled_for_utc=claim.scheduled_for_utc,
            scheduled_local=scheduled_local,
            timezone=schedule.timezone,
            timezone_fold=timezone_fold,
            recurrence_version=claim.recurrence_version,
            state=OccurrenceState.CLAIMED,
            misfire_policy_applied=(schedule.misfire_policy if misfire_plan is not None else None),
            misfire_grace_seconds=(
                schedule.misfire_grace_seconds if misfire_plan is not None else None
            ),
            misfire_evaluated_at_utc=(claim.claimed_at_utc if misfire_plan is not None else None),
            first_missed_at_utc=(
                misfire_plan.first_missed_at_utc if misfire_plan is not None else None
            ),
            last_missed_at_utc=(
                misfire_plan.last_missed_at_utc if misfire_plan is not None else None
            ),
            missed_count=(misfire_plan.missed_count if misfire_plan is not None else None),
        )
        incoming = self._validator.validate(
            AdmissionEnvelope(
                source="schedule",
                event_id=pending.id,
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

        occurrence_insert = await unit_of_work.schedules.add_occurrence_or_get(pending)
        if (
            not occurrence_insert.inserted
            and occurrence_insert.occurrence.state is not OccurrenceState.ENQUEUED
        ):
            raise ScheduleOccurrenceIngressError(
                "occurrence_partial_receipt",
                "persisted occurrence lacks its atomic WorkItem and Run receipt",
            )

        receipt = await self._receipt.receive_in_uow(
            unit_of_work,
            incoming,
            audit_context=AuditContext.worker(
                claim.lease_owner,
                correlation_id=pending.id,
            ),
        )
        receipt_created = receipt.disposition is WorkRunReceiptDisposition.CREATED
        if occurrence_insert.inserted != receipt_created:
            raise ScheduleOccurrenceIngressError(
                "occurrence_receipt_disposition_mismatch",
                "occurrence and WorkItem/Run receipt must create or replay together",
            )

        occurrence_link = await unit_of_work.schedules.mark_occurrence_enqueued(
            occurrence_id=pending.id,
            work_item_id=receipt.work_item.id,
            run_id=receipt.run.id,
        )
        if occurrence_link.linked != receipt_created:
            raise ScheduleOccurrenceIngressError(
                "occurrence_link_disposition_mismatch",
                "occurrence link and WorkItem/Run receipt dispositions disagree",
            )
        return ScheduleOccurrenceIngressResult(
            occurrence=occurrence_link.occurrence,
            work_item=receipt.work_item,
            run=receipt.run,
            disposition=(
                ScheduleOccurrenceIngressDisposition.CREATED
                if receipt_created
                else ScheduleOccurrenceIngressDisposition.REPLAYED
            ),
        )
