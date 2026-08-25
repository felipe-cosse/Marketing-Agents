"""SCHED-05: occurrence outcome and next occurrence commit as one transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.services import (
    ConfiguredIncomingTrigger,
    IncomingWorkValidator,
    ScheduleClaimProcessingError,
    ScheduleClaimProcessingService,
    ScheduleClaimService,
    ScheduleOccurrenceCommand,
    WorkflowAdmissionDefinition,
)
from marketing_agents.domain.audit import AuditEvent
from marketing_agents.domain.entities import Schedule, ScheduleClaim
from marketing_agents.domain.enums import MisfirePolicy, OccurrenceState, TriggerKind, WorkMode
from marketing_agents.domain.schedule_misfire import ScheduleDisposition
from marketing_agents.domain.schedule_occurrence_identity import SCHEDULE_RECURRENCE_VERSION
from marketing_agents.infrastructure.db import (
    AuditEventRecord,
    Base,
    DatabaseRuntime,
    RunRecord,
    RunStateTransitionRecord,
    ScheduleOccurrenceRecord,
    ScheduleRecord,
    SQLAlchemyAuditRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyScheduleRepository,
    SQLAlchemyUnitOfWork,
    SQLAlchemyUnitOfWorkFactory,
    WorkItemRecord,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import Table, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

DUE = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)
LEASE_DURATION = timedelta(minutes=10)
CATALOG_HASH = "catalog-sha256-v1:" + ("5" * 64)
TEMPLATE_ID = "template.sched-05.target"
INSTANCE_ID = "instance.sched-05.target"
TRIGGER_ID = "trigger.sched-05.target"
WORKFLOW_ID = "workflow.sched-05.target"
SCHEDULE_ID = "schedule.sched-05.target"
SCHEMA_ID = "schema.sched-05.input"


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class IncrementingIds:
    def __init__(self) -> None:
        self._next = 0

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.sched-05.{self._next:04d}"


class MinuteRecurrence:
    def next_after(
        self,
        *,
        cron: str,
        timezone: str,
        after_utc: datetime,
    ) -> datetime:
        assert cron == "* * * * *"
        assert timezone == "UTC"
        return after_utc + timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class TemplateStub:
    id: str = TEMPLATE_ID
    supported_trigger_types: tuple[str, ...] = ("schedule",)


@dataclass(frozen=True, slots=True)
class InstanceStub:
    id: str = INSTANCE_ID
    template_id: str = TEMPLATE_ID
    enabled: bool = True
    configuration_revision: int = 7


@dataclass(slots=True)
class AdvanceFaultProbe:
    uncommitted_counts: tuple[int, int, int, int, int] | None = None
    advanced_schedule: tuple[Any, ...] | None = None


class FaultBeforeCommitUnitOfWork(SQLAlchemyUnitOfWork):
    """Expose the complete intended write set, then fail at the sole commit."""

    def __init__(self, *args: Any, probe: AdvanceFaultProbe) -> None:
        super().__init__(*args)
        self._probe = probe

    async def commit(self) -> NoReturn:
        session = self._require_session()
        self._probe.uncommitted_counts = await _session_counts(session)
        self._probe.advanced_schedule = await _schedule_snapshot(
            session,
            SCHEDULE_ID,
        )
        raise RuntimeError("injected failure at sole processing commit")


class ExpireAtFinalAdvanceScheduleRepository(SQLAlchemyScheduleRepository):
    """Move only the final CAS timestamp beyond expiry after receipt creation."""

    def __init__(self, session: AsyncSession, probe: AdvanceFaultProbe) -> None:
        super().__init__(session)
        self._probe_session = session
        self._probe = probe

    async def advance_and_release_claim(
        self,
        *,
        claim: ScheduleClaim,
        next_run_at_utc: datetime,
        completed_at_utc: datetime,
    ) -> Schedule | None:
        del completed_at_utc
        self._probe.uncommitted_counts = await _session_counts(self._probe_session)
        return await super().advance_and_release_claim(
            claim=claim,
            next_run_at_utc=next_run_at_utc,
            completed_at_utc=claim.lease_expires_at_utc + timedelta(microseconds=1),
        )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


def _uow_factory(
    runtime: DatabaseRuntime,
    *,
    schedule_factory: Any = SQLAlchemyScheduleRepository,
) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            schedules=schedule_factory,
        ),
    )


def _dependencies(
    factory: SQLAlchemyUnitOfWorkFactory,
    clock: MutableClock,
) -> OrchestrationDependencies:
    return OrchestrationDependencies(clock, IncrementingIds(), factory)


def _schedule(
    *,
    policy: MisfirePolicy,
    grace_seconds: int,
) -> Schedule:
    return Schedule(
        id=SCHEDULE_ID,
        trigger_id=TRIGGER_ID,
        instance_id=INSTANCE_ID,
        workflow_id=WORKFLOW_ID,
        cron="* * * * *",
        timezone="UTC",
        next_run_at_utc=DUE,
        misfire_policy=policy,
        misfire_grace_seconds=grace_seconds,
        enabled=True,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
    )


def _guard() -> RuntimePolicyGuard:
    return RuntimePolicyGuard(
        RuntimePolicySnapshot(
            allowed_capabilities=(
                CapabilityPolicy(
                    capability_id="sched-05.read",
                    effect="read",
                    connector_family="test",
                ),
            ),
            input_max_bytes=1_048_576,
            output_max_bytes=1_048_576,
            max_json_depth=64,
            max_content_parts=256,
            max_content_characters=1_000_000,
            max_model_calls=1,
            max_tool_calls=1,
            rate_window_max_calls=1,
            rate_window_seconds=60,
            step_timeout_seconds=10,
            run_timeout_seconds=60,
        )
    )


def _validator() -> IncomingWorkValidator:
    return IncomingWorkValidator(
        catalog_hash=CATALOG_HASH,
        templates=(TemplateStub(),),
        instances=(InstanceStub(),),
        input_schemas_by_template={
            TEMPLATE_ID: {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": SCHEMA_ID,
                "type": "object",
                "properties": {"campaign_id": {"type": "string", "maxLength": 80}},
                "required": ["campaign_id"],
                "additionalProperties": False,
            }
        },
        triggers=(
            ConfiguredIncomingTrigger(
                id=TRIGGER_ID,
                instance_id=INSTANCE_ID,
                kind=TriggerKind.SCHEDULE,
                source="schedule",
                workflow_ids=(WORKFLOW_ID,),
            ),
        ),
        workflows=(
            WorkflowAdmissionDefinition(
                id=WORKFLOW_ID,
                eligible_template_ids=(TEMPLATE_ID,),
                eligible_trigger_kinds=(TriggerKind.SCHEDULE,),
                allowed_modes=(WorkMode.MOCK_EXECUTION,),
                input_schema_ids_by_template={TEMPLATE_ID: SCHEMA_ID},
            ),
        ),
        campaign_brief_revisions=(),
        guard=_guard(),
    )


def _key() -> DigestKey:
    return DigestKey(bytes(range(32)))


def _command(claim: ScheduleClaim) -> ScheduleOccurrenceCommand:
    return ScheduleOccurrenceCommand(
        claim=claim,
        mode=WorkMode.MOCK_EXECUTION,
        configuration_revision=7,
        admitted_payload={"campaign_id": "campaign.sched-05"},
    )


def _service(
    dependencies: OrchestrationDependencies,
) -> ScheduleClaimProcessingService:
    return ScheduleClaimProcessingService(
        dependencies,
        _key(),
        _validator(),
        recurrence=MinuteRecurrence(),
        current_catalog_hash=CATALOG_HASH,
    )


async def _seed_claim(
    runtime: DatabaseRuntime,
    *,
    claimed_at_utc: datetime,
    policy: MisfirePolicy,
    grace_seconds: int,
    schedule_factory: Any = SQLAlchemyScheduleRepository,
) -> tuple[SQLAlchemyUnitOfWorkFactory, OrchestrationDependencies, ScheduleClaim]:
    factory = _uow_factory(runtime, schedule_factory=schedule_factory)
    async with factory() as unit_of_work:
        inserted = await unit_of_work.schedules.add_or_get(
            _schedule(policy=policy, grace_seconds=grace_seconds)
        )
        assert inserted.inserted is True
        await unit_of_work.commit()
    dependencies = _dependencies(factory, MutableClock(claimed_at_utc))
    claim = await ScheduleClaimService(
        dependencies,
        lease_duration=LEASE_DURATION,
    ).claim_due_once(lease_owner="worker.sched-05.primary")
    assert claim is not None
    return factory, dependencies, claim


async def _session_counts(session: AsyncSession) -> tuple[int, int, int, int, int]:
    models = (
        ScheduleOccurrenceRecord,
        WorkItemRecord,
        RunRecord,
        RunStateTransitionRecord,
        AuditEventRecord,
    )
    counts = [
        int(await session.scalar(select(func.count()).select_from(model)) or 0) for model in models
    ]
    return cast(tuple[int, int, int, int, int], tuple(counts))


async def _counts(runtime: DatabaseRuntime) -> tuple[int, int, int, int, int]:
    async with runtime.session_factory() as session:
        return await _session_counts(session)


async def _schedule_snapshot(
    session: AsyncSession,
    schedule_id: str,
) -> tuple[Any, ...]:
    row = (
        await session.execute(
            select(
                ScheduleRecord.next_run_at_utc,
                ScheduleRecord.last_scheduled_at_utc,
                ScheduleRecord.version,
                ScheduleRecord.lease_owner,
                ScheduleRecord.lease_claimed_at_utc,
                ScheduleRecord.lease_expires_at_utc,
                ScheduleRecord.integrity_digest,
            ).where(ScheduleRecord.id == schedule_id)
        )
    ).one()
    return tuple(row)


async def _snapshot(runtime: DatabaseRuntime) -> tuple[Any, ...]:
    async with runtime.session_factory() as session:
        return await _schedule_snapshot(session, SCHEDULE_ID)


async def _audit_rows(runtime: DatabaseRuntime) -> tuple[tuple[Any, ...], ...]:
    async with runtime.session_factory() as session:
        rows = await session.execute(
            select(
                AuditEventRecord.event_type,
                AuditEventRecord.aggregate_type,
                AuditEventRecord.aggregate_id,
                AuditEventRecord.run_id,
                AuditEventRecord.run_sequence,
                AuditEventRecord.mutation_version,
                AuditEventRecord.safe_metadata,
            ).order_by(AuditEventRecord.global_sequence)
        )
        return tuple(tuple(row) for row in rows)


def _assert_scheduler_audits(
    events: tuple[AuditEvent, AuditEvent],
    *,
    occurrence_event_type: str,
) -> None:
    assert tuple(event.event_type for event in events) == (
        occurrence_event_type,
        "schedule.next_occurrence_persisted",
    )
    assert all(event.run_id is None and event.run_sequence is None for event in events)
    assert events[0].aggregate_type == "schedule_occurrence"
    assert events[1].aggregate_type == "schedule"


@pytest.mark.asyncio
async def test_sched_05_on_time_outcome_advances_and_releases_in_one_commit(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "on-time-atomic.db")
    factory, dependencies, claim = await _seed_claim(
        runtime,
        claimed_at_utc=DUE,
        policy=MisfirePolicy.RUN_ONCE,
        grace_seconds=300,
    )
    try:
        result = await _service(dependencies).process_claimed_once(_command(claim))

        assert result.plan.disposition is ScheduleDisposition.ON_TIME
        assert result.plan.next_run_at_utc == DUE + timedelta(minutes=1)
        assert result.occurrence.state is OccurrenceState.ENQUEUED
        assert result.work_item is not None and result.run is not None
        assert result.occurrence.work_item_id == result.work_item.id
        assert result.occurrence.run_id == result.run.id
        assert result.occurrence.misfire_policy_applied is None
        assert result.resulting_schedule.last_scheduled_at_utc == DUE
        assert result.resulting_schedule.next_run_at_utc == DUE + timedelta(minutes=1)
        assert result.resulting_schedule.version == claim.version + 1
        _assert_scheduler_audits(
            result.audit_events,
            occurrence_event_type="schedule.occurrence_created",
        )

        async with factory() as unit_of_work:
            persisted_schedule = await unit_of_work.schedules.get(SCHEDULE_ID)
            persisted_occurrence = await unit_of_work.schedules.get_occurrence(result.occurrence.id)
        assert persisted_schedule == result.resulting_schedule
        assert persisted_occurrence == result.occurrence
        assert await _counts(runtime) == (1, 1, 1, 1, 3)
        assert (await _snapshot(runtime))[:6] == (
            DUE + timedelta(minutes=1),
            DUE,
            claim.version + 1,
            None,
            None,
            None,
        )

        audit_rows = await _audit_rows(runtime)
        assert tuple(row[0] for row in audit_rows) == (
            "run.received",
            "schedule.occurrence_created",
            "schedule.next_occurrence_persisted",
        )
        assert all(row[3] is None and row[4] is None for row in audit_rows[1:])
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_05_skip_persists_exact_missed_range_without_work(
    tmp_path: Path,
) -> None:
    evaluated_at = DUE + timedelta(minutes=3)
    runtime = await _runtime(tmp_path / "skip-atomic.db")
    _, dependencies, claim = await _seed_claim(
        runtime,
        claimed_at_utc=evaluated_at,
        policy=MisfirePolicy.SKIP,
        grace_seconds=0,
    )
    try:
        result = await _service(dependencies).process_claimed_once(_command(claim))

        assert result.plan.disposition is ScheduleDisposition.SKIP
        assert result.plan.next_run_at_utc == DUE + timedelta(minutes=4)
        assert result.occurrence.state is OccurrenceState.SKIPPED
        assert result.work_item is None and result.run is None
        assert result.occurrence.work_item_id is None and result.occurrence.run_id is None
        assert result.occurrence.misfire_policy_applied is MisfirePolicy.SKIP
        assert result.occurrence.misfire_grace_seconds == 0
        assert result.occurrence.misfire_evaluated_at_utc == evaluated_at
        assert result.occurrence.first_missed_at_utc == DUE
        assert result.occurrence.last_missed_at_utc == evaluated_at
        assert result.occurrence.missed_count == 4
        assert result.resulting_schedule.last_scheduled_at_utc == DUE
        assert result.resulting_schedule.next_run_at_utc == DUE + timedelta(minutes=4)
        assert result.resulting_schedule.version == claim.version + 1
        _assert_scheduler_audits(
            result.audit_events,
            occurrence_event_type="schedule.misfire_skipped",
        )
        assert await _counts(runtime) == (1, 0, 0, 0, 2)

        audit_rows = await _audit_rows(runtime)
        assert tuple(row[0] for row in audit_rows) == (
            "schedule.misfire_skipped",
            "schedule.next_occurrence_persisted",
        )
        assert all(row[3] is None and row[4] is None for row in audit_rows)
        assert audit_rows[0][6]["missed_count"] == 4
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_05_run_once_coalesces_range_to_exactly_one_receipt(
    tmp_path: Path,
) -> None:
    evaluated_at = DUE + timedelta(minutes=3, seconds=30)
    runtime = await _runtime(tmp_path / "run-once-atomic.db")
    _, dependencies, claim = await _seed_claim(
        runtime,
        claimed_at_utc=evaluated_at,
        policy=MisfirePolicy.RUN_ONCE,
        grace_seconds=0,
    )
    try:
        result = await _service(dependencies).process_claimed_once(_command(claim))

        assert result.plan.disposition is ScheduleDisposition.RUN_ONCE
        assert result.plan.next_run_at_utc == DUE + timedelta(minutes=4)
        assert result.occurrence.state is OccurrenceState.ENQUEUED
        assert result.work_item is not None and result.run is not None
        assert result.occurrence.work_item_id == result.work_item.id
        assert result.occurrence.run_id == result.run.id
        assert result.occurrence.misfire_policy_applied is MisfirePolicy.RUN_ONCE
        assert result.occurrence.misfire_grace_seconds == 0
        assert result.occurrence.misfire_evaluated_at_utc == evaluated_at
        assert result.occurrence.first_missed_at_utc == DUE
        assert result.occurrence.last_missed_at_utc == DUE + timedelta(minutes=3)
        assert result.occurrence.missed_count == 4
        _assert_scheduler_audits(
            result.audit_events,
            occurrence_event_type="schedule.misfire_run_once",
        )
        assert await _counts(runtime) == (1, 1, 1, 1, 3)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_05_fault_after_advance_rolls_back_to_exact_precommitted_claim(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "post-advance-rollback.db")
    probe = AdvanceFaultProbe()
    factory, dependencies, claim = await _seed_claim(
        runtime,
        claimed_at_utc=DUE,
        policy=MisfirePolicy.RUN_ONCE,
        grace_seconds=300,
    )
    precommitted_claim = await _snapshot(runtime)

    def faulting_uow_factory() -> FaultBeforeCommitUnitOfWork:
        return FaultBeforeCommitUnitOfWork(
            runtime.session_factory,
            factory.repository_factories,
            probe=probe,
        )

    faulting_dependencies = OrchestrationDependencies(
        dependencies.clock,
        IncrementingIds(),
        faulting_uow_factory,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="injected failure at sole processing commit",
        ):
            await _service(faulting_dependencies).process_claimed_once(_command(claim))

        assert probe.uncommitted_counts == (1, 1, 1, 1, 3)
        assert probe.advanced_schedule is not None
        assert probe.advanced_schedule[:6] == (
            DUE + timedelta(minutes=1),
            DUE,
            claim.version + 1,
            None,
            None,
            None,
        )
        assert await _counts(runtime) == (0, 0, 0, 0, 0)
        assert await _snapshot(runtime) == precommitted_claim
        assert precommitted_claim[:6] == (
            claim.scheduled_for_utc,
            None,
            claim.version,
            claim.lease_owner,
            claim.claimed_at_utc,
            claim.lease_expires_at_utc,
        )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_05_final_cas_expiry_rolls_back_receipt_and_preserves_claim(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "final-cas-expiry.db")
    probe = AdvanceFaultProbe()

    def expiring_schedule_factory(session: AsyncSession) -> SQLAlchemyScheduleRepository:
        return ExpireAtFinalAdvanceScheduleRepository(session, probe)

    _, dependencies, claim = await _seed_claim(
        runtime,
        claimed_at_utc=DUE,
        policy=MisfirePolicy.RUN_ONCE,
        grace_seconds=300,
        schedule_factory=expiring_schedule_factory,
    )
    precommitted_claim = await _snapshot(runtime)
    try:
        with pytest.raises(ScheduleClaimProcessingError) as expired:
            await _service(dependencies).process_claimed_once(_command(claim))

        assert expired.value.code == "claim_fence_lost"
        assert probe.uncommitted_counts == (1, 1, 1, 1, 1)
        assert await _counts(runtime) == (0, 0, 0, 0, 0)
        assert await _snapshot(runtime) == precommitted_claim
    finally:
        await runtime.dispose()


def test_sched_05_schema_has_portable_advance_and_misfire_constraints() -> None:
    schedule_table = cast(Table, ScheduleRecord.__table__)
    occurrence_table = cast(Table, ScheduleOccurrenceRecord.__table__)
    audit_table = cast(Table, AuditEventRecord.__table__)
    postgres_dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    for table in (schedule_table, occurrence_table, audit_table):
        sqlite_ddl = " ".join(
            str(CreateTable(table).compile(dialect=sqlite.dialect())).lower().split()
        )
        postgres_ddl = " ".join(
            str(CreateTable(table).compile(dialect=postgres_dialect)).lower().split()
        )
        for ddl in (sqlite_ddl, postgres_ddl):
            if table is schedule_table:
                assert "last_scheduled_at_utc" in ddl
                assert "ck_schedules_last_precedes_next" in ddl
            elif table is occurrence_table:
                assert "misfire_policy_applied" in ddl
                assert "misfire_evaluated_at_utc" in ddl
                assert "first_missed_at_utc" in ddl
                assert "last_missed_at_utc" in ddl
                assert "missed_count" in ddl
                assert "ck_schedule_occurrences_misfire_complete" in ddl
                assert "ck_schedule_occurrences_misfire_state" in ddl
                assert "uq_schedule_occurrences_id_schedule" in ddl
            else:
                assert "run_id" in ddl and "run_sequence" in ddl
                assert "schedule_id" in ddl and "occurrence_id" in ddl
                assert "ck_audit_events_timeline_scope" in ddl
                assert "fk_audit_events_occurrence_schedule" in ddl
