"""SCHED-03: stable schedule occurrence identity feeds idempotent Run receipt."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    ScheduleClaimService,
    ScheduleOccurrenceCommand,
    ScheduleOccurrenceIngressDisposition,
    ScheduleOccurrenceIngressError,
    ScheduleOccurrenceIngressService,
    WorkflowAdmissionDefinition,
    WorkIdempotencyError,
)
from marketing_agents.domain.entities import Schedule, ScheduleClaim, ScheduleOccurrence
from marketing_agents.domain.enums import MisfirePolicy, OccurrenceState, TriggerKind, WorkMode
from marketing_agents.domain.schedule_occurrence_identity import (
    SCHEDULE_RECURRENCE_VERSION,
    schedule_local_snapshot,
    schedule_occurrence_id,
)
from marketing_agents.infrastructure.db import (
    AuditEventRecord,
    Base,
    DatabaseRuntime,
    RunRecord,
    RunStateTransitionRecord,
    ScheduleOccurrenceRecord,
    SchedulePersistenceConflict,
    SQLAlchemyAuditRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyScheduleRepository,
    SQLAlchemyUnitOfWorkFactory,
    WorkItemRecord,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

DUE = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)
NOW = DUE + timedelta(minutes=1)
LEASE_DURATION = timedelta(minutes=2)
CATALOG_HASH = "catalog-sha256-v1:" + ("a" * 64)
TEMPLATE_ID = "template.sched-03.target"
INSTANCE_ID = "instance.sched-03.target"
TRIGGER_ID = "trigger.sched-03.target"
WORKFLOW_ID = "workflow.sched-03.target"
SCHEDULE_ID = "schedule.sched-03.target"
SCHEMA_ID = "schema.sched-03.input"


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class IncrementingIds:
    def __init__(self, start: int = 0) -> None:
        self._next = start

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.sched-03.{self._next:04d}"


@dataclass(frozen=True, slots=True)
class TemplateStub:
    id: str = TEMPLATE_ID
    supported_trigger_types: tuple[str, ...] = ("schedule",)


@dataclass(frozen=True, slots=True)
class InstanceStub:
    id: str = INSTANCE_ID
    template_id: str = TEMPLATE_ID
    enabled: bool = True
    configuration_revision: int = 3


@dataclass(slots=True)
class LinkFaultProbe:
    observed_uncommitted_counts: tuple[int, int, int, int, int] | None = None


class FaultAfterReceiptScheduleRepository(SQLAlchemyScheduleRepository):
    """Raise only after the receipt rows are visible in the caller transaction."""

    def __init__(self, session: AsyncSession, probe: LinkFaultProbe) -> None:
        super().__init__(session)
        self._probe_session = session
        self._probe = probe

    async def mark_occurrence_enqueued(
        self,
        *,
        occurrence_id: str,
        work_item_id: str,
        run_id: str,
    ) -> NoReturn:
        del occurrence_id, work_item_id, run_id
        self._probe.observed_uncommitted_counts = await _session_counts(self._probe_session)
        raise RuntimeError("injected post-receipt occurrence-link failure")


class ExpireAfterFenceScheduleRepository(SQLAlchemyScheduleRepository):
    """Advance the injected clock after the row fence but before any insert."""

    def __init__(self, session: AsyncSession, clock: MutableClock) -> None:
        super().__init__(session)
        self._clock = clock

    async def fence_claim(
        self,
        claim: ScheduleClaim,
        *,
        now: datetime,
    ) -> bool:
        fenced = await super().fence_claim(claim, now=now)
        if fenced:
            self._clock.current = claim.lease_expires_at_utc + timedelta(microseconds=1)
        return fenced


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
    *,
    id_start: int = 0,
) -> OrchestrationDependencies:
    return OrchestrationDependencies(clock, IncrementingIds(id_start), factory)


def _schedule() -> Schedule:
    return Schedule(
        id=SCHEDULE_ID,
        trigger_id=TRIGGER_ID,
        instance_id=INSTANCE_ID,
        workflow_id=WORKFLOW_ID,
        cron="0 9 * * *",
        timezone="America/Los_Angeles",
        next_run_at_utc=DUE,
        misfire_policy=MisfirePolicy.RUN_ONCE,
        enabled=True,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
    )


async def _persist_schedule(factory: SQLAlchemyUnitOfWorkFactory) -> None:
    async with factory() as unit_of_work:
        inserted = await unit_of_work.schedules.add_or_get(_schedule())
        assert inserted.inserted is True
        await unit_of_work.commit()


def _guard() -> RuntimePolicyGuard:
    return RuntimePolicyGuard(
        RuntimePolicySnapshot(
            allowed_capabilities=(
                CapabilityPolicy(
                    capability_id="sched-03.read",
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


def _command(
    claim: ScheduleClaim,
    *,
    campaign_id: str = "campaign.restart-safe",
) -> ScheduleOccurrenceCommand:
    return ScheduleOccurrenceCommand(
        claim=claim,
        mode=WorkMode.MOCK_EXECUTION,
        configuration_revision=3,
        admitted_payload={"campaign_id": campaign_id},
    )


def _ingress_service(
    dependencies: OrchestrationDependencies,
) -> ScheduleOccurrenceIngressService:
    return ScheduleOccurrenceIngressService(
        dependencies,
        _key(),
        _validator(),
        current_catalog_hash=CATALOG_HASH,
    )


async def _seed_claim(
    runtime: DatabaseRuntime,
    clock: MutableClock,
    *,
    schedule_factory: Any = SQLAlchemyScheduleRepository,
) -> tuple[SQLAlchemyUnitOfWorkFactory, OrchestrationDependencies, ScheduleClaim]:
    factory = _uow_factory(runtime, schedule_factory=schedule_factory)
    await _persist_schedule(factory)
    dependencies = _dependencies(factory, clock)
    claim = await ScheduleClaimService(
        dependencies,
        lease_duration=LEASE_DURATION,
    ).claim_due_once(lease_owner="worker.sched-03.primary")
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
    counts: list[int] = []
    for model in models:
        counts.append(int(await session.scalar(select(func.count()).select_from(model)) or 0))
    return cast(tuple[int, int, int, int, int], tuple(counts))


async def _counts(runtime: DatabaseRuntime) -> tuple[int, int, int, int, int]:
    async with runtime.session_factory() as session:
        return await _session_counts(session)


@pytest.mark.asyncio
async def test_sched_03_occurrence_event_creates_one_atomic_run_receipt_and_restarts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "occurrence-restart.db"
    runtime = await _runtime(database_path)
    clock = MutableClock()
    factory, dependencies, claim = await _seed_claim(runtime, clock)
    expected_occurrence_id = schedule_occurrence_id(
        claim.schedule_id,
        claim.scheduled_for_utc,
        recurrence_version=claim.recurrence_version,
    )
    try:
        created = await _ingress_service(dependencies).admit_claimed_once(_command(claim))
        assert created.disposition is ScheduleOccurrenceIngressDisposition.CREATED
        assert created.occurrence.id == expected_occurrence_id
        assert created.occurrence.state is OccurrenceState.ENQUEUED
        assert created.occurrence.work_item_id == created.work_item.id
        assert created.occurrence.run_id == created.run.id
        assert created.occurrence.scheduled_local == "2026-08-25T09:00:00.000000"
        assert created.occurrence.timezone == "America/Los_Angeles"
        assert created.occurrence.timezone_fold == 0
        assert created.work_item.source == "schedule"
        assert created.work_item.event_id == expected_occurrence_id
        assert created.work_item.instance_id == INSTANCE_ID
        assert created.work_item.trigger_id == TRIGGER_ID
        assert created.work_item.workflow_id == WORKFLOW_ID
        assert created.run.work_item_id == created.work_item.id
        assert await _counts(runtime) == (1, 1, 1, 1, 1)

        async with factory() as unit_of_work:
            history = await unit_of_work.runs.list_transitions(created.run.id)
            persisted = await unit_of_work.schedules.get_occurrence(expected_occurrence_id)
        assert len(history) == 1
        assert history[0].sequence == 1
        assert persisted == created.occurrence
    finally:
        await runtime.dispose()

    restarted = await _runtime(database_path)
    replay_clock = MutableClock(NOW + timedelta(seconds=30))
    try:
        restarted_factory = _uow_factory(restarted)
        restarted_dependencies = _dependencies(restarted_factory, replay_clock, id_start=100)
        replayed = await _ingress_service(restarted_dependencies).admit_claimed_once(
            _command(claim)
        )
        assert replayed.disposition is ScheduleOccurrenceIngressDisposition.REPLAYED
        assert replayed.occurrence.id == created.occurrence.id
        assert replayed.work_item.id == created.work_item.id
        assert replayed.run.id == created.run.id
        assert await _counts(restarted) == (1, 1, 1, 1, 1)
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_03_changed_payload_collides_without_duplicate_receipt(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "payload-collision.db")
    clock = MutableClock()
    _, dependencies, claim = await _seed_claim(runtime, clock)
    service = _ingress_service(dependencies)
    try:
        created = await service.admit_claimed_once(_command(claim))
        with pytest.raises(WorkIdempotencyError) as collided:
            await service.admit_claimed_once(
                _command(claim, campaign_id="campaign.changed-payload")
            )
        assert collided.value.code == "idempotency_conflict"
        assert collided.value.existing_work_item_id == created.work_item.id
        assert await _counts(runtime) == (1, 1, 1, 1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_case", ("stale", "expired", "replaced"))
async def test_sched_03_invalid_claim_fails_before_any_receipt_mutation(
    tmp_path: Path,
    claim_case: str,
) -> None:
    runtime = await _runtime(tmp_path / f"claim-{claim_case}.db")
    clock = MutableClock()
    factory, dependencies, claim = await _seed_claim(runtime, clock)
    rejected_claim = claim
    if claim_case == "stale":
        rejected_claim = replace(claim, lease_owner="worker.sched-03.stale")
    elif claim_case == "expired":
        clock.current = claim.lease_expires_at_utc + timedelta(microseconds=1)
    else:
        clock.current = claim.lease_expires_at_utc + timedelta(microseconds=1)
        replacement = await ScheduleClaimService(
            _dependencies(factory, clock, id_start=50),
            lease_duration=LEASE_DURATION,
        ).claim_due_once(lease_owner="worker.sched-03.replacement")
        assert replacement is not None and replacement.version == claim.version + 1
    try:
        with pytest.raises(ScheduleOccurrenceIngressError) as rejected:
            await _ingress_service(dependencies).admit_claimed_once(_command(rejected_claim))
        assert rejected.value.code == "claim_fence_lost"
        assert await _counts(runtime) == (0, 0, 0, 0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_03_claim_remains_processable_at_exact_expiry(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "claim-exact-expiry.db")
    clock = MutableClock()
    _, dependencies, claim = await _seed_claim(runtime, clock)
    clock.current = claim.lease_expires_at_utc
    try:
        created = await _ingress_service(dependencies).admit_claimed_once(_command(claim))
        assert created.disposition is ScheduleOccurrenceIngressDisposition.CREATED
        assert await _counts(runtime) == (1, 1, 1, 1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_03_claim_expiring_during_fence_rolls_back_before_receipt(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "claim-expires-during-fence.db")
    clock = MutableClock()

    def expiring_schedule_factory(session: AsyncSession) -> SQLAlchemyScheduleRepository:
        return ExpireAfterFenceScheduleRepository(session, clock)

    _, dependencies, claim = await _seed_claim(
        runtime,
        clock,
        schedule_factory=expiring_schedule_factory,
    )
    try:
        with pytest.raises(ScheduleOccurrenceIngressError) as expired:
            await _ingress_service(dependencies).admit_claimed_once(_command(claim))
        assert expired.value.code == "claim_fence_lost"
        assert await _counts(runtime) == (0, 0, 0, 0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_03_occurrence_id_and_schedule_due_constraints_reject_conflicts(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "occurrence-identity-conflicts.db")
    clock = MutableClock()
    factory, dependencies, claim = await _seed_claim(runtime, clock)
    try:
        created = await _ingress_service(dependencies).admit_claimed_once(_command(claim))
        changed_timezone_alias = ScheduleOccurrence(
            id=created.occurrence.id,
            schedule_id=claim.schedule_id,
            scheduled_for_utc=claim.scheduled_for_utc,
            scheduled_local=created.occurrence.scheduled_local,
            timezone="US/Pacific",
            timezone_fold=0,
            recurrence_version=claim.recurrence_version,
            state=OccurrenceState.CLAIMED,
        )
        async with factory() as unit_of_work:
            with pytest.raises(SchedulePersistenceConflict) as id_conflict:
                await unit_of_work.schedules.add_occurrence_or_get(changed_timezone_alias)
        assert id_conflict.value.code == "occurrence_id_conflict"

        changed_recurrence = "five-field-cron-adr0008-v2"
        same_due_new_id = ScheduleOccurrence(
            id=schedule_occurrence_id(
                claim.schedule_id,
                claim.scheduled_for_utc,
                recurrence_version=changed_recurrence,
            ),
            schedule_id=claim.schedule_id,
            scheduled_for_utc=claim.scheduled_for_utc,
            scheduled_local=created.occurrence.scheduled_local,
            timezone=created.occurrence.timezone,
            timezone_fold=created.occurrence.timezone_fold,
            recurrence_version=changed_recurrence,
            state=OccurrenceState.CLAIMED,
        )
        async with factory() as unit_of_work:
            with pytest.raises(SchedulePersistenceConflict) as due_conflict:
                await unit_of_work.schedules.add_occurrence_or_get(same_due_new_id)
        assert due_conflict.value.code == "occurrence_identity_conflict"
        assert await _counts(runtime) == (1, 1, 1, 1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_03_repository_rejects_a_run_from_another_work_item(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "occurrence-run-work-mismatch.db")
    clock = MutableClock()
    factory, dependencies, claim = await _seed_claim(runtime, clock)
    try:
        created = await _ingress_service(dependencies).admit_claimed_once(_command(claim))
        second_due = claim.scheduled_for_utc + timedelta(days=1)
        scheduled_local, timezone_fold = schedule_local_snapshot(
            second_due,
            created.occurrence.timezone,
        )
        pending = ScheduleOccurrence(
            id=schedule_occurrence_id(
                claim.schedule_id,
                second_due,
                recurrence_version=claim.recurrence_version,
            ),
            schedule_id=claim.schedule_id,
            scheduled_for_utc=second_due,
            scheduled_local=scheduled_local,
            timezone=created.occurrence.timezone,
            timezone_fold=timezone_fold,
            recurrence_version=claim.recurrence_version,
            state=OccurrenceState.CLAIMED,
        )
        async with factory() as unit_of_work:
            inserted = await unit_of_work.schedules.add_occurrence_or_get(pending)
            assert inserted.inserted is True
            await unit_of_work.commit()

        async with factory() as unit_of_work:
            with pytest.raises(SchedulePersistenceConflict) as mismatched:
                await unit_of_work.schedules.mark_occurrence_enqueued(
                    occurrence_id=pending.id,
                    work_item_id="work.sched-03.different",
                    run_id=created.run.id,
                )
        assert mismatched.value.code == "occurrence_receipt_conflict"
        async with factory() as unit_of_work:
            restored = await unit_of_work.schedules.get_occurrence(pending.id)
        assert restored == pending
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_sched_03_occurrence_digest_drift_fails_hydration(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "occurrence-digest-drift.db")
    clock = MutableClock()
    factory, dependencies, claim = await _seed_claim(runtime, clock)
    try:
        created = await _ingress_service(dependencies).admit_claimed_once(_command(claim))
        async with runtime.engine.begin() as connection:
            await connection.execute(
                update(ScheduleOccurrenceRecord)
                .where(ScheduleOccurrenceRecord.id == created.occurrence.id)
                .values(timezone_name="UTC")
            )
        async with factory() as unit_of_work:
            with pytest.raises(SchedulePersistenceConflict) as tampered:
                await unit_of_work.schedules.get_occurrence(created.occurrence.id)
        assert tampered.value.code == "occurrence_tampered"
        assert await _counts(runtime) == (1, 1, 1, 1, 1)
    finally:
        await runtime.dispose()


def test_sched_03_occurrence_schema_has_portable_identity_and_receipt_constraints() -> None:
    occurrence_table = cast(Table, ScheduleOccurrenceRecord.__table__)
    postgres_dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    sqlite_ddl = " ".join(
        str(CreateTable(occurrence_table).compile(dialect=sqlite.dialect())).lower().split()
    )
    postgres_ddl = " ".join(
        str(CreateTable(occurrence_table).compile(dialect=postgres_dialect)).lower().split()
    )
    expected_constraints = (
        "uq_schedule_occurrences_schedule_due",
        "uq_schedule_occurrences_work_item",
        "uq_schedule_occurrences_run",
        "ck_schedule_occurrences_identity_scheme_bounded",
        "ck_schedule_occurrences_recurrence_version_bounded",
        "ck_schedule_occurrences_local_canonical_length",
        "ck_schedule_occurrences_timezone_bounded",
        "ck_schedule_occurrences_fold_supported",
        "ck_schedule_occurrences_state_supported",
        "ck_schedule_occurrences_receipt_complete",
        "ck_schedule_occurrences_state_receipt",
        "ck_schedule_occurrences_integrity_digest_length",
    )
    for ddl in (sqlite_ddl, postgres_ddl):
        for constraint in expected_constraints:
            assert constraint in ddl
        assert "foreign key(schedule_id) references schedules (id) on delete restrict" in ddl
        assert "foreign key(work_item_id) references work_items (id) on delete restrict" in ddl
        assert "foreign key(run_id) references runs (id) on delete restrict" in ddl
    assert "scheduled_for_utc timestamp with time zone not null" in postgres_ddl


@pytest.mark.asyncio
async def test_sched_03_post_receipt_link_fault_rolls_back_every_new_row(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "post-receipt-rollback.db")
    clock = MutableClock()
    probe = LinkFaultProbe()

    def faulting_schedule_factory(session: AsyncSession) -> SQLAlchemyScheduleRepository:
        return FaultAfterReceiptScheduleRepository(session, probe)

    _, dependencies, claim = await _seed_claim(
        runtime,
        clock,
        schedule_factory=faulting_schedule_factory,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="injected post-receipt occurrence-link failure",
        ):
            await _ingress_service(dependencies).admit_claimed_once(_command(claim))
        assert probe.observed_uncommitted_counts == (1, 1, 1, 1, 1)
        assert await _counts(runtime) == (0, 0, 0, 0, 0)
    finally:
        await runtime.dispose()
