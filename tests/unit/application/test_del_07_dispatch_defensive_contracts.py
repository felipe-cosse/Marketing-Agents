"""DEL-07: malformed dependency snapshots fail closed at dispatch/recovery guards.

Deliberate post-construction mutations represent a broken/corrupt port, not states
accepted by normal domain constructors. The defensive checks remain executable.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.write_authorization import WriteAuthorizationGuard
from marketing_agents.application.services import external_action_dispatcher as dispatch_module
from marketing_agents.application.services.approval_boundaries import ApprovalBoundaryService
from marketing_agents.application.services.external_action_dispatcher import (
    ExternalActionDispatcher,
    ExternalActionDispatchError,
    _DispatchCallStart,
    _safe_audit_retry_after,
)
from marketing_agents.domain.entities import ActionReservationSnapshot, DispatchLease
from marketing_agents.domain.enums import ExternalActionState

from tests.unit.application.test_del_07_approval_decision_faults import _decision_fixture
from tests.unit.domain.test_run_08_approval_records import NOW


def _dispatch_fixture(*, started: bool = True) -> SimpleNamespace:
    fixture = _decision_fixture()
    source = fixture.action
    reservation = ActionReservationSnapshot(
        "reservation.del07",
        source.envelope.authorization_set_id,
        fixture.request.id,
        "approval-decision.del07",
        source.action_hash,
        source.envelope.capability_id,
        source.connector_binding_id,
        source.idempotency_key,
        NOW,
    )
    action = replace(
        source,
        state=ExternalActionState.DISPATCHING,
        reservation=reservation,
        lease=DispatchLease("worker.del07", 1, NOW, NOW + timedelta(seconds=60)),
        delivery_attempt_count=1,
        call_started_at=NOW if started else None,
        call_deadline_at=NOW + timedelta(seconds=30) if started else None,
        version=4,
    )
    fixture.clock.now.return_value = NOW + timedelta(seconds=120)
    fixture.uow.external_actions.get.return_value = action
    fixture.gateway = SimpleNamespace(execute=AsyncMock(), contract_for=Mock())
    fixture.dependencies = OrchestrationDependencies(fixture.clock, fixture.ids, fixture.factory)
    fixture.dispatcher = ExternalActionDispatcher(
        fixture.dependencies, fixture.gateway, WriteAuthorizationGuard()
    )
    fixture.action = action
    return fixture


@pytest.mark.parametrize("missing", ("lease", "deadline"))
@pytest.mark.asyncio
async def test_del_07_single_action_recovery_rejects_missing_exact_call_authority(
    missing: str,
) -> None:
    fixture = _dispatch_fixture()
    object.__setattr__(fixture.action, "lease" if missing == "lease" else "call_deadline_at", None)
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher.recover_action(fixture.action.id, lease_owner="worker.recovery")
    assert failure.value.code == "recovery_call_authority_invalid"
    fixture.gateway.execute.assert_not_awaited()
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_snapshot_recovery_rejects_missing_deadline_and_ignores_absent_lease() -> None:
    fixture = _dispatch_fixture()
    object.__setattr__(fixture.action, "call_deadline_at", None)
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher._recover_snapshot(
            fixture.action, lease_owner="worker.recovery", now=fixture.clock.now()
        )
    assert failure.value.code == "recovery_call_authority_invalid"
    object.__setattr__(fixture.action, "lease", None)
    assert (
        await fixture.dispatcher._recover_snapshot(
            fixture.action, lease_owner="worker.recovery", now=fixture.clock.now()
        )
        is None
    )
    assert (
        await fixture.dispatcher._release_stale(
            fixture.action, fixture.clock.now(), "pre_call_expired"
        )
        is None
    )
    fixture.factory.assert_not_called()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_terminal_parent_reconciliation_rejects_missing_call_lease() -> None:
    fixture = _dispatch_fixture()
    object.__setattr__(fixture.action, "lease", None)
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher._finalize_terminal_parent_call(fixture.action)
    assert failure.value.code == "recovery_call_authority_invalid"
    fixture.gateway.execute.assert_not_awaited()
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_unknown_recovery_classification_is_not_treated_as_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _dispatch_fixture(started=False)
    monkeypatch.setattr(
        dispatch_module, "classify_stale_action_recovery", lambda *a, **kw: "unknown"
    )
    with pytest.raises(ExternalActionDispatchError) as failure:
        await fixture.dispatcher._recover_snapshot(
            fixture.action, lease_owner="worker.recovery", now=fixture.clock.now()
        )
    assert failure.value.code == "recovery_decision_invalid"
    fixture.factory.assert_not_called()
    fixture.gateway.execute.assert_not_awaited()


@pytest.mark.parametrize("missing", ("state", "call_started", "authorization", "permit", "output"))
@pytest.mark.asyncio
async def test_del_07_dispatch_call_start_invariant_is_checked_before_connector_invocation(
    missing: str,
) -> None:
    fixture = _dispatch_fixture()
    authorization = fixture.dispatcher._authorize(fixture.action)
    permit = SimpleNamespace(call_deadline_at=NOW + timedelta(seconds=30))
    if missing == "state":
        object.__setattr__(fixture.action, "state", ExternalActionState.DISPATCH_RESERVED)
    elif missing == "call_started":
        object.__setattr__(fixture.action, "call_started_at", None)
    start = _DispatchCallStart(
        fixture.action,
        None if missing == "authorization" else authorization,
        None if missing == "permit" else permit,
        None if missing == "output" else 1024,
    )
    fixture.dispatcher._claim_and_mark_call_started = AsyncMock(return_value=start)
    with pytest.raises(AssertionError, match="call-start transaction returned an invalid result"):
        await fixture.dispatcher.dispatch_once(fixture.action.id, lease_owner="worker.del07")
    fixture.gateway.execute.assert_not_awaited()
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_del_07_release_witness_requires_a_decision_even_if_a_caller_bypasses_its_guard() -> (
    None
):
    fixture = _dispatch_fixture()
    service = ApprovalBoundaryService(fixture.dependencies)
    with pytest.raises(AssertionError, match="approved leaf lost its decision"):
        await service._require_release_decision_witnesses(
            fixture.uow, fixture.stored, fixture.action
        )
    fixture.uow.audits.append_many.assert_not_awaited()
    fixture.uow.commit.assert_not_awaited()


@pytest.mark.parametrize("seconds", (0, 601, -1))
def test_del_07_dispatch_lease_is_bounded_before_any_repository_use(seconds: int) -> None:
    fixture = _dispatch_fixture()
    with pytest.raises(ValueError, match="one second through ten minutes"):
        ExternalActionDispatcher(
            fixture.dependencies,
            fixture.gateway,
            WriteAuthorizationGuard(),
            lease_duration=timedelta(seconds=seconds),
        )
    fixture.factory.assert_not_called()


@pytest.mark.parametrize("limit", (0, 33, -1))
@pytest.mark.asyncio
async def test_del_07_recovery_scan_is_bounded_before_repository_access(limit: int) -> None:
    fixture = _dispatch_fixture()
    with pytest.raises(ValueError, match="1 through 32"):
        await fixture.dispatcher.recover_stale(lease_owner="worker.recovery", limit=limit)
    fixture.factory.assert_not_called()


@pytest.mark.parametrize("value", (None, True, 0, -1, 3601, "60", 1.0))
def test_del_07_audit_retry_delay_rejects_unbounded_or_non_integer_provider_values(
    value: object,
) -> None:
    assert _safe_audit_retry_after(value) is None  # type: ignore[arg-type]


def test_del_07_audit_retry_delay_retains_exact_inclusive_integer_bounds() -> None:
    assert _safe_audit_retry_after(1) == 1
    assert _safe_audit_retry_after(3600) == 3600
