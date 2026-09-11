"""DEL-07: complete approval sets reject partial/corrupt repository snapshots."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.repositories import ApprovalRepositoryConflict
from marketing_agents.application.services.approval_boundaries import (
    ApprovalBoundaryDisposition,
    ApprovalBoundaryResult,
    ApprovalBoundaryService,
    ApprovalBoundaryServiceError,
)
from marketing_agents.domain.approval import AuthorizationSetStatus, StoredActionApprovalRequest
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run
from marketing_agents.domain.enums import Effect, RunState
from marketing_agents.domain.runtime_policy import AttemptKind

from tests.unit.application.test_del_07_approval_decision_faults import _decision_fixture
from tests.unit.application.test_orch_06_controlled_read_executor_boundaries import _step
from tests.unit.domain.test_run_08_approval_records import NOW


def _boundary_fixture() -> SimpleNamespace:
    fixture = _decision_fixture()
    request = fixture.request
    step = replace(
        _step(effect=Effect.WRITE, kind=AttemptKind.TOOL),
        id=request.step_id,
        run_id=request.run_id,
        key=request.step_key,
        plan_hash=request.plan_hash,
        created_at=NOW,
        updated_at=NOW,
    )
    run = Run(
        id=request.run_id,
        work_item_id="work.del07",
        state=RunState.AWAITING_APPROVAL,
        catalog_hash="a" * 64,
        configuration_revision=1,
        created_at=NOW,
        updated_at=NOW,
        version=4,
        approval_required=True,
    )
    fixture.uow.runs.get.return_value = run
    fixture.uow.run_steps = SimpleNamespace(
        validate_plan_for_execution=AsyncMock(return_value=(step,)),
    )
    fixture.uow.approvals.list_set_history = AsyncMock(return_value=(fixture.stored,))
    fixture.run = run
    fixture.step = step
    fixture.context = AuditContext.worker("worker.del07", correlation_id="correlation.del07")
    fixture.service = ApprovalBoundaryService(
        OrchestrationDependencies(fixture.clock, fixture.ids, fixture.factory)
    )
    return fixture


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    (
        ("set_corrupt", "test_integrity_failure"),
        ("set_missing", "authorization_set_missing"),
        ("run_missing", "run_not_found"),
        ("leaves_unavailable", "authorization_set_corrupt"),
        ("history_unavailable", "authorization_set_corrupt"),
        ("plan_unavailable", "authorization_set_corrupt"),
        ("missing_leaf", "authorization_set_member_mismatch"),
        ("missing_action", "authorization_set_member_mismatch"),
        ("missing_step", "authorization_set_member_mismatch"),
        ("request_set", "authorization_set_member_mismatch"),
        ("request_hash", "authorization_set_member_mismatch"),
        ("request_step", "authorization_set_member_mismatch"),
        ("request_step_key", "authorization_set_member_mismatch"),
        ("action_hash", "authorization_set_member_mismatch"),
        ("action_run", "authorization_set_member_mismatch"),
        ("action_step", "authorization_set_member_mismatch"),
        ("step_run", "authorization_set_member_mismatch"),
        ("step_key", "authorization_set_member_mismatch"),
        ("step_plan", "authorization_set_member_mismatch"),
        ("extra_leaf", "authorization_set_member_mismatch"),
        ("not_write_plan", "authorization_set_plan_mismatch"),
        ("extra_write_step", "authorization_set_plan_mismatch"),
    ),
)
@pytest.mark.asyncio
async def test_del_07_boundary_loading_requires_complete_exact_set_members(
    fault: str, expected_code: str
) -> None:
    fixture = _boundary_fixture()
    uow = fixture.uow
    if fault == "set_corrupt":
        uow.approvals.get_current_authorization_set.side_effect = ApprovalRepositoryConflict(
            "test_integrity_failure", "private persistence diagnostic"
        )
    elif fault == "set_missing":
        uow.approvals.get_current_authorization_set.return_value = None
    elif fault == "run_missing":
        uow.runs.get.return_value = None
    elif fault.endswith("_unavailable"):
        port = {
            "leaves_unavailable": uow.approvals.list_current_set,
            "history_unavailable": uow.approvals.list_set_history,
            "plan_unavailable": uow.run_steps.validate_plan_for_execution,
        }[fault]
        port.side_effect = RuntimeError("private persistence diagnostic")
    elif fault == "missing_leaf":
        uow.approvals.list_current_set.return_value = ()
    elif fault == "missing_action":
        uow.external_actions.get.return_value = None
    elif fault == "missing_step":
        uow.run_steps.validate_plan_for_execution.return_value = ()
    elif fault.startswith("request_"):
        field, value = {
            "request_set": ("authorization_set_id", "set.other"),
            "request_hash": ("action_hash", "a" * 64),
            "request_step": ("step_id", "step.other"),
            "request_step_key": ("step_key", "other"),
        }[fault]
        object.__setattr__(fixture.request, field, value)
    elif fault == "action_hash":
        object.__setattr__(fixture.action.proposal, "action_hash", "a" * 64)
    elif fault.startswith("action_"):
        object.__setattr__(
            fixture.action.envelope, "run_id" if fault == "action_run" else "step_id", "other"
        )
    elif fault.startswith("step_"):
        field, value = {
            "step_run": ("run_id", "run.other"),
            "step_key": ("key", "other"),
            "step_plan": ("plan_hash", "a" * 64),
        }[fault]
        object.__setattr__(fixture.step, field, value)
    elif fault == "extra_leaf":
        extra = StoredActionApprovalRequest.created(
            replace(fixture.request, id="request.extra", action_id="action.extra")
        )
        uow.approvals.list_current_set.return_value = (fixture.stored, extra)
    elif fault == "not_write_plan":
        object.__setattr__(fixture.run, "approval_required", False)
    else:
        uow.run_steps.validate_plan_for_execution.return_value = (
            fixture.step,
            replace(fixture.step, id="step.extra", key="extra", ordinal=2, source_order=2),
        )
    with pytest.raises(ApprovalBoundaryServiceError) as failure:
        await fixture.service.evaluate_in_uow(uow, fixture.run.id, audit_context=fixture.context)
    assert failure.value.code == expected_code
    assert "private" not in str(failure.value)
    uow.commit.assert_not_awaited()
    uow.approvals.record_decision.assert_not_awaited()
    uow.audits.append_many.assert_not_awaited()


@pytest.mark.parametrize("fault", ("superseded_set", "wrong_run_state"))
@pytest.mark.asyncio
async def test_del_07_boundary_evaluation_requires_an_open_awaiting_set(fault: str) -> None:
    fixture = _boundary_fixture()
    if fault == "superseded_set":
        object.__setattr__(
            fixture.selection.authorization_set, "status", AuthorizationSetStatus.SUPERSEDED
        )
    else:
        fixture.uow.runs.get.return_value = replace(fixture.run, state=RunState.EXECUTING)
    with pytest.raises(ApprovalBoundaryServiceError) as failure:
        await fixture.service.evaluate_in_uow(
            fixture.uow, fixture.run.id, audit_context=fixture.context
        )
    assert failure.value.code == (
        "authorization_set_not_actionable"
        if fault == "superseded_set"
        else "run_not_awaiting_approval"
    )
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.parametrize("conflict", ("stale_execution_control", "other_conflict"))
@pytest.mark.asyncio
async def test_del_07_cancellation_retries_only_stale_control_and_stops_at_three_attempts(
    conflict: str,
) -> None:
    fixture = _boundary_fixture()
    error = ApprovalBoundaryServiceError(conflict, "safe denial", run_id=fixture.run.id)
    fixture.service._cancel_once = AsyncMock(side_effect=error)
    with pytest.raises(ApprovalBoundaryServiceError) as failure:
        await fixture.service.cancel(fixture.run.id, audit_context=fixture.context)
    assert failure.value is error
    assert fixture.service._cancel_once.await_count == (
        3 if conflict == "stale_execution_control" else 1
    )
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_cancellation_retry_returns_the_first_authoritative_success() -> None:
    fixture = _boundary_fixture()
    error = ApprovalBoundaryServiceError(
        "stale_execution_control", "safe denial", run_id=fixture.run.id
    )
    result = ApprovalBoundaryResult(
        ApprovalBoundaryDisposition.CANCELLED, fixture.run, fixture.selection.authorization_set.id
    )
    fixture.service._cancel_once = AsyncMock(side_effect=(error, result))
    assert await fixture.service.cancel(fixture.run.id, audit_context=fixture.context) is result
    assert fixture.service._cancel_once.await_count == 2
