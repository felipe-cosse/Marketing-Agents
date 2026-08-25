"""ORCH-06/ORCH-08 approval boundary, cancellation, and zero-call branches."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import RoutingResult
from marketing_agents.application.policies.write_authorization import WriteAuthorizationGuard
from marketing_agents.application.ports.external_writes import (
    ConnectorDeliveryContract,
    ConnectorDeliveryFailure,
)
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.application.services.approval_boundaries import (
    ApprovalBoundaryDisposition,
    ApprovalBoundaryService,
    ApprovalBoundaryServiceError,
)
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionService,
)
from marketing_agents.application.services.approval_records import ApprovalRecordService
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.application.services.controlled_read_executor import (
    ControlledReadCommand,
    ControlledReadExecutor,
)
from marketing_agents.application.services.external_action_dispatcher import (
    DispatchDisposition,
    ExternalActionDispatcher,
    ExternalActionDispatchError,
)
from marketing_agents.application.services.idempotent_work_receipt import (
    IdempotentWorkRunReceiptService,
)
from marketing_agents.application.services.plan_persistence import (
    AuditedPlanPersistenceService,
)
from marketing_agents.application.services.run_lifecycle import (
    RunLifecycleService,
    RunLifecycleServiceError,
)
from marketing_agents.application.services.run_step_lifecycle import (
    RunStepLifecycleService,
    RunStepLifecycleServiceError,
)
from marketing_agents.domain.audit import AuditEventDraft, _issue_audit_event_draft
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
    RunState,
    StepState,
)
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.run_lifecycle import (
    ApprovalBarrierContext,
    CancellationContext,
    NoRunTransitionContext,
    RunLifecycleCommand,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
)
from marketing_agents.infrastructure.db.models import (
    ApprovalRequestRecord,
    ApprovalUseRecord,
    AuditEventRecord,
    AuthorizationSetHeadRecord,
    AuthorizationSetRecord,
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
    RunPlanRecord,
    RunStateTransitionRecord,
    RunStepRecord,
    RunStepStateTransitionRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyAuditRepository
from marketing_agents.security.audit_metadata import seal_audit_metadata
from sqlalchemy import delete, func, select, update

from tests.integration.db.test_run_08_approval_persistence import (
    APPROVAL_INTEGRITY_KEY,
    IncrementingIds,
    MutableClock,
    _context,
    _dependencies,
    _envelope,
    _multi_plan,
    _runtime,
)
from tests.support.identity import human_principal
from tests.support.incoming_work import validate_incoming_for_test
from tests.support.read_adapter import ExactReadContractAdapter, observation_for
from tests.unit.application.test_run_02_effect_aware_planning import CATALOG, REGISTRY


def _principal(suffix: str):
    return human_principal(
        actor_id=f"principal.approver.orch-08.{suffix}",
        roles=frozenset({"approver", "local_admin"}),
        scopes=frozenset({"approvals:decide", "scope.external-write", "approvals:read"}),
    )


def _decision(request, kind: ApprovalDecisionKind, *, suffix: str):  # type: ignore[no-untyped-def]
    return ApprovalDecisionCommand(
        request_id=request.id,
        expected_generation=request.generation,
        expected_action_hash=request.action_hash,
        decision=kind,
        correlation_id=f"correlation.orch-08.{suffix}",
    )


def _reissue_audit_draft(
    draft: AuditEventDraft,
    **changes: object,
) -> AuditEventDraft:
    values = {
        item.name: getattr(draft, item.name)
        for item in fields(AuditEventDraft)
        if item.name not in {"schema_version", "issuance_fingerprint"}
    }
    values.update(changes)
    return _issue_audit_event_draft(**values)


async def _compose_write_plan(dependencies, *, event_id: str, seed: int):  # type: ignore[no-untyped-def]
    validated = await _receive_and_validate(dependencies, event_id=event_id)
    plan, request = _multi_plan(validated.run.id, seed=seed)
    persisted = await AuditedPlanPersistenceService(dependencies).persist(
        plan,
        request.graph,
        cast(RoutingResult, request.routing),
        expected_run_version=validated.run.version,
        audit_context=_context(f"{event_id}.persist"),
    )
    return plan, persisted


async def _receive_and_validate(dependencies, *, event_id: str):  # type: ignore[no-untyped-def]
    received = await IdempotentWorkRunReceiptService(
        dependencies,
        APPROVAL_INTEGRITY_KEY,
        current_catalog_hash=CATALOG.content_hash,
    ).receive(
        validate_incoming_for_test(
            _envelope(event_id),
            catalog_hash=CATALOG.content_hash,
        ),
        audit_context=_context(f"{event_id}.receive"),
    )
    validated = await RunLifecycleService(dependencies).advance(
        received.run.id,
        received.run.version,
        RunLifecycleCommand.MARK_VALIDATED,
        NoRunTransitionContext(),
        audit_context=_context(f"{event_id}.validate"),
    )
    return validated


class _FaultAfterNthAppendMany:
    def __init__(self, delegate: SQLAlchemyAuditRepository, fail_on_call: int) -> None:
        self._delegate = delegate
        self._fail_on_call = fail_on_call
        self._calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def append_many(self, events):  # type: ignore[no-untyped-def]
        self._calls += 1
        result = await self._delegate.append_many(events)
        if self._calls == self._fail_on_call:
            raise RuntimeError(f"injected plan activation fault {self._fail_on_call}")
        return result


class _ObservingRejectGateway:
    def __init__(self, runtime) -> None:  # type: ignore[no-untyped-def]
        self._runtime = runtime
        self.calls = 0
        self.observed_committed_start = False
        self.observed_event_types: tuple[str, ...] = ()

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        snapshot = action.delivery_contract
        return ConnectorDeliveryContract(
            capability_id=snapshot.capability_id,
            connector_family=snapshot.connector_family,
            binding_id=snapshot.binding_id,
            binding_configuration_revision=snapshot.binding_configuration_revision,
            request_schema_id=snapshot.request_schema_id,
            idempotency_support=snapshot.idempotency_support,
            timeout_seconds=snapshot.timeout_seconds,
        )

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        self.calls += 1
        async with self._runtime.session_factory() as session:
            action = await session.get(
                ExternalActionRecord,
                authorization.action.action_id,
            )
            step = await session.get(
                RunStepRecord,
                authorization.action.step_id,
            )
            event_types = tuple(
                (
                    await session.execute(
                        select(AuditEventRecord.event_type)
                        .where(
                            AuditEventRecord.run_id == authorization.action.run_id,
                            (AuditEventRecord.action_id == authorization.action.action_id)
                            | (AuditEventRecord.step_id == authorization.action.step_id),
                        )
                        .order_by(AuditEventRecord.run_sequence)
                    )
                ).scalars()
            )
        self.observed_committed_start = (
            action is not None
            and action.connector_call_started_at is not None
            and action.state == ExternalActionState.DISPATCHING.value
            and step is not None
            and step.state == StepState.EXECUTING.value
        )
        self.observed_event_types = event_types
        raise ConnectorDeliveryFailure(
            "connector_request_rejected",
            "injected post-commit connector refusal",
            request_may_have_left_process=False,
        )


async def _current(dependencies, run_id: str):  # type: ignore[no-untyped-def]
    async with dependencies.unit_of_work() as unit_of_work:
        run = await unit_of_work.runs.get(run_id)
        steps = await unit_of_work.run_steps.list_for_run(run_id)
        selected = await unit_of_work.approvals.get_current_authorization_set(run_id)
        assert selected is not None
        requests = await unit_of_work.approvals.list_current_set(
            run_id,
            selected.authorization_set.plan_hash,
            selected.authorization_set.proposal_revision,
        )
        actions = tuple(
            [
                await unit_of_work.external_actions.get(member.action_id)
                for member in selected.authorization_set.members
            ]
        )
    assert run is not None and all(action is not None for action in actions)
    return run, steps, selected, requests, actions


async def _approve_complete_set(
    dependencies,  # type: ignore[no-untyped-def]
    clock: MutableClock,
    run_id: str,
    *,
    suffix: str,
) -> None:
    _, _, _, requests, _ = await _current(dependencies, run_id)
    for index, stored in enumerate(requests, start=1):
        clock.current += timedelta(seconds=1)
        await ApprovalDecisionService(dependencies).decide(
            _decision(
                stored.request,
                ApprovalDecisionKind.APPROVE,
                suffix=f"{suffix}.{index}",
            ),
            principal=_principal(f"{suffix}.{index}"),
        )


async def _complete_read_dependency(
    dependencies,  # type: ignore[no-untyped-def]
    clock: MutableClock,
    run_id: str,
) -> None:
    _, steps, _, _, _ = await _current(dependencies, run_id)
    read = steps[0]
    clock.current += timedelta(seconds=1)
    ready = await RunStepLifecycleService(dependencies).advance(
        read.id,
        read.version,
        StepLifecycleCommand.MARK_READY,
        NoStepTransitionContext(),
        audit_context=_context("dispatch.read.mark_ready"),
    )

    class _ReadAdapter(ExactReadContractAdapter):
        def output_contract_for(
            self,
            operation: OperationExecutionPolicy,
        ) -> RuntimeOutputContract:
            if operation.result_schema_id is None:
                raise ValueError("callable test operation requires a result schema")
            result_type = REGISTRY.resolve(operation.capability_id).result_type
            return RuntimeOutputContract(
                schema_id=operation.result_schema_id,
                schema_version="v1",
                schema=result_type.model_json_schema(),  # type: ignore[union-attr]
                classification=operation.data_classification,
                provider_kind="connector",
                provider_mode="mock",
                provider_name=operation.connector_family,
                provider_version=result_type.__name__,
            )

        async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
            assert request.step_id == ready.step.id
            return observation_for(request, {"records": []})

    clock.current += timedelta(seconds=1)
    completed = await ControlledReadExecutor(dependencies, _ReadAdapter()).execute(
        ControlledReadCommand(ready.step.id, {"query": "release-write-dependency"}),
        audit_context=_context("dispatch.read.controlled"),
    )
    assert completed.step.state is StepState.SUCCEEDED


@pytest.mark.asyncio
async def test_orch_08_one_of_two_approvals_has_zero_release_then_final_is_atomic(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-complete-set.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(800))
    try:
        plan, persisted = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.complete-set",
            seed=800,
        )
        assert persisted.run.state is RunState.AWAITING_APPROVAL
        assert [step.state for step in persisted.steps] == [
            StepState.PENDING,
            StepState.AWAITING_APPROVAL,
            StepState.AWAITING_APPROVAL,
        ]
        run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.AWAITING_APPROVAL
        assert selected.authorization_set.status.value == "open"
        assert all(request.status is ApprovalStatus.PENDING for request in requests)
        assert all(
            action is not None and action.state is ExternalActionState.AWAITING_APPROVAL
            for action in actions
        )

        forged_hashes = tuple(request.request.action_hash for request in requests)
        forged_expiry = {
            request.request.action_hash: request.request.expires_at for request in requests
        }
        with pytest.raises(RunLifecycleServiceError) as forged_release:
            await RunLifecycleService(dependencies).advance(
                run.id,
                run.version,
                RunLifecycleCommand.RELEASE_APPROVED_PLAN,
                ApprovalBarrierContext(
                    required_action_hashes=forged_hashes,
                    current_action_hashes=forged_hashes,
                    approved_action_hashes=forged_hashes,
                    expires_at_by_hash=forged_expiry,
                ),
                audit_context=_context("complete-set.forged-release"),
            )
        assert forged_release.value.code == "approval_boundary_service_required"
        write_step = steps[1]
        with pytest.raises(RunStepLifecycleServiceError) as forged_step:
            await RunStepLifecycleService(dependencies).advance(
                write_step.id,
                write_step.version,
                StepLifecycleCommand.RELEASE_APPROVAL,
                NoStepTransitionContext(),
                audit_context=_context("complete-set.forged-step-release"),
            )
        assert forged_step.value.code == "approval_boundary_service_required"
        run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.AWAITING_APPROVAL
        assert selected.authorization_set.status.value == "open"
        assert all(request.use is None for request in requests)
        assert all(action is not None and action.reservation is None for action in actions)

        clock.current += timedelta(seconds=1)
        first = await ApprovalDecisionService(dependencies).decide(
            _decision(
                requests[0].request,
                ApprovalDecisionKind.APPROVE,
                suffix="complete-set.first",
            ),
            principal=_principal("complete-set.first"),
        )
        assert first.request.status is ApprovalStatus.APPROVED
        run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.AWAITING_APPROVAL
        assert [step.state for step in steps] == [
            StepState.PENDING,
            StepState.AWAITING_APPROVAL,
            StepState.AWAITING_APPROVAL,
        ]
        assert selected.authorization_set.status.value == "open"
        assert [request.status for request in requests] == [
            ApprovalStatus.APPROVED,
            ApprovalStatus.PENDING,
        ]
        assert all(request.use is None for request in requests)
        assert all(action is not None and action.reservation is None for action in actions)
        async with runtime.session_factory() as session:
            release_counts = (
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
                int(
                    (
                        await session.execute(
                            select(
                                func.count(ExternalActionDispatchAttemptRecord.external_action_id)
                            )
                        )
                    ).scalar_one()
                ),
            )
        assert release_counts == (0, 0)

        clock.current += timedelta(seconds=1)
        await ApprovalDecisionService(dependencies).decide(
            _decision(
                requests[1].request,
                ApprovalDecisionKind.APPROVE,
                suffix="complete-set.final",
            ),
            principal=_principal("complete-set.final"),
        )
        run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.EXECUTING
        assert selected.authorization_set.status.value == "released"
        assert selected.authorization_set.released_run_version == run.version
        assert [step.state for step in steps] == [
            StepState.PENDING,
            StepState.READY,
            StepState.READY,
        ]
        assert all(request.status is ApprovalStatus.CONSUMED for request in requests)
        assert all(request.use is not None for request in requests)
        assert all(
            action is not None
            and action.state is ExternalActionState.DISPATCH_RESERVED
            and action.reservation is not None
            for action in actions
        )
        async with runtime.session_factory() as session:
            release_counts = (
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
                int(
                    (
                        await session.execute(
                            select(func.count(ExternalActionRecord.reservation_id)).where(
                                ExternalActionRecord.reservation_id.is_not(None)
                            )
                        )
                    ).scalar_one()
                ),
                int(
                    (
                        await session.execute(
                            select(
                                func.count(ExternalActionDispatchAttemptRecord.external_action_id)
                            )
                        )
                    ).scalar_one()
                ),
            )
        assert release_counts == (2, 2, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_transition_tamper", ("run", "step"))
async def test_orch_08_rejection_closes_full_mixed_plan_without_release(
    tmp_path: Path,
    terminal_transition_tamper: str,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-reject.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(900))
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.reject",
            seed=900,
        )
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        clock.current += timedelta(seconds=1)
        await ApprovalDecisionService(dependencies).decide(
            _decision(
                requests[0].request,
                ApprovalDecisionKind.REJECT,
                suffix="reject",
            ),
            principal=_principal("reject"),
        )
        run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.REJECTED
        assert selected.authorization_set.status.value == "rejected"
        assert [step.state for step in steps] == [
            StepState.CANCELLED,
            StepState.REJECTED,
            StepState.CANCELLED,
        ]
        assert requests[0].status is ApprovalStatus.REJECTED
        assert requests[1].status is ApprovalStatus.SUPERSEDED
        assert all(request.use is None for request in requests)
        assert [action.state if action is not None else None for action in actions] == [
            ExternalActionState.REJECTED,
            ExternalActionState.CANCELLED,
        ]
        assert all(action is not None and action.reservation is None for action in actions)
        replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("reject.replay"),
        )
        assert replay.disposition is ApprovalBoundaryDisposition.REJECTED
        async with runtime.session_factory() as session:
            counts = (
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
                int(
                    (
                        await session.execute(
                            select(
                                func.count(ExternalActionDispatchAttemptRecord.external_action_id)
                            )
                        )
                    ).scalar_one()
                ),
                int(
                    (
                        await session.execute(select(func.count(AuthorizationSetRecord.id)))
                    ).scalar_one()
                ),
            )
        assert counts == (0, 0, 1)

        if terminal_transition_tamper == "run":
            aggregate_type = "run"
            aggregate_id = run.id
            mutation_version = run.version
        else:
            aggregate_type = "step"
            aggregate_id = steps[0].id
            mutation_version = steps[0].version
        async with dependencies.unit_of_work() as unit_of_work:
            transition_event = await unit_of_work.audits.get_mutation_event(
                aggregate_type,
                aggregate_id,
                mutation_version,
            )
        assert transition_event is not None
        drifted_at = transition_event.draft.occurred_at + timedelta(microseconds=1)
        drifted_audit = _reissue_audit_draft(
            transition_event.draft,
            occurred_at=drifted_at,
        )
        async with runtime.session_factory() as session, session.begin():
            if terminal_transition_tamper == "run":
                await session.execute(
                    update(RunStateTransitionRecord)
                    .where(
                        RunStateTransitionRecord.run_id == run.id,
                        RunStateTransitionRecord.sequence == run.version,
                    )
                    .values(occurred_at=drifted_at)
                )
            else:
                await session.execute(
                    update(RunStepStateTransitionRecord)
                    .where(
                        RunStepStateTransitionRecord.step_id == steps[0].id,
                        RunStepStateTransitionRecord.sequence == steps[0].version,
                    )
                    .values(occurred_at=drifted_at)
                )
            await session.execute(
                update(AuditEventRecord)
                .where(AuditEventRecord.id == transition_event.draft.id)
                .values(
                    occurred_at=drifted_at,
                    event_fingerprint=drifted_audit.issuance_fingerprint,
                )
            )
        with pytest.raises(RuntimeError) as drifted_replay:
            await ApprovalBoundaryService(dependencies).evaluate(
                plan.run_id,
                audit_context=_context(f"reject.replay.drifted-{terminal_transition_tamper}"),
            )
        assert getattr(drifted_replay.value, "code", None) in {
            "authorization_close_transition_missing",
            "authorization_close_step_audit_mismatch",
            "step_history_state_mismatch",
        }
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expiry_audit_tamper",
    ("previous_state", "occurred_at", "metadata"),
)
async def test_orch_08_expiry_and_pre_release_cancel_consume_nothing(
    tmp_path: Path,
    expiry_audit_tamper: str,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-expiry-cancel.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1000))
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.expiry",
            seed=1000,
        )
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        clock.current += timedelta(seconds=1)
        approved = await ApprovalDecisionService(dependencies).decide(
            _decision(
                requests[0].request,
                ApprovalDecisionKind.APPROVE,
                suffix="expiry.approved-before-expiry",
            ),
            principal=_principal("expiry.approved-before-expiry"),
        )
        assert approved.request.status is ApprovalStatus.APPROVED
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        clock.current = min(request.request.expires_at for request in requests)
        expired = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("expiry.evaluate"),
        )
        assert expired.disposition is ApprovalBoundaryDisposition.EXPIRED
        run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.AWAITING_APPROVAL
        assert selected.authorization_set.status.value == "open"
        assert all(request.status is ApprovalStatus.EXPIRED for request in requests)
        assert all(request.use is None for request in requests)
        assert all(action is not None and action.reservation is None for action in actions)
        assert all(step.state in {StepState.PENDING, StepState.AWAITING_APPROVAL} for step in steps)
        expired_replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("expiry.evaluate.replay"),
        )
        assert expired_replay.disposition is ApprovalBoundaryDisposition.EXPIRED

        with pytest.raises(RunLifecycleServiceError) as forged_cancel:
            await RunLifecycleService(dependencies).advance(
                run.id,
                run.version,
                RunLifecycleCommand.CANCEL,
                CancellationContext(reason_code="operator_cancelled"),
                audit_context=_context("expiry.forged-cancel"),
            )
        assert forged_cancel.value.code == "approval_boundary_service_required"

        cancelled = await ApprovalBoundaryService(dependencies).cancel(
            plan.run_id,
            audit_context=_context("expiry.cancel"),
        )
        assert cancelled.disposition is ApprovalBoundaryDisposition.CANCELLED
        run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.CANCELLED
        assert selected.authorization_set.status.value == "cancelled"
        assert all(step.state is StepState.CANCELLED for step in steps)
        assert all(request.use is None for request in requests)
        assert all(action is not None and action.reservation is None for action in actions)
        replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("expiry.cancel.replay"),
        )
        assert replay.disposition is ApprovalBoundaryDisposition.CANCELLED

        approved_expiry = requests[0]
        async with dependencies.unit_of_work() as unit_of_work:
            expiry_event = await unit_of_work.audits.get_mutation_event(
                "approval_request",
                approved_expiry.request.id,
                approved_expiry.version,
            )
        assert expiry_event is not None
        changes: dict[str, object]
        if expiry_audit_tamper == "previous_state":
            changes = {"previous_state": ApprovalStatus.PENDING.value}
        elif expiry_audit_tamper == "occurred_at":
            changes = {"occurred_at": expiry_event.draft.occurred_at + timedelta(microseconds=1)}
        else:
            metadata = dict(expiry_event.draft.safe_metadata.values)
            metadata["action_version"] += 1
            changes = {
                "safe_metadata": seal_audit_metadata(
                    "approval.expired",
                    metadata,
                    occurred_at=expiry_event.draft.occurred_at,
                    classification=expiry_event.draft.safe_metadata.classification,
                )
            }
        tampered = _reissue_audit_draft(expiry_event.draft, **changes)
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(AuditEventRecord)
                .where(AuditEventRecord.id == expiry_event.draft.id)
                .values(
                    safe_metadata=dict(tampered.safe_metadata.values),
                    metadata_classification=tampered.safe_metadata.classification.value,
                    metadata_expires_at=tampered.safe_metadata.expires_at,
                    metadata_fingerprint=tampered.safe_metadata.issuance_fingerprint,
                    event_fingerprint=tampered.issuance_fingerprint,
                    occurred_at=tampered.occurred_at,
                    previous_state=tampered.previous_state,
                )
            )

        with pytest.raises(ApprovalBoundaryServiceError) as rejected_replay:
            await ApprovalBoundaryService(dependencies).evaluate(
                plan.run_id,
                audit_context=_context(f"expiry.cancel.tampered.{expiry_audit_tamper}"),
            )
        assert rejected_replay.value.code in {
            "authorization_boundary_audit_mismatch",
            "authorization_member_history_invalid",
        }
        async with runtime.session_factory() as session:
            counts = (
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
                int(
                    (
                        await session.execute(
                            select(
                                func.count(ExternalActionDispatchAttemptRecord.external_action_id)
                            )
                        )
                    ).scalar_one()
                ),
            )
        assert counts == (0, 0)

    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_expiry_index", (0, 1))
async def test_orch_08_open_expiry_replay_requires_every_audit_without_healing(
    tmp_path: Path,
    missing_expiry_index: int,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-open-expiry-replay.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1050))
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.open-expiry-replay",
            seed=1050,
        )
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        clock.current += timedelta(seconds=1)
        await ApprovalDecisionService(dependencies).decide(
            _decision(
                requests[0].request,
                ApprovalDecisionKind.APPROVE,
                suffix="open-expiry-replay.approve",
            ),
            principal=_principal("open-expiry-replay.approve"),
        )
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        clock.current = min(request.request.expires_at for request in requests)
        expired = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("open-expiry-replay.expire"),
        )
        assert expired.disposition is ApprovalBoundaryDisposition.EXPIRED
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        assert all(request.status is ApprovalStatus.EXPIRED for request in requests)
        assert requests[0].decision is not None
        assert requests[1].decision is None

        valid_replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("open-expiry-replay.valid"),
        )
        assert valid_replay.disposition is ApprovalBoundaryDisposition.EXPIRED
        removed_request_id = requests[missing_expiry_index].request.id
        async with runtime.session_factory() as session, session.begin():
            removed_id = (
                await session.execute(
                    select(AuditEventRecord.id).where(
                        AuditEventRecord.approval_request_id == removed_request_id,
                        AuditEventRecord.event_type == "approval.expired",
                    )
                )
            ).scalar_one()
            await session.execute(delete(AuditEventRecord).where(AuditEventRecord.id == removed_id))
        async with runtime.session_factory() as session:
            before = (
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
            )
        with pytest.raises(ApprovalBoundaryServiceError) as missing:
            await ApprovalBoundaryService(dependencies).evaluate(
                plan.run_id,
                audit_context=_context(f"open-expiry-replay.missing-{missing_expiry_index}"),
            )
        assert missing.value.code == "authorization_member_history_invalid"
        async with runtime.session_factory() as session:
            after = (
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
            )
        assert after == before
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_08_mixed_generation_expiry_result_is_replay_stable(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-mixed-expiry-result.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1075))
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.mixed-expiry-result",
            seed=1075,
        )
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        clock.current = min(request.request.expires_at for request in requests)
        await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("mixed-expiry-result.initial-expiry"),
        )
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        clock.current += timedelta(seconds=1)
        renewed = await ApprovalRecordService(dependencies).renew_expired(
            request_id=requests[1].request.id,
            expected_version=requests[1].version,
            expected_action_hash=requests[1].request.action_hash,
            audit_context=_context("mixed-expiry-result.renew"),
        )
        clock.current = renewed.replacement.request.expires_at
        first = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("mixed-expiry-result.second-expiry"),
        )
        _, _, _, current, _ = await _current(dependencies, plan.run_id)
        expected_ids = tuple(stored.request.id for stored in current)
        assert all(stored.status is ApprovalStatus.EXPIRED for stored in current)
        assert first.expired_request_ids == expected_ids

        replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("mixed-expiry-result.replay"),
        )
        assert replay.expired_request_ids == first.expired_request_ids
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_08_rejection_closes_an_expired_sibling_without_release(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-expired-rejected.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1100))
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.expired-rejected",
            seed=1100,
        )
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        clock.current = min(request.request.expires_at for request in requests)
        expired = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("expired-rejected.expire"),
        )
        assert expired.disposition is ApprovalBoundaryDisposition.EXPIRED

        clock.current += timedelta(seconds=1)
        renewed = await ApprovalRecordService(dependencies).renew_expired(
            request_id=requests[1].request.id,
            expected_version=2,
            expected_action_hash=requests[1].request.action_hash,
            audit_context=_context("expired-rejected.renew"),
        )
        _, _, _, requests, _ = await _current(dependencies, plan.run_id)
        assert [request.status for request in requests] == [
            ApprovalStatus.EXPIRED,
            ApprovalStatus.PENDING,
        ]

        clock.current += timedelta(seconds=1)
        await ApprovalDecisionService(dependencies).decide(
            _decision(
                renewed.replacement.request,
                ApprovalDecisionKind.REJECT,
                suffix="expired-rejected.reject",
            ),
            principal=_principal("expired-rejected.reject"),
        )
        run, steps, selected, requests, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.REJECTED
        assert selected.authorization_set.status.value == "rejected"
        assert [request.status for request in requests] == [
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REJECTED,
        ]
        assert [action.state if action is not None else None for action in actions] == [
            ExternalActionState.CANCELLED,
            ExternalActionState.REJECTED,
        ]
        assert actions[0] is not None
        assert actions[0].terminal_reason_code == "sibling_approval_rejected"
        assert all(step.state in {StepState.CANCELLED, StepState.REJECTED} for step in steps)
        assert all(request.use is None for request in requests)
        replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("expired-rejected.replay"),
        )
        assert replay.disposition is ApprovalBoundaryDisposition.REJECTED
        async with runtime.session_factory() as session, session.begin():
            renewed_audit_id = (
                await session.execute(
                    select(AuditEventRecord.id).where(
                        AuditEventRecord.approval_request_id == renewed.expired.request.id,
                        AuditEventRecord.event_type == "approval.renewed",
                    )
                )
            ).scalar_one()
            await session.execute(
                delete(AuditEventRecord).where(AuditEventRecord.id == renewed_audit_id)
            )
        async with runtime.session_factory() as session:
            before_replay = (
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
            )
        with pytest.raises(ApprovalBoundaryServiceError) as missing_renewal:
            await ApprovalBoundaryService(dependencies).evaluate(
                plan.run_id,
                audit_context=_context("expired-rejected.missing-renewal"),
            )
        assert missing_renewal.value.code == "authorization_member_history_invalid"
        async with runtime.session_factory() as session:
            after_replay = (
                int((await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()),
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
            )
        assert after_replay == before_replay
        async with runtime.session_factory() as session:
            counts = (
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
                int(
                    (
                        await session.execute(
                            select(
                                func.count(ExternalActionDispatchAttemptRecord.external_action_id)
                            )
                        )
                    ).scalar_one()
                ),
            )
        assert counts == (0, 0)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_before_cancel", (False, True))
async def test_orch_08_post_release_cancel_blocks_claim_and_connector_call(
    tmp_path: Path,
    claim_before_cancel: bool,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-08-post-release-cancel-{claim_before_cancel}.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1300))
    gateway = _ObservingRejectGateway(runtime)
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.post-release-cancel",
            seed=1300,
        )
        await _approve_complete_set(
            dependencies,
            clock,
            plan.run_id,
            suffix="post-release-cancel",
        )
        if claim_before_cancel:
            await _complete_read_dependency(dependencies, clock, plan.run_id)
        run, _, selected, _, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.EXECUTING
        assert selected.authorization_set.status.value == "released"
        action = next(
            candidate
            for candidate in actions
            if candidate is not None and candidate.envelope.step_key == "welcome-z"
        )
        if claim_before_cancel:
            async with dependencies.unit_of_work() as unit_of_work:
                authority = await unit_of_work.approvals.get_release_authority(action.id)
                assert authority is not None
                clock.current += timedelta(seconds=1)
                claimed = await unit_of_work.external_actions.claim_reserved(
                    action_id=action.id,
                    expected_version=action.version,
                    authority=authority,
                    lease_owner="worker.orch-06.claimed-before-cancel",
                    claimed_at=clock.current,
                    lease_expires_at=clock.current + timedelta(minutes=1),
                )
                assert claimed is not None
                await unit_of_work.audits.append(
                    AuditEventFactory(
                        _context("post-release-cancel.claim")
                    ).action_dispatch_claimed(action, claimed)
                )
                await unit_of_work.commit()
                action = claimed

        clock.current += timedelta(seconds=1)
        cancelled = await ApprovalBoundaryService(dependencies).cancel(
            plan.run_id,
            audit_context=_context("post-release-cancel.cancel"),
        )
        assert cancelled.run.state is RunState.CANCELLED
        with pytest.raises(ExternalActionDispatchError) as blocked:
            await ExternalActionDispatcher(
                dependencies,
                gateway,
                WriteAuthorizationGuard(),
            ).dispatch_once(action.id, lease_owner="worker.orch-08.cancelled")
        assert blocked.value.code == "action_not_dispatchable"
        assert gateway.calls == 0

        run, steps, selected, _, actions = await _current(dependencies, plan.run_id)
        assert run.state is RunState.CANCELLED
        assert selected.authorization_set.status.value == "released"
        assert all(candidate is not None for candidate in actions)
        assert all(
            candidate.state is ExternalActionState.CANCELLED
            and candidate.terminal_reason_code == "operator_cancelled"
            and candidate.lease is None
            and candidate.call_started_at is None
            for candidate in actions
            if candidate is not None
        )
        assert all(step.state is not StepState.READY for step in steps)
        replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("post-release-cancel.replay"),
        )
        assert replay.disposition is ApprovalBoundaryDisposition.CANCELLED
        async with runtime.session_factory() as session:
            attempt_rows = tuple(
                (await session.execute(select(ExternalActionDispatchAttemptRecord))).scalars()
            )
        assert len(attempt_rows) == int(claim_before_cancel)
        if attempt_rows:
            assert attempt_rows[0].call_started_at is None
            assert attempt_rows[0].conclusion == "cancelled"
            assert attempt_rows[0].reason_code == "operator_cancelled"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_orch_08_dispatch_forged_authority_is_zero_call_and_first_call_is_committed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "orch-08-dispatch-fence.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1400))
    gateway = _ObservingRejectGateway(runtime)
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.dispatch-fence",
            seed=1400,
        )
        await _approve_complete_set(
            dependencies,
            clock,
            plan.run_id,
            suffix="dispatch-fence",
        )
        _, steps_before_start, _, _, _ = await _current(dependencies, plan.run_id)
        write_before_start = steps_before_start[1]
        with pytest.raises(RunStepLifecycleServiceError) as forged_start:
            await RunStepLifecycleService(dependencies).advance(
                write_before_start.id,
                write_before_start.version,
                StepLifecycleCommand.START,
                NoStepTransitionContext(),
                audit_context=_context("dispatch.forged-generic-write-start"),
            )
        assert forged_start.value.code == "approval_boundary_service_required"
        _, steps_after_start, _, _, _ = await _current(dependencies, plan.run_id)
        assert steps_after_start == steps_before_start
        assert gateway.calls == 0

        await _complete_read_dependency(dependencies, clock, plan.run_id)
        _, _, _, _, actions = await _current(dependencies, plan.run_id)
        action = actions[0]
        assert action is not None

        async with dependencies.unit_of_work() as unit_of_work:
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            assert authority is not None
            clock.current += timedelta(seconds=1)
            forged = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=action.version,
                authority=replace(authority, release_hash="f" * 64),
                lease_owner="worker.orch-08.forged",
                claimed_at=clock.current,
                lease_expires_at=clock.current + timedelta(minutes=1),
            )
            assert forged is None
        assert gateway.calls == 0

        clock.current += timedelta(seconds=1)
        result = await ExternalActionDispatcher(
            dependencies,
            gateway,
            WriteAuthorizationGuard(),
        ).dispatch_once(action.id, lease_owner="worker.orch-08.first-call")
        assert result.disposition is DispatchDisposition.FAILED
        assert gateway.calls == 1
        assert gateway.observed_committed_start is True
        assert "action.dispatch_claimed" in gateway.observed_event_types
        assert "action.call_started" in gateway.observed_event_types
        assert "step.transitioned" in gateway.observed_event_types

        async with runtime.session_factory() as session:
            attempt = (
                await session.execute(
                    select(ExternalActionDispatchAttemptRecord).where(
                        ExternalActionDispatchAttemptRecord.external_action_id == action.id
                    )
                )
            ).scalar_one()
        assert attempt.call_started_at is not None
        assert attempt.conclusion == ExternalActionState.FAILED.value
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_event_type",
    (
        "action.proposed",
        "action.awaiting_approval",
        "approval.requested",
        "approval.consumed",
    ),
)
async def test_orch_08_released_replay_requires_every_member_audit_without_healing(
    tmp_path: Path,
    missing_event_type: str,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-08-release-replay-{missing_event_type}.db")
    clock = MutableClock()
    dependencies = _dependencies(runtime, clock=clock, ids=IncrementingIds(1500))
    try:
        plan, _ = await _compose_write_plan(
            dependencies,
            event_id="event.orch-08.release-replay",
            seed=1500,
        )
        await _approve_complete_set(
            dependencies,
            clock,
            plan.run_id,
            suffix="release-replay",
        )
        replay = await ApprovalBoundaryService(dependencies).evaluate(
            plan.run_id,
            audit_context=_context("release-replay.valid"),
        )
        assert replay.disposition is ApprovalBoundaryDisposition.RELEASED

        async with runtime.session_factory() as session, session.begin():
            removed_id = (
                await session.execute(
                    select(AuditEventRecord.id)
                    .where(
                        AuditEventRecord.run_id == plan.run_id,
                        AuditEventRecord.event_type == missing_event_type,
                    )
                    .order_by(AuditEventRecord.run_sequence)
                    .limit(1)
                )
            ).scalar_one()
            await session.execute(delete(AuditEventRecord).where(AuditEventRecord.id == removed_id))
        async with runtime.session_factory() as session:
            before = (
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
                int(
                    (
                        await session.execute(
                            select(func.count(AuditEventRecord.id)).where(
                                AuditEventRecord.run_id == plan.run_id
                            )
                        )
                    ).scalar_one()
                ),
            )
        with pytest.raises(ApprovalBoundaryServiceError) as missing:
            await ApprovalBoundaryService(dependencies).evaluate(
                plan.run_id,
                audit_context=_context("release-replay.missing"),
            )
        assert missing.value.code == "authorization_member_history_invalid"
        async with runtime.session_factory() as session:
            after = (
                int((await session.execute(select(func.count(ApprovalUseRecord.id)))).scalar_one()),
                int(
                    (
                        await session.execute(
                            select(func.count(AuditEventRecord.id)).where(
                                AuditEventRecord.run_id == plan.run_id
                            )
                        )
                    ).scalar_one()
                ),
            )
        assert after == before
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("fail_on_append_many", [2, 3])
@pytest.mark.asyncio
async def test_orch_08_write_activation_fault_rolls_back_the_complete_plan_boundary(
    tmp_path: Path,
    fail_on_append_many: int,
) -> None:
    runtime = await _runtime(tmp_path / f"orch-08-activation-rollback-{fail_on_append_many}.db")
    clock = MutableClock()
    normal = _dependencies(runtime, clock=clock, ids=IncrementingIds(1100))
    try:
        validated = await _receive_and_validate(
            normal,
            event_id=f"event.orch-08.rollback.{fail_on_append_many}",
        )
        plan, request = _multi_plan(validated.run.id, seed=1100 + fail_on_append_many)

        def faulting(session):  # type: ignore[no-untyped-def]
            return _FaultAfterNthAppendMany(
                SQLAlchemyAuditRepository(session),
                fail_on_append_many,
            )

        faulting_dependencies = _dependencies(
            runtime,
            clock=clock,
            ids=IncrementingIds(1200),
            audit_factory=faulting,
        )
        with pytest.raises(
            RuntimeError,
            match=f"injected plan activation fault {fail_on_append_many}",
        ):
            await AuditedPlanPersistenceService(faulting_dependencies).persist(
                plan,
                request.graph,
                cast(RoutingResult, request.routing),
                expected_run_version=validated.run.version,
                audit_context=_context(f"rollback.{fail_on_append_many}"),
            )

        async with normal.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(validated.run.id)
        assert run == validated.run
        async with runtime.session_factory() as session:
            counts = tuple(
                [
                    int((await session.execute(select(func.count(column)))).scalar_one())
                    for column in (
                        RunPlanRecord.run_id,
                        RunStepRecord.id,
                        ExternalActionRecord.id,
                        ApprovalRequestRecord.id,
                        AuthorizationSetRecord.id,
                        AuthorizationSetHeadRecord.run_id,
                    )
                ]
            )
            audit_count = int(
                (await session.execute(select(func.count(AuditEventRecord.id)))).scalar_one()
            )
        assert counts == (0, 0, 0, 0, 0, 0)
        assert audit_count == 2
    finally:
        await runtime.dispose()
