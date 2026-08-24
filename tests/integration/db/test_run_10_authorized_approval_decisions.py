"""RUN-10 authenticated decisions are exact, one-winner, and atomically audited."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.ports.repositories import (
    ApprovalDecisionInsertResult,
    ApprovalRepository,
)
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionService,
    ApprovalDecisionServiceError,
)
from marketing_agents.application.services.approval_records import ApprovalRecordService
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.approval import ApprovalDecision
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
)
from marketing_agents.infrastructure.db import (
    Base,
    SQLAlchemyApprovalRepository,
    SQLAlchemyAuditRepository,
)
from marketing_agents.infrastructure.db.models import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    AuditEventRecord,
    ConnectorActionReceiptRecord,
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
)
from sqlalchemy import delete, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from tests.integration.db.test_run_08_approval_persistence import (
    APPROVAL_INTEGRITY_KEY,
    FaultAfterAuditAppend,
    IncrementingIds,
    MutableClock,
    TwoPartyBarrier,
    _context,
    _dependencies,
    _runtime,
    _seed_run_and_plan,
    _timeline,
)
from tests.support.identity import human_principal


def _principal(suffix: str = "primary"):
    return human_principal(
        actor_id=f"principal.approver.run-10.{suffix}",
        roles=frozenset({"approver", "local_admin"}),
        scopes=frozenset({"approvals:decide", "scope.external-write", "approvals:read"}),
    )


def _command(
    request,
    decision: ApprovalDecisionKind,
    *,
    suffix: str,
    reason: str | None = None,
) -> ApprovalDecisionCommand:
    return ApprovalDecisionCommand(
        request_id=request.id,
        expected_generation=request.generation,
        expected_action_hash=request.action_hash,
        decision=decision,
        correlation_id=f"correlation.run-10.{suffix}",
        reason=reason,
    )


class DecisionBarrierApprovalRepository:
    def __init__(self, session: AsyncSession, barrier: TwoPartyBarrier) -> None:
        self._delegate = SQLAlchemyApprovalRepository(session, APPROVAL_INTEGRITY_KEY)
        self._barrier = barrier

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def record_decision(
        self,
        *,
        expected_version: int,
        expected_action_version: int,
        decision: ApprovalDecision,
    ) -> ApprovalDecisionInsertResult:
        await self._barrier.wait()
        return await self._delegate.record_decision(
            expected_version=expected_version,
            expected_action_version=expected_action_version,
            decision=decision,
        )


class CountingIds(IncrementingIds):
    def __init__(self, seed: int = 0) -> None:
        super().__init__(seed)
        self.calls: list[str] = []

    def new(self, namespace: str) -> str:
        self.calls.append(namespace)
        return super().new(namespace)


def test_run_10_linked_decision_audit_schema_compiles_for_both_dialects() -> None:
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        for table in Base.metadata.sorted_tables:
            str(CreateTable(table).compile(dialect=dialect))

    audit_ddl = " ".join(
        str(CreateTable(AuditEventRecord.__table__).compile(dialect=sqlite.dialect()))
        .lower()
        .split()
    )
    decision_ddl = " ".join(
        str(CreateTable(ApprovalDecisionRecord.__table__).compile(dialect=sqlite.dialect()))
        .lower()
        .split()
    )
    assert (
        "foreign key(approval_decision_id, approval_request_id, action_id, run_id, step_id)"
        in audit_ddl
    )
    assert "approval_decisions (id, request_id, action_id, run_id, step_id)" in audit_ddl
    assert "uq_approval_decision_audit_binding" in decision_ddl


@pytest.mark.parametrize(
    ("decision_kind", "expected_status", "expected_action", "event_types"),
    [
        (
            ApprovalDecisionKind.APPROVE,
            ApprovalStatus.APPROVED,
            ExternalActionState.APPROVED,
            ("action.approved", "approval.approved"),
        ),
        (
            ApprovalDecisionKind.REJECT,
            ApprovalStatus.REJECTED,
            ExternalActionState.REJECTED,
            ("action.rejected", "approval.rejected"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_10_decision_derives_minimum_authority_and_two_linked_audits(
    tmp_path: Path,
    decision_kind: ApprovalDecisionKind,
    expected_status: ApprovalStatus,
    expected_action: ExternalActionState,
    event_types: tuple[str, str],
) -> None:
    runtime = await _runtime(tmp_path / f"run-10-{decision_kind.value}.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(500))
    plan = await _seed_run_and_plan(
        dependencies,
        event_id=f"event.run-10.{decision_kind.value}",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context(f"run-10.{decision_kind.value}.register"),
    )
    before_request = registered.requests[0]
    before_action = registered.actions.actions[0].action
    clock.current += timedelta(seconds=1)
    reason_canary = f"private-reason-canary-{decision_kind.value}"
    try:
        result = await ApprovalDecisionService(dependencies).decide(
            _command(
                before_request.request,
                decision_kind,
                suffix=decision_kind.value,
                reason=reason_canary,
            ),
            principal=_principal(decision_kind.value),
        )
        assert result.request.status is expected_status
        assert result.request.version == 2
        assert result.action.state is expected_action
        assert result.action.version == 3
        assert result.action.reservation is None
        assert result.action.delivery_attempt_count == 0
        assert result.decision.actor_id == f"principal.approver.run-10.{decision_kind.value}"
        assert result.decision.authority_roles == frozenset({"approver"})
        assert result.decision.authority_scopes == frozenset(
            {"approvals:decide", "scope.external-write"}
        )
        assert "local_admin" not in result.decision.authority_roles
        assert "approvals:read" not in result.decision.authority_scopes

        async with runtime.session_factory() as session:
            decision_record = (await session.execute(select(ApprovalDecisionRecord))).scalar_one()
            decision_audits = tuple(
                (
                    await session.execute(
                        select(AuditEventRecord)
                        .where(AuditEventRecord.event_type.in_(event_types))
                        .order_by(AuditEventRecord.run_sequence)
                    )
                ).scalars()
            )
            attempts = int(
                (
                    await session.execute(
                        select(func.count(ExternalActionDispatchAttemptRecord.external_action_id))
                    )
                ).scalar_one()
            )
            receipts = int(
                (
                    await session.execute(
                        select(func.count(ConnectorActionReceiptRecord.receipt_id))
                    )
                ).scalar_one()
            )
        assert decision_record.actor_id == result.decision.actor_id
        assert decision_record.authentication_method == "bearer"
        assert decision_record.authority_roles == ["approver"]
        assert decision_record.authority_scopes == [
            "approvals:decide",
            "scope.external-write",
        ]
        assert tuple(event.event_type for event in decision_audits) == event_types
        assert all(
            event.approval_request_id == result.request.request.id
            and event.approval_decision_id == result.decision.id
            for event in decision_audits
        )
        persisted_audits = json.dumps(
            [event.safe_metadata for event in decision_audits],
            sort_keys=True,
        )
        assert result.decision.actor_id not in persisted_audits
        assert reason_canary not in persisted_audits
        assert reason_canary not in repr(result.decision)
        assert (attempts, receipts) == (0, 0)
        assert (await _timeline(dependencies, plan.run_id))[-2:] == event_types

        context = AuditContext.authenticated_user(
            result.decision.actor_id,
            authentication_method=result.decision.authentication_method,
            correlation_id=result.decision.correlation_id,
        )
        drifted_projection = dict(result.action.proposal.redacted_projection)
        drifted_projection["payload"] = {"body": "[ALTERED]"}
        drifted_proposal = replace(
            result.action.proposal,
            redacted_projection=drifted_projection,
        )
        with pytest.raises(ValueError, match="exact authorized transition"):
            AuditEventFactory(context).action_decided(
                before_action,
                replace(result.action, proposal=drifted_proposal),
                result.request,
            )
        drifted_decision = replace(
            result.decision,
            authority_roles=result.decision.authority_roles | frozenset({"extra-role"}),
        )
        drifted_request = replace(result.request, decision=drifted_decision)
        with pytest.raises(ValueError, match="exact authorized transition"):
            AuditEventFactory(context).approval_decided(
                before_request,
                drifted_request,
                result.action,
            )
        if decision_kind is ApprovalDecisionKind.REJECT:
            with pytest.raises(ValueError, match="exact authorized transition"):
                AuditEventFactory(context).action_decided(
                    before_action,
                    replace(result.action, terminal_reason_code="unrelated_reason"),
                    result.request,
                )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_10_audit_fault_rolls_back_decision_request_and_action(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-10-audit-rollback.db")
    clock = MutableClock()
    normal = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(normal, event_id="event.run-10.audit-rollback")
    registered = await ApprovalRecordService(normal).register_plan(
        plan,
        audit_context=_context("run-10.audit-rollback.register"),
    )
    source = registered.requests[0]
    clock.current += timedelta(seconds=1)

    def faulting(session: AsyncSession):  # type: ignore[no-untyped-def]
        return FaultAfterAuditAppend(SQLAlchemyAuditRepository(session))

    faulting_dependencies = _dependencies(
        runtime,
        clock=clock,
        ids=IncrementingIds(800),
        audit_factory=faulting,
    )
    try:
        with pytest.raises(RuntimeError, match="injected approval audit failure"):
            await ApprovalDecisionService(faulting_dependencies).decide(
                _command(
                    source.request,
                    ApprovalDecisionKind.APPROVE,
                    suffix="audit-rollback",
                ),
                principal=_principal("audit-rollback"),
            )
        async with normal.unit_of_work() as unit_of_work:
            stored_request = await unit_of_work.approvals.get(source.request.id)
            stored_action = await unit_of_work.external_actions.get(source.request.action_id)
        async with runtime.session_factory() as session:
            decision_count = int(
                (await session.execute(select(func.count(ApprovalDecisionRecord.id)))).scalar_one()
            )
        assert stored_request == source
        assert stored_action == registered.actions.actions[0].action
        assert decision_count == 0
        assert await _timeline(normal, plan.run_id) == (
            "run.received",
            "action.proposed",
            "action.awaiting_approval",
            "approval.requested",
        )
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("generation", "approval_generation_conflict"),
        ("hash", "approval_hash_mismatch"),
        ("expiry", "approval_expired"),
        ("policy_scope", "approval_scope_missing"),
    ],
)
@pytest.mark.asyncio
async def test_run_10_preconditions_fail_without_decision_or_audit_mutation(
    tmp_path: Path,
    failure: str,
    expected_code: str,
) -> None:
    runtime = await _runtime(tmp_path / f"run-10-precondition-{failure}.db")
    clock = MutableClock()
    ids = CountingIds(850)
    dependencies = _dependencies(runtime, clock=clock, ids=ids)
    plan = await _seed_run_and_plan(
        dependencies,
        event_id=f"event.run-10.precondition-{failure}",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context(f"run-10.precondition-{failure}.register"),
    )
    source_request = registered.requests[0]
    source_action = registered.actions.actions[0].action
    request = source_request.request
    command = _command(
        request,
        ApprovalDecisionKind.APPROVE,
        suffix=f"precondition-{failure}",
    )
    principal = _principal(f"precondition-{failure}")
    if failure == "generation":
        command = replace(command, expected_generation=request.generation + 1)
    elif failure == "hash":
        wrong_hash = "0" * 64 if request.action_hash != "0" * 64 else "1" * 64
        command = replace(command, expected_action_hash=wrong_hash)
    elif failure == "expiry":
        clock.current = request.expires_at
    else:
        principal = human_principal(
            actor_id="principal.approver.run-10.policy-scope",
            roles=frozenset({"approver"}),
            scopes=frozenset({"approvals:decide"}),
        )
    id_calls_before = tuple(ids.calls)
    try:
        with pytest.raises(ApprovalDecisionServiceError) as captured:
            await ApprovalDecisionService(dependencies).decide(
                command,
                principal=principal,
            )
        assert captured.value.code == expected_code
        assert tuple(ids.calls) == id_calls_before
        async with dependencies.unit_of_work() as unit_of_work:
            stored_request = await unit_of_work.approvals.get(request.id)
            stored_action = await unit_of_work.external_actions.get(request.action_id)
        async with runtime.session_factory() as session:
            decisions = int(
                (await session.execute(select(func.count(ApprovalDecisionRecord.id)))).scalar_one()
            )
            decision_audits = int(
                (
                    await session.execute(
                        select(func.count(AuditEventRecord.id)).where(
                            AuditEventRecord.approval_decision_id.is_not(None)
                        )
                    )
                ).scalar_one()
            )
        assert stored_request == source_request
        assert stored_action == source_action
        assert (decisions, decision_audits) == (0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_10_concurrent_approve_reject_has_one_decision_and_one_audit_pair(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-10-one-winner.db")
    clock = MutableClock()
    normal = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(normal, event_id="event.run-10.one-winner")
    registered = await ApprovalRecordService(normal).register_plan(
        plan,
        audit_context=_context("run-10.one-winner.register"),
    )
    request = registered.requests[0].request
    clock.current += timedelta(seconds=1)
    barrier = TwoPartyBarrier()

    def approvals(session: AsyncSession) -> ApprovalRepository:
        return cast(
            ApprovalRepository,
            DecisionBarrierApprovalRepository(session, barrier),
        )

    racing = _dependencies(
        runtime,
        clock=clock,
        ids=IncrementingIds(900),
        approval_factory=approvals,
    )
    service = ApprovalDecisionService(racing)
    try:
        outcomes = await asyncio.gather(
            service.decide(
                _command(
                    request,
                    ApprovalDecisionKind.APPROVE,
                    suffix="race-approve",
                ),
                principal=_principal("race-approve"),
            ),
            service.decide(
                _command(
                    request,
                    ApprovalDecisionKind.REJECT,
                    suffix="race-reject",
                ),
                principal=_principal("race-reject"),
            ),
            return_exceptions=True,
        )
        winners = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
        losers = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        assert len(winners) == 1
        assert len(losers) == 1
        assert isinstance(losers[0], ApprovalDecisionServiceError)
        assert losers[0].code == "approval_decision_conflict"
        winner = winners[0]
        assert not isinstance(winner, BaseException)

        async with runtime.session_factory() as session:
            decisions = tuple((await session.execute(select(ApprovalDecisionRecord))).scalars())
            decision_audits = tuple(
                (
                    await session.execute(
                        select(AuditEventRecord)
                        .where(AuditEventRecord.approval_decision_id.is_not(None))
                        .order_by(AuditEventRecord.run_sequence)
                    )
                ).scalars()
            )
            request_record = await session.get(ApprovalRequestRecord, request.id)
            action_record = await session.get(ExternalActionRecord, request.action_id)
        assert len(decisions) == 1
        assert len(decision_audits) == 2
        assert {event.approval_decision_id for event in decision_audits} == {decisions[0].id}
        assert request_record is not None and action_record is not None
        assert request_record.version == 2
        assert action_record.version == 3
        if decisions[0].decision == ApprovalDecisionKind.APPROVE.value:
            assert request_record.status == ApprovalStatus.APPROVED.value
            assert action_record.state == ExternalActionState.APPROVED.value
            assert tuple(event.event_type for event in decision_audits) == (
                "action.approved",
                "approval.approved",
            )
        else:
            assert request_record.status == ApprovalStatus.REJECTED.value
            assert action_record.state == ExternalActionState.REJECTED.value
            assert tuple(event.event_type for event in decision_audits) == (
                "action.rejected",
                "approval.rejected",
            )
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("corrupt_target", ["request", "action"])
@pytest.mark.asyncio
async def test_run_10_hydration_corruption_is_sanitized_before_decision(
    tmp_path: Path,
    corrupt_target: str,
) -> None:
    runtime = await _runtime(tmp_path / f"run-10-corrupt-{corrupt_target}.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(
        dependencies,
        event_id=f"event.run-10.corrupt-{corrupt_target}",
    )
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context(f"run-10.corrupt-{corrupt_target}.register"),
    )
    request = registered.requests[0].request
    corruption_canary = "database-corruption-canary"
    async with runtime.session_factory() as session, session.begin():
        if corrupt_target == "request":
            record = await session.get(ApprovalRequestRecord, request.id)
            assert record is not None
            record.required_roles = ["approver", corruption_canary]
        else:
            action = await session.get(ExternalActionRecord, request.action_id)
            assert action is not None
            action.canonical_envelope = {
                **action.canonical_envelope,
                "minimized_payload": {"canary": corruption_canary},
            }
    clock.current += timedelta(seconds=1)
    try:
        with pytest.raises(ApprovalDecisionServiceError) as captured:
            await ApprovalDecisionService(dependencies).decide(
                _command(
                    request,
                    ApprovalDecisionKind.APPROVE,
                    suffix=f"corrupt-{corrupt_target}",
                ),
                principal=_principal(f"corrupt-{corrupt_target}"),
            )
        assert captured.value.code in {
            "approval_record_corrupt",
            "approval_action_corrupt",
        }
        assert corruption_canary not in str(captured.value)
        assert captured.value.__cause__ is None
        async with runtime.session_factory() as session:
            decision_count = int(
                (await session.execute(select(func.count(ApprovalDecisionRecord.id)))).scalar_one()
            )
        assert decision_count == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_10_renewed_leaf_decides_at_action_version_five_and_old_leaf_fails(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-10-renewed-leaf.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1_000))
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-10.renewed-leaf")
    records = ApprovalRecordService(dependencies)
    registered = await records.register_plan(
        plan,
        audit_context=_context("run-10.renewed-leaf.register"),
    )
    original = registered.requests[0].request
    service = ApprovalDecisionService(dependencies)
    clock.current += timedelta(seconds=1)
    first_decision = await service.decide(
        _command(
            original,
            ApprovalDecisionKind.APPROVE,
            suffix="renewed-original",
        ),
        principal=_principal("renewed-original"),
    )
    assert first_decision.action.version == 3
    clock.current = original.expires_at
    expired = await records.mark_expired(
        request_id=original.id,
        expected_version=2,
        audit_context=_context("run-10.renewed-leaf.expire"),
    )
    clock.current += timedelta(seconds=1)
    renewed = await records.renew_expired(
        request_id=original.id,
        expected_version=expired.version,
        expected_action_hash=original.action_hash,
        audit_context=_context("run-10.renewed-leaf.renew"),
    )
    replacement = renewed.replacement.request
    try:
        with pytest.raises(ApprovalDecisionServiceError) as stale:
            await service.decide(
                _command(
                    original,
                    ApprovalDecisionKind.APPROVE,
                    suffix="renewed-old",
                ),
                principal=_principal("renewed-old"),
            )
        assert stale.value.code == "approval_decision_conflict"

        clock.current += timedelta(seconds=1)
        decided = await service.decide(
            _command(
                replacement,
                ApprovalDecisionKind.APPROVE,
                suffix="renewed-current",
            ),
            principal=_principal("renewed-current"),
        )
        assert decided.request.request.id == replacement.id
        assert decided.request.status is ApprovalStatus.APPROVED
        assert decided.action.state is ExternalActionState.APPROVED
        assert decided.action.version == 5
        assert (await _timeline(dependencies, plan.run_id))[-2:] == (
            "action.approved",
            "approval.approved",
        )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_10_replay_never_heals_a_missing_decision_audit(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-10-no-audit-heal.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock)
    plan = await _seed_run_and_plan(dependencies, event_id="event.run-10.no-audit-heal")
    registered = await ApprovalRecordService(dependencies).register_plan(
        plan,
        audit_context=_context("run-10.no-audit-heal.register"),
    )
    request = registered.requests[0].request
    command = _command(
        request,
        ApprovalDecisionKind.APPROVE,
        suffix="no-audit-heal",
    )
    clock.current += timedelta(seconds=1)
    service = ApprovalDecisionService(dependencies)
    result = await service.decide(command, principal=_principal("no-audit-heal"))
    async with runtime.session_factory() as session, session.begin():
        await session.execute(
            delete(AuditEventRecord).where(
                AuditEventRecord.approval_decision_id == result.decision.id,
                AuditEventRecord.event_type == "approval.approved",
            )
        )
    try:
        async with runtime.session_factory() as session:
            before = int(
                (await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()
            )
        with pytest.raises(ApprovalDecisionServiceError) as replay:
            await service.decide(command, principal=_principal("no-audit-heal"))
        assert replay.value.code == "approval_decision_conflict"
        async with runtime.session_factory() as session:
            after = int(
                (await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()
            )
            decisions = int(
                (await session.execute(select(func.count(ApprovalDecisionRecord.id)))).scalar_one()
            )
        assert after == before
        assert decisions == 1
    finally:
        await runtime.dispose()
