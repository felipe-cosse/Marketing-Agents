"""DEL-07: completion/recovery respects exact receipts, CAS losses, and call fences."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from marketing_agents.application.policies.action_recovery import StaleActionRecoveryDecision
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.application.services import external_action_dispatcher as dispatch_module
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.application.services.external_action_dispatcher import (
    DispatchDisposition,
    ExternalActionDispatchError,
    ExternalActionDispatchResult,
    _DispatchCallStart,
)
from marketing_agents.application.services.terminal_execution_cleanup import (
    TerminalExecutionCleanupService,
)
from marketing_agents.domain.audit import AuditEvent
from marketing_agents.domain.entities import ConnectorActionReceipt, ExternalActionResultSnapshot
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState

from tests.unit.application.test_del_07_dispatch_admission_guards import _admission_fixture
from tests.unit.application.test_del_07_dispatch_defensive_contracts import _dispatch_fixture
from tests.unit.domain.test_run_08_approval_records import NOW


def _completion_fixture() -> SimpleNamespace:
    fixture = _admission_fixture()
    fixture.action = replace(
        fixture.action, call_started_at=NOW, call_deadline_at=NOW + timedelta(seconds=30)
    )
    fixture.step = replace(fixture.step, state=StepState.EXECUTING, version=3)
    fixture.uow.external_actions.get.return_value = fixture.action
    fixture.uow.run_steps.validate_plan_for_execution.return_value = (fixture.step,)
    fixture.uow.run_steps.get.return_value = fixture.step
    fixture.uow.run_steps.apply_transition = AsyncMock(return_value=True)
    fixture.uow.execution_control.get.return_value.cancel_requested_at = None
    fixture.receipt = ConnectorActionReceipt(
        fixture.action.id,
        fixture.action.connector_binding_id,
        fixture.action.idempotency_key,
        fixture.action.action_hash,
        fixture.action.envelope.capability_id,
        "receipt.del07",
        "mock_succeeded",
        {"mock": True},
        NOW + timedelta(seconds=1),
    )
    fixture.uow.connector_receipts = SimpleNamespace(get=AsyncMock(return_value=fixture.receipt))
    fixture.result = ExternalActionResultSnapshot(
        fixture.receipt.receipt_id,
        fixture.receipt.status,
        fixture.receipt.safe_metadata,
        fixture.clock.now(),
    )
    fixture.completed = replace(
        fixture.action,
        state=ExternalActionState.SUCCEEDED,
        lease=None,
        call_started_at=None,
        call_deadline_at=None,
        result=fixture.result,
        version=5,
        updated_at=fixture.result.completed_at,
    )
    fixture.uow.external_actions.complete_succeeded = AsyncMock(return_value=fixture.completed)
    return fixture


@pytest.mark.parametrize("fault", ("plan_error", "missing_step", "wrong_run", "wrong_state"))
@pytest.mark.asyncio
async def test_del_07_completion_loads_only_the_exact_sealed_write_context(fault: str) -> None:
    fixture = _completion_fixture()
    if fault == "plan_error":
        fixture.uow.run_steps.validate_plan_for_execution.side_effect = RuntimeError("private plan")
    elif fault == "missing_step":
        fixture.uow.run_steps.validate_plan_for_execution.return_value = ()
    elif fault == "wrong_run":
        fixture.uow.runs.get.return_value = None
    else:
        fixture.uow.run_steps.validate_plan_for_execution.return_value = (
            replace(fixture.step, state=StepState.READY),
        )
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher._write_completion_context(fixture.uow, fixture.action)
    assert (
        failure.value.code
        == {
            "plan_error": "execution_plan_invalid",
            "missing_step": "execution_plan_mismatch",
            "wrong_run": "write_completion_context_invalid",
            "wrong_state": "write_completion_context_invalid",
        }[fault]
    )
    fixture.uow.commit.assert_not_awaited()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.parametrize("fault", ("action_cas", "time", "step_cas", "cleanup_step"))
@pytest.mark.asyncio
async def test_del_07_completion_cas_or_time_conflict_never_emits_success_audit(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _completion_fixture()
    if fault == "action_cas":
        fixture.uow.external_actions.complete_succeeded.return_value = None
    elif fault == "time":
        fixture.uow.external_actions.complete_succeeded.return_value = replace(
            fixture.completed, updated_at=fixture.result.completed_at + timedelta(seconds=1)
        )
    elif fault == "step_cas":
        fixture.uow.run_steps.apply_transition.return_value = False
    else:
        failed = replace(
            fixture.completed,
            state=ExternalActionState.FAILED,
            result=None,
            terminal_reason_code="connector_request_rejected",
        )
        fixture.uow.external_actions.complete_failed = AsyncMock(return_value=failed)
        monkeypatch.setattr(
            TerminalExecutionCleanupService,
            "fail_execution_in_uow",
            AsyncMock(return_value=SimpleNamespace(denied_step=fixture.step, audit_events=())),
        )
    arguments = {
        "previous": fixture.action,
        "run": fixture.run,
        "step": fixture.step,
        "lease_owner": "worker.del07",
        "occurred_at": fixture.clock.now(),
        "result": fixture.result if fault != "cleanup_step" else None,
        "reason_code": "connector_request_rejected" if fault == "cleanup_step" else None,
    }
    if fault == "action_cas":
        assert (
            await fixture.dispatcher._finalize_write_outcome_in_uow(fixture.uow, **arguments)
            is None
        )
    else:
        with pytest.raises(ExternalActionDispatchError) as failure:
            await fixture.dispatcher._finalize_write_outcome_in_uow(fixture.uow, **arguments)
        assert failure.value.code == (
            "write_completion_time_invalid" if fault == "time" else "write_completion_step_conflict"
        )
    fixture.uow.audits.append_many.assert_not_awaited()
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.parametrize("fault", ("changed", "receipt", "completion_cas"))
@pytest.mark.asyncio
async def test_del_07_dispatch_response_cannot_override_a_changed_action_or_missing_receipt(
    fault: str,
) -> None:
    fixture = _completion_fixture()
    fixture.clock.now.return_value = NOW + timedelta(seconds=1)
    start = _DispatchCallStart(
        fixture.action,
        fixture.dispatcher._authorize(fixture.action),
        SimpleNamespace(call_deadline_at=NOW + timedelta(seconds=30)),
        1024,
    )
    fixture.dispatcher._claim_and_mark_call_started = AsyncMock(return_value=start)
    fixture.gateway.execute.return_value = ConnectorWriteResult(
        receipt_id=fixture.receipt.receipt_id, status=fixture.receipt.status, safe_metadata={}
    )
    if fault == "changed":
        fixture.uow.external_actions.get.return_value = fixture.completed
        result = await fixture.dispatcher.dispatch_once(
            fixture.action.id, lease_owner="worker.del07"
        )
        assert result.disposition is DispatchDisposition.ALREADY_SUCCEEDED
    else:
        if fault == "receipt":
            fixture.uow.connector_receipts.get.return_value = None
        else:
            fixture.dispatcher._finalize_write_outcome_in_uow = AsyncMock(return_value=None)
        with pytest.raises(ExternalActionDispatchError) as failure:
            await fixture.dispatcher.dispatch_once(fixture.action.id, lease_owner="worker.del07")
        assert failure.value.code == (
            "receipt_not_authoritative" if fault == "receipt" else "completion_cas_lost"
        )
    fixture.gateway.execute.assert_awaited_once()
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.parametrize("fault", ("changed", "completion_cas"))
@pytest.mark.asyncio
async def test_del_07_failure_finalization_respects_concurrent_action_change(fault: str) -> None:
    fixture = _completion_fixture()
    if fault == "changed":
        fixture.uow.external_actions.get.return_value = fixture.completed
        result = await fixture.dispatcher._complete_failure(
            fixture.action,
            lease_owner="worker.del07",
            reason_code="connector_request_rejected",
            outcome_unknown=False,
        )
        assert result.disposition is DispatchDisposition.ALREADY_SUCCEEDED
    else:
        fixture.dispatcher._finalize_write_outcome_in_uow = AsyncMock(return_value=None)
        with pytest.raises(ExternalActionDispatchError) as failure:
            await fixture.dispatcher._complete_failure(
                fixture.action,
                lease_owner="worker.del07",
                reason_code="connector_request_rejected",
                outcome_unknown=False,
            )
        assert failure.value.code == "completion_cas_lost"
    fixture.uow.commit.assert_not_awaited()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.parametrize("state", ("not_dispatching", "pending", "pre_call", "recovered", "lost"))
@pytest.mark.asyncio
async def test_del_07_single_action_recovery_honors_call_fence_and_authoritative_result(
    state: str,
) -> None:
    fixture = _completion_fixture()
    recovered = ExternalActionDispatchResult(fixture.completed, DispatchDisposition.SUCCEEDED)
    fixture.dispatcher._recover_snapshot = AsyncMock(
        return_value=recovered if state == "recovered" else None
    )
    if state == "not_dispatching":
        fixture.uow.external_actions.get.return_value = fixture.reserved
    elif state in {"pending", "pre_call"}:
        fixture.clock.now.return_value = NOW + timedelta(seconds=1)
        if state == "pre_call":
            fixture.uow.external_actions.get.return_value = replace(
                fixture.action, call_started_at=None, call_deadline_at=None
            )
    result = await fixture.dispatcher.recover_action(
        fixture.action.id, lease_owner="worker.recovery"
    )
    assert (
        result.disposition
        is {
            "not_dispatching": DispatchDisposition.LOST_CLAIM,
            "pending": DispatchDisposition.RECOVERY_PENDING,
            "pre_call": DispatchDisposition.RECOVERY_PENDING,
            "recovered": DispatchDisposition.SUCCEEDED,
            "lost": DispatchDisposition.LOST_CLAIM,
        }[state]
    )
    if state in {"pending", "pre_call", "not_dispatching"}:
        fixture.dispatcher._recover_snapshot.assert_not_awaited()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "fault", ("not_expired", "changed", "pre_call_cas", "unknown_cas", "retry_cas", "retry_lost")
)
@pytest.mark.asyncio
async def test_del_07_stale_snapshot_recovery_keeps_cas_losses_and_live_leases_non_mutating(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _completion_fixture()
    snapshot = (
        fixture.action
        if fault == "unknown_cas"
        else replace(fixture.action, call_started_at=None, call_deadline_at=None)
    )
    fixture.uow.external_actions.get.return_value = snapshot
    fixture.dispatcher._finalize_terminal_parent_call = AsyncMock(return_value=None)
    fixture.dispatcher._reconcile_durable_receipt = AsyncMock(return_value=None)
    fixture.dispatcher._finalize_write_outcome_in_uow = AsyncMock(return_value=None)
    if fault == "not_expired":
        fixture.clock.now.return_value = NOW + timedelta(seconds=1)
    elif fault in {"changed", "pre_call_cas", "unknown_cas"}:
        decision = (
            StaleActionRecoveryDecision.OUTCOME_UNKNOWN
            if fault == "unknown_cas"
            else StaleActionRecoveryDecision.FAIL_PRE_CALL_EXHAUSTED
        )
        monkeypatch.setattr(
            dispatch_module, "classify_stale_action_recovery", lambda *a, **kw: decision
        )
        if fault == "changed":
            fixture.uow.external_actions.get.return_value = fixture.completed
    else:
        fixture.dispatcher._release_stale = AsyncMock(
            return_value=None if fault == "retry_cas" else fixture.reserved
        )
        fixture.dispatcher.dispatch_once = AsyncMock(
            side_effect=ExternalActionDispatchError("claim_cas_lost", "safe CAS denial")
        )
    result = await fixture.dispatcher._recover_snapshot(
        snapshot, lease_owner="worker.recovery", now=fixture.clock.now()
    )
    if fault == "changed":
        assert result is not None and result.disposition is DispatchDisposition.ALREADY_SUCCEEDED
    elif fault == "retry_lost":
        assert result is not None and result.disposition is DispatchDisposition.LOST_CLAIM
    else:
        assert result is None
    fixture.uow.commit.assert_not_awaited()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_stale_release_cas_loss_does_not_commit_and_missing_action_is_explicit() -> (
    None
):
    fixture = _dispatch_fixture(started=False)
    fixture.uow.external_actions.release_stale_for_retry = AsyncMock(return_value=None)
    assert (
        await fixture.dispatcher._release_stale(
            fixture.action, fixture.clock.now(), "pre_call_expired"
        )
        is None
    )
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.external_actions.get.return_value = None
    for operation in (
        fixture.dispatcher._load_required(fixture.action.id),
        fixture.dispatcher._changed_dispatch_result(fixture.uow, fixture.action),
    ):
        with pytest.raises(ExternalActionDispatchError) as failure:
            await operation
        assert failure.value.code == "action_not_found"


@pytest.mark.parametrize(
    ("fault", "expected"),
    (
        ("active_call", None),
        ("action_missing", "action_not_found"),
        ("changed", DispatchDisposition.ALREADY_SUCCEEDED),
        ("control_missing", "execution_control_invalid"),
        ("step_not_executing", "terminal_call_step_invalid"),
        ("not_terminal", None),
        ("receipt_identity", "receipt_identity_corrupt"),
        ("receipt_cas", DispatchDisposition.LOST_CLAIM),
        ("no_receipt_cas", DispatchDisposition.LOST_CLAIM),
    ),
)
@pytest.mark.asyncio
async def test_del_07_terminal_parent_reconciliation_uses_exact_current_context(
    fault: str, expected: str | None
) -> None:
    fixture = _completion_fixture()
    fixture.dispatcher._finalize_write_outcome_in_uow = AsyncMock(return_value=None)
    if fault == "active_call":
        fixture.clock.now.return_value = NOW + timedelta(seconds=1)
    elif fault == "action_missing":
        fixture.uow.external_actions.get.return_value = None
    elif fault == "changed":
        fixture.uow.external_actions.get.return_value = fixture.completed
    elif fault == "control_missing":
        fixture.uow.execution_control.get.return_value = None
    elif fault == "step_not_executing":
        fixture.uow.run_steps.validate_plan_for_execution.return_value = (
            replace(fixture.step, state=StepState.READY),
        )
    elif fault != "not_terminal":
        fixture.uow.execution_control.get.return_value.cancel_requested_at = NOW
        if fault == "receipt_identity":
            fixture.uow.connector_receipts.get.return_value = replace(
                fixture.receipt, external_action_id="action.unrelated"
            )
        elif fault == "no_receipt_cas":
            fixture.uow.connector_receipts.get.return_value = None
    if expected is not None and not isinstance(expected, DispatchDisposition):
        with pytest.raises(ExternalActionDispatchError) as failure:
            await fixture.dispatcher._finalize_terminal_parent_call(fixture.action)
        assert failure.value.code == expected
    else:
        result = await fixture.dispatcher._finalize_terminal_parent_call(fixture.action)
        if expected is None:
            assert result is None
        else:
            assert result is not None and result.disposition is expected
    fixture.uow.commit.assert_not_awaited()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.parametrize("origin", ("unknown-future-origin", {"unsafe": "shape"}))
@pytest.mark.asyncio
async def test_del_07_failed_parent_origin_must_be_an_exact_known_value(origin: object) -> None:
    fixture = _completion_fixture()
    run = replace(fixture.run, state=RunState.FAILED, terminal_reason_code="connector_timeout")
    event = SimpleNamespace(
        event_type="run.transitioned",
        run_id=run.id,
        aggregate_id=run.id,
        mutation_version=run.version,
        occurred_at=run.updated_at,
        previous_state=RunState.EXECUTING.value,
        new_state=RunState.FAILED.value,
        reason_code=run.terminal_reason_code,
        safe_metadata=SimpleNamespace(
            values={"command": "fail", "terminal_failure_origin": origin}
        ),
    )
    fixture.uow.audits.get_mutation_event = AsyncMock(return_value=event)
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher._failed_parent_call_reason(fixture.uow, run)
    assert failure.value.code == "terminal_parent_audit_invalid"
    fixture.uow.audits.append_many.assert_not_awaited()


@pytest.mark.parametrize("latest", ("succeeded", "reserved"))
@pytest.mark.asyncio
async def test_del_07_receipt_reconciliation_cas_loss_returns_latest_state_without_claiming_success(
    latest: str,
) -> None:
    fixture = _completion_fixture()
    fixture.uow.external_actions.get.side_effect = (
        fixture.action,
        fixture.completed if latest == "succeeded" else fixture.reserved,
    )
    fixture.dispatcher._finalize_write_outcome_in_uow = AsyncMock(return_value=None)
    result = await fixture.dispatcher._reconcile_durable_receipt(
        fixture.action, lease_owner="worker.del07"
    )
    assert result is not None
    assert result.disposition is (
        DispatchDisposition.ALREADY_SUCCEEDED
        if latest == "succeeded"
        else DispatchDisposition.LOST_CLAIM
    )
    fixture.uow.commit.assert_not_awaited()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("external_action_id", "action.other"),
        ("connector_binding_id", "binding.other"),
        ("idempotency_key", "idempotency.other"),
        ("action_hash", "0" * 64),
        ("capability_id", "capability.other"),
    ),
)
@pytest.mark.asyncio
async def test_del_07_reconciliation_rejects_each_foreign_receipt_identity_field(
    field: str, changed: str
) -> None:
    fixture = _completion_fixture()
    fixture.uow.connector_receipts.get.return_value = replace(fixture.receipt, **{field: changed})
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher._reconcile_durable_receipt(
            fixture.action, lease_owner="worker.del07"
        )
    assert failure.value.code == "receipt_identity_corrupt"
    fixture.uow.commit.assert_not_awaited()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.parametrize("fault", ("missing_action", "missing_step", "corrupt_witness"))
@pytest.mark.asyncio
async def test_del_07_runtime_denial_audit_never_repairs_missing_or_foreign_action_context(
    fault: str,
) -> None:
    fixture = _completion_fixture()
    error = ExternalActionDispatchError("rate_limit_exhausted", "safe bounded rate denial")
    if fault == "missing_action":
        fixture.uow.external_actions.get.return_value = None
        await fixture.dispatcher._record_runtime_control_denial(
            fixture.action.id, lease_owner="worker.del07", error=error
        )
    else:
        if fault == "missing_step":
            fixture.uow.run_steps.get.return_value = None
        else:
            draft = AuditEventFactory(
                fixture.dispatcher._dispatch_audit_context("worker.del07", fixture.action)
            ).runtime_control_denied(
                run_id=fixture.action.run_id,
                step_id=fixture.action.step_id,
                action_id=fixture.action.id,
                operation_key=fixture.step.runtime_policy.operation_key,
                denial_code=error.code,
                occurred_at=fixture.clock.now(),
            )
            event = AuditEvent(draft, global_sequence=1, run_sequence=1, feed_sequence=1)
            # A repository is required to validate seals. This additional guard
            # still rejects a broken port returning a foreign mutation witness.
            object.__setattr__(draft, "action_id", "action.foreign")
            fixture.uow.audits.get = AsyncMock(return_value=event)
        with pytest.raises(RuntimeError, match="corrupt"):
            await fixture.dispatcher._record_runtime_control_denial(
                fixture.action.id, lease_owner="worker.del07", error=error
            )
    fixture.uow.commit.assert_not_awaited()
    fixture.uow.audits.append_many.assert_not_awaited()
