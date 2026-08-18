"""Pure classified stale-action recovery policy."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import ExternalActionState
from marketing_agents.domain.validation import require_utc


class StaleActionRecoveryDecision(StrEnum):
    RETRY_PRE_CALL = "retry_pre_call"
    FAIL_PRE_CALL_EXHAUSTED = "fail_pre_call_exhausted"
    RETRY_PROVIDER_IDEMPOTENT = "retry_provider_idempotent"
    OUTCOME_UNKNOWN = "outcome_unknown"


def classify_stale_action_recovery(
    action: ExternalAction,
    *,
    now: datetime,
) -> StaleActionRecoveryDecision:
    """Classify one persisted expired dispatch lease without performing I/O."""

    if type(action) is not ExternalAction:
        raise TypeError("stale recovery requires an authoritative ExternalAction snapshot")
    require_utc(now, "stale recovery time")
    lease = action.lease
    if action.state is not ExternalActionState.DISPATCHING or lease is None:
        raise ValueError("stale recovery requires a dispatching action with a current lease")
    if lease.expires_at > now:
        raise ValueError("stale recovery cannot classify an unexpired dispatch lease")

    attempts_remain = action.delivery_attempt_count < action.delivery_attempt_limit
    if action.call_started_at is None:
        return (
            StaleActionRecoveryDecision.RETRY_PRE_CALL
            if attempts_remain
            else StaleActionRecoveryDecision.FAIL_PRE_CALL_EXHAUSTED
        )
    if attempts_remain and action.delivery_contract.idempotency_support in {
        "required",
        "supported",
    }:
        return StaleActionRecoveryDecision.RETRY_PROVIDER_IDEMPOTENT
    return StaleActionRecoveryDecision.OUTCOME_UNKNOWN
