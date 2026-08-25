"""SCHED-06: strict-expiry recovery and committed-outcome suppression."""

from __future__ import annotations

import asyncio
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
from marketing_agents.application.ports.repositories import ScheduleRepository
from marketing_agents.application.services import (
    ConfiguredIncomingTrigger,
    IncomingWorkValidator,
    ScheduleClaimProcessingDisposition,
    ScheduleClaimProcessingError,
    ScheduleClaimProcessingService,
    ScheduleClaimService,
    ScheduleOccurrenceCommand,
    WorkflowAdmissionDefinition,
)
from marketing_agents.domain.entities import Schedule, ScheduleClaim
from marketing_agents.domain.enums import (
    MisfirePolicy,
    OccurrenceState,
    TriggerKind,
    WorkMode,
)
from marketing_agents.domain.schedule_occurrence_identity import (
    SCHEDULE_RECURRENCE_VERSION,
    schedule_occurrence_id,
)
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
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

DUE = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)
LEASE_DURATION = timedelta(minutes=2)
CATALOG_HASH = "catalog-sha256-v1:" + ("6" * 64)
RESTARTED_CATALOG_HASH = "catalog-sha256-v1:" + ("7" * 64)
TEMPLATE_ID = "template.sched-06.target"
INSTANCE_ID = "instance.sched-06.target"
TRIGGER_ID = "trigger.sched-06.target"
WORKFLOW_ID = "workflow.sched-06.target"
SCHEDULE_ID = "schedule.sched-06.target"
SCHEMA_ID = "schema.sched-06.input"


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class IncrementingIds:
    def __init__(self, start: int = 0) -> None:
        self._next = start

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.sched-06.{self._next:04d}"


class ForbiddenClock:
    def now(self) -> NoReturn:
        raise AssertionError("committed duplicate suppression must not read the clock")


class ForbiddenIds:
    def new(self, namespace: str) -> NoReturn:
        raise AssertionError(f"committed duplicate suppression must not allocate {namespace} IDs")


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
    configuration_revision: int
    id: str = INSTANCE_ID
    template_id: str = TEMPLATE_ID
    enabled: bool = True


class ResponseLostAfterCommit(RuntimeError):
    """The database acknowledged commit but the caller observed no response."""


class ProcessingCommitFailed(RuntimeError):
    """The occurrence transaction failed before any write became durable."""


class CommitThenLoseResponseUnitOfWork(SQLAlchemyUnitOfWork):
    async def commit(self) -> NoReturn:
        await super().commit()
        raise ResponseLostAfterCommit("injected response loss after durable commit")


class CommitForbiddenUnitOfWork(SQLAlchemyUnitOfWork):
    async def commit(self) -> NoReturn:
        raise AssertionError("committed duplicate suppression must not commit")


class FailProcessingCommitUnitOfWork(SQLAlchemyUnitOfWork):
    async def commit(self) -> NoReturn:
        raise ProcessingCommitFailed("injected processing commit failure")


class AsyncBarrier:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrivals = 0
        self._released = asyncio.Event()

    async def wait(self) -> None:
        self._arrivals += 1
        if self._arrivals == self._parties:
            self._released.set()
        await self._released.wait()


class BarrierScheduleRepository:
    """Make both restarted workers retain the same expired-lease scan."""

    def __init__(self, delegate: ScheduleRepository, barrier: AsyncBarrier) -> None:
        self._delegate = delegate
        self._barrier = barrier

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def list_claimable_due(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Schedule, ...]:
        candidates = await self._delegate.list_claimable_due(now=now, limit=limit)
        await self._barrier.wait()
        return candidates


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


def _custom_uow_factory(
    runtime: DatabaseRuntime,
    repository_factories: SQLAlchemyRepositoryFactories,
    uow_type: type[SQLAlchemyUnitOfWork],
) -> Any:
    def factory() -> SQLAlchemyUnitOfWork:
        return uow_type(runtime.session_factory, repository_factories)

    return factory


def _dependencies(
    factory: Any,
    clock: Any,
    *,
    ids: Any | None = None,
) -> OrchestrationDependencies:
    return OrchestrationDependencies(clock, ids or IncrementingIds(), factory)


def _schedule(
    *,
    policy: MisfirePolicy = MisfirePolicy.RUN_ONCE,
    grace_seconds: int = 0,
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
                    capability_id="sched-06.read",
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


def _validator(
    *,
    configuration_revision: int = 7,
    catalog_hash: str = CATALOG_HASH,
) -> IncomingWorkValidator:
    return IncomingWorkValidator(
        catalog_hash=catalog_hash,
        templates=(TemplateStub(),),
        instances=(InstanceStub(configuration_revision),),
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
                allowed_modes=(WorkMode.MOCK_EXECUTION, WorkMode.DRY_RUN),
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
        admitted_payload={"campaign_id": "campaign.sched-06"},
    )


def _service(
    dependencies: OrchestrationDependencies,
    *,
    configuration_revision: int = 7,
    catalog_hash: str = CATALOG_HASH,
) -> ScheduleClaimProcessingService:
    return ScheduleClaimProcessingService(
        dependencies,
        _key(),
        _validator(
            configuration_revision=configuration_revision,
            catalog_hash=catalog_hash,
        ),
        recurrence=MinuteRecurrence(),
        current_catalog_hash=catalog_hash,
    )


async def _seed_claim(
    runtime: DatabaseRuntime,
    clock: MutableClock,
    *,
    lease_owner: str = "worker.sched-06.original",
    policy: MisfirePolicy = MisfirePolicy.RUN_ONCE,
    grace_seconds: int = 0,
) -> tuple[SQLAlchemyUnitOfWorkFactory, ScheduleClaim]:
    factory = _uow_factory(runtime)
    async with factory() as unit_of_work:
        inserted = await unit_of_work.schedules.add_or_get(
            _schedule(policy=policy, grace_seconds=grace_seconds)
        )
        assert inserted.inserted is True
        await unit_of_work.commit()
    claim = await ScheduleClaimService(
        _dependencies(factory, clock),
        lease_duration=LEASE_DURATION,
    ).claim_due_once(lease_owner=lease_owner)
    assert claim is not None
    return factory, claim


async def _session_counts(session: AsyncSession) -> tuple[int, int, int, int, int]:
    models = (
        ScheduleOccurrenceRecord,
        WorkItemRecord,
        RunRecord,
        RunStateTransitionRecord,
        AuditEventRecord,
    )
    values = [
        int(await session.scalar(select(func.count()).select_from(model)) or 0) for model in models
    ]
    return cast(tuple[int, int, int, int, int], tuple(values))


async def _counts(runtime: DatabaseRuntime) -> tuple[int, int, int, int, int]:
    async with runtime.session_factory() as session:
        return await _session_counts(session)


async def _schedule_snapshot(runtime: DatabaseRuntime) -> tuple[Any, ...]:
    async with runtime.session_factory() as session:
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
                ).where(ScheduleRecord.id == SCHEDULE_ID)
            )
        ).one()
        return tuple(row)


async def _audit_snapshot(runtime: DatabaseRuntime) -> tuple[tuple[Any, ...], ...]:
    async with runtime.session_factory() as session:
        rows = await session.execute(
            select(
                AuditEventRecord.id,
                AuditEventRecord.global_sequence,
                AuditEventRecord.run_sequence,
                AuditEventRecord.event_type,
                AuditEventRecord.aggregate_type,
                AuditEventRecord.aggregate_id,
                AuditEventRecord.mutation_version,
                AuditEventRecord.event_fingerprint,
            ).order_by(AuditEventRecord.global_sequence)
        )
        return tuple(tuple(row) for row in rows)


@pytest.mark.asyncio
async def test_sched_06_restart_before_processing_requires_strict_expiry_then_recovers_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "strict-expiry-restart.db"
    original_runtime = await _runtime(path)
    original_clock = MutableClock(DUE)
    _, original_claim = await _seed_claim(original_runtime, original_clock)
    assert await _counts(original_runtime) == (0, 0, 0, 0, 0)
    await original_runtime.dispose()

    restarted = await _runtime(path)
    clock = MutableClock(original_claim.lease_expires_at_utc)
    factory = _uow_factory(restarted)
    claiming = ScheduleClaimService(
        _dependencies(factory, clock),
        lease_duration=LEASE_DURATION,
    )
    try:
        assert await claiming.claim_due_once(lease_owner="worker.sched-06.exact") is None
        async with factory() as unit_of_work:
            assert await unit_of_work.schedules.get_claim(SCHEDULE_ID) == original_claim

        clock.current += timedelta(microseconds=1)
        replacement = await claiming.claim_due_once(lease_owner="worker.sched-06.replacement")
        assert replacement is not None
        assert replacement.scheduled_for_utc == original_claim.scheduled_for_utc
        assert replacement.recurrence_version == original_claim.recurrence_version
        assert replacement.version == original_claim.version + 1

        dependencies = _dependencies(factory, clock)
        result = await _service(dependencies).process_claimed_once(_command(replacement))
        assert result.disposition is ScheduleClaimProcessingDisposition.PROCESSED
        assert result.occurrence.id == schedule_occurrence_id(
            original_claim.schedule_id,
            original_claim.scheduled_for_utc,
            recurrence_version=original_claim.recurrence_version,
        )
        assert result.resulting_schedule.version == original_claim.version + 2
        assert result.resulting_schedule.last_scheduled_at_utc == DUE
        assert result.resulting_schedule.next_run_at_utc == DUE + timedelta(minutes=3)
        assert await _counts(restarted) == (1, 1, 1, 1, 3)
        assert (await _schedule_snapshot(restarted))[3:6] == (None, None, None)
        assert tuple(row[3] for row in await _audit_snapshot(restarted)) == (
            "run.received",
            "schedule.misfire_run_once",
            "schedule.next_occurrence_persisted",
        )

        with pytest.raises(ScheduleClaimProcessingError) as stale:
            await _service(dependencies).process_claimed_once(_command(original_claim))
        assert stale.value.code == "claim_fence_lost"
        assert await _counts(restarted) == (1, 1, 1, 1, 3)
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_06_recovered_skip_advances_without_work_or_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recovered-skip.db"
    original_runtime = await _runtime(path)
    _, original_claim = await _seed_claim(
        original_runtime,
        MutableClock(DUE),
        policy=MisfirePolicy.SKIP,
    )
    await original_runtime.dispose()

    restarted = await _runtime(path)
    recovery_time = original_claim.lease_expires_at_utc + timedelta(microseconds=1)
    factory = _uow_factory(restarted)
    dependencies = _dependencies(factory, MutableClock(recovery_time))
    try:
        replacement = await ScheduleClaimService(
            dependencies,
            lease_duration=LEASE_DURATION,
        ).claim_due_once(lease_owner="worker.sched-06.skip-recovery")
        assert replacement is not None

        result = await _service(dependencies).process_claimed_once(_command(replacement))
        assert result.disposition is ScheduleClaimProcessingDisposition.PROCESSED
        assert result.occurrence.state is OccurrenceState.SKIPPED
        assert result.work_item is None and result.run is None
        assert result.resulting_schedule.next_run_at_utc == DUE + timedelta(minutes=3)
        assert result.resulting_schedule.version == original_claim.version + 2
        assert await _counts(restarted) == (1, 0, 0, 0, 2)
        assert tuple(row[3] for row in await _audit_snapshot(restarted)) == (
            "schedule.misfire_skipped",
            "schedule.next_occurrence_persisted",
        )
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_06_rolled_back_processing_recovers_once_after_expiry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollback-then-recover.db"
    runtime = await _runtime(path)
    clock = MutableClock(DUE)
    plain_factory, original_claim = await _seed_claim(runtime, clock)
    failing_factory = _custom_uow_factory(
        runtime,
        plain_factory.repository_factories,
        FailProcessingCommitUnitOfWork,
    )
    try:
        with pytest.raises(ProcessingCommitFailed):
            await _service(_dependencies(failing_factory, clock)).process_claimed_once(
                _command(original_claim)
            )
        assert await _counts(runtime) == (0, 0, 0, 0, 0)
        async with plain_factory() as unit_of_work:
            assert await unit_of_work.schedules.get_claim(SCHEDULE_ID) == original_claim
    finally:
        await runtime.dispose()

    restarted = await _runtime(path)
    recovery_time = original_claim.lease_expires_at_utc + timedelta(microseconds=1)
    factory = _uow_factory(restarted)
    dependencies = _dependencies(factory, MutableClock(recovery_time))
    try:
        replacement = await ScheduleClaimService(
            dependencies,
            lease_duration=LEASE_DURATION,
        ).claim_due_once(lease_owner="worker.sched-06.rollback-recovery")
        assert replacement is not None
        recovered = await _service(dependencies).process_claimed_once(_command(replacement))
        assert recovered.disposition is ScheduleClaimProcessingDisposition.PROCESSED
        assert recovered.resulting_schedule.version == original_claim.version + 2
        assert await _counts(restarted) == (1, 1, 1, 1, 3)
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_06_two_restarted_workers_race_to_one_recovered_outcome(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recovered-worker-race.db"
    seeded = await _runtime(path)
    _, original_claim = await _seed_claim(seeded, MutableClock(DUE))
    await seeded.dispose()

    first_runtime = await _runtime(path)
    second_runtime = await _runtime(path)
    barrier = AsyncBarrier(2)

    def first_barrier_repository(session: AsyncSession) -> ScheduleRepository:
        return cast(
            ScheduleRepository,
            BarrierScheduleRepository(SQLAlchemyScheduleRepository(session), barrier),
        )

    def second_barrier_repository(session: AsyncSession) -> ScheduleRepository:
        return cast(
            ScheduleRepository,
            BarrierScheduleRepository(SQLAlchemyScheduleRepository(session), barrier),
        )

    first_race_factory = _uow_factory(
        first_runtime,
        schedule_factory=first_barrier_repository,
    )
    second_race_factory = _uow_factory(
        second_runtime,
        schedule_factory=second_barrier_repository,
    )
    recovery_time = original_claim.lease_expires_at_utc + timedelta(microseconds=1)
    try:
        outcomes = await asyncio.gather(
            ScheduleClaimService(
                _dependencies(first_race_factory, MutableClock(recovery_time)),
                lease_duration=LEASE_DURATION,
            ).claim_due_once(lease_owner="worker.sched-06.race-a"),
            ScheduleClaimService(
                _dependencies(second_race_factory, MutableClock(recovery_time)),
                lease_duration=LEASE_DURATION,
            ).claim_due_once(lease_owner="worker.sched-06.race-b"),
        )
        recovered = [claim for claim in outcomes if claim is not None]
        assert len(recovered) == 1
        assert sum(claim is None for claim in outcomes) == 1
        replacement = recovered[0]
        assert replacement.version == original_claim.version + 1
        assert replacement.scheduled_for_utc == original_claim.scheduled_for_utc

        plain_factory = _uow_factory(first_runtime)
        dependencies = _dependencies(plain_factory, MutableClock(recovery_time))
        result = await _service(dependencies).process_claimed_once(_command(replacement))
        assert result.disposition is ScheduleClaimProcessingDisposition.PROCESSED

        with pytest.raises(ScheduleClaimProcessingError) as stale:
            await _service(dependencies).process_claimed_once(_command(original_claim))
        assert stale.value.code == "claim_fence_lost"
        assert await _counts(first_runtime) == (1, 1, 1, 1, 3)
        assert tuple(row[3] for row in await _audit_snapshot(first_runtime)) == (
            "run.received",
            "schedule.misfire_run_once",
            "schedule.next_occurrence_persisted",
        )
    finally:
        await first_runtime.dispose()
        await second_runtime.dispose()


@pytest.mark.asyncio
async def test_sched_06_response_loss_after_commit_replays_without_any_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "response-loss.db"
    runtime = await _runtime(path)
    clock = MutableClock(DUE)
    plain_factory, claim = await _seed_claim(runtime, clock)
    losing_factory = _custom_uow_factory(
        runtime,
        plain_factory.repository_factories,
        CommitThenLoseResponseUnitOfWork,
    )
    try:
        with pytest.raises(ResponseLostAfterCommit):
            await _service(_dependencies(losing_factory, clock)).process_claimed_once(
                _command(claim)
            )
        assert await _counts(runtime) == (1, 1, 1, 1, 3)
        committed_schedule = await _schedule_snapshot(runtime)
        committed_audits = await _audit_snapshot(runtime)
    finally:
        await runtime.dispose()

    restarted = await _runtime(path)
    restarted_factory = _uow_factory(restarted)
    read_only_factory = _custom_uow_factory(
        restarted,
        restarted_factory.repository_factories,
        CommitForbiddenUnitOfWork,
    )
    dependencies = _dependencies(
        read_only_factory,
        ForbiddenClock(),
        ids=ForbiddenIds(),
    )
    try:
        service = _service(dependencies)
        replays = await asyncio.gather(
            service.process_claimed_once(_command(claim)),
            service.process_claimed_once(_command(claim)),
        )
        for replayed in replays:
            assert replayed.disposition is ScheduleClaimProcessingDisposition.DUPLICATE_SUPPRESSED
            assert replayed.occurrence.id == schedule_occurrence_id(
                claim.schedule_id,
                claim.scheduled_for_utc,
                recurrence_version=claim.recurrence_version,
            )
            assert replayed.work_item is not None and replayed.run is not None
            assert replayed.occurrence.work_item_id == replayed.work_item.id
            assert replayed.occurrence.run_id == replayed.run.id
            assert replayed.resulting_schedule.version == claim.version + 1
            assert tuple(event.id for event in replayed.audit_events) == (
                committed_audits[1][0],
                committed_audits[2][0],
            )
        assert replays[0].occurrence == replays[1].occurrence
        assert replays[0].work_item == replays[1].work_item
        assert replays[0].run == replays[1].run
        assert await _counts(restarted) == (1, 1, 1, 1, 3)
        assert await _schedule_snapshot(restarted) == committed_schedule
        assert await _audit_snapshot(restarted) == committed_audits
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("forged_coordinate", ("claimed_at", "lease_expires_at"))
async def test_sched_06_committed_replay_rejects_forged_claim_lease_coordinates(
    tmp_path: Path,
    forged_coordinate: str,
) -> None:
    path = tmp_path / f"forged-{forged_coordinate}.db"
    runtime = await _runtime(path)
    clock = MutableClock(DUE)
    plain_factory, claim = await _seed_claim(runtime, clock, grace_seconds=300)
    losing_factory = _custom_uow_factory(
        runtime,
        plain_factory.repository_factories,
        CommitThenLoseResponseUnitOfWork,
    )
    clock.current = DUE + timedelta(seconds=30)
    try:
        with pytest.raises(ResponseLostAfterCommit):
            await _service(_dependencies(losing_factory, clock)).process_claimed_once(
                _command(claim)
            )
        committed_schedule = await _schedule_snapshot(runtime)
        committed_audits = await _audit_snapshot(runtime)
        assert await _counts(runtime) == (1, 1, 1, 1, 3)
    finally:
        await runtime.dispose()

    if forged_coordinate == "claimed_at":
        forged_claim = replace(
            claim,
            claimed_at_utc=claim.claimed_at_utc + timedelta(seconds=1),
        )
    else:
        forged_claim = replace(
            claim,
            lease_expires_at_utc=claim.lease_expires_at_utc + timedelta(seconds=1),
        )

    restarted = await _runtime(path)
    restarted_factory = _uow_factory(restarted)
    read_only_factory = _custom_uow_factory(
        restarted,
        restarted_factory.repository_factories,
        CommitForbiddenUnitOfWork,
    )
    try:
        with pytest.raises(ScheduleClaimProcessingError) as incomplete:
            await _service(
                _dependencies(
                    read_only_factory,
                    ForbiddenClock(),
                    ids=ForbiddenIds(),
                )
            ).process_claimed_once(_command(forged_claim))
        assert incomplete.value.code == "committed_outcome_incomplete"
        assert await _counts(restarted) == (1, 1, 1, 1, 3)
        assert await _schedule_snapshot(restarted) == committed_schedule
        assert await _audit_snapshot(restarted) == committed_audits
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_06_committed_replay_survives_current_catalog_hash_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog-change-replay.db"
    runtime = await _runtime(path)
    factory, claim = await _seed_claim(runtime, MutableClock(DUE))
    created = await _service(_dependencies(factory, MutableClock(DUE))).process_claimed_once(
        _command(claim)
    )
    assert created.disposition is ScheduleClaimProcessingDisposition.PROCESSED
    assert created.work_item is not None and created.run is not None
    assert created.run.catalog_hash == CATALOG_HASH
    committed_schedule = await _schedule_snapshot(runtime)
    committed_audits = await _audit_snapshot(runtime)
    await runtime.dispose()

    restarted = await _runtime(path)
    restarted_factory = _uow_factory(restarted)
    read_only_factory = _custom_uow_factory(
        restarted,
        restarted_factory.repository_factories,
        CommitForbiddenUnitOfWork,
    )
    try:
        replayed = await _service(
            _dependencies(
                read_only_factory,
                ForbiddenClock(),
                ids=ForbiddenIds(),
            ),
            catalog_hash=RESTARTED_CATALOG_HASH,
        ).process_claimed_once(_command(claim))
        assert replayed.disposition is ScheduleClaimProcessingDisposition.DUPLICATE_SUPPRESSED
        assert replayed.occurrence == created.occurrence
        assert replayed.work_item == created.work_item
        assert replayed.run == created.run
        assert replayed.run is not None
        assert replayed.run.catalog_hash == CATALOG_HASH
        assert replayed.run.catalog_hash != RESTARTED_CATALOG_HASH
        assert await _counts(restarted) == (1, 1, 1, 1, 3)
        assert await _schedule_snapshot(restarted) == committed_schedule
        assert await _audit_snapshot(restarted) == committed_audits
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_fact", ("payload", "mode", "configuration"))
async def test_sched_06_committed_replay_rejects_changed_command_facts(
    tmp_path: Path,
    changed_fact: str,
) -> None:
    path = tmp_path / f"changed-{changed_fact}.db"
    runtime = await _runtime(path)
    factory, claim = await _seed_claim(runtime, MutableClock(DUE))
    created = await _service(_dependencies(factory, MutableClock(DUE))).process_claimed_once(
        _command(claim)
    )
    assert created.disposition is ScheduleClaimProcessingDisposition.PROCESSED
    committed_schedule = await _schedule_snapshot(runtime)
    committed_audits = await _audit_snapshot(runtime)
    await runtime.dispose()

    restarted = await _runtime(path)
    restarted_factory = _uow_factory(restarted)
    read_only_factory = _custom_uow_factory(
        restarted,
        restarted_factory.repository_factories,
        CommitForbiddenUnitOfWork,
    )
    command = _command(claim)
    validator_revision = 7
    if changed_fact == "payload":
        command = replace(
            command,
            admitted_payload={"campaign_id": "campaign.sched-06.changed"},
        )
    elif changed_fact == "mode":
        command = replace(command, mode=WorkMode.DRY_RUN)
    else:
        validator_revision = 8
        command = replace(command, configuration_revision=validator_revision)
    try:
        with pytest.raises(ScheduleClaimProcessingError) as conflict:
            await _service(
                _dependencies(
                    read_only_factory,
                    ForbiddenClock(),
                    ids=ForbiddenIds(),
                ),
                configuration_revision=validator_revision,
            ).process_claimed_once(command)
        assert conflict.value.code == "committed_replay_conflict"
        assert await _counts(restarted) == (1, 1, 1, 1, 3)
        assert await _schedule_snapshot(restarted) == committed_schedule
        assert await _audit_snapshot(restarted) == committed_audits
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_sched_06_committed_replay_does_not_heal_a_missing_audit_witness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-advance-audit.db"
    runtime = await _runtime(path)
    factory, claim = await _seed_claim(runtime, MutableClock(DUE))
    result = await _service(_dependencies(factory, MutableClock(DUE))).process_claimed_once(
        _command(claim)
    )
    assert result.disposition is ScheduleClaimProcessingDisposition.PROCESSED
    async with runtime.engine.begin() as connection:
        deleted = await connection.execute(
            delete(AuditEventRecord).where(
                AuditEventRecord.event_type == "schedule.next_occurrence_persisted"
            )
        )
        assert deleted.rowcount == 1
    incomplete_schedule = await _schedule_snapshot(runtime)
    incomplete_audits = await _audit_snapshot(runtime)
    assert await _counts(runtime) == (1, 1, 1, 1, 2)
    await runtime.dispose()

    restarted = await _runtime(path)
    restarted_factory = _uow_factory(restarted)
    read_only_factory = _custom_uow_factory(
        restarted,
        restarted_factory.repository_factories,
        CommitForbiddenUnitOfWork,
    )
    try:
        with pytest.raises(ScheduleClaimProcessingError) as incomplete:
            await _service(
                _dependencies(
                    read_only_factory,
                    ForbiddenClock(),
                    ids=ForbiddenIds(),
                )
            ).process_claimed_once(_command(claim))
        assert incomplete.value.code == "committed_outcome_incomplete"
        assert await _counts(restarted) == (1, 1, 1, 1, 2)
        assert await _schedule_snapshot(restarted) == incomplete_schedule
        assert await _audit_snapshot(restarted) == incomplete_audits
    finally:
        await restarted.dispose()
