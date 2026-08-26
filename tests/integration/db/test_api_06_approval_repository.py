"""API-06 approval repository query and immutable renewal persistence evidence."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionService,
)
from marketing_agents.application.services.approval_records import ApprovalRecordService
from marketing_agents.application.services.approval_resources import (
    ApprovalListQuery,
    ApprovalRequestCommand,
    ApprovalRequestDisposition,
    ApprovalResourceService,
)
from marketing_agents.domain.enums import ApprovalDecisionKind, ApprovalStatus
from marketing_agents.infrastructure.db import ApprovalPersistenceConflict
from marketing_agents.infrastructure.db.models import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    AuditEventRecord,
)
from marketing_agents.infrastructure.db.repositories.approval import _seal_request_record
from sqlalchemy import delete, func, select

from tests.integration.db.test_run_08_approval_persistence import (
    APPROVAL_INTEGRITY_KEY,
    IncrementingIds,
    MutableClock,
    _context,
    _dependencies,
    _runtime,
    _seed_run_and_plan,
)
from tests.support.identity import human_principal


def _reader():  # type: ignore[no-untyped-def]
    return human_principal(
        actor_id="principal.api-06.viewer",
        roles=frozenset({"viewer"}),
        scopes=frozenset({"approvals:read"}),
    )


def _requester():  # type: ignore[no-untyped-def]
    return human_principal(
        actor_id="principal.api-06.operator",
        roles=frozenset({"operator"}),
        scopes=frozenset({"approvals:read", "approvals:request"}),
    )


def _approver():  # type: ignore[no-untyped-def]
    return human_principal(
        actor_id="principal.api-06.approver",
        roles=frozenset({"approver"}),
        scopes=frozenset({"approvals:decide", "scope.external-write", "approvals:read"}),
    )


@pytest.mark.asyncio
async def test_api_06_keyset_pages_are_bounded_deterministic_and_filter_exactly(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "api-06-keyset.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(3_000))
    records = ApprovalRecordService(dependencies)
    try:
        plan = await _seed_run_and_plan(
            dependencies,
            event_id="event.api-06.keyset",
            seed=110,
            multiple_writes=True,
        )
        registered = await records.register_plan(
            plan,
            audit_context=_context("api-06.keyset.register"),
        )
        initial = registered.requests
        assert len({item.request.requested_at for item in initial}) == 1
        clock.current = initial[0].request.expires_at + timedelta(seconds=1)
        for index, item in enumerate(initial, start=1):
            renewed = await records.renew_expired(
                request_id=item.request.id,
                expected_version=1,
                expected_action_hash=item.request.action_hash,
                audit_context=_context(f"api-06.keyset.renew.{index}"),
            )
            assert renewed.replacement.request.generation == 2

        async with dependencies.unit_of_work() as unit_of_work:
            stored = list(
                await unit_of_work.approvals.list_set_history(
                    plan.run_id,
                    plan.plan_hash,
                    initial[0].request.proposal_revision,
                )
            )
        assert len(stored) == 4
        expected = tuple(
            sorted(
                stored,
                key=lambda item: (item.request.requested_at, item.request.id),
                reverse=True,
            )
        )
        resources = ApprovalResourceService(dependencies)
        first = await resources.list(ApprovalListQuery(limit=2), principal=_reader())
        first_replay = await resources.list(ApprovalListQuery(limit=2), principal=_reader())
        assert tuple(item.approval_id for item in first.items) == tuple(
            item.request.id for item in expected[:2]
        )
        assert first_replay == first
        assert first.next_cursor is not None

        second = await resources.list(
            ApprovalListQuery(limit=2, cursor=first.next_cursor),
            principal=_reader(),
        )
        assert tuple(item.approval_id for item in second.items) == tuple(
            item.request.id for item in expected[2:]
        )
        assert second.next_cursor is None
        assert len({item.approval_id for item in first.items + second.items}) == 4

        pending = await resources.list(
            ApprovalListQuery(status=ApprovalStatus.PENDING, limit=100),
            principal=_reader(),
        )
        expired_page = await resources.list(
            ApprovalListQuery(status=ApprovalStatus.EXPIRED, limit=100),
            principal=_reader(),
        )
        by_run = await resources.list(
            ApprovalListQuery(run_id=stored[1].request.run_id, limit=100),
            principal=_reader(),
        )
        by_action = await resources.list(
            ApprovalListQuery(action_id=stored[2].request.action_id, limit=100),
            principal=_reader(),
        )
        assert {item.approval_id for item in pending.items} == {
            item.request.id for item in stored if item.status is ApprovalStatus.PENDING
        }
        assert {item.approval_id for item in expired_page.items} == {
            item.request.id for item in stored if item.status is ApprovalStatus.EXPIRED
        }
        assert {item.approval_id for item in by_run.items} == {item.request.id for item in stored}
        assert {item.approval_id for item in by_action.items} == {
            item.request.id
            for item in stored
            if item.request.action_id == stored[2].request.action_id
        }
        with pytest.raises(ValueError, match="outside the supported range"):
            ApprovalListQuery(limit=101)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_06_renewal_restart_replay_hydrates_one_exact_action_chain(
    tmp_path: Path,
) -> None:
    path = tmp_path / "api-06-renewal-restart.db"
    runtime = await _runtime(path)
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(4_000))
    records = ApprovalRecordService(dependencies)
    try:
        plan = await _seed_run_and_plan(
            dependencies,
            event_id="event.api-06.renewal",
            seed=510,
        )
        registered = await records.register_plan(
            plan,
            audit_context=_context("api-06.renewal.register"),
        )
        source = registered.requests[0].request
        clock.current = source.expires_at + timedelta(seconds=1)
        command = ApprovalRequestCommand(
            action_id=source.action_id,
            expected_generation=1,
            expected_action_hash=source.action_hash,
            correlation_id="request.api-06.renewal",
        )
        created = await ApprovalResourceService(dependencies).request(
            command,
            principal=_requester(),
        )
        assert created.disposition is ApprovalRequestDisposition.RENEWED
        assert created.approval.generation == 2
        assert created.approval.status is ApprovalStatus.PENDING
        assert created.approval.action_id == source.action_id
        assert created.approval.payload_hash == source.action_hash
        assert created.approval.requested_by == _requester().actor_id

        record_replay = await records.renew_expired(
            request_id=source.id,
            expected_version=1,
            expected_action_hash=source.action_hash,
            audit_context=_context("api-06.renewal.record-replay"),
            requested_by=_requester().actor_id,
        )
        assert record_replay.created is False
        assert record_replay.replacement.request.id == created.approval.approval_id
        assert record_replay.replacement.request.requested_by == _requester().actor_id
    finally:
        await runtime.dispose()

    restarted = await _runtime(path)
    restarted_dependencies = _dependencies(
        restarted,
        clock=clock,
        ids=IncrementingIds(5_000),
    )
    try:
        replayed = await ApprovalResourceService(restarted_dependencies).request(
            command,
            principal=_requester(),
        )
        assert replayed.disposition is ApprovalRequestDisposition.EXISTING
        assert replayed.approval.approval_id == created.approval.approval_id
        assert replayed.approval.resource_version == created.approval.resource_version

        async with restarted_dependencies.unit_of_work() as unit_of_work:
            chain = await unit_of_work.approvals.list_for_action(source.action_id)
            action = await unit_of_work.external_actions.get(source.action_id)
        assert action is not None
        assert tuple(item.request.generation for item in chain) == (1, 2)
        assert chain[0].status is ApprovalStatus.EXPIRED
        assert chain[0].replacement_request_id == chain[1].request.id
        assert chain[1].status is ApprovalStatus.PENDING
        assert chain[1].replacement_request_id is None
        assert all(
            item.request.action_id == action.id
            and item.request.action_hash == action.action_hash
            and item.request.run_id == action.run_id
            and item.request.step_id == action.step_id
            and item.request.authorization_set_id == action.envelope.authorization_set_id
            and item.request.redacted_projection == action.proposal.redacted_projection
            for item in chain
        )

        historical = await ApprovalResourceService(restarted_dependencies).read(
            source.id,
            principal=_reader(),
        )
        assert historical.approval_id == source.id
        assert historical.generation == 1
        assert historical.status is ApprovalStatus.EXPIRED
        assert historical.replacement_approval_id == chain[1].request.id
        assert historical.is_actionable is False
        async with restarted_dependencies.unit_of_work() as unit_of_work:
            inspectable = await unit_of_work.approvals.get_inspectable(source.id)
        assert inspectable == chain[0]
        async with restarted.session_factory() as session:
            request_count = int(
                (await session.execute(select(func.count(ApprovalRequestRecord.id)))).scalar_one()
            )
        assert request_count == 2
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_api_06_resealed_request_projection_tamper_fails_action_hydration(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "api-06-resealed-tamper.db")
    dependencies = _dependencies(runtime, ids=IncrementingIds(6_000))
    try:
        plan = await _seed_run_and_plan(
            dependencies,
            event_id="event.api-06.resealed-tamper",
            seed=610,
        )
        registered = await ApprovalRecordService(dependencies).register_plan(
            plan,
            audit_context=_context("api-06.resealed-tamper.register"),
        )
        request_id = registered.requests[0].request.id
        async with runtime.session_factory() as session, session.begin():
            record = await session.get(ApprovalRequestRecord, request_id)
            assert record is not None
            forged_projection = dict(record.redacted_projection)
            forged_projection["payload"] = {"body": "[FORGED SAFE PROJECTION]"}
            record.redacted_projection = forged_projection
            _seal_request_record(record, APPROVAL_INTEGRITY_KEY)

        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as captured:
                await unit_of_work.approvals.get(request_id)
        assert captured.value.code == "approval_request_corrupt"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_06_decision_reason_round_trips_and_is_covered_by_keyed_integrity(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "api-06-decision-reason.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(6_500))
    reason = "Declined until the campaign owner confirms the audience."
    try:
        plan = await _seed_run_and_plan(
            dependencies,
            event_id="event.api-06.decision-reason",
            seed=660,
        )
        registered = await ApprovalRecordService(dependencies).register_plan(
            plan,
            audit_context=_context("api-06.decision-reason.register"),
        )
        source = registered.requests[0].request
        clock.current += timedelta(seconds=1)
        decided = await ApprovalDecisionService(dependencies).decide(
            ApprovalDecisionCommand(
                request_id=source.id,
                expected_generation=source.generation,
                expected_action_hash=source.action_hash,
                decision=ApprovalDecisionKind.REJECT,
                correlation_id="request.api-06.decision-reason",
                reason=reason,
            ),
            principal=_approver(),
        )
        assert decided.decision.reason == reason

        async with dependencies.unit_of_work() as unit_of_work:
            hydrated = await unit_of_work.approvals.get(source.id)
        assert hydrated is not None and hydrated.decision is not None
        assert hydrated.decision.reason == reason

        async with runtime.session_factory() as session, session.begin():
            record = await session.get(ApprovalDecisionRecord, decided.decision.id)
            assert record is not None
            assert record.reason == reason
            record.reason = "Tampered persisted reason"

        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as captured:
                await unit_of_work.approvals.get(source.id)
        assert captured.value.code == "approval_integrity_corrupt"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_06_missing_generation_in_persisted_chain_fails_closed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "api-06-generation-gap.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(7_000))
    try:
        plan = await _seed_run_and_plan(
            dependencies,
            event_id="event.api-06.generation-gap",
            seed=710,
        )
        registered = await ApprovalRecordService(dependencies).register_plan(
            plan,
            audit_context=_context("api-06.generation-gap.register"),
        )
        source = registered.requests[0].request
        clock.current = source.expires_at + timedelta(seconds=1)
        renewed = await ApprovalResourceService(dependencies).request(
            ApprovalRequestCommand(
                action_id=source.action_id,
                expected_generation=1,
                expected_action_hash=source.action_hash,
                correlation_id="request.api-06.generation-gap",
            ),
            principal=_requester(),
        )
        assert renewed.approval.generation == 2

        # Simulate an incomplete restore that retained the new leaf but lost its
        # immutable predecessor. Remove dependent audit rows only to make the
        # corruption fixture physically representable with foreign keys on.
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                delete(AuditEventRecord).where(AuditEventRecord.run_id == source.run_id)
            )
            deleted = await session.execute(
                delete(ApprovalRequestRecord).where(ApprovalRequestRecord.id == source.id)
            )
            assert deleted.rowcount == 1

        async with dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ApprovalPersistenceConflict) as captured:
                await unit_of_work.approvals.list_for_action(source.action_id)
        assert captured.value.code == "approval_generation_gap"
    finally:
        await runtime.dispose()
