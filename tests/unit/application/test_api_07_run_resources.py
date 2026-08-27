"""API-07 run-resource authorization, ordering, and cursor boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest
from marketing_agents.application.ports.repositories import (
    InspectableArtifact,
    InspectableRun,
    InspectableRunPlan,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.application.services.run_resources import (
    RunListQuery,
    RunResourceService,
    RunResourceServiceError,
    RunTimelineQuery,
    _plain_redacted_mapping,
)
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    semantic_action_hash,
)
from marketing_agents.domain.approval import ApprovalPolicySnapshot, ProposedExternalAction
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import (
    ActionReservationSnapshot,
    ConnectorActionReceipt,
    DeliveryContractSnapshot,
    ExternalAction,
    ExternalActionResultSnapshot,
    Run,
    RunPlanSelectedInstance,
    RunPlanSnapshot,
    WorkItem,
)
from marketing_agents.domain.enums import (
    ApprovalStatus,
    Effect,
    ExternalActionState,
    RunState,
    WorkMode,
)
from marketing_agents.domain.execution_control import RunExecutionControl
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.run_lifecycle import (
    RunLifecycleCommand,
    RunStateTransition,
    initial_received_transition,
)
from marketing_agents.domain.runtime_policy import (
    RunRuntimePolicy,
    effective_call_timeout_seconds,
)
from marketing_agents.domain.step_lifecycle import initial_pending_transition

from tests.support.identity import human_principal, service_principal

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)
CATALOG_HASH = "catalog-sha256-v1:" + ("a" * 64)


def _inspectable(instance_id: str, suffix: str, created_at: datetime) -> InspectableRun:
    work = WorkItem(
        id=f"work.{suffix}",
        source="manual.local",
        event_id=f"event.{suffix}",
        instance_id=instance_id,
        trigger_id="trigger.manual",
        workflow_id="workflow.api-07",
        mode=WorkMode.DRY_RUN,
        brief_id=None,
        configuration_revision=1,
        input_digest="b" * 64,
        admission_digest="c" * 64,
        created_at=created_at,
        brief_revision=None,
        digest_key_version="admission-hmac-sha256-v1:" + ("d" * 64),
        admitted_payload={},
        redacted_input_projection={},
        input_schema_id="schema.api-07.input",
        input_schema_hash="schema-sha256-v1:" + ("e" * 64),
        input_classification=DataClassification.INTERNAL,
    )
    run = Run(
        id=f"run.{suffix}",
        work_item_id=work.id,
        state=RunState.RECEIVED,
        catalog_hash=CATALOG_HASH,
        configuration_revision=1,
        created_at=created_at,
        updated_at=created_at,
    )
    return InspectableRun(
        run=run,
        work_item=work,
        transitions=(initial_received_transition(run),),
    )


def _planned_fixture() -> tuple[
    InspectableRun,
    InspectableRunPlan,
    RunExecutionControl,
    Any,
    InspectableArtifact,
]:
    from tests.unit.application.test_api_07_artifact_resources import _artifact

    artifact = _artifact()
    step = artifact.step
    work = WorkItem(
        id="work.api-07",
        source="manual.local",
        event_id="event.api-07.planned",
        instance_id=step.selected_instance_id,
        trigger_id="trigger.manual",
        workflow_id="workflow.api-07",
        mode=WorkMode.DRY_RUN,
        brief_id=None,
        configuration_revision=1,
        input_digest="1" * 64,
        admission_digest="2" * 64,
        created_at=NOW,
        brief_revision=None,
        digest_key_version="admission-hmac-sha256-v1:" + ("3" * 64),
        admitted_payload={},
        redacted_input_projection={},
        input_schema_id="schema.api-07.input",
        input_schema_hash="schema-sha256-v1:" + ("4" * 64),
        input_classification=DataClassification.INTERNAL,
    )
    run = Run(
        id="run.api-07",
        work_item_id=work.id,
        state=RunState.PLANNED,
        catalog_hash="catalog-sha256-v1:" + ("a" * 64),
        configuration_revision=1,
        created_at=NOW,
        updated_at=NOW,
        version=3,
        approval_required=False,
    )
    transitions = (
        RunStateTransition(
            run_id=run.id,
            sequence=1,
            command=RunLifecycleCommand.RECEIVE,
            previous_state=None,
            new_state=RunState.RECEIVED,
            reason_code="work_admitted",
            occurred_at=NOW,
            expected_version=0,
            resulting_version=1,
        ),
        RunStateTransition(
            run_id=run.id,
            sequence=2,
            command=RunLifecycleCommand.MARK_VALIDATED,
            previous_state=RunState.RECEIVED,
            new_state=RunState.VALIDATED,
            reason_code="work_validated",
            occurred_at=NOW,
            expected_version=1,
            resulting_version=2,
        ),
        RunStateTransition(
            run_id=run.id,
            sequence=3,
            command=RunLifecycleCommand.RECORD_PLAN,
            previous_state=RunState.VALIDATED,
            new_state=RunState.PLANNED,
            reason_code="plan_recorded",
            occurred_at=NOW,
            expected_version=2,
            resulting_version=3,
        ),
    )
    inspectable = InspectableRun(run=run, work_item=work, transitions=transitions)
    plan = RunPlanSnapshot(
        run_id=run.id,
        plan_hash=step.plan_hash,
        workflow_id=work.workflow_id,
        workflow_version=1,
        workflow_definition_hash="5" * 64,
        catalog_content_hash=run.catalog_hash,
        graph_hash=step.graph_hash,
        routing_hash="6" * 64,
        approval_required=False,
        step_count=1,
        runtime_policy=RunRuntimePolicy(20, 10, 20, 3600),
        created_at=NOW,
    )
    plan_item = InspectableRunPlan(
        plan=plan,
        selected_instances=(
            RunPlanSelectedInstance(
                run_id=run.id,
                plan_hash=plan.plan_hash,
                instance_id=step.selected_instance_id,
                template_id=step.template_id,
                configuration_revision=1,
                display_order=1,
                source_ordinal=None,
                selection_order=1,
                target=True,
            ),
        ),
        assignments=(),
        steps=(step,),
    )
    control = RunExecutionControl(
        run_id=run.id,
        policy_hash="7" * 64,
        run_timeout_seconds=3600,
        max_model_calls=10,
        max_tool_calls=20,
        model_calls=2,
        tool_calls=3,
        started_at=None,
        deadline_at=None,
        cancel_requested_at=None,
        cancel_actor_digest=None,
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )
    approval = SimpleNamespace(
        status=ApprovalStatus.PENDING,
        request=SimpleNamespace(
            id="approval.api-07",
            action_id="action.api-07",
            step_id=step.id,
            run_id=run.id,
            redacted_destination="configured destination",
            requested_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        ),
    )
    return inspectable, plan_item, control, approval, artifact


def _succeeded_action_fixture(
    plan: InspectableRunPlan,
) -> tuple[ExternalAction, ConnectorActionReceipt]:
    step = plan.steps[0]
    semantic = SemanticExternalAction(
        template_id=step.template_id,
        instance_id=step.selected_instance_id,
        action_type="inspect",
        capability_id=step.capability_id,
        connector_family=step.connector_family,
        binding_id=step.binding_id,
        destination="configured:api-07",
        payload_schema_id=step.request_schema_id,
        minimized_payload={"mode": "mock"},
    )
    envelope = CanonicalExternalAction(
        action_id="action.api-07.succeeded",
        authorization_set_id="authorization-set.api-07.succeeded",
        run_id=step.run_id,
        plan_hash=step.plan_hash,
        proposal_revision=1,
        step_id=step.id,
        step_key=step.key,
        template_id=step.template_id,
        instance_id=step.selected_instance_id,
        action_type=semantic.action_type,
        capability_id=semantic.capability_id,
        connector_family=semantic.connector_family,
        binding_id=semantic.binding_id,
        destination=semantic.destination,
        payload_schema_id=semantic.payload_schema_id,
        minimized_payload=semantic.minimized_payload,
        semantic_action_hash=semantic_action_hash(semantic),
    )
    proposal = ProposedExternalAction.create(
        envelope,
        redacted_destination="configured destination",
        payload_schema={"type": "object", "properties": {"mode": {"type": "string"}}},
    )
    policy = ApprovalPolicySnapshot(
        policy_id="policy.api-07.external-action",
        required_roles=frozenset({"approver"}),
        required_scopes=frozenset({"external_action.approve"}),
        expires_after_seconds=900,
        allow_self_approval=False,
    )
    proposed = ExternalAction.proposed(
        proposal,
        policy,
        DeliveryContractSnapshot(
            capability_id=step.capability_id,
            connector_family=step.connector_family,
            binding_id=step.binding_id,
            binding_configuration_revision=step.binding_configuration_revision or 1,
            request_schema_id=step.request_schema_id,
            idempotency_support="required",
            timeout_seconds=effective_call_timeout_seconds(
                step.runtime_policy,
                step.timeout_seconds,
            ),
        ),
        NOW,
    )
    reservation = ActionReservationSnapshot(
        reservation_id="reservation.api-07.succeeded",
        authorization_set_id=envelope.authorization_set_id,
        approval_request_id="approval-request.api-07.succeeded",
        approval_decision_id="approval-decision.api-07.succeeded",
        action_hash=proposal.action_hash,
        capability_id=envelope.capability_id,
        binding_id=envelope.binding_id,
        idempotency_key=proposed.idempotency_key,
        reserved_at=NOW,
    )
    result = ExternalActionResultSnapshot(
        receipt_id="receipt.api-07.succeeded",
        status="delivered",
        safe_metadata={"mode": "mock"},
        completed_at=NOW,
    )
    action = replace(
        proposed,
        state=ExternalActionState.SUCCEEDED,
        version=4,
        delivery_attempt_count=1,
        reservation=reservation,
        result=result,
    )
    receipt = ConnectorActionReceipt(
        external_action_id=action.id,
        connector_binding_id=action.connector_binding_id,
        idempotency_key=action.idempotency_key,
        action_hash=action.action_hash,
        capability_id=action.envelope.capability_id,
        receipt_id=result.receipt_id,
        status=result.status,
        safe_metadata=result.safe_metadata,
        created_at=NOW,
    )
    return action, receipt


def _write_action_plan(plan: InspectableRunPlan) -> InspectableRunPlan:
    step = replace(
        plan.steps[0],
        kind="connector.write",
        effect=Effect.WRITE,
        idempotency_support="required",
        timeout_seconds=60,
        approval_policy_id="policy.api-07.external-action",
        approval_required_roles=("approver",),
        approval_required_scopes=("external_action.approve",),
        approval_expires_after_seconds=900,
        approval_allow_self_approval=False,
    )
    return replace(plan, steps=(step,))


class _Runs:
    def __init__(self, items: tuple[InspectableRun, ...]) -> None:
        self.items = items
        self.calls: list[dict[str, object]] = []

    async def list_inspectable(self, **values: object) -> tuple[InspectableRun, ...]:
        self.calls.append(values)
        instance_id = values["instance_id"]
        limit = values["limit"]
        assert type(limit) is int
        selected = (
            self.items
            if instance_id is None
            else tuple(item for item in self.items if item.work_item.instance_id == instance_id)
        )
        return selected[:limit]


class _UnitOfWork:
    def __init__(self, runs: _Runs) -> None:
        self.runs = runs
        self.entries = 0

    async def __aenter__(self) -> _UnitOfWork:
        self.entries += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        raise AssertionError("runtime reads must never commit")


class _Factory:
    def __init__(self, unit_of_work: _UnitOfWork) -> None:
        self.unit_of_work = unit_of_work
        self.calls = 0

    def __call__(self) -> _UnitOfWork:
        self.calls += 1
        return self.unit_of_work


class _ExplodingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _UnitOfWork:
        self.calls += 1
        raise AssertionError("authorization must precede unit-of-work access")


class _CompositeRuns:
    def __init__(self, snapshots: list[InspectableRun]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    async def get_inspectable(self, run_id: str) -> InspectableRun | None:
        self.calls += 1
        if not self.snapshots or self.snapshots[0].run.id != run_id:
            return None
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


class _RunSteps:
    def __init__(self, plan: InspectableRunPlan | None) -> None:
        self.plan = plan

    async def get_inspectable_plan(self, run_id: str) -> InspectableRunPlan | None:
        if self.plan is None or self.plan.plan.run_id != run_id:
            return None
        return self.plan

    async def get(self, step_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"raw step lookup is forbidden for API-07: {step_id}")

    async def list_transitions(self, step_id: str):  # type: ignore[no-untyped-def]
        if self.plan is None:
            return ()
        matches = tuple(value for value in self.plan.steps if value.id == step_id)
        return () if len(matches) != 1 else (initial_pending_transition(matches[0]),)


class _ExecutionControl:
    def __init__(self, control: RunExecutionControl | None) -> None:
        self.control = control

    async def get(self, run_id: str) -> RunExecutionControl | None:
        return self.control if self.control is not None and self.control.run_id == run_id else None


class _Approvals:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self.values = values

    async def list_requests(self, **_: object) -> tuple[Any, ...]:
        return self.values


class _Artifacts:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self.values = values

    async def list_for_run_page(self, run_id: str, **_: object) -> tuple[Any, ...]:
        return tuple(value for value in self.values if value.artifact.provenance.run_id == run_id)


class _Actions:
    def __init__(self, action: Any | None = None) -> None:
        self.action = action

    async def list_run_plan(self, run_id: str, plan_hash: str) -> tuple[Any, ...]:
        if self.action is None:
            return ()
        envelope = self.action.proposal.envelope
        return (
            (self.action,) if (envelope.run_id, envelope.plan_hash) == (run_id, plan_hash) else ()
        )

    async def get(self, action_id: str):  # type: ignore[no-untyped-def]
        if self.action is None:
            return None
        return self.action if self.action.proposal.envelope.action_id == action_id else None


class _ConnectorReceipts:
    def __init__(self, receipt: ConnectorActionReceipt | None = None) -> None:
        self.receipt = receipt

    async def get(
        self,
        connector_binding_id: str,
        idempotency_key: str,
    ) -> ConnectorActionReceipt | None:
        if self.receipt is None:
            return None
        if (
            self.receipt.connector_binding_id,
            self.receipt.idempotency_key,
        ) != (connector_binding_id, idempotency_key):
            return None
        return self.receipt


class _Audits:
    def __init__(self, events: tuple[AuditEvent, ...] = ()) -> None:
        self.events = events
        self.calls = 0

    async def list_run(self, run_id: str, **_: object) -> tuple[AuditEvent, ...]:
        self.calls += 1
        return tuple(value for value in self.events if value.run_id == run_id)


class _CompositeUnitOfWork:
    def __init__(
        self,
        *,
        runs: _CompositeRuns,
        plan: InspectableRunPlan | None,
        control: RunExecutionControl | None,
        approvals: tuple[Any, ...] = (),
        artifacts: tuple[Any, ...] = (),
        action: Any | None = None,
        receipt: ConnectorActionReceipt | None = None,
        audits: tuple[AuditEvent, ...] = (),
    ) -> None:
        self.runs = runs
        self.run_steps = _RunSteps(plan)
        self.execution_control = _ExecutionControl(control)
        self.approvals = _Approvals(approvals)
        self.artifacts = _Artifacts(artifacts)
        self.external_actions = _Actions(action)
        self.connector_receipts = _ConnectorReceipts(receipt)
        self.audits = _Audits(audits)

    async def __aenter__(self) -> _CompositeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        raise AssertionError("runtime reads must never commit")


class _CompositeFactory:
    def __init__(self, unit: _CompositeUnitOfWork) -> None:
        self.unit = unit

    def __call__(self) -> _CompositeUnitOfWork:
        return self.unit


def _reader() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.api-07.viewer",
        roles=frozenset({"viewer"}),
        scopes=frozenset(),
    )


def test_api_07_external_action_result_metadata_is_centrally_redacted() -> None:
    canary = "api-07-external-action-secret-canary"

    projected = _plain_redacted_mapping(
        {
            "mode": "mock",
            "api_key": canary,
            "nested": {"authorization": canary},
        }
    )

    assert projected == {
        "mode": "mock",
        "api_key": "[REDACTED]",
        "nested": {"authorization": "[REDACTED]"},
    }
    assert canary not in repr(projected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal",
    [
        service_principal(roles=frozenset({"viewer"}), scopes=frozenset()),
        human_principal(roles=frozenset({"auditor"}), scopes=frozenset()),
    ],
)
async def test_api_07_run_authorization_precedes_repository_access(principal: object) -> None:
    units = _ExplodingFactory()
    service = RunResourceService(
        units,  # type: ignore[arg-type]
        catalog_instance_ids=("instance.one",),
        utc_now=lambda: NOW,
    )

    with pytest.raises(RunResourceServiceError) as captured:
        await service.list(RunListQuery(), principal=principal)  # type: ignore[arg-type]

    assert captured.value.code in {"runtime_human_required", "runtime_read_role_missing"}
    assert units.calls == 0


@pytest.mark.asyncio
async def test_api_07_run_cursor_is_filter_bound_and_opaque() -> None:
    runs = _Runs(
        (
            _inspectable("instance.one", "new", NOW),
            _inspectable("instance.two", "old", NOW - timedelta(minutes=1)),
        )
    )
    units = _Factory(_UnitOfWork(runs))
    service = RunResourceService(
        units,  # type: ignore[arg-type]
        catalog_instance_ids=("instance.one", "instance.two"),
        utc_now=lambda: NOW,
    )

    first = await service.list(RunListQuery(limit=1), principal=_reader())

    assert tuple(item.run_id for item in first.items) == ("run.new",)
    assert first.next_cursor is not None
    calls_before_rejection = units.calls
    with pytest.raises(RunResourceServiceError) as captured:
        await service.list(
            RunListQuery(instance_id="instance.one", cursor=first.next_cursor, limit=1),
            principal=_reader(),
        )
    assert captured.value.code == "run_cursor_invalid"
    assert units.calls == calls_before_rejection


@pytest.mark.asyncio
async def test_api_07_status_preserves_injected_catalog_order_and_is_stable() -> None:
    catalog_ids = tuple(f"instance.api-07.{index:02d}" for index in range(43))
    runs = _Runs(
        (
            _inspectable(catalog_ids[17], "seventeen", NOW),
            _inspectable(catalog_ids[0], "zero", NOW - timedelta(minutes=1)),
        )
    )
    service = RunResourceService(
        _Factory(_UnitOfWork(runs)),  # type: ignore[arg-type]
        catalog_instance_ids=catalog_ids,
        utc_now=lambda: NOW,
    )

    first = await service.read_instance_status_summary(principal=_reader())
    second = await service.read_instance_status_summary(principal=_reader())

    assert tuple(item.instance_id for item in first.items) == catalog_ids
    assert len(first.items) == 43
    assert first.items[-1].status == "never_run"
    assert first.etag == second.etag
    assert first.etag.startswith('"instance-status-sha256-v1:')
    assert first.scope == "single-local-installation"


@pytest.mark.asyncio
async def test_api_07_status_etag_covers_latest_run_created_at() -> None:
    original = _inspectable("instance.one", "one", NOW)
    changed = replace(
        original,
        run=replace(original.run, created_at=NOW - timedelta(minutes=1)),
    )
    original_service = RunResourceService(
        _Factory(_UnitOfWork(_Runs((original,)))),  # type: ignore[arg-type]
        catalog_instance_ids=("instance.one",),
        utc_now=lambda: NOW,
    )
    changed_service = RunResourceService(
        _Factory(_UnitOfWork(_Runs((changed,)))),  # type: ignore[arg-type]
        catalog_instance_ids=("instance.one",),
        utc_now=lambda: NOW,
    )

    original_summary = await original_service.read_instance_status_summary(principal=_reader())
    changed_summary = await changed_service.read_instance_status_summary(principal=_reader())

    assert original_summary.items[0].latest_run_created_at == NOW
    assert changed_summary.items[0].latest_run_created_at == NOW - timedelta(minutes=1)
    assert original_summary.items[0].latest_run_updated_at == NOW
    assert changed_summary.items[0].latest_run_updated_at == NOW
    assert original_summary.etag != changed_summary.etag


@pytest.mark.asyncio
async def test_api_07_composite_run_detail_projects_bounded_control_approval_and_artifact() -> None:
    run, plan, control, approval, artifact = _planned_fixture()
    unit = _CompositeUnitOfWork(
        runs=_CompositeRuns([run]),
        plan=plan,
        control=control,
        approvals=(approval,),
        artifacts=(artifact,),
    )
    service = RunResourceService(
        _CompositeFactory(unit),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: NOW,
    )

    resource = await service.read(run.run.id, principal=_reader())

    assert resource.execution_control is not None
    assert resource.execution_control.remaining_model_calls == 8
    assert resource.execution_control.remaining_tool_calls == 17
    assert tuple(value.approval_id for value in resource.pending_approvals) == ("approval.api-07",)
    assert tuple(value.artifact_id for value in resource.artifact_summaries) == ("artifact.api-07",)
    assert resource.artifacts_truncated is False
    assert unit.runs.calls == 2


@pytest.mark.asyncio
async def test_api_07_composite_run_detail_fails_closed_on_coherence_drift() -> None:
    run, plan, control, approval, artifact = _planned_fixture()
    changed = InspectableRun(
        run=replace(run.run, updated_at=NOW + timedelta(seconds=1)),
        work_item=run.work_item,
        transitions=run.transitions,
    )
    service = RunResourceService(
        _CompositeFactory(
            _CompositeUnitOfWork(
                runs=_CompositeRuns([run, changed]),
                plan=plan,
                control=control,
                approvals=(approval,),
                artifacts=(artifact,),
            )
        ),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: NOW,
    )

    with pytest.raises(RunResourceServiceError) as captured:
        await service.read(run.run.id, principal=_reader())

    assert captured.value.code == "runtime_record_corrupt"


@pytest.mark.asyncio
async def test_api_07_external_action_projection_re_redacts_persisted_payload() -> None:
    run, base_plan, control, _approval, _artifact = _planned_fixture()
    plan = _write_action_plan(base_plan)
    action, receipt = _succeeded_action_fixture(plan)
    canary = "api-07-persisted-action-proposal-secret-canary"
    drifted_projection = dict(action.proposal.redacted_projection)
    drifted_projection["payload"] = {"mode": "mock", "api_key": canary}
    action = replace(
        action,
        proposal=replace(action.proposal, redacted_projection=drifted_projection),
    )
    service = RunResourceService(
        _CompositeFactory(
            _CompositeUnitOfWork(
                runs=_CompositeRuns([run]),
                plan=plan,
                control=control,
                action=action,
                receipt=receipt,
            )
        ),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: NOW,
    )

    resource = await service.read_external_action(action.id, principal=_reader())

    assert plan.steps[0].timeout_seconds == 60
    assert action.delivery_contract.timeout_seconds == 30
    assert resource.redacted_payload == {"mode": "mock", "api_key": "[REDACTED]"}
    assert canary not in repr(resource)


@pytest.mark.asyncio
async def test_api_07_action_and_run_detail_require_exact_succeeded_receipt() -> None:
    run, base_plan, control, _approval, _artifact = _planned_fixture()
    plan = _write_action_plan(base_plan)
    action, receipt = _succeeded_action_fixture(plan)

    metadata_drift = replace(
        action,
        result=replace(action.result, safe_metadata={"mode": "drifted"}),
    )
    metadata_service = RunResourceService(
        _CompositeFactory(
            _CompositeUnitOfWork(
                runs=_CompositeRuns([run]),
                plan=plan,
                control=control,
                action=metadata_drift,
                receipt=receipt,
            )
        ),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: NOW,
    )
    with pytest.raises(RunResourceServiceError) as action_detail:
        await metadata_service.read_external_action(action.id, principal=_reader())
    assert action_detail.value.code == "runtime_record_corrupt"

    status_drift = replace(
        action,
        result=replace(action.result, status="drifted"),
    )
    status_service = RunResourceService(
        _CompositeFactory(
            _CompositeUnitOfWork(
                runs=_CompositeRuns([run]),
                plan=plan,
                control=control,
                action=status_drift,
                receipt=receipt,
            )
        ),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: NOW,
    )
    with pytest.raises(RunResourceServiceError) as run_detail:
        await status_service.read(run.run.id, principal=_reader())
    assert run_detail.value.code == "runtime_record_corrupt"

    timestamp_drift = replace(action, updated_at=NOW + timedelta(seconds=1))
    timestamp_service = RunResourceService(
        _CompositeFactory(
            _CompositeUnitOfWork(
                runs=_CompositeRuns([run]),
                plan=plan,
                control=control,
                action=timestamp_drift,
                receipt=receipt,
            )
        ),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: NOW,
    )
    with pytest.raises(RunResourceServiceError) as timestamp_detail:
        await timestamp_service.read_external_action(action.id, principal=_reader())
    assert timestamp_detail.value.code == "runtime_record_corrupt"


@pytest.mark.asyncio
async def test_api_07_action_and_run_detail_revalidate_sealed_write_step() -> None:
    run, base_plan, control, _approval, _artifact = _planned_fixture()
    plan = _write_action_plan(base_plan)
    action, receipt = _succeeded_action_fixture(plan)
    drifted_plan = replace(
        plan,
        steps=(replace(plan.steps[0], request_schema_id="schema.api-07.drifted"),),
    )

    def service() -> RunResourceService:
        return RunResourceService(
            _CompositeFactory(
                _CompositeUnitOfWork(
                    runs=_CompositeRuns([run]),
                    plan=drifted_plan,
                    control=control,
                    action=action,
                    receipt=receipt,
                )
            ),  # type: ignore[arg-type]
            catalog_instance_ids=(run.work_item.instance_id,),
            utc_now=lambda: NOW,
        )

    with pytest.raises(RunResourceServiceError) as action_detail:
        await service().read_external_action(action.id, principal=_reader())
    assert action_detail.value.code == "runtime_record_corrupt"

    with pytest.raises(RunResourceServiceError) as run_detail:
        await service().read(run.run.id, principal=_reader())
    assert run_detail.value.code == "runtime_record_corrupt"


@pytest.mark.asyncio
async def test_api_07_timeline_projects_pseudonyms_expiry_and_only_live_links() -> None:
    run = _inspectable("instance.one", "timeline", NOW)
    draft = AuditEventFactory(
        AuditContext.system(
            "service.api-07.timeline",
            correlation_id="correlation.api-07.timeline",
        )
    ).run_transition(run.run, run.transitions[0])
    event = AuditEvent(draft, global_sequence=99, run_sequence=1, feed_sequence=1)
    unit = _CompositeUnitOfWork(
        runs=_CompositeRuns([run]),
        plan=None,
        control=None,
        audits=(event,),
    )
    service = RunResourceService(
        _CompositeFactory(unit),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: event.safe_metadata.expires_at,
    )

    item = (
        await service.read_timeline(
            run.run.id,
            RunTimelineQuery(),
            principal=_reader(),
        )
    ).items[0]

    assert item.actor_id.startswith("audit-actor-v1:")
    assert item.correlation_id.startswith("audit-correlation-v1:")
    assert item.metadata_expired is True
    assert item.metadata == {}
    assert item.run_url == f"/api/v1/runs/{run.run.id}"
    assert not hasattr(item, "audit_url")


@pytest.mark.asyncio
async def test_api_07_unknown_run_timeline_returns_not_found_before_audit_lookup() -> None:
    unit = _CompositeUnitOfWork(
        runs=_CompositeRuns([]),
        plan=None,
        control=None,
    )
    service = RunResourceService(
        _CompositeFactory(unit),  # type: ignore[arg-type]
        catalog_instance_ids=("instance.one",),
        utc_now=lambda: NOW,
    )

    with pytest.raises(RunResourceServiceError) as captured:
        await service.read_timeline(
            "run.missing",
            RunTimelineQuery(),
            principal=_reader(),
        )

    assert captured.value.code == "run_not_found"
    assert unit.audits.calls == 0


@pytest.mark.asyncio
async def test_api_07_step_uses_inspectable_plan_and_action_mismatch_fails_closed() -> None:
    run, plan, control, _approval, _artifact = _planned_fixture()
    unit = _CompositeUnitOfWork(
        runs=_CompositeRuns([run]),
        plan=plan,
        control=control,
    )
    service = RunResourceService(
        _CompositeFactory(unit),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: NOW,
    )

    step = await service.read_step(run.run.id, plan.steps[0].id, principal=_reader())

    assert step.step_id == plan.steps[0].id

    envelope = SimpleNamespace(
        action_id="action.api-07.mismatch",
        run_id=run.run.id,
        plan_hash=plan.plan.plan_hash,
        step_id=plan.steps[0].id,
        step_key="wrong-step-key",
        template_id=plan.steps[0].template_id,
        instance_id=plan.steps[0].selected_instance_id,
        capability_id=plan.steps[0].capability_id,
        connector_family=plan.steps[0].connector_family,
        binding_id=plan.steps[0].binding_id,
    )
    action = SimpleNamespace(proposal=SimpleNamespace(envelope=envelope))
    action_service = RunResourceService(
        _CompositeFactory(
            _CompositeUnitOfWork(
                runs=_CompositeRuns([run]),
                plan=plan,
                control=control,
                action=action,
            )
        ),  # type: ignore[arg-type]
        catalog_instance_ids=(run.work_item.instance_id,),
        utc_now=lambda: NOW,
    )

    with pytest.raises(RunResourceServiceError) as captured:
        await action_service.read_external_action(
            envelope.action_id,
            principal=_reader(),
        )

    assert captured.value.code == "runtime_record_corrupt"
