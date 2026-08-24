"""ORCH-08 portable storage, tamper, rollback, and retry-lineage fences."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.application.ports.repositories import ReleaseCallMode
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionService,
)
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
    StepState,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    transition_step,
)
from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.models import (
    ApprovalDecisionRecord,
    ApprovalUseRecord,
    AuthorizationSetHeadRecord,
    AuthorizationSetMemberRecord,
    AuthorizationSetRecord,
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
    RunStepStateTransitionRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyAuditRepository
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from tests.integration.db.test_orch_08_approval_boundary import (
    _approve_complete_set,
    _complete_read_dependency,
    _compose_write_plan,
    _current,
    _decision,
    _ObservingRejectGateway,
    _principal,
)
from tests.integration.db.test_run_08_approval_persistence import (
    IncrementingIds,
    MutableClock,
    _dependencies,
    _runtime,
)


class _ReleaseAuditFault:
    """Raise after release-transition audits were staged in the same UoW."""

    def __init__(self, delegate: SQLAlchemyAuditRepository) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def append_many(self, events):  # type: ignore[no-untyped-def]
        appended = await self._delegate.append_many(events)
        if any(
            event.event_type == "step.transitioned"
            and event.safe_metadata.values.get("command") == "release_approval"
            for event in events
        ):
            raise RuntimeError("injected release audit fault")
        return appended


def _ddl(table, dialect) -> str:  # type: ignore[no-untyped-def]
    return " ".join(str(CreateTable(table).compile(dialect=dialect)).lower().split())


def test_orch_08_authorization_schema_compiles_with_exact_portable_constraints() -> None:
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        for table in Base.metadata.sorted_tables:
            str(CreateTable(table).compile(dialect=dialect))

    set_ddl = _ddl(AuthorizationSetRecord.__table__, sqlite.dialect())
    head_ddl = _ddl(AuthorizationSetHeadRecord.__table__, sqlite.dialect())
    member_ddl = _ddl(AuthorizationSetMemberRecord.__table__, sqlite.dialect())
    action_ddl = _ddl(ExternalActionRecord.__table__, sqlite.dialect())
    transition_ddl = _ddl(RunStepStateTransitionRecord.__table__, sqlite.dialect())

    assert "constraint uq_authorization_sets_id_run unique (id, run_id)" in set_ddl
    assert (
        "foreign key(current_set_id, run_id, plan_hash, proposal_revision, membership_hash)"
        in head_ddl
    )
    assert "constraint ck_set_members_release_complete" in member_ddl
    assert "references approval_uses (id, request_id, decision_id, action_id" in member_ddl
    assert "reservation_authorization_set_id = authorization_set_id" in action_ddl
    assert "'release_approval','start_reserved_write'" in transition_ddl


@pytest.mark.asyncio
async def test_orch_08_fresh_sqlite_schema_has_foreign_keys_and_no_violations(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-portable-schema.db")
    try:
        async with runtime.engine.connect() as connection:
            enabled = int((await connection.execute(text("PRAGMA foreign_keys"))).scalar_one())
            violations = tuple((await connection.execute(text("PRAGMA foreign_key_check"))).all())
        assert enabled == 1
        assert violations == ()
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_08_release_audit_fault_rolls_back_every_release_projection(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-release-rollback.db")
    clock = MutableClock()
    normal = _dependencies(runtime, clock=clock, ids=IncrementingIds(1600))
    try:
        plan, _ = await _compose_write_plan(
            normal,
            event_id="event.orch-08.release-rollback",
            seed=1600,
        )
        _, _, _, requests, _ = await _current(normal, plan.run_id)
        clock.current += timedelta(seconds=1)
        await ApprovalDecisionService(normal).decide(
            _decision(
                requests[0].request,
                ApprovalDecisionKind.APPROVE,
                suffix="release-rollback.first",
            ),
            principal=_principal("release-rollback.first"),
        )
        _, _, _, requests, _ = await _current(normal, plan.run_id)

        def faulting(session):  # type: ignore[no-untyped-def]
            return _ReleaseAuditFault(SQLAlchemyAuditRepository(session))

        faulting_dependencies = _dependencies(
            runtime,
            clock=clock,
            ids=IncrementingIds(1700),
            audit_factory=faulting,
        )
        clock.current += timedelta(seconds=1)
        with pytest.raises(RuntimeError, match="injected release audit fault"):
            await ApprovalDecisionService(faulting_dependencies).decide(
                _decision(
                    requests[1].request,
                    ApprovalDecisionKind.APPROVE,
                    suffix="release-rollback.final",
                ),
                principal=_principal("release-rollback.final"),
            )

        run, steps, selected, requests, actions = await _current(normal, plan.run_id)
        assert run.state.value == "awaiting_approval"
        assert selected.authorization_set.status.value == "open"
        assert [request.status for request in requests] == [
            ApprovalStatus.APPROVED,
            ApprovalStatus.PENDING,
        ]
        assert all(request.use is None for request in requests)
        assert all(step.state is not StepState.READY for step in steps[1:])
        assert all(action is not None and action.reservation is None for action in actions)
        async with runtime.session_factory() as session:
            use_count = int(
                (await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()
            )
            reserved_count = int(
                (
                    await session.execute(
                        select(func.count(ExternalActionRecord.reservation_id)).where(
                            ExternalActionRecord.reservation_id.is_not(None)
                        )
                    )
                ).scalar_one()
            )
        assert (use_count, reserved_count) == (0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_08_released_decision_tamper_blocks_attempt_and_connector(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-release-tamper.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1800))
    gateway = _ObservingRejectGateway(runtime)
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.release-tamper",
            seed=1800,
        )
        await _approve_complete_set(
            dependencies,
            clock,
            plan.run_id,
            suffix="release-tamper",
        )
        await _complete_read_dependency(dependencies, clock, plan.run_id)
        _, _, _, _, actions = await _current(dependencies, plan.run_id)
        action = actions[0]
        assert action is not None
        async with dependencies.unit_of_work() as unit_of_work:
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            assert authority is not None

        decision_id = authority.approval_decision_id
        async with runtime.session_factory() as session:
            await session.execute(
                update(ApprovalDecisionRecord)
                .where(ApprovalDecisionRecord.id == decision_id)
                .values(authority_roles=["approver", "tampered_role"])
            )
            await session.commit()

        from marketing_agents.application.policies.write_authorization import (
            WriteAuthorizationGuard,
        )
        from marketing_agents.application.services.external_action_dispatcher import (
            ExternalActionDispatcher,
            ExternalActionDispatchError,
        )

        with pytest.raises(ExternalActionDispatchError) as rejected:
            await ExternalActionDispatcher(
                dependencies,
                gateway,
                WriteAuthorizationGuard(),
            ).dispatch_once(action.id, lease_owner="worker.orch-08.tamper")
        assert rejected.value.code == "execution_plan_invalid"
        assert gateway.calls == 0
        async with runtime.session_factory() as session:
            stored_action = await session.get(ExternalActionRecord, action.id)
            attempt_count = int(
                (
                    await session.execute(
                        select(func.count(ExternalActionDispatchAttemptRecord.external_action_id))
                    )
                ).scalar_one()
            )
        assert stored_action is not None
        assert stored_action.state == ExternalActionState.DISPATCH_RESERVED.value
        assert attempt_count == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_08_retry_lineage_survives_intervening_pre_call_expiry(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-retry-lineage.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1900))
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.retry-lineage",
            seed=1900,
        )
        await _approve_complete_set(
            dependencies,
            clock,
            plan.run_id,
            suffix="retry-lineage",
        )
        await _complete_read_dependency(dependencies, clock, plan.run_id)
        _, _, _, _, actions = await _current(dependencies, plan.run_id)
        action = actions[0]
        assert action is not None

        # The production default is two attempts; widen this fixture to exercise
        # the three-row lineage without changing any release identity or version.
        async with runtime.session_factory() as session:
            await session.execute(
                update(ExternalActionRecord)
                .where(ExternalActionRecord.id == action.id)
                .values(delivery_attempt_limit=3)
            )
            await session.commit()

        clock.current += timedelta(seconds=1)
        async with dependencies.unit_of_work() as unit_of_work:
            action = await unit_of_work.external_actions.get(action.id)
            assert action is not None
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            assert authority is not None
            assert authority.call_mode is ReleaseCallMode.FIRST_CALL
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=action.version,
                authority=authority,
                lease_owner="worker.orch-08.lineage.1",
                claimed_at=clock.current,
                lease_expires_at=clock.current + timedelta(minutes=1),
            )
            assert claimed is not None
            await unit_of_work.commit()

        clock.current += timedelta(seconds=1)
        async with dependencies.unit_of_work() as unit_of_work:
            claimed = await unit_of_work.external_actions.get(action.id)
            assert claimed is not None
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            step = await unit_of_work.run_steps.get(authority.step_id) if authority else None
            assert authority is not None and step is not None
            transition = transition_step(
                step,
                StepLifecycleCommand.START_RESERVED_WRITE,
                NoStepTransitionContext(),
                clock.current,
            )
            started = await unit_of_work.external_actions.mark_call_started(
                action_id=claimed.id,
                expected_version=claimed.version,
                authority=authority,
                lease_owner="worker.orch-08.lineage.1",
                attempt_number=1,
                started_at=clock.current,
                call_deadline_at=clock.current + timedelta(seconds=30),
                step_transition=transition,
            )
            assert started is not None and started.step_transition == transition
            await unit_of_work.commit()

        clock.current += timedelta(minutes=2)
        async with dependencies.unit_of_work() as unit_of_work:
            started_action = await unit_of_work.external_actions.get(action.id)
            assert started_action is not None
            released = await unit_of_work.external_actions.release_stale_for_retry(
                action_id=action.id,
                expected_version=started_action.version,
                attempt_number=1,
                occurred_at=clock.current,
                conclusion="provider_retry",
            )
            assert released is not None
            await unit_of_work.commit()

        clock.current += timedelta(seconds=1)
        async with dependencies.unit_of_work() as unit_of_work:
            released = await unit_of_work.external_actions.get(action.id)
            assert released is not None
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            assert authority is not None
            assert authority.call_mode is ReleaseCallMode.PROVIDER_RETRY
            assert authority.prior_started_attempt_number == 1
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=released.version,
                authority=authority,
                lease_owner="worker.orch-08.lineage.2",
                claimed_at=clock.current,
                lease_expires_at=clock.current + timedelta(minutes=1),
            )
            assert claimed is not None
            await unit_of_work.commit()

        clock.current += timedelta(minutes=2)
        async with dependencies.unit_of_work() as unit_of_work:
            claimed = await unit_of_work.external_actions.get(action.id)
            assert claimed is not None
            released = await unit_of_work.external_actions.release_stale_for_retry(
                action_id=action.id,
                expected_version=claimed.version,
                attempt_number=2,
                occurred_at=clock.current,
                conclusion="pre_call_expired",
            )
            assert released is not None
            await unit_of_work.commit()

        clock.current += timedelta(seconds=1)
        async with dependencies.unit_of_work() as unit_of_work:
            released = await unit_of_work.external_actions.get(action.id)
            assert released is not None
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            assert authority is not None
            assert authority.call_mode is ReleaseCallMode.PROVIDER_RETRY
            assert authority.prior_started_attempt_number == 1
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=released.version,
                authority=authority,
                lease_owner="worker.orch-08.lineage.3",
                claimed_at=clock.current,
                lease_expires_at=clock.current + timedelta(minutes=1),
            )
            assert claimed is not None
            await unit_of_work.commit()

        clock.current += timedelta(seconds=1)
        async with dependencies.unit_of_work() as unit_of_work:
            claimed = await unit_of_work.external_actions.get(action.id)
            assert claimed is not None
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            assert authority is not None
            assert authority.call_mode is ReleaseCallMode.PROVIDER_RETRY
            assert authority.prior_started_attempt_number == 1
            restarted = await unit_of_work.external_actions.mark_call_started(
                action_id=claimed.id,
                expected_version=claimed.version,
                authority=authority,
                lease_owner="worker.orch-08.lineage.3",
                attempt_number=3,
                started_at=clock.current,
                call_deadline_at=clock.current + timedelta(seconds=30),
                step_transition=None,
            )
            assert restarted is not None and restarted.step_transition is None
            await unit_of_work.commit()

        async with runtime.session_factory() as session:
            attempts = tuple(
                (
                    await session.execute(
                        select(ExternalActionDispatchAttemptRecord)
                        .where(ExternalActionDispatchAttemptRecord.external_action_id == action.id)
                        .order_by(ExternalActionDispatchAttemptRecord.attempt_number)
                    )
                ).scalars()
            )
            start_transition_count = int(
                (
                    await session.execute(
                        select(func.count(RunStepStateTransitionRecord.step_id)).where(
                            RunStepStateTransitionRecord.command
                            == StepLifecycleCommand.START_RESERVED_WRITE.value
                        )
                    )
                ).scalar_one()
            )
        assert [attempt.conclusion for attempt in attempts] == [
            "provider_retry",
            "pre_call_expired",
            None,
        ]
        assert attempts[0].call_started_at is not None
        assert attempts[1].call_started_at is None
        assert attempts[2].call_started_at is not None
        assert start_transition_count == 1
    finally:
        await runtime.dispose()
