"""ORCH-07: pure persisted-action stale recovery classification."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from marketing_agents.application.policies import (
    StaleActionRecoveryDecision,
    classify_stale_action_recovery,
)
from marketing_agents.domain.entities import (
    ActionReservationSnapshot,
    DeliveryContractSnapshot,
    DispatchLease,
    ExternalAction,
)
from marketing_agents.domain.enums import ExternalActionState

from tests.unit.application.test_run_02_effect_aware_planning import (
    RecordingIds,
    _planner,
    _request,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _dispatching_action(
    *,
    call_started: bool,
    idempotency_support: Literal["required", "supported", "unavailable"],
    attempts_remain: bool,
) -> ExternalAction:
    planner, _, _ = _planner(ids=RecordingIds(seed=700))
    plan = planner.plan(_request(include_write=True, run_id="run.orch-07.recovery"))
    proposal = plan.proposed_actions[0]
    request = plan.approval_requests[0]
    step = next(item for item in plan.steps if item.step_key == proposal.envelope.step_key)
    assert step.binding_configuration_revision is not None
    assert step.connector_timeout_seconds is not None
    attempt_limit = 2
    attempt_count = 1 if attempts_remain else attempt_limit
    created_at = NOW - timedelta(minutes=5)
    action = ExternalAction.proposed(
        proposal,
        request.policy,
        DeliveryContractSnapshot(
            capability_id=step.capability_id,
            connector_family=step.connector_family,
            binding_id=proposal.envelope.binding_id,
            binding_configuration_revision=step.binding_configuration_revision,
            request_schema_id=proposal.envelope.payload_schema_id,
            idempotency_support=idempotency_support,
            timeout_seconds=step.connector_timeout_seconds,
        ),
        created_at,
        delivery_attempt_limit=attempt_limit,
    )
    reservation = ActionReservationSnapshot(
        reservation_id="reservation.orch-07",
        authorization_set_id=proposal.envelope.authorization_set_id,
        approval_request_id="approval-request.orch-07",
        approval_decision_id="approval-decision.orch-07",
        action_hash=proposal.action_hash,
        capability_id=proposal.envelope.capability_id,
        binding_id=proposal.envelope.binding_id,
        idempotency_key=action.idempotency_key,
        reserved_at=created_at,
    )
    claimed_at = NOW - timedelta(minutes=3)
    return replace(
        action,
        state=ExternalActionState.DISPATCHING,
        updated_at=claimed_at,
        version=3,
        delivery_attempt_count=attempt_count,
        reservation=reservation,
        lease=DispatchLease(
            owner="worker.orch-07",
            attempt_number=attempt_count,
            claimed_at=claimed_at,
            expires_at=NOW - timedelta(minutes=1),
        ),
        call_started_at=claimed_at if call_started else None,
        call_deadline_at=(claimed_at + timedelta(seconds=30) if call_started else None),
    )


@pytest.mark.parametrize(
    ("call_started", "idempotency_support", "attempts_remain", "expected"),
    [
        (False, "required", True, StaleActionRecoveryDecision.RETRY_PRE_CALL),
        (False, "supported", True, StaleActionRecoveryDecision.RETRY_PRE_CALL),
        (False, "unavailable", True, StaleActionRecoveryDecision.RETRY_PRE_CALL),
        (False, "required", False, StaleActionRecoveryDecision.FAIL_PRE_CALL_EXHAUSTED),
        (False, "supported", False, StaleActionRecoveryDecision.FAIL_PRE_CALL_EXHAUSTED),
        (False, "unavailable", False, StaleActionRecoveryDecision.FAIL_PRE_CALL_EXHAUSTED),
        (True, "required", True, StaleActionRecoveryDecision.RETRY_PROVIDER_IDEMPOTENT),
        (True, "supported", True, StaleActionRecoveryDecision.RETRY_PROVIDER_IDEMPOTENT),
        (True, "unavailable", True, StaleActionRecoveryDecision.OUTCOME_UNKNOWN),
        (True, "required", False, StaleActionRecoveryDecision.OUTCOME_UNKNOWN),
        (True, "supported", False, StaleActionRecoveryDecision.OUTCOME_UNKNOWN),
        (True, "unavailable", False, StaleActionRecoveryDecision.OUTCOME_UNKNOWN),
    ],
)
def test_orch_07_recovery_classifier_has_complete_fail_closed_truth_table(
    call_started: bool,
    idempotency_support: Literal["required", "supported", "unavailable"],
    attempts_remain: bool,
    expected: StaleActionRecoveryDecision,
) -> None:
    action = _dispatching_action(
        call_started=call_started,
        idempotency_support=idempotency_support,
        attempts_remain=attempts_remain,
    )

    assert classify_stale_action_recovery(action, now=NOW) is expected


def test_orch_07_recovery_classifier_rejects_nonpersisted_or_unexpired_state() -> None:
    action = _dispatching_action(
        call_started=True,
        idempotency_support="required",
        attempts_remain=True,
    )

    with pytest.raises(TypeError, match="authoritative ExternalAction"):
        classify_stale_action_recovery(object(), now=NOW)  # type: ignore[arg-type]
    pre_call_action = _dispatching_action(
        call_started=False,
        idempotency_support="required",
        attempts_remain=True,
    )
    with pytest.raises(ValueError, match="unexpired"):
        classify_stale_action_recovery(pre_call_action, now=NOW - timedelta(minutes=2))
    active_call = replace(
        action,
        call_started_at=NOW - timedelta(seconds=2),
        call_deadline_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="cannot preempt"):
        classify_stale_action_recovery(active_call, now=NOW)
    with pytest.raises(ValueError, match="dispatching action"):
        classify_stale_action_recovery(
            replace(
                action,
                state=ExternalActionState.DISPATCH_RESERVED,
                lease=None,
                call_started_at=None,
                call_deadline_at=None,
                delivery_attempt_count=0,
            ),
            now=NOW,
        )
