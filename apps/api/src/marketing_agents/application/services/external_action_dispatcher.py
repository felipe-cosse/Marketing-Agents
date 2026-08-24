"""Three-phase exact-action dispatch and bounded stale-claim recovery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from marketing_agents.application.orchestration.dependencies import (
    OrchestrationDependencies,
)
from marketing_agents.application.policies.action_recovery import (
    StaleActionRecoveryDecision,
    classify_stale_action_recovery,
)
from marketing_agents.application.policies.write_authorization import (
    ApprovalReservation,
    AuthorizedExternalWrite,
    WriteAuthorizationError,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.external_writes import (
    ConnectorDeliveryFailure,
    ExternalWriteConnectorGateway,
)
from marketing_agents.application.ports.repositories import ReleaseAuthority, ReleaseCallMode
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import (
    ExternalAction,
    ExternalActionResultSnapshot,
)
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepTransitionResult,
    transition_step,
)

_SAFE_FAILURE_CODES = frozenset(
    {
        "authorization_mismatch",
        "binding_mismatch",
        "capability_mismatch",
        "connector_delivery_uncertain",
        "connector_request_rejected",
        "idempotency_conflict",
        "invalid_request",
        "operation_disabled",
        "schema_mismatch",
    }
)


class ExternalActionDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DispatchDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    ALREADY_SUCCEEDED = "already_succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECOVERY_PENDING = "recovery_pending"
    LOST_CLAIM = "lost_claim"
    REQUEUED = "requeued"


@dataclass(frozen=True, slots=True)
class ExternalActionDispatchResult:
    action: ExternalAction
    disposition: DispatchDisposition


class ExternalActionDispatcher:
    """Never hold a database transaction open across a connector await."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        gateway: ExternalWriteConnectorGateway,
        guard: WriteAuthorizationGuard,
        *,
        lease_duration: timedelta = timedelta(minutes=2),
    ) -> None:
        if not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=10):
            raise ValueError("dispatch lease must be from one second through ten minutes")
        self._dependencies = dependencies
        self._gateway = gateway
        self._guard = guard
        self._lease_duration = lease_duration

    async def dispatch_once(
        self,
        action_id: str,
        *,
        lease_owner: str,
    ) -> ExternalActionDispatchResult:
        claimed = await self._claim(action_id, lease_owner)
        if claimed.state is ExternalActionState.SUCCEEDED:
            return ExternalActionDispatchResult(claimed, DispatchDisposition.ALREADY_SUCCEEDED)
        if claimed.state is not ExternalActionState.DISPATCHING:
            return ExternalActionDispatchResult(claimed, DispatchDisposition.LOST_CLAIM)
        marked, authorization = await self._mark_call_started(claimed, lease_owner)
        if marked is None:
            latest = await self._load_required(action_id)
            return ExternalActionDispatchResult(latest, DispatchDisposition.LOST_CLAIM)

        classified_failure: tuple[str, bool] | None = None
        try:
            async with asyncio.timeout(marked.delivery_contract.timeout_seconds):
                connector_result = await self._gateway.execute(authorization)
        except ConnectorDeliveryFailure as exc:
            classified_failure = (
                (exc.code if exc.code in _SAFE_FAILURE_CODES else "connector_delivery_uncertain"),
                exc.request_may_have_left_process,
            )
        except TimeoutError:
            classified_failure = ("connector_timeout", True)
        except Exception:
            classified_failure = ("connector_delivery_uncertain", True)
        if classified_failure is not None:
            return await self._complete_failure(
                marked,
                lease_owner=lease_owner,
                reason_code=classified_failure[0],
                outcome_unknown=classified_failure[1],
            )

        async with self._dependencies.unit_of_work() as unit_of_work:
            receipt = await unit_of_work.connector_receipts.get(
                marked.connector_binding_id, marked.idempotency_key
            )
            if (
                receipt is None
                or receipt.external_action_id != marked.id
                or receipt.action_hash != marked.action_hash
                or receipt.capability_id != marked.envelope.capability_id
                or receipt.receipt_id != connector_result.receipt_id
                or receipt.status != connector_result.status
            ):
                raise ExternalActionDispatchError(
                    "receipt_not_authoritative",
                    "connector response lacks its exact durable receipt",
                )
            result = ExternalActionResultSnapshot(
                receipt_id=receipt.receipt_id,
                status=receipt.status,
                safe_metadata=receipt.safe_metadata,
                completed_at=self._dependencies.utc_now(),
            )
            completed = await unit_of_work.external_actions.complete_succeeded(
                action_id=marked.id,
                expected_version=marked.version,
                lease_owner=lease_owner,
                attempt_number=marked.delivery_attempt_count,
                result=result,
            )
            if completed is None:
                raise ExternalActionDispatchError(
                    "completion_cas_lost",
                    "connector returned but the durable action completion was not accepted",
                )
            await unit_of_work.commit()
        return ExternalActionDispatchResult(completed, DispatchDisposition.SUCCEEDED)

    async def _claim(self, action_id: str, lease_owner: str) -> ExternalAction:
        async with self._dependencies.unit_of_work() as unit_of_work:
            action = await unit_of_work.external_actions.get(action_id)
            if action is None:
                raise ExternalActionDispatchError(
                    "action_not_found", "external action does not exist"
                )
            if action.state is ExternalActionState.SUCCEEDED:
                return action
            if action.state is not ExternalActionState.DISPATCH_RESERVED:
                raise ExternalActionDispatchError(
                    "action_not_dispatchable", "external action is not dispatch reserved"
                )
            self._authorize(action)
            self._gateway.contract_for(action)
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            if authority is None:
                raise ExternalActionDispatchError(
                    "release_authority_missing",
                    "external action lacks its exact committed release authority",
                )
            self._require_authority(action, authority)
            run = await unit_of_work.runs.get(action.run_id)
            if (
                run is None
                or run.state is not RunState.EXECUTING
                or run.approval_required is not True
                or run.version != authority.released_run_version
            ):
                raise ExternalActionDispatchError(
                    "run_not_executing", "parent Run does not permit external effects"
                )
            claimed_at = self._dependencies.utc_now()
            claimed = await unit_of_work.external_actions.claim_reserved(
                action_id=action.id,
                expected_version=action.version,
                authority=authority,
                lease_owner=lease_owner,
                claimed_at=claimed_at,
                lease_expires_at=claimed_at + self._lease_duration,
            )
            if claimed is None:
                raise ExternalActionDispatchError(
                    "claim_cas_lost", "external action dispatch claim was not acquired"
                )
            await unit_of_work.audits.append(
                AuditEventFactory(
                    self._dispatch_audit_context(
                        lease_owner,
                        claimed,
                    )
                ).action_dispatch_claimed(action, claimed)
            )
            await unit_of_work.commit()
            return claimed

    async def _mark_call_started(
        self,
        claimed: ExternalAction,
        lease_owner: str,
    ) -> tuple[ExternalAction | None, AuthorizedExternalWrite]:
        async with self._dependencies.unit_of_work() as unit_of_work:
            action = await unit_of_work.external_actions.get(claimed.id)
            if action is None:
                raise ExternalActionDispatchError(
                    "action_not_found", "claimed external action disappeared"
                )
            authorization = self._authorize(action)
            self._gateway.contract_for(action)
            authority = await unit_of_work.approvals.get_release_authority(action.id)
            if authority is None:
                return None, authorization
            self._require_authority(action, authority)
            run = await unit_of_work.runs.get(action.run_id)
            if (
                run is None
                or run.state is not RunState.EXECUTING
                or run.approval_required is not True
                or run.version != authority.released_run_version
            ):
                return None, authorization
            started_at = self._dependencies.utc_now()
            step_transition = await self._call_start_step_transition(
                unit_of_work,
                authority,
                started_at=started_at,
            )
            started = await unit_of_work.external_actions.mark_call_started(
                action_id=action.id,
                expected_version=action.version,
                authority=authority,
                lease_owner=lease_owner,
                attempt_number=action.delivery_attempt_count,
                started_at=started_at,
                step_transition=step_transition,
            )
            if started is not None:
                factory = AuditEventFactory(
                    self._dispatch_audit_context(
                        lease_owner,
                        started.action,
                    )
                )
                await unit_of_work.audits.append_many(
                    (
                        factory.action_call_started(action, started.action),
                        *(
                            ()
                            if started.step_transition is None
                            else (
                                factory.step_transition(
                                    started.step_transition.step,
                                    started.step_transition.transition,
                                ),
                            )
                        ),
                    )
                )
                await unit_of_work.commit()
                return started.action, authorization
            return None, authorization

    async def _call_start_step_transition(
        self,
        unit_of_work: UnitOfWork,
        authority: ReleaseAuthority,
        *,
        started_at: datetime,
    ) -> StepTransitionResult | None:
        if authority.call_mode is ReleaseCallMode.PROVIDER_RETRY:
            if (
                authority.step_state is not StepState.EXECUTING
                or authority.step_version != authority.released_step_version + 1
                or authority.prior_started_attempt_number is None
            ):
                raise ExternalActionDispatchError(
                    "release_authority_mismatch",
                    "provider retry lacks its exact prior call-start step witness",
                )
            return None
        if authority.call_mode is not ReleaseCallMode.FIRST_CALL:
            raise ExternalActionDispatchError(
                "release_authority_mismatch",
                "release authority selected an unsupported call mode",
            )
        if (
            authority.step_state is not StepState.READY
            or authority.step_version != authority.released_step_version
            or authority.prior_started_attempt_number is not None
        ):
            raise ExternalActionDispatchError(
                "release_authority_mismatch",
                "first call lacks its exact released ready step",
            )
        step = await unit_of_work.run_steps.get(authority.step_id)
        if (
            step is None
            or step.state is not authority.step_state
            or step.version != authority.step_version
            or step.run_id != authority.run_id
            or step.key != authority.step_key
        ):
            raise ExternalActionDispatchError(
                "release_authority_mismatch",
                "released write step changed before connector call-start",
            )
        return transition_step(
            step,
            StepLifecycleCommand.START_RESERVED_WRITE,
            NoStepTransitionContext(),
            started_at,
        )

    @staticmethod
    def _require_authority(action: ExternalAction, authority: ReleaseAuthority) -> None:
        reservation = action.reservation
        if (
            reservation is None
            or authority.action_id != action.id
            or authority.action_hash != action.action_hash
            or authority.run_id != action.run_id
            or authority.step_id != action.step_id
            or authority.step_key != action.envelope.step_key
            or authority.authorization_set_id != reservation.authorization_set_id
            or authority.reservation_id != reservation.reservation_id
            or authority.approval_request_id != reservation.approval_request_id
            or authority.approval_decision_id != reservation.approval_decision_id
            or authority.action_hash != reservation.action_hash
        ):
            raise ExternalActionDispatchError(
                "release_authority_mismatch",
                "committed release authority differs from the action reservation",
            )

    @staticmethod
    def _dispatch_audit_context(
        lease_owner: str,
        action: ExternalAction,
    ) -> AuditContext:
        return AuditContext.worker(
            lease_owner,
            correlation_id=(
                f"dispatch-attempt.{action.delivery_attempt_count}.{action.action_hash[:32]}"
            ),
        )

    def _authorize(self, action: ExternalAction) -> AuthorizedExternalWrite:
        reservation = action.reservation
        if reservation is None:
            raise ExternalActionDispatchError(
                "reservation_missing", "external action lacks durable approval reservation"
            )
        try:
            return self._guard.authorize(
                action.envelope,
                ApprovalReservation(
                    reservation_id=reservation.reservation_id,
                    authorization_set_id=reservation.authorization_set_id,
                    state="dispatch_reserved",
                    action_id=action.id,
                    action_hash=reservation.action_hash,
                    capability_id=reservation.capability_id,
                    binding_id=reservation.binding_id,
                    approval_request_id=reservation.approval_request_id,
                    approval_decision_id=reservation.approval_decision_id,
                    idempotency_key=reservation.idempotency_key,
                    reserved_at=reservation.reserved_at,
                ),
                action.idempotency_key,
            )
        except WriteAuthorizationError as exc:
            raise ExternalActionDispatchError(
                exc.code, "durable action reservation failed exact authorization"
            ) from None

    async def _complete_failure(
        self,
        action: ExternalAction,
        *,
        lease_owner: str,
        reason_code: str,
        outcome_unknown: bool,
    ) -> ExternalActionDispatchResult:
        if outcome_unknown:
            reconciled = await self._reconcile_durable_receipt(action, lease_owner=lease_owner)
            if reconciled is not None:
                return reconciled
        if (
            outcome_unknown
            and action.delivery_contract.idempotency_support in {"required", "supported"}
            and action.delivery_attempt_count < action.delivery_attempt_limit
        ):
            # The call marker and lease remain durable. Only stale recovery may
            # replay this exact provider-idempotent key after the fence expires.
            return ExternalActionDispatchResult(action, DispatchDisposition.RECOVERY_PENDING)
        occurred_at = self._dependencies.utc_now()
        async with self._dependencies.unit_of_work() as unit_of_work:
            if outcome_unknown:
                completed = await unit_of_work.external_actions.mark_outcome_unknown(
                    action_id=action.id,
                    expected_version=action.version,
                    lease_owner=lease_owner,
                    attempt_number=action.delivery_attempt_count,
                    reason_code=reason_code,
                    occurred_at=occurred_at,
                )
                disposition = DispatchDisposition.OUTCOME_UNKNOWN
            else:
                completed = await unit_of_work.external_actions.complete_failed(
                    action_id=action.id,
                    expected_version=action.version,
                    lease_owner=lease_owner,
                    attempt_number=action.delivery_attempt_count,
                    reason_code=reason_code,
                    occurred_at=occurred_at,
                )
                disposition = DispatchDisposition.FAILED
            if completed is None:
                raise ExternalActionDispatchError(
                    "completion_cas_lost", "external action failure was not persisted"
                )
            await unit_of_work.commit()
        return ExternalActionDispatchResult(completed, disposition)

    async def recover_stale(
        self,
        *,
        lease_owner: str,
        limit: int = 32,
    ) -> tuple[ExternalActionDispatchResult, ...]:
        if not 1 <= limit <= 32:
            raise ValueError("stale recovery limit must be from 1 through 32")
        now = self._dependencies.utc_now()
        async with self._dependencies.unit_of_work() as unit_of_work:
            stale = await unit_of_work.external_actions.list_stale(now=now, limit=limit)
        results: list[ExternalActionDispatchResult] = []
        for snapshot in stale:
            lease = snapshot.lease
            if lease is None:  # pragma: no cover - domain invariant
                continue
            if (
                snapshot.call_started_at is not None
                and (
                    reconciled := await self._reconcile_durable_receipt(
                        snapshot, lease_owner=lease.owner
                    )
                )
                is not None
            ):
                results.append(reconciled)
                continue
            decision = classify_stale_action_recovery(snapshot, now=now)
            if decision is StaleActionRecoveryDecision.FAIL_PRE_CALL_EXHAUSTED:
                async with self._dependencies.unit_of_work() as unit_of_work:
                    failed = await unit_of_work.external_actions.fail_exhausted_stale_pre_call(
                        action_id=snapshot.id,
                        expected_version=snapshot.version,
                        attempt_number=lease.attempt_number,
                        occurred_at=now,
                        reason_code="pre_call_attempts_exhausted",
                    )
                    if failed is not None:
                        await unit_of_work.commit()
                        results.append(
                            ExternalActionDispatchResult(failed, DispatchDisposition.FAILED)
                        )
                continue
            if decision is StaleActionRecoveryDecision.OUTCOME_UNKNOWN:
                async with self._dependencies.unit_of_work() as unit_of_work:
                    unknown = await unit_of_work.external_actions.mark_outcome_unknown(
                        action_id=snapshot.id,
                        expected_version=snapshot.version,
                        lease_owner=lease.owner,
                        attempt_number=lease.attempt_number,
                        reason_code="stale_delivery_outcome_unknown",
                        occurred_at=now,
                    )
                    if unknown is not None:
                        await unit_of_work.commit()
                        results.append(
                            ExternalActionDispatchResult(
                                unknown, DispatchDisposition.OUTCOME_UNKNOWN
                            )
                        )
                continue
            if decision is StaleActionRecoveryDecision.RETRY_PRE_CALL:
                conclusion = "pre_call_expired"
            elif decision is StaleActionRecoveryDecision.RETRY_PROVIDER_IDEMPOTENT:
                conclusion = "provider_retry"
            else:  # pragma: no cover - exhaustive fail-closed enum guard
                raise ExternalActionDispatchError(
                    "recovery_decision_invalid",
                    "stale action recovery returned an unsupported decision",
                )
            released = await self._release_stale(snapshot, now, conclusion)
            if released is None:
                continue
            try:
                results.append(await self.dispatch_once(released.id, lease_owner=lease_owner))
            except ExternalActionDispatchError:
                latest = await self._load_required(released.id)
                results.append(ExternalActionDispatchResult(latest, DispatchDisposition.LOST_CLAIM))
        return tuple(results)

    async def _reconcile_durable_receipt(
        self,
        action: ExternalAction,
        *,
        lease_owner: str,
    ) -> ExternalActionDispatchResult | None:
        """Complete from exact durable connector evidence without another provider call."""

        async with self._dependencies.unit_of_work() as unit_of_work:
            receipt = await unit_of_work.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
            if receipt is None:
                return None
            if (
                receipt.external_action_id != action.id
                or receipt.connector_binding_id != action.connector_binding_id
                or receipt.idempotency_key != action.idempotency_key
                or receipt.action_hash != action.action_hash
                or receipt.capability_id != action.envelope.capability_id
            ):
                raise ExternalActionDispatchError(
                    "receipt_identity_corrupt",
                    "durable connector receipt does not match the exact action",
                )
            result = ExternalActionResultSnapshot(
                receipt_id=receipt.receipt_id,
                status=receipt.status,
                safe_metadata=receipt.safe_metadata,
                completed_at=self._dependencies.utc_now(),
            )
            completed = await unit_of_work.external_actions.complete_succeeded(
                action_id=action.id,
                expected_version=action.version,
                lease_owner=lease_owner,
                attempt_number=action.delivery_attempt_count,
                result=result,
            )
            if completed is not None:
                await unit_of_work.commit()
                return ExternalActionDispatchResult(completed, DispatchDisposition.SUCCEEDED)
        latest = await self._load_required(action.id)
        if latest.state is ExternalActionState.SUCCEEDED:
            return ExternalActionDispatchResult(latest, DispatchDisposition.ALREADY_SUCCEEDED)
        return ExternalActionDispatchResult(latest, DispatchDisposition.LOST_CLAIM)

    async def _release_stale(
        self,
        snapshot: ExternalAction,
        now: datetime,
        conclusion: str,
    ) -> ExternalAction | None:
        lease = snapshot.lease
        if lease is None:  # pragma: no cover - domain invariant
            return None
        async with self._dependencies.unit_of_work() as unit_of_work:
            released = await unit_of_work.external_actions.release_stale_for_retry(
                action_id=snapshot.id,
                expected_version=snapshot.version,
                attempt_number=lease.attempt_number,
                occurred_at=now,
                conclusion=conclusion,
            )
            if released is not None:
                await unit_of_work.commit()
            return released

    async def _load_required(self, action_id: str) -> ExternalAction:
        async with self._dependencies.unit_of_work() as unit_of_work:
            action = await unit_of_work.external_actions.get(action_id)
        if action is None:
            raise ExternalActionDispatchError("action_not_found", "external action does not exist")
        return action
