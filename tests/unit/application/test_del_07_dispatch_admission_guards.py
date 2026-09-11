"""DEL-07: durable dispatch admission rejects faults before any provider call."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from marketing_agents.application.ports.external_writes import ConnectorDeliveryFailure
from marketing_agents.application.ports.repositories import (
    ExecutionControlRepositoryConflict,
    ReleaseAuthority,
    ReleaseCallMode,
)
from marketing_agents.application.services.external_action_dispatcher import (
    ExternalActionDispatchError,
)
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState, WorkMode

from tests.unit.application.test_del_07_boundary_snapshot_guards import _boundary_fixture
from tests.unit.application.test_del_07_dispatch_defensive_contracts import _dispatch_fixture


def _admission_fixture() -> SimpleNamespace:
    fixture = _dispatch_fixture(started=False)
    boundary = _boundary_fixture()
    action = fixture.action
    reservation = action.reservation
    assert reservation is not None
    fixture.reserved = replace(
        action,
        state=ExternalActionState.DISPATCH_RESERVED,
        lease=None,
        delivery_attempt_count=0,
        version=3,
    )
    fixture.run = replace(boundary.run, state=RunState.EXECUTING, version=5)
    fixture.step = boundary.step
    fixture.authority = ReleaseAuthority(
        action.envelope.authorization_set_id,
        fixture.selection.authorization_set.membership_hash,
        "c" * 64,
        2,
        1,
        action.run_id,
        fixture.run.version,
        action.id,
        action.action_hash,
        action.step_id,
        action.envelope.step_key,
        fixture.step.version,
        fixture.step.state,
        fixture.step.version,
        ReleaseCallMode.FIRST_CALL,
        None,
        reservation.approval_request_id,
        reservation.approval_decision_id,
        "approval-use.del07",
        reservation.reservation_id,
    )
    fixture.uow.external_actions.get.return_value = fixture.reserved
    fixture.uow.external_actions.claim_reserved = AsyncMock(return_value=action)
    fixture.uow.external_actions.mark_call_started = AsyncMock(return_value=None)
    fixture.uow.runs.get.return_value = fixture.run
    fixture.uow.works = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(id=fixture.run.work_item_id, mode=WorkMode.MOCK_EXECUTION)
        )
    )
    fixture.uow.approvals.get_release_authority = AsyncMock(return_value=fixture.authority)
    fixture.uow.run_steps = SimpleNamespace(
        validate_plan_for_execution=AsyncMock(return_value=(fixture.step,)),
        get=AsyncMock(return_value=fixture.step),
    )
    fixture.uow.execution_control = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(policy_hash=action.envelope.plan_hash, version=1)
        ),
        reserve_delivery_call=AsyncMock(
            return_value=SimpleNamespace(
                permit=SimpleNamespace(call_deadline_at=fixture.clock.now() + timedelta(seconds=10))
            )
        ),
    )
    return fixture


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    (
        ("action_missing", "action_not_found"),
        ("run_missing", "execution_run_missing"),
        ("work_missing", "execution_policy_source_corrupt"),
        ("work_identity", "execution_policy_source_corrupt"),
        ("dry_run", "dry_run_external_effect_forbidden"),
        ("not_reserved", "action_not_dispatchable"),
        ("no_reservation", "reservation_missing"),
        ("reservation_hash", "approval_hash_mismatch"),
        ("contract_drift", "delivery_contract_drift"),
        ("contract_private_code", "delivery_contract_invalid"),
        ("contract_exception", "delivery_contract_invalid"),
        ("authority_corrupt", "execution_plan_invalid"),
        ("authority_missing", "release_authority_missing"),
        ("authority_identity", "release_authority_mismatch"),
        ("run_not_executing", "run_not_executing"),
        ("plan_corrupt", "execution_plan_invalid"),
        ("step_missing", "execution_plan_mismatch"),
        ("step_version", "execution_plan_mismatch"),
        ("input_bytes", "input_payload_too_large"),
        ("input_field", "input_field_too_large"),
        ("claim_cas", "claim_cas_lost"),
        ("control_missing", "execution_control_invalid"),
        ("control_plan", "execution_control_invalid"),
        ("control_denied", "rate_limit_exhausted"),
        ("call_start_cas", "call_start_cas_lost"),
    ),
)
@pytest.mark.asyncio
async def test_del_07_admission_failure_never_commits_a_call_start_or_invokes_provider(
    fault: str, expected_code: str
) -> None:
    fixture = _admission_fixture()
    uow = fixture.uow
    if fault == "action_missing":
        uow.external_actions.get.return_value = None
    elif fault == "run_missing":
        uow.runs.get.return_value = None
    elif fault == "work_missing":
        uow.works.get.return_value = None
    elif fault == "work_identity":
        uow.works.get.return_value.id = "work.other"
    elif fault == "dry_run":
        uow.works.get.return_value.mode = WorkMode.DRY_RUN
    elif fault == "not_reserved":
        uow.external_actions.get.return_value = fixture.action
    elif fault == "no_reservation":
        object.__setattr__(fixture.reserved, "reservation", None)
    elif fault == "reservation_hash":
        object.__setattr__(fixture.reserved.reservation, "action_hash", "a" * 64)
    elif fault.startswith("contract_"):
        fixture.gateway.contract_for.side_effect = (
            RuntimeError("private provider diagnostic")
            if fault == "contract_exception"
            else ConnectorDeliveryFailure(
                "delivery_contract_drift" if fault == "contract_drift" else "private-provider-code",
                "private provider diagnostic",
                request_may_have_left_process=False,
            )
        )
    elif fault == "authority_corrupt":
        uow.approvals.get_release_authority.side_effect = RuntimeError("private persistence detail")
    elif fault == "authority_missing":
        uow.approvals.get_release_authority.return_value = None
    elif fault == "authority_identity":
        uow.approvals.get_release_authority.return_value = replace(
            fixture.authority, action_id="action.other"
        )
    elif fault == "run_not_executing":
        uow.runs.get.return_value = replace(fixture.run, state=RunState.AWAITING_APPROVAL)
    elif fault == "plan_corrupt":
        uow.run_steps.validate_plan_for_execution.side_effect = RuntimeError("private plan detail")
    elif fault == "step_missing":
        uow.run_steps.validate_plan_for_execution.return_value = ()
    elif fault == "step_version":
        uow.run_steps.validate_plan_for_execution.return_value = (
            replace(fixture.step, version=fixture.step.version + 1),
        )
    elif fault in {"input_bytes", "input_field"}:
        limits = {"max_input_field_bytes": 1}
        if fault == "input_bytes":
            limits["max_input_bytes"] = 1
        changed = replace(
            fixture.step,
            runtime_policy=replace(
                fixture.step.runtime_policy,
                budget=replace(fixture.step.runtime_policy.budget, **limits),
            ),
        )
        uow.run_steps.validate_plan_for_execution.return_value = (changed,)
    elif fault == "claim_cas":
        uow.external_actions.claim_reserved.return_value = None
    elif fault == "control_missing":
        uow.execution_control.get.return_value = None
    elif fault == "control_plan":
        uow.execution_control.get.return_value.policy_hash = "a" * 64
    elif fault == "control_denied":
        uow.execution_control.reserve_delivery_call.side_effect = (
            ExecutionControlRepositoryConflict(
                "rate_limit_exhausted", "private rate detail", retry_after_seconds=27
            )
        )
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher._claim_and_mark_call_started(fixture.action.id, "worker.del07")
    assert failure.value.code == expected_code
    assert "private" not in str(failure.value)
    if fault == "control_denied":
        assert failure.value.retry_after_seconds == 27
    fixture.gateway.execute.assert_not_awaited()
    uow.commit.assert_not_awaited()
    uow.audits.append_many.assert_not_awaited()


@pytest.mark.parametrize("fault", ("retry_state", "unknown_mode", "first_state", "missing_step"))
@pytest.mark.asyncio
async def test_del_07_call_start_requires_exact_first_or_retry_step_lineage(fault: str) -> None:
    fixture = _admission_fixture()
    authority = fixture.authority
    if fault == "retry_state":
        object.__setattr__(authority, "call_mode", ReleaseCallMode.PROVIDER_RETRY)
    elif fault == "unknown_mode":
        object.__setattr__(authority, "call_mode", "future-unsupported-mode")
    elif fault == "first_state":
        object.__setattr__(authority, "step_state", StepState.EXECUTING)
    else:
        fixture.uow.run_steps.get.return_value = None
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher._call_start_step_transition(
            fixture.uow, authority, started_at=fixture.clock.now()
        )
    assert failure.value.code == "release_authority_mismatch"
    fixture.gateway.execute.assert_not_awaited()
    fixture.uow.commit.assert_not_awaited()
