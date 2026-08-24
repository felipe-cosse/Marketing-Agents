"""Three-phase exact-action dispatch and bounded stale-claim recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
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
from marketing_agents.application.ports.repositories import (
    ExecutionControlRepositoryConflict,
    ReleaseAuthority,
    ReleaseCallMode,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import (
    RUNTIME_CONTROL_DENIAL_CODES,
    TERMINAL_RUNTIME_CONTROL_DENIAL_CODES,
    AuditContext,
    AuditEventDraft,
)
from marketing_agents.domain.entities import (
    ExternalAction,
    ExternalActionResultSnapshot,
    RunStep,
)
from marketing_agents.domain.enums import Effect, ExternalActionState, RunState, StepState
from marketing_agents.domain.execution_control import (
    DeliveryCallPermit,
    DeliveryCallReservationCommand,
)
from marketing_agents.domain.runtime_policy import (
    canonical_payload_size_bytes,
    payload_fields_within_byte_limit,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepTerminalContext,
    StepTransitionResult,
    transition_step,
)

from .terminal_execution_cleanup import TerminalExecutionCleanupService

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

_OUTPUT_PROJECTION_OMITTED = {"omitted": "output_payload_too_large"}


class ExternalActionDispatchError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


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


@dataclass(frozen=True, slots=True)
class _DispatchCallStart:
    action: ExternalAction
    authorization: AuthorizedExternalWrite | None = None
    permit: DeliveryCallPermit | None = None
    max_output_bytes: int | None = None


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
        try:
            start = await self._claim_and_mark_call_started(action_id, lease_owner)
        except ExternalActionDispatchError as exc:
            await self._record_runtime_control_denial(
                action_id,
                lease_owner=lease_owner,
                error=exc,
            )
            raise
        marked = start.action
        if marked.state is ExternalActionState.SUCCEEDED:
            return ExternalActionDispatchResult(marked, DispatchDisposition.ALREADY_SUCCEEDED)
        authorization = start.authorization
        permit = start.permit
        max_output_bytes = start.max_output_bytes
        if (
            marked.state is not ExternalActionState.DISPATCHING
            or marked.call_started_at is None
            or authorization is None
            or permit is None
            or max_output_bytes is None
        ):  # pragma: no cover - return contract invariant
            raise AssertionError("dispatch call-start transaction returned an invalid result")

        remaining_seconds = (permit.call_deadline_at - self._dependencies.utc_now()).total_seconds()
        if remaining_seconds <= 0:
            return await self._complete_failure(
                marked,
                lease_owner=lease_owner,
                reason_code="connector_timeout",
                outcome_unknown=False,
            )

        classified_failure: tuple[str, bool] | None = None
        try:
            loop = asyncio.get_running_loop()
            monotonic_deadline = loop.time() + remaining_seconds
            async with asyncio.timeout(remaining_seconds):
                connector_result = await self._gateway.execute(authorization)
                if (
                    self._dependencies.utc_now() >= permit.call_deadline_at
                    or loop.time() >= monotonic_deadline
                ):
                    raise TimeoutError
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

        # Receipt ID and status are control-plane evidence. Connector metadata is
        # an optional output projection and must never cross the sealed byte
        # boundary merely because the provider call already succeeded.
        response_receipt_id = connector_result.receipt_id
        response_status = connector_result.status
        _bounded_output_projection(connector_result.safe_metadata, max_output_bytes)
        del connector_result

        async with self._dependencies.unit_of_work() as unit_of_work:
            receipt = await unit_of_work.connector_receipts.get(
                marked.connector_binding_id, marked.idempotency_key
            )
            if (
                receipt is None
                or receipt.external_action_id != marked.id
                or receipt.action_hash != marked.action_hash
                or receipt.capability_id != marked.envelope.capability_id
                or receipt.receipt_id != response_receipt_id
                or receipt.status != response_status
            ):
                raise ExternalActionDispatchError(
                    "receipt_not_authoritative",
                    "connector response lacks its exact durable receipt",
                )
            result = ExternalActionResultSnapshot(
                receipt_id=receipt.receipt_id,
                status=receipt.status,
                safe_metadata=_bounded_output_projection(
                    receipt.safe_metadata,
                    max_output_bytes,
                ),
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

    async def _record_runtime_control_denial(
        self,
        action_id: str,
        *,
        lease_owner: str,
        error: ExternalActionDispatchError,
    ) -> None:
        """Persist a denial witness in a fresh transaction after admission rollback."""

        if error.code not in RUNTIME_CONTROL_DENIAL_CODES:
            return
        occurred_at = self._dependencies.utc_now()
        async with self._dependencies.unit_of_work() as unit_of_work:
            action = await unit_of_work.external_actions.get(action_id)
            if action is None:
                return
            step = await unit_of_work.run_steps.get(action.step_id)
            if step is None or step.run_id != action.run_id:
                raise RuntimeError("runtime-control denial action binding is corrupt")
            event = AuditEventFactory(
                self._dispatch_audit_context(lease_owner, action)
            ).runtime_control_denied(
                run_id=action.run_id,
                step_id=action.step_id,
                action_id=action.id,
                operation_key=step.runtime_policy.operation_key,
                denial_code=error.code,
                retry_after_seconds=_safe_audit_retry_after(error.retry_after_seconds),
                occurred_at=occurred_at,
            )
            existing = await unit_of_work.audits.get(event.id)
            if existing is not None:
                metadata = existing.safe_metadata.values
                retry_after_seconds = metadata.get("retry_after_seconds")
                if (
                    existing.event_type != event.event_type
                    or existing.aggregate_id != event.aggregate_id
                    or existing.run_id != event.run_id
                    or existing.step_id != event.step_id
                    or existing.action_id != event.action_id
                    or frozenset(metadata)
                    not in {
                        frozenset({"denial_code", "operation_key"}),
                        frozenset({"denial_code", "operation_key", "retry_after_seconds"}),
                    }
                    or metadata.get("denial_code") != error.code
                    or metadata.get("operation_key") != step.runtime_policy.operation_key
                    or (
                        retry_after_seconds is not None
                        and (
                            type(retry_after_seconds) is not int
                            or not 1 <= retry_after_seconds <= 3_600
                        )
                    )
                ):
                    raise RuntimeError("runtime-control denial audit identity is corrupt")
            transition_events: list[AuditEventDraft] = []
            run = await unit_of_work.runs.get(action.run_id)
            if (
                error.code in TERMINAL_RUNTIME_CONTROL_DENIAL_CODES
                and run is not None
                and run.state is RunState.EXECUTING
            ):
                cleanup = await TerminalExecutionCleanupService().fail_runtime_control_in_uow(
                    unit_of_work,
                    run_id=action.run_id,
                    denied_step_id=action.step_id,
                    plan_hash=action.envelope.plan_hash,
                    denial_code=error.code,
                    occurred_at=occurred_at,
                    audit_context=self._dispatch_audit_context(lease_owner, action),
                )
                transition_events.extend(cleanup.audit_events)
            events = (
                *((event,) if existing is None else ()),
                *transition_events,
            )
            if events:
                await unit_of_work.audits.append_many(events)
            await unit_of_work.commit()

    async def _claim_and_mark_call_started(
        self,
        action_id: str,
        lease_owner: str,
    ) -> _DispatchCallStart:
        """Atomically claim, admit, and persist the exact pre-provider call marker."""

        async with self._dependencies.unit_of_work() as unit_of_work:
            action = await unit_of_work.external_actions.get(action_id)
            if action is None:
                raise ExternalActionDispatchError(
                    "action_not_found", "external action does not exist"
                )
            if action.state is ExternalActionState.SUCCEEDED:
                return _DispatchCallStart(action)
            if action.state is not ExternalActionState.DISPATCH_RESERVED:
                raise ExternalActionDispatchError(
                    "action_not_dispatchable", "external action is not dispatch reserved"
                )
            authorization = self._authorize(action)
            try:
                self._gateway.contract_for(action)
            except ConnectorDeliveryFailure as exc:
                safe_code = (
                    exc.code
                    if exc.code in {"delivery_contract_drift", "delivery_contract_unavailable"}
                    else "delivery_contract_invalid"
                )
                raise ExternalActionDispatchError(
                    safe_code,
                    "connector delivery contract rejected the sealed action",
                ) from exc
            except Exception as exc:
                raise ExternalActionDispatchError(
                    "delivery_contract_invalid",
                    "connector delivery contract could not be validated",
                ) from exc
            try:
                authority = await unit_of_work.approvals.get_release_authority(action.id)
            except RuntimeError as exc:
                raise ExternalActionDispatchError(
                    "execution_plan_invalid",
                    "external action release authority could not validate its sealed plan",
                ) from exc
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
            try:
                validated_steps = await unit_of_work.run_steps.validate_plan_for_execution(run.id)
            except RuntimeError as exc:
                raise ExternalActionDispatchError(
                    "execution_plan_invalid",
                    "external action cannot validate its sealed execution plan",
                ) from exc
            validated_step = next(
                (item for item in validated_steps if item.id == authority.step_id),
                None,
            )
            if (
                validated_step is None
                or validated_step.run_id != authority.run_id
                or validated_step.key != authority.step_key
                or validated_step.state is not authority.step_state
                or validated_step.version != authority.step_version
                or validated_step.plan_hash != action.envelope.plan_hash
            ):
                raise ExternalActionDispatchError(
                    "execution_plan_mismatch",
                    "released WRITE step differs from its sealed execution plan",
                )
            payload = action.envelope.minimized_payload
            budget = validated_step.runtime_policy.budget
            if canonical_payload_size_bytes(payload) > budget.max_input_bytes:
                raise ExternalActionDispatchError(
                    "input_payload_too_large",
                    "external action input exceeds its sealed byte budget",
                )
            if not payload_fields_within_byte_limit(payload, budget.max_input_field_bytes):
                raise ExternalActionDispatchError(
                    "input_field_too_large",
                    "external action input field exceeds its sealed byte budget",
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
            control = await unit_of_work.execution_control.get(action.run_id)
            if control is None or control.policy_hash != action.envelope.plan_hash:
                raise ExternalActionDispatchError(
                    "execution_control_invalid",
                    "external action lacks its exact sealed execution control",
                )
            try:
                permit_result = await unit_of_work.execution_control.reserve_delivery_call(
                    DeliveryCallReservationCommand(
                        run_id=claimed.run_id,
                        step_id=claimed.step_id,
                        action_id=claimed.id,
                        delivery_attempt_number=claimed.delivery_attempt_count,
                        expected_control_version=control.version,
                        expected_step_version=authority.step_version,
                        expected_action_version=claimed.version,
                        reserved_at=claimed_at,
                    )
                )
            except ExecutionControlRepositoryConflict as exc:
                raise ExternalActionDispatchError(
                    exc.code,
                    "external action call was denied by durable runtime control",
                    retry_after_seconds=exc.retry_after_seconds,
                ) from exc
            step_transition = await self._call_start_step_transition(
                unit_of_work,
                authority,
                started_at=claimed_at,
            )
            started = await unit_of_work.external_actions.mark_call_started(
                action_id=claimed.id,
                expected_version=claimed.version,
                authority=authority,
                lease_owner=lease_owner,
                attempt_number=claimed.delivery_attempt_count,
                started_at=claimed_at,
                call_deadline_at=permit_result.permit.call_deadline_at,
                step_transition=step_transition,
            )
            if started is None:
                raise ExternalActionDispatchError(
                    "call_start_cas_lost",
                    "external action call-start transaction was not acquired",
                )
            factory = AuditEventFactory(
                self._dispatch_audit_context(
                    lease_owner,
                    started.action,
                )
            )
            await unit_of_work.audits.append_many(
                (
                    factory.action_dispatch_claimed(action, claimed),
                    factory.action_call_started(claimed, started.action),
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
            return _DispatchCallStart(
                action=started.action,
                authorization=authorization,
                permit=permit_result.permit,
                max_output_bytes=budget.max_output_bytes,
            )

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
            if snapshot.call_started_at is not None:
                call_deadline_at = snapshot.call_deadline_at
                if call_deadline_at is None:  # pragma: no cover - domain invariant
                    raise ExternalActionDispatchError(
                        "recovery_call_authority_invalid",
                        "stale action lacks its exact provider-call deadline",
                    )
                if now < call_deadline_at:
                    continue
                terminalized = await self._finalize_terminal_parent_call(snapshot)
                if terminalized is not None:
                    results.append(terminalized)
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

    async def _finalize_terminal_parent_call(
        self,
        snapshot: ExternalAction,
    ) -> ExternalActionDispatchResult | None:
        """Close expired call authority under a terminal/cancelled parent without replay."""

        now = self._dependencies.utc_now()
        call_deadline_at = snapshot.call_deadline_at
        if snapshot.call_started_at is None or call_deadline_at is None or now < call_deadline_at:
            return None
        async with self._dependencies.unit_of_work() as unit_of_work:
            current = await unit_of_work.external_actions.get(snapshot.id)
            if current is None:
                raise ExternalActionDispatchError(
                    "action_not_found",
                    "external action does not exist",
                )
            if current != snapshot:
                return _terminal_dispatch_result(current)
            lease = current.lease
            if lease is None:  # pragma: no cover - domain invariant
                raise ExternalActionDispatchError(
                    "recovery_call_authority_invalid",
                    "terminal action lacks its exact provider-call lease",
                )
            run = await unit_of_work.runs.get(current.run_id)
            control = await unit_of_work.execution_control.get(current.run_id)
            if run is None or control is None or control.policy_hash != current.envelope.plan_hash:
                raise ExternalActionDispatchError(
                    "execution_control_invalid",
                    "terminal action lacks its exact parent control",
                )
            step = await _sealed_write_step(unit_of_work, current)
            if step.state is not StepState.EXECUTING:
                raise ExternalActionDispatchError(
                    "terminal_call_step_invalid",
                    "terminal action lacks its exact executing WRITE step",
                )
            if run.state is RunState.CANCELLED:
                reason_code = "run_cancelled_after_call_start"
            elif (
                run.state is RunState.FAILED
                and run.terminal_reason_code in TERMINAL_RUNTIME_CONTROL_DENIAL_CODES
            ):
                reason_code = "runtime_control_denied_after_call_start"
            elif control.cancel_requested_at is not None:
                reason_code = "run_cancelled_after_call_start"
            else:
                return None

            receipt = await unit_of_work.connector_receipts.get(
                current.connector_binding_id,
                current.idempotency_key,
            )
            factory = AuditEventFactory(self._dispatch_audit_context(lease.owner, current))
            if receipt is not None:
                if (
                    receipt.external_action_id != current.id
                    or receipt.connector_binding_id != current.connector_binding_id
                    or receipt.idempotency_key != current.idempotency_key
                    or receipt.action_hash != current.action_hash
                    or receipt.capability_id != current.envelope.capability_id
                ):
                    raise ExternalActionDispatchError(
                        "receipt_identity_corrupt",
                        "durable connector receipt does not match the exact action",
                    )
                result = ExternalActionResultSnapshot(
                    receipt_id=receipt.receipt_id,
                    status=receipt.status,
                    safe_metadata=_bounded_output_projection(
                        receipt.safe_metadata,
                        step.runtime_policy.budget.max_output_bytes,
                    ),
                    completed_at=now,
                )
                completed = await unit_of_work.external_actions.complete_succeeded(
                    action_id=current.id,
                    expected_version=current.version,
                    lease_owner=lease.owner,
                    attempt_number=lease.attempt_number,
                    result=result,
                )
                if completed is not None:
                    step_transition = transition_step(
                        step,
                        StepLifecycleCommand.SUCCEED,
                        NoStepTransitionContext(),
                        now,
                    )
                    if not await unit_of_work.run_steps.apply_transition(
                        expected_run_version=run.version,
                        expected_run_state=run.state,
                        expected_version=step.version,
                        expected_state=step.state,
                        result=step_transition,
                    ):
                        raise ExternalActionDispatchError(
                            "terminal_call_step_conflict",
                            "terminal WRITE step changed before receipt reconciliation",
                        )
                    await unit_of_work.audits.append_many(
                        (
                            factory.action_receipt_reconciled(current, completed),
                            factory.step_transition(
                                step_transition.step,
                                step_transition.transition,
                            ),
                        )
                    )
                    await unit_of_work.commit()
                    return ExternalActionDispatchResult(
                        completed,
                        DispatchDisposition.SUCCEEDED,
                    )
            else:
                completed = await unit_of_work.external_actions.mark_outcome_unknown(
                    action_id=current.id,
                    expected_version=current.version,
                    lease_owner=lease.owner,
                    attempt_number=lease.attempt_number,
                    reason_code=reason_code,
                    occurred_at=now,
                )
                if completed is not None:
                    step_transition = transition_step(
                        step,
                        StepLifecycleCommand.FAIL,
                        StepTerminalContext(reason_code),
                        now,
                    )
                    if not await unit_of_work.run_steps.apply_transition(
                        expected_run_version=run.version,
                        expected_run_state=run.state,
                        expected_version=step.version,
                        expected_state=step.state,
                        result=step_transition,
                    ):
                        raise ExternalActionDispatchError(
                            "terminal_call_step_conflict",
                            "terminal WRITE step changed before outcome closure",
                        )
                    await unit_of_work.audits.append_many(
                        (
                            factory.action_outcome_unknown(current, completed),
                            factory.step_transition(
                                step_transition.step,
                                step_transition.transition,
                            ),
                        )
                    )
                    await unit_of_work.commit()
                    return ExternalActionDispatchResult(
                        completed,
                        DispatchDisposition.OUTCOME_UNKNOWN,
                    )
        latest = await self._load_required(snapshot.id)
        return _terminal_dispatch_result(latest)

    async def _reconcile_durable_receipt(
        self,
        action: ExternalAction,
        *,
        lease_owner: str,
    ) -> ExternalActionDispatchResult | None:
        """Complete from exact durable connector evidence without another provider call."""

        async with self._dependencies.unit_of_work() as unit_of_work:
            step = await _sealed_write_step(unit_of_work, action)
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
                safe_metadata=_bounded_output_projection(
                    receipt.safe_metadata,
                    step.runtime_policy.budget.max_output_bytes,
                ),
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


def _safe_audit_retry_after(value: int | None) -> int | None:
    if type(value) is int and 1 <= value <= 3_600:
        return value
    return None


async def _sealed_write_step(unit_of_work: UnitOfWork, action: ExternalAction) -> RunStep:
    try:
        steps = await unit_of_work.run_steps.validate_plan_for_execution(action.run_id)
    except RuntimeError as exc:
        raise ExternalActionDispatchError(
            "execution_plan_invalid",
            "external action output cannot validate its sealed execution plan",
        ) from exc
    step = next((item for item in steps if item.id == action.step_id), None)
    if (
        step is None
        or step.run_id != action.run_id
        or step.plan_hash != action.envelope.plan_hash
        or step.key != action.envelope.step_key
        or step.effect is not Effect.WRITE
    ):
        raise ExternalActionDispatchError(
            "execution_plan_mismatch",
            "external action output differs from its sealed WRITE step",
        )
    return step


def _bounded_output_projection(
    safe_metadata: object,
    max_output_bytes: int,
) -> Mapping[str, object]:
    """Retain metadata only when its canonical projection fits the sealed limit."""

    if isinstance(safe_metadata, Mapping):
        try:
            if canonical_payload_size_bytes(safe_metadata) <= max_output_bytes:
                return safe_metadata
        except Exception:
            # Connector result objects are outside the trust boundary. Treat an
            # uncanonicalizable projection exactly like an oversized projection
            # and never expose provider data through the error path.
            pass
    if canonical_payload_size_bytes(_OUTPUT_PROJECTION_OMITTED) <= max_output_bytes:
        return _OUTPUT_PROJECTION_OMITTED
    # Empty metadata is the domain's representation of no retained output
    # payload. Receipt identity and status remain exact control-plane evidence.
    return {}


def _terminal_dispatch_result(action: ExternalAction) -> ExternalActionDispatchResult:
    if action.state is ExternalActionState.SUCCEEDED:
        disposition = DispatchDisposition.ALREADY_SUCCEEDED
    elif action.state is ExternalActionState.OUTCOME_UNKNOWN:
        disposition = DispatchDisposition.OUTCOME_UNKNOWN
    else:
        disposition = DispatchDisposition.LOST_CLAIM
    return ExternalActionDispatchResult(action, disposition)
