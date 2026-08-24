"""All-or-none approval-boundary composition with zero inline connector calls."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Never

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.policies.approval_authorization import (
    APPROVAL_DECIDE_SCOPE,
    APPROVER_ROLE,
)
from marketing_agents.application.ports.repositories import (
    ApprovalRepositoryConflict,
    ApprovalSetReleaseMember,
    AuthorizationSetCloseCommand,
    AuthorizationSetCloseResult,
    AuthorizationSetReleaseCommand,
    AuthorizationSetReleaseResult,
    CurrentAuthorizationSet,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.approval import (
    ApprovalUse,
    AuthorizationSetStatus,
    StoredActionApprovalRequest,
)
from marketing_agents.domain.audit import AuditContext, AuditEvent, AuditEventDraft
from marketing_agents.domain.entities import (
    ActionReservationSnapshot,
    ExternalAction,
    Run,
    RunStep,
)
from marketing_agents.domain.enums import (
    ApprovalStatus,
    ExternalActionState,
    RunState,
    StepState,
)
from marketing_agents.domain.run_lifecycle import (
    ApprovalBarrierContext,
    ApprovalRejectionContext,
    CancellationContext,
    RunLifecycleCommand,
    RunStateTransition,
    RunTransitionResult,
    transition_run,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepStateTransition,
    StepTerminalContext,
    StepTransitionResult,
    transition_step,
)

from .approval_records import (
    ApprovalRecordService,
    ApprovalRecordServiceError,
    require_complete_member_history,
)
from .audit_events import AuditEventFactory

_CANCEL_ACTOR_DOMAIN = b"marketing-agents:execution-cancel-actor:v1\x00"


def _cancellation_actor_digest(audit_context: AuditContext) -> str:
    return hashlib.sha256(_CANCEL_ACTOR_DOMAIN + audit_context.actor_id.encode("utf-8")).hexdigest()


class ApprovalBoundaryServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id


class ApprovalBoundaryDisposition(StrEnum):
    AWAITING = "awaiting"
    EXPIRED = "expired"
    RELEASED = "released"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ApprovalBoundaryResult:
    disposition: ApprovalBoundaryDisposition
    run: Run
    authorization_set_id: str
    release: AuthorizationSetReleaseResult | None = None
    closure: AuthorizationSetCloseResult | None = None
    expired_request_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _BoundarySnapshot:
    selection: CurrentAuthorizationSet
    run: Run
    requests: tuple[StoredActionApprovalRequest, ...]
    request_history: tuple[StoredActionApprovalRequest, ...]
    actions: tuple[ExternalAction, ...]
    member_steps: tuple[RunStep, ...]
    plan_steps: tuple[RunStep, ...]


class ApprovalBoundaryService:
    """Derive the current complete set and persist one all-or-none boundary outcome."""

    _MAX_STALE_CONTROL_RETRIES = 3

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies
        self._records = ApprovalRecordService(dependencies)

    async def evaluate(
        self,
        run_id: str,
        *,
        audit_context: AuditContext,
    ) -> ApprovalBoundaryResult:
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self.evaluate_in_uow(
                unit_of_work,
                run_id,
                audit_context=audit_context,
            )
            await unit_of_work.commit()
            return result

    async def evaluate_in_uow(
        self,
        unit_of_work: UnitOfWork,
        run_id: str,
        *,
        audit_context: AuditContext,
    ) -> ApprovalBoundaryResult:
        snapshot = await self._load_current(unit_of_work, run_id)
        authorization_set = snapshot.selection.authorization_set
        if authorization_set.status is AuthorizationSetStatus.RELEASED:
            await self._require_released_replay(unit_of_work, snapshot)
            return ApprovalBoundaryResult(
                (
                    ApprovalBoundaryDisposition.CANCELLED
                    if snapshot.run.state is RunState.CANCELLED
                    else ApprovalBoundaryDisposition.RELEASED
                ),
                snapshot.run,
                authorization_set.id,
            )
        if authorization_set.status is AuthorizationSetStatus.REJECTED:
            await self._require_closed_replay(unit_of_work, snapshot)
            return ApprovalBoundaryResult(
                ApprovalBoundaryDisposition.REJECTED,
                snapshot.run,
                authorization_set.id,
            )
        if authorization_set.status is AuthorizationSetStatus.CANCELLED:
            await self._require_closed_replay(unit_of_work, snapshot)
            return ApprovalBoundaryResult(
                ApprovalBoundaryDisposition.CANCELLED,
                snapshot.run,
                authorization_set.id,
            )
        if authorization_set.status is not AuthorizationSetStatus.OPEN:
            raise ApprovalBoundaryServiceError(
                "authorization_set_not_actionable",
                "current authorization set cannot cross the approval boundary",
                run_id=run_id,
            )
        if snapshot.run.state is not RunState.AWAITING_APPROVAL:
            raise ApprovalBoundaryServiceError(
                "run_not_awaiting_approval",
                "open authorization set requires an awaiting-approval Run",
                run_id=run_id,
            )
        await self._require_complete_history(unit_of_work, snapshot)

        rejected = tuple(
            stored for stored in snapshot.requests if stored.status is ApprovalStatus.REJECTED
        )
        if rejected:
            return await self._reject_in_uow(
                unit_of_work,
                snapshot,
                audit_context=audit_context,
            )

        now = self._dependencies.utc_now()
        stale = tuple(
            stored
            for stored in snapshot.requests
            if stored.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
            and now >= stored.request.expires_at
        )
        if stale:
            newly_expired_by_action: dict[str, StoredActionApprovalRequest] = {}
            for stored in stale:
                try:
                    expired = await self._records.mark_expired_in_uow(
                        unit_of_work,
                        request_id=stored.request.id,
                        expected_version=stored.version,
                        audit_context=audit_context,
                        expired_at=now,
                    )
                except ApprovalRecordServiceError as exc:
                    raise ApprovalBoundaryServiceError(
                        exc.code,
                        "approval expiry could not be persisted at the boundary",
                        run_id=run_id,
                    ) from None
                newly_expired_by_action[expired.request.action_id] = expired
            current_expired_ids = tuple(
                current.request.id
                for stored in snapshot.requests
                for current in (newly_expired_by_action.get(stored.request.action_id, stored),)
                if current.status is ApprovalStatus.EXPIRED
            )
            return ApprovalBoundaryResult(
                ApprovalBoundaryDisposition.EXPIRED,
                snapshot.run,
                authorization_set.id,
                expired_request_ids=current_expired_ids,
            )

        already_expired = tuple(
            (stored, action)
            for stored, action in zip(
                snapshot.requests,
                snapshot.actions,
                strict=True,
            )
            if stored.status is ApprovalStatus.EXPIRED
        )
        if already_expired:
            for stored, action in already_expired:
                if action.state is not ExternalActionState.AWAITING_APPROVAL:
                    self._replay_error(
                        run_id,
                        "authorization_expiry_action_mismatch",
                        "open expired approval no longer binds its awaiting action",
                    )
                await self._require_expiry_audit(
                    unit_of_work,
                    stored,
                    action,
                    action_version=action.version,
                )
            return ApprovalBoundaryResult(
                ApprovalBoundaryDisposition.EXPIRED,
                snapshot.run,
                authorization_set.id,
                expired_request_ids=tuple(stored.request.id for stored, _ in already_expired),
            )

        if any(stored.status is not ApprovalStatus.APPROVED for stored in snapshot.requests):
            return ApprovalBoundaryResult(
                ApprovalBoundaryDisposition.AWAITING,
                snapshot.run,
                authorization_set.id,
            )
        return await self._release_in_uow(
            unit_of_work,
            snapshot,
            released_at=now,
            audit_context=audit_context,
        )

    async def cancel(
        self,
        run_id: str,
        *,
        audit_context: AuditContext,
    ) -> ApprovalBoundaryResult:
        for retry_index in range(self._MAX_STALE_CONTROL_RETRIES):
            try:
                return await self._cancel_once(
                    run_id,
                    audit_context=audit_context,
                )
            except ApprovalBoundaryServiceError as exc:
                if (
                    exc.code != "stale_execution_control"
                    or retry_index == self._MAX_STALE_CONTROL_RETRIES - 1
                ):
                    raise
        raise AssertionError("bounded boundary cancellation retry exhausted without an outcome")

    async def _cancel_once(
        self,
        run_id: str,
        *,
        audit_context: AuditContext,
    ) -> ApprovalBoundaryResult:
        async with self._dependencies.unit_of_work() as unit_of_work:
            snapshot = await self._load_current(unit_of_work, run_id)
            authorization_set = snapshot.selection.authorization_set
            if authorization_set.status is AuthorizationSetStatus.RELEASED:
                await self._require_released_replay(unit_of_work, snapshot)
                return await self._cancel_released_in_uow(
                    unit_of_work,
                    snapshot,
                    audit_context=audit_context,
                )
            if authorization_set.status is not AuthorizationSetStatus.OPEN:
                raise ApprovalBoundaryServiceError(
                    "authorization_set_already_terminal",
                    "only an unreleased authorization set can be cancelled atomically",
                    run_id=run_id,
                )
            if snapshot.run.state is not RunState.AWAITING_APPROVAL:
                raise ApprovalBoundaryServiceError(
                    "run_not_awaiting_approval",
                    "pre-release cancellation requires an awaiting-approval Run",
                    run_id=run_id,
                )
            await self._require_complete_history(unit_of_work, snapshot)
            closed_at = self._dependencies.utc_now()
            control = await unit_of_work.execution_control.get(run_id)
            if (
                control is None
                or control.policy_hash != authorization_set.plan_hash
                or control.started_at is not None
            ):
                raise ApprovalBoundaryServiceError(
                    "execution_control_invalid",
                    "pre-release cancellation lacks its exact unstarted execution control",
                    run_id=run_id,
                )
            try:
                await unit_of_work.execution_control.request_cancel(
                    run_id=run_id,
                    expected_control_version=control.version,
                    actor_digest=_cancellation_actor_digest(audit_context),
                    requested_at=closed_at,
                )
            except RuntimeError as exc:
                raise ApprovalBoundaryServiceError(
                    getattr(exc, "code", "cancellation_conflict"),
                    "pre-release cancellation fence could not be persisted",
                    run_id=run_id,
                ) from exc
            run_transition = transition_run(
                snapshot.run,
                RunLifecycleCommand.CANCEL,
                CancellationContext(reason_code="operator_cancelled"),
                closed_at,
            )
            step_transitions = self._terminal_step_transitions(
                snapshot,
                rejected_action_ids=frozenset(),
                occurred_at=closed_at,
                cancelling=True,
            )
            command = AuthorizationSetCloseCommand(
                authorization_set=snapshot.selection.authorization_set,
                head=snapshot.selection.head,
                status=AuthorizationSetStatus.CANCELLED,
                run_transition=run_transition,
                actions=snapshot.actions,
                requests=snapshot.requests,
                step_transitions=step_transitions,
                closed_at=closed_at,
            )
            try:
                closed = await unit_of_work.approvals.close_current_set(command)
            except ApprovalRepositoryConflict as exc:
                raise ApprovalBoundaryServiceError(
                    exc.code,
                    "authorization set changed before cancellation committed",
                    run_id=run_id,
                ) from None
            await self._append_closure_audits(
                unit_of_work,
                snapshot,
                closed,
                run_transition,
                step_transitions,
                audit_context=audit_context,
            )
            await unit_of_work.commit()
            return ApprovalBoundaryResult(
                ApprovalBoundaryDisposition.CANCELLED,
                closed.run,
                closed.authorization_set.id,
                closure=closed,
            )

    async def _cancel_released_in_uow(
        self,
        unit_of_work: UnitOfWork,
        snapshot: _BoundarySnapshot,
        *,
        audit_context: AuditContext,
    ) -> ApprovalBoundaryResult:
        """Fence future dispatch after release without pretending in-flight calls stop."""

        authorization_set = snapshot.selection.authorization_set
        if (
            snapshot.run.state is not RunState.EXECUTING
            or authorization_set.released_run_version != snapshot.run.version
        ):
            raise ApprovalBoundaryServiceError(
                "released_run_not_cancellable",
                "post-release cancellation requires the exact unmodified released Run",
                run_id=snapshot.run.id,
            )
        occurred_at = self._dependencies.utc_now()
        control = await unit_of_work.execution_control.get(snapshot.run.id)
        if (
            control is None
            or control.policy_hash != authorization_set.plan_hash
            or control.started_at != authorization_set.released_at
        ):
            raise ApprovalBoundaryServiceError(
                "execution_control_invalid",
                "post-release cancellation lacks its exact started execution control",
                run_id=snapshot.run.id,
            )
        try:
            await unit_of_work.execution_control.request_cancel(
                run_id=snapshot.run.id,
                expected_control_version=control.version,
                actor_digest=_cancellation_actor_digest(audit_context),
                requested_at=occurred_at,
            )
        except RuntimeError as exc:
            raise ApprovalBoundaryServiceError(
                getattr(exc, "code", "cancellation_conflict"),
                "post-release cancellation fence could not be persisted",
                run_id=snapshot.run.id,
            ) from exc
        cancellable_actions = tuple(
            action
            for action in snapshot.actions
            if action.state
            in {
                ExternalActionState.DISPATCH_RESERVED,
                ExternalActionState.DISPATCHING,
            }
            and action.call_started_at is None
        )
        cancelled_actions: list[tuple[ExternalAction, ExternalAction]] = []
        for action in cancellable_actions:
            cancelled = await unit_of_work.external_actions.cancel_unstarted_after_release(
                action_id=action.id,
                run_id=snapshot.run.id,
                plan_hash=authorization_set.plan_hash,
                expected_version=action.version,
                occurred_at=occurred_at,
            )
            if cancelled is None:
                raise ApprovalBoundaryServiceError(
                    "action_cancellation_conflict",
                    "released action changed before its pre-call cancellation committed",
                    run_id=snapshot.run.id,
                )
            cancelled_actions.append((action, cancelled))

        cancellable_steps = tuple(
            step
            for step in snapshot.plan_steps
            if step.state in {StepState.PENDING, StepState.READY}
        )
        step_results = tuple(
            transition_step(
                step,
                StepLifecycleCommand.CANCEL,
                StepTerminalContext("run_cancelled"),
                occurred_at,
            )
            for step in cancellable_steps
        )
        for result in step_results:
            applied_step = await unit_of_work.run_steps.apply_transition(
                expected_run_version=snapshot.run.version,
                expected_run_state=RunState.EXECUTING,
                expected_version=result.transition.expected_version,
                expected_state=result.transition.previous_state or StepState.PENDING,
                result=result,
            )
            if not applied_step:
                raise ApprovalBoundaryServiceError(
                    "step_cancellation_conflict",
                    "queued step changed before cancellation committed",
                    run_id=snapshot.run.id,
                )
        completed_effect_count = sum(
            action.state is ExternalActionState.SUCCEEDED for action in snapshot.actions
        )
        outcome_unknown_effect_count = sum(
            action.state is ExternalActionState.OUTCOME_UNKNOWN for action in snapshot.actions
        )
        transition = transition_run(
            snapshot.run,
            RunLifecycleCommand.CANCEL,
            CancellationContext(
                reason_code="operator_cancelled",
                completed_effect_count=completed_effect_count,
                outcome_unknown_effect_count=outcome_unknown_effect_count,
            ),
            occurred_at,
        )
        applied = await unit_of_work.runs.apply_transition(
            expected_version=snapshot.run.version,
            expected_state=RunState.EXECUTING,
            result=transition,
        )
        if not applied:
            raise ApprovalBoundaryServiceError(
                "run_transition_conflict",
                "released Run changed before cancellation committed",
                run_id=snapshot.run.id,
            )
        factory = AuditEventFactory(audit_context)
        await unit_of_work.audits.append_many(
            (
                *(
                    factory.action_runtime_cancelled(previous, cancelled)
                    for previous, cancelled in cancelled_actions
                ),
                *(
                    factory.step_transition(result.step, result.transition)
                    for result in step_results
                ),
                factory.run_transition(transition.run, transition.transition),
            )
        )
        await unit_of_work.commit()
        return ApprovalBoundaryResult(
            ApprovalBoundaryDisposition.CANCELLED,
            transition.run,
            authorization_set.id,
        )

    async def _release_in_uow(
        self,
        unit_of_work: UnitOfWork,
        snapshot: _BoundarySnapshot,
        *,
        released_at: datetime,
        audit_context: AuditContext,
    ) -> ApprovalBoundaryResult:
        authorization_set = snapshot.selection.authorization_set
        request_by_action = {stored.request.action_id: stored for stored in snapshot.requests}
        action_by_id = {action.id: action for action in snapshot.actions}
        step_by_id = {step.id: step for step in snapshot.member_steps}
        action_hashes = tuple(member.action_hash for member in authorization_set.members)
        expires_at_by_hash = {
            member.action_hash: request_by_action[member.action_id].request.expires_at
            for member in authorization_set.members
        }
        run_transition = transition_run(
            snapshot.run,
            RunLifecycleCommand.RELEASE_APPROVED_PLAN,
            ApprovalBarrierContext(
                required_action_hashes=action_hashes,
                current_action_hashes=action_hashes,
                approved_action_hashes=action_hashes,
                expires_at_by_hash=expires_at_by_hash,
            ),
            released_at,
        )
        members: list[ApprovalSetReleaseMember] = []
        for member in authorization_set.members:
            stored = request_by_action[member.action_id]
            decision = stored.decision
            action = action_by_id[member.action_id]
            step = step_by_id[member.step_id]
            if decision is None:
                raise ApprovalBoundaryServiceError(
                    "approval_decision_missing",
                    "approved current leaf lacks its immutable decision",
                    run_id=snapshot.run.id,
                )
            await self._require_release_decision_witnesses(
                unit_of_work,
                stored,
                action,
            )
            step_transition = transition_step(
                step,
                StepLifecycleCommand.RELEASE_APPROVAL,
                NoStepTransitionContext(),
                released_at,
            )
            reservation_id = self._dependencies.new_id("action-reservation")
            use = ApprovalUse(
                id=self._dependencies.new_id("approval-use"),
                request_id=stored.request.id,
                decision_id=decision.id,
                action_id=action.id,
                action_hash=action.action_hash,
                authorization_set_id=authorization_set.id,
                run_id=action.run_id,
                plan_hash=action.envelope.plan_hash,
                proposal_revision=action.envelope.proposal_revision,
                step_id=action.step_id,
                step_key=action.envelope.step_key,
                reservation_id=reservation_id,
                used_at=released_at,
            )
            reservation = ActionReservationSnapshot(
                reservation_id=reservation_id,
                authorization_set_id=authorization_set.id,
                approval_request_id=stored.request.id,
                approval_decision_id=decision.id,
                action_hash=action.action_hash,
                capability_id=action.envelope.capability_id,
                binding_id=action.envelope.binding_id,
                idempotency_key=action.idempotency_key,
                reserved_at=released_at,
            )
            members.append(
                ApprovalSetReleaseMember(
                    request=stored,
                    action=action,
                    step_transition=step_transition,
                    use=use,
                    reservation=reservation,
                )
            )
        command = AuthorizationSetReleaseCommand(
            authorization_set=authorization_set,
            head=snapshot.selection.head,
            run_transition=run_transition,
            members=tuple(members),
            released_at=released_at,
        )
        try:
            released = await unit_of_work.approvals.release_current_set(command)
        except ApprovalRepositoryConflict as exc:
            raise ApprovalBoundaryServiceError(
                exc.code,
                f"authorization set changed before the complete barrier committed ({exc.code})",
                run_id=snapshot.run.id,
            ) from None
        control = await unit_of_work.execution_control.get(snapshot.run.id)
        if control is None or control.policy_hash != authorization_set.plan_hash:
            raise ApprovalBoundaryServiceError(
                "execution_control_invalid",
                "approval release lacks its exact sealed execution control",
                run_id=snapshot.run.id,
            )
        try:
            await unit_of_work.execution_control.start_execution(
                run_id=snapshot.run.id,
                expected_control_version=control.version,
                started_at=released_at,
            )
        except RuntimeError as exc:
            raise ApprovalBoundaryServiceError(
                getattr(exc, "code", "execution_start_conflict"),
                "Run execution deadline could not start atomically with approval release",
                run_id=snapshot.run.id,
            ) from exc
        await self._append_release_audits(
            unit_of_work,
            snapshot,
            released,
            run_transition,
            tuple(member.step_transition for member in members),
            audit_context=audit_context,
        )
        return ApprovalBoundaryResult(
            ApprovalBoundaryDisposition.RELEASED,
            released.run,
            released.authorization_set.id,
            release=released,
        )

    async def _reject_in_uow(
        self,
        unit_of_work: UnitOfWork,
        snapshot: _BoundarySnapshot,
        *,
        audit_context: AuditContext,
    ) -> ApprovalBoundaryResult:
        closed_at = self._dependencies.utc_now()
        required_hashes = tuple(
            member.action_hash for member in snapshot.selection.authorization_set.members
        )
        rejected_hashes = tuple(
            stored.request.action_hash
            for stored in snapshot.requests
            if stored.status is ApprovalStatus.REJECTED
        )
        run_transition = transition_run(
            snapshot.run,
            RunLifecycleCommand.REJECT_APPROVAL,
            ApprovalRejectionContext(
                required_action_hashes=required_hashes,
                rejected_action_hashes=rejected_hashes,
            ),
            closed_at,
        )
        rejected_action_ids = frozenset(
            stored.request.action_id
            for stored in snapshot.requests
            if stored.status is ApprovalStatus.REJECTED
        )
        step_transitions = self._terminal_step_transitions(
            snapshot,
            rejected_action_ids=rejected_action_ids,
            occurred_at=closed_at,
            cancelling=False,
        )
        command = AuthorizationSetCloseCommand(
            authorization_set=snapshot.selection.authorization_set,
            head=snapshot.selection.head,
            status=AuthorizationSetStatus.REJECTED,
            run_transition=run_transition,
            actions=snapshot.actions,
            requests=snapshot.requests,
            step_transitions=step_transitions,
            closed_at=closed_at,
        )
        try:
            closed = await unit_of_work.approvals.close_current_set(command)
        except ApprovalRepositoryConflict as exc:
            raise ApprovalBoundaryServiceError(
                exc.code,
                "authorization set changed before rejection committed",
                run_id=snapshot.run.id,
            ) from None
        await self._append_closure_audits(
            unit_of_work,
            snapshot,
            closed,
            run_transition,
            step_transitions,
            audit_context=audit_context,
        )
        return ApprovalBoundaryResult(
            ApprovalBoundaryDisposition.REJECTED,
            closed.run,
            closed.authorization_set.id,
            closure=closed,
        )

    async def _require_complete_history(
        self,
        unit_of_work: UnitOfWork,
        snapshot: _BoundarySnapshot,
    ) -> None:
        try:
            await require_complete_member_history(
                unit_of_work,
                snapshot.request_history,
                snapshot.actions,
            )
        except (ApprovalRecordServiceError, RuntimeError):
            self._replay_error(
                snapshot.run.id,
                "authorization_member_history_invalid",
                "authorization member history lacks an exact audit witness",
            )

    async def _require_released_replay(
        self,
        unit_of_work: UnitOfWork,
        snapshot: _BoundarySnapshot,
    ) -> None:
        """Require every immutable member witness before accepting a released-set replay."""

        await self._require_complete_history(unit_of_work, snapshot)
        authorization_set = snapshot.selection.authorization_set
        if (
            authorization_set.released_at is None
            or authorization_set.released_run_version is None
            or authorization_set.release_hash is None
            or snapshot.run.version < authorization_set.released_run_version
            or snapshot.run.state
            not in {
                RunState.EXECUTING,
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLED,
            }
        ):
            self._replay_error(
                snapshot.run.id,
                "authorization_release_replay_mismatch",
                "released authorization set no longer binds its parent Run",
            )
        control = await unit_of_work.execution_control.get(snapshot.run.id)
        if (
            control is None
            or control.policy_hash != authorization_set.plan_hash
            or control.started_at != authorization_set.released_at
            or control.deadline_at is None
        ):
            self._replay_error(
                snapshot.run.id,
                "authorization_release_control_mismatch",
                "released authorization set lacks its exact execution deadline witness",
            )
        run_history = await unit_of_work.runs.list_transitions(snapshot.run.id)
        release_transitions = tuple(
            transition
            for transition in run_history
            if transition.command is RunLifecycleCommand.RELEASE_APPROVED_PLAN
        )
        if len(release_transitions) != 1:
            self._replay_error(
                snapshot.run.id,
                "authorization_release_transition_missing",
                "released authorization set lacks one parent Run transition",
            )
        release_transition = release_transitions[0]
        if (
            release_transition.resulting_version != authorization_set.released_run_version
            or release_transition.occurred_at != authorization_set.released_at
            or release_transition.previous_state is not RunState.AWAITING_APPROVAL
            or release_transition.new_state is not RunState.EXECUTING
            or release_transition.reason_code != "approval_barrier_satisfied"
        ):
            self._replay_error(
                snapshot.run.id,
                "authorization_release_transition_mismatch",
                "released authorization set Run transition is not authoritative",
            )
        await self._require_run_transition_audit(
            unit_of_work,
            release_transition,
            run_id=snapshot.run.id,
        )
        if snapshot.run.state is RunState.CANCELLED:
            cancellations = tuple(
                transition
                for transition in run_history
                if transition.command is RunLifecycleCommand.CANCEL
            )
            cancellation = cancellations[0] if len(cancellations) == 1 else None
            if (
                cancellation is None
                or cancellation.expected_version != authorization_set.released_run_version
                or cancellation.resulting_version != snapshot.run.version
                or cancellation.resulting_version != authorization_set.released_run_version + 1
                or cancellation.previous_state is not RunState.EXECUTING
                or cancellation.new_state is not RunState.CANCELLED
                or cancellation.occurred_at != snapshot.run.updated_at
                or cancellation.reason_code != "operator_cancelled"
            ):
                self._replay_error(
                    snapshot.run.id,
                    "post_release_cancel_audit_missing",
                    "cancelled released Run lacks its one terminal transition",
                )
            assert cancellation is not None
            if control.cancel_requested_at != cancellation.occurred_at:
                self._replay_error(
                    snapshot.run.id,
                    "post_release_cancel_control_missing",
                    "cancelled released Run lacks its exact durable cancellation fence",
                )
            await self._require_run_transition_audit(
                unit_of_work,
                cancellation,
                run_id=snapshot.run.id,
            )

        request_by_action = {stored.request.action_id: stored for stored in snapshot.requests}
        action_by_id = {action.id: action for action in snapshot.actions}
        step_by_id = {step.id: step for step in snapshot.member_steps}
        if snapshot.run.state is RunState.CANCELLED:
            cancelled_at = control.cancel_requested_at
            assert cancelled_at is not None
            for plan_step in snapshot.plan_steps:
                if plan_step.state in {StepState.PENDING, StepState.READY}:
                    self._replay_error(
                        snapshot.run.id,
                        "post_release_cancel_step_incomplete",
                        "cancelled released Run retains queued step work",
                    )
                if plan_step.state is StepState.CANCELLED:
                    step_cancellations = tuple(
                        transition
                        for transition in await unit_of_work.run_steps.list_transitions(
                            plan_step.id
                        )
                        if transition.command is StepLifecycleCommand.CANCEL
                        and transition.occurred_at == cancelled_at
                    )
                    if len(step_cancellations) != 1:
                        self._replay_error(
                            snapshot.run.id,
                            "post_release_cancel_step_audit_missing",
                            "cancelled queued step lacks its exact transition witness",
                        )
                    await self._require_step_transition_audit(
                        unit_of_work,
                        step_cancellations[0],
                        step=plan_step,
                    )
            for action in snapshot.actions:
                if action.state is ExternalActionState.DISPATCH_RESERVED or (
                    action.state is ExternalActionState.DISPATCHING
                    and action.call_started_at is None
                ):
                    self._replay_error(
                        snapshot.run.id,
                        "post_release_cancel_action_incomplete",
                        "cancelled released Run retains unstarted connector work",
                    )
                if action.state is ExternalActionState.CANCELLED:
                    event = await unit_of_work.audits.get_mutation_event(
                        "external_action",
                        action.id,
                        action.version,
                    )
                    previous_state = None if event is None else event.previous_state
                    if previous_state not in {
                        ExternalActionState.DISPATCH_RESERVED.value,
                        ExternalActionState.DISPATCHING.value,
                    }:
                        self._replay_error(
                            snapshot.run.id,
                            "post_release_cancel_action_audit_missing",
                            "cancelled released action lacks its exact pre-call witness",
                        )
                    reservation = action.reservation
                    if reservation is None or action.updated_at != cancelled_at:
                        self._replay_error(
                            snapshot.run.id,
                            "post_release_cancel_action_mismatch",
                            "cancelled released action lost its reservation or cancellation time",
                        )
                    self._require_bound_event(
                        event,
                        event_type="action.cancelled",
                        run_id=snapshot.run.id,
                        aggregate_id=action.id,
                        mutation_version=action.version,
                        occurred_at=cancelled_at,
                        previous_state=previous_state,
                        new_state=ExternalActionState.CANCELLED.value,
                        reason_code="operator_cancelled",
                        action_id=action.id,
                        step_id=action.step_id,
                        approval_request_id=reservation.approval_request_id,
                        approval_decision_id=reservation.approval_decision_id,
                        metadata_bindings={
                            "approval_set_id": authorization_set.id,
                            "approval_status": "released",
                            "closure_reason": "operator_cancelled",
                        },
                    )
                    expected_attempt = (
                        action.delivery_attempt_count
                        if previous_state == ExternalActionState.DISPATCHING.value
                        else None
                    )
                    if event is None or event.action_attempt_number != expected_attempt:
                        self._replay_error(
                            snapshot.run.id,
                            "post_release_cancel_action_attempt_mismatch",
                            "cancelled released action audit lost its dispatch attempt identity",
                        )
        for member in authorization_set.members:
            stored = request_by_action[member.action_id]
            action = action_by_id[member.action_id]
            step = step_by_id[member.step_id]
            use = stored.use
            reservation = action.reservation
            decision = stored.decision
            if (
                stored.status is not ApprovalStatus.CONSUMED
                or stored.version != 3
                or use is None
                or reservation is None
                or decision is None
                or use.authorization_set_id != authorization_set.id
                or reservation.authorization_set_id != authorization_set.id
                or use.reservation_id != reservation.reservation_id
                or use.used_at != authorization_set.released_at
                or reservation.reserved_at != authorization_set.released_at
            ):
                self._replay_error(
                    snapshot.run.id,
                    "authorization_release_member_mismatch",
                    "released authorization member lost its one-time use or reservation",
                )
            approval_event = await unit_of_work.audits.get_mutation_event(
                "approval_request",
                stored.request.id,
                stored.version,
            )
            self._require_bound_event(
                approval_event,
                event_type="approval.consumed",
                run_id=snapshot.run.id,
                aggregate_id=stored.request.id,
                mutation_version=stored.version,
                occurred_at=use.used_at,
                previous_state=ApprovalStatus.APPROVED.value,
                new_state=ApprovalStatus.CONSUMED.value,
                reason_code="approval_consumed",
                action_id=action.id,
                step_id=step.id,
                approval_request_id=stored.request.id,
                approval_decision_id=decision.id,
                metadata_bindings={
                    "approval_use_id": use.id,
                    "approval_set_id": authorization_set.id,
                    "reservation_id": reservation.reservation_id,
                },
            )
            action_events: list[AuditEvent] = []
            for version in range(2, action.version + 1):
                candidate = await unit_of_work.audits.get_mutation_event(
                    "external_action",
                    action.id,
                    version,
                )
                if candidate is not None and candidate.event_type == "action.dispatch_reserved":
                    action_events.append(candidate)
            if len(action_events) != 1:
                self._replay_error(
                    snapshot.run.id,
                    "authorization_release_action_audit_missing",
                    "released action lacks one dispatch-reservation witness",
                )
            action_event = action_events[0]
            self._require_bound_event(
                action_event,
                event_type="action.dispatch_reserved",
                run_id=snapshot.run.id,
                aggregate_id=action.id,
                mutation_version=action_event.mutation_version,
                occurred_at=reservation.reserved_at,
                previous_state=ExternalActionState.APPROVED.value,
                new_state=ExternalActionState.DISPATCH_RESERVED.value,
                reason_code="approval_consumed",
                action_id=action.id,
                step_id=step.id,
                approval_request_id=stored.request.id,
                approval_decision_id=decision.id,
                metadata_bindings={
                    "approval_use_id": use.id,
                    "approval_set_id": authorization_set.id,
                    "reservation_id": reservation.reservation_id,
                },
            )
            if action_event.mutation_version is None:
                self._replay_error(
                    snapshot.run.id,
                    "authorization_release_action_audit_mismatch",
                    "dispatch-reservation witness lacks its action version",
                )
            approved_request = replace(
                stored,
                status=ApprovalStatus.APPROVED,
                version=2,
                updated_at=decision.decided_at,
                use=None,
            )
            approved_action = replace(
                action,
                state=ExternalActionState.APPROVED,
                updated_at=decision.decided_at,
                version=action_event.mutation_version - 1,
                delivery_attempt_count=0,
                reservation=None,
                lease=None,
                call_started_at=None,
                call_deadline_at=None,
                result=None,
                terminal_reason_code=None,
            )
            await self._require_release_decision_witnesses(
                unit_of_work,
                approved_request,
                approved_action,
            )
            step_history = await unit_of_work.run_steps.list_transitions(step.id)
            releases = tuple(
                transition
                for transition in step_history
                if transition.command is StepLifecycleCommand.RELEASE_APPROVAL
            )
            if len(releases) != 1 or releases[0].occurred_at != authorization_set.released_at:
                self._replay_error(
                    snapshot.run.id,
                    "authorization_release_step_audit_missing",
                    "released write step lacks one approval-release transition",
                )
            await self._require_step_transition_audit(
                unit_of_work,
                releases[0],
                step=step,
            )

    async def _require_closed_replay(
        self,
        unit_of_work: UnitOfWork,
        snapshot: _BoundarySnapshot,
    ) -> None:
        await self._require_complete_history(unit_of_work, snapshot)
        authorization_set = snapshot.selection.authorization_set
        expected_run_state = (
            RunState.REJECTED
            if authorization_set.status is AuthorizationSetStatus.REJECTED
            else RunState.CANCELLED
        )
        expected_command = (
            RunLifecycleCommand.REJECT_APPROVAL
            if authorization_set.status is AuthorizationSetStatus.REJECTED
            else RunLifecycleCommand.CANCEL
        )
        if authorization_set.status is AuthorizationSetStatus.CANCELLED:
            control = await unit_of_work.execution_control.get(snapshot.run.id)
            if (
                control is None
                or control.policy_hash != authorization_set.plan_hash
                or control.started_at is not None
                or control.cancel_requested_at != authorization_set.updated_at
            ):
                self._replay_error(
                    snapshot.run.id,
                    "authorization_close_control_mismatch",
                    "cancelled approval set lacks its exact durable cancellation fence",
                )
        if (
            snapshot.run.state is not expected_run_state
            or snapshot.run.updated_at != authorization_set.updated_at
        ):
            self._replay_error(
                snapshot.run.id,
                "authorization_close_replay_mismatch",
                "closed authorization set no longer binds its terminal Run",
            )
        run_history = await unit_of_work.runs.list_transitions(snapshot.run.id)
        closures = tuple(
            transition for transition in run_history if transition.command is expected_command
        )
        closure = closures[0] if len(closures) == 1 else None
        if (
            closure is None
            or closure.resulting_version != snapshot.run.version
            or closure.previous_state is not RunState.AWAITING_APPROVAL
            or closure.new_state is not expected_run_state
            or closure.occurred_at != authorization_set.updated_at
            or closure.reason_code != authorization_set.terminal_reason_code
            or closure.completed_effect_count != 0
            or closure.outcome_unknown_effect_count != 0
        ):
            self._replay_error(
                snapshot.run.id,
                "authorization_close_transition_missing",
                "closed authorization set lacks one parent Run transition",
            )
        assert closure is not None
        await self._require_run_transition_audit(
            unit_of_work,
            closure,
            run_id=snapshot.run.id,
        )
        request_by_step = {stored.request.step_id: stored for stored in snapshot.requests}
        for step in snapshot.plan_steps:
            if step.state not in {
                StepState.REJECTED,
                StepState.CANCELLED,
                StepState.SKIPPED,
            }:
                self._replay_error(
                    snapshot.run.id,
                    "authorization_close_step_mismatch",
                    "closed authorization set left a mutable plan step",
                )
            history = await unit_of_work.run_steps.list_transitions(step.id)
            matching = tuple(
                transition for transition in history if transition.resulting_version == step.version
            )
            if len(matching) != 1:
                self._replay_error(
                    snapshot.run.id,
                    "authorization_close_step_audit_missing",
                    "closed plan step lacks its terminal transition",
                )
            transition = matching[0]
            stored = request_by_step.get(step.id)
            directly_rejected = (
                authorization_set.status is AuthorizationSetStatus.REJECTED
                and stored is not None
                and stored.status is ApprovalStatus.REJECTED
            )
            expected_step_command = (
                StepLifecycleCommand.REJECT if directly_rejected else StepLifecycleCommand.CANCEL
            )
            expected_step_reason = (
                "approval_rejected"
                if directly_rejected
                else (
                    "operator_cancelled"
                    if authorization_set.status is AuthorizationSetStatus.CANCELLED
                    else "sibling_approval_rejected"
                )
            )
            expected_step_state = StepState.REJECTED if directly_rejected else StepState.CANCELLED
            if (
                step.state is not expected_step_state
                or step.updated_at != authorization_set.updated_at
                or transition.command is not expected_step_command
                or transition.new_state is not expected_step_state
                or transition.reason_code != expected_step_reason
                or transition.occurred_at != authorization_set.updated_at
            ):
                self._replay_error(
                    snapshot.run.id,
                    "authorization_close_step_audit_mismatch",
                    "closed plan step transition differs from its boundary outcome",
                )
            await self._require_step_transition_audit(
                unit_of_work,
                transition,
                step=step,
            )
        for stored, action in zip(snapshot.requests, snapshot.actions, strict=True):
            decision = stored.decision
            if stored.status is ApprovalStatus.REJECTED:
                await self._require_release_decision_witnesses(
                    unit_of_work,
                    stored,
                    action,
                )
                continue
            if action.state is not ExternalActionState.CANCELLED:
                self._replay_error(
                    snapshot.run.id,
                    "authorization_close_action_mismatch",
                    "closed sibling action is not terminally cancelled",
                )
            action_event = await unit_of_work.audits.get_mutation_event(
                "external_action",
                action.id,
                action.version,
            )
            self._require_bound_event(
                action_event,
                event_type="action.cancelled",
                run_id=snapshot.run.id,
                aggregate_id=action.id,
                mutation_version=action.version,
                occurred_at=action.updated_at,
                previous_state=(
                    ExternalActionState.APPROVED.value
                    if stored.status is ApprovalStatus.SUPERSEDED and decision is not None
                    else ExternalActionState.AWAITING_APPROVAL.value
                ),
                new_state=ExternalActionState.CANCELLED.value,
                reason_code=action.terminal_reason_code,
                action_id=action.id,
                step_id=action.step_id,
                approval_request_id=stored.request.id,
                approval_decision_id=None if decision is None else decision.id,
                metadata_bindings={
                    "approval_set_id": authorization_set.id,
                    "approval_status": stored.status.value,
                    "closure_reason": action.terminal_reason_code,
                },
            )
            if stored.status is ApprovalStatus.SUPERSEDED:
                approval_event = await unit_of_work.audits.get_mutation_event(
                    "approval_request",
                    stored.request.id,
                    stored.version,
                )
                self._require_bound_event(
                    approval_event,
                    event_type="approval.superseded",
                    run_id=snapshot.run.id,
                    aggregate_id=stored.request.id,
                    mutation_version=stored.version,
                    occurred_at=stored.updated_at,
                    previous_state=(
                        ApprovalStatus.APPROVED.value
                        if decision is not None
                        else ApprovalStatus.PENDING.value
                    ),
                    new_state=ApprovalStatus.SUPERSEDED.value,
                    reason_code=stored.superseded_reason_code,
                    action_id=action.id,
                    step_id=action.step_id,
                    approval_request_id=stored.request.id,
                    approval_decision_id=None if decision is None else decision.id,
                    metadata_bindings={
                        "approval_set_id": authorization_set.id,
                        "status": ApprovalStatus.SUPERSEDED.value,
                    },
                )
            elif stored.status is ApprovalStatus.EXPIRED:
                await self._require_expiry_audit(
                    unit_of_work,
                    stored,
                    action,
                    action_version=action.version - 1,
                )
            else:
                self._replay_error(
                    snapshot.run.id,
                    "authorization_close_request_mismatch",
                    "closed sibling approval is not terminal",
                )

    async def _require_expiry_audit(
        self,
        unit_of_work: UnitOfWork,
        stored: StoredActionApprovalRequest,
        action: ExternalAction,
        *,
        action_version: int,
    ) -> None:
        if stored.expired_at is None:
            self._replay_error(
                stored.request.run_id,
                "authorization_expiry_mismatch",
                "expired approval lost its expiration time",
            )
        expiry_event = await unit_of_work.audits.get_mutation_event(
            "approval_request",
            stored.request.id,
            stored.version,
        )
        self._require_bound_event(
            expiry_event,
            event_type="approval.expired",
            run_id=stored.request.run_id,
            aggregate_id=stored.request.id,
            mutation_version=stored.version,
            occurred_at=stored.expired_at,
            previous_state=(
                ApprovalStatus.APPROVED.value
                if stored.decision is not None
                else ApprovalStatus.PENDING.value
            ),
            new_state=ApprovalStatus.EXPIRED.value,
            reason_code="approval_expired",
            action_id=action.id,
            step_id=action.step_id,
            approval_request_id=stored.request.id,
            metadata_bindings={
                "action_state": ExternalActionState.AWAITING_APPROVAL.value,
                "action_version": action_version,
                "generation": stored.request.generation,
                "policy_id": stored.request.policy.policy_id,
                "proposal_revision": stored.request.proposal_revision,
                "status": ApprovalStatus.EXPIRED.value,
            },
        )

    async def _require_run_transition_audit(
        self,
        unit_of_work: UnitOfWork,
        transition: RunStateTransition,
        *,
        run_id: str,
    ) -> None:
        event = await unit_of_work.audits.get_mutation_event(
            "run",
            run_id,
            transition.resulting_version,
        )
        self._require_bound_event(
            event,
            event_type="run.transitioned",
            run_id=run_id,
            aggregate_id=run_id,
            mutation_version=transition.resulting_version,
            occurred_at=transition.occurred_at,
            previous_state=(
                None if transition.previous_state is None else transition.previous_state.value
            ),
            new_state=transition.new_state.value,
            reason_code=transition.reason_code,
            metadata_bindings={"command": transition.command.value},
        )
        if event is None or event.transition_sequence != transition.sequence:
            self._replay_error(
                run_id,
                "authorization_run_audit_mismatch",
                "authorization boundary Run audit is not authoritative",
            )

    async def _require_step_transition_audit(
        self,
        unit_of_work: UnitOfWork,
        transition: StepStateTransition,
        *,
        step: RunStep,
    ) -> None:
        event = await unit_of_work.audits.get_mutation_event(
            "step",
            transition.step_id,
            transition.resulting_version,
        )
        self._require_bound_event(
            event,
            event_type="step.transitioned",
            run_id=transition.run_id,
            aggregate_id=transition.step_id,
            mutation_version=transition.resulting_version,
            occurred_at=transition.occurred_at,
            previous_state=(
                None if transition.previous_state is None else transition.previous_state.value
            ),
            new_state=transition.new_state.value,
            reason_code=transition.reason_code,
            step_id=transition.step_id,
            metadata_bindings={
                "command": transition.command.value,
                "ordinal": step.ordinal,
                "step_kind": step.kind,
            },
        )
        if event is None or event.transition_sequence != transition.sequence:
            self._replay_error(
                transition.run_id,
                "authorization_step_audit_mismatch",
                "authorization boundary step audit is not authoritative",
            )

    def _require_bound_event(
        self,
        event: AuditEvent | None,
        *,
        event_type: str,
        run_id: str,
        aggregate_id: str,
        mutation_version: int | None,
        occurred_at: datetime,
        previous_state: str | None,
        new_state: str | None,
        reason_code: str | None,
        action_id: str | None = None,
        step_id: str | None = None,
        approval_request_id: str | None = None,
        approval_decision_id: str | None = None,
        metadata_bindings: dict[str, object] | None = None,
    ) -> None:
        if event is None:
            self._replay_error(
                run_id,
                "authorization_boundary_audit_missing",
                "authorization boundary mutation lacks its audit witness",
            )
        assert event is not None
        draft = event.draft
        metadata = draft.safe_metadata.values
        if (
            draft.event_type != event_type
            or draft.run_id != run_id
            or draft.aggregate_id != aggregate_id
            or draft.mutation_version != mutation_version
            or draft.occurred_at != occurred_at
            or draft.previous_state != previous_state
            or draft.new_state != new_state
            or draft.reason_code != reason_code
            or draft.action_id != action_id
            or draft.step_id != step_id
            or draft.approval_request_id != approval_request_id
            or draft.approval_decision_id != approval_decision_id
            or any(metadata.get(key) != value for key, value in (metadata_bindings or {}).items())
        ):
            self._replay_error(
                run_id,
                "authorization_boundary_audit_mismatch",
                "authorization boundary audit witness is not authoritative",
            )

    @staticmethod
    def _replay_error(run_id: str, code: str, message: str) -> Never:
        raise ApprovalBoundaryServiceError(code, message, run_id=run_id)

    async def _require_release_decision_witnesses(
        self,
        unit_of_work: UnitOfWork,
        stored: StoredActionApprovalRequest,
        action: ExternalAction,
    ) -> None:
        request = stored.request
        decision = stored.decision
        if decision is None:  # pragma: no cover - caller already rejects this
            raise AssertionError("approved leaf lost its decision")
        expected_roles = request.policy.required_roles | frozenset({APPROVER_ROLE})
        expected_scopes = request.policy.required_scopes | frozenset({APPROVAL_DECIDE_SCOPE})
        if (
            decision.authentication_method not in {"local_fixed", "bearer"}
            or decision.authority_roles != expected_roles
            or decision.authority_scopes != expected_scopes
            or (
                not request.policy.allow_self_approval and decision.actor_id == request.requested_by
            )
        ):
            raise ApprovalBoundaryServiceError(
                "approval_authority_snapshot_mismatch",
                "approval decision no longer proves the exact human authority policy",
                run_id=request.run_id,
            )
        action_event = await unit_of_work.audits.get_mutation_event(
            "external_action",
            action.id,
            action.version,
        )
        approval_event = await unit_of_work.audits.get_mutation_event(
            "approval_request",
            request.id,
            stored.version,
        )
        if action_event is None or approval_event is None:
            raise ApprovalBoundaryServiceError(
                "approval_decision_audit_missing",
                "approved leaf lacks its authoritative decision audit",
                run_id=request.run_id,
            )
        pending_action = replace(
            action,
            state=ExternalActionState.AWAITING_APPROVAL,
            updated_at=request.requested_at,
            version=action.version - 1,
            terminal_reason_code=None,
        )
        pending_request = StoredActionApprovalRequest.created(request)
        decision_context = AuditContext.authenticated_user(
            decision.actor_id,
            authentication_method=decision.authentication_method,
            correlation_id=decision.correlation_id,
        )
        factory = AuditEventFactory(decision_context)
        expected_action = factory.action_decided(pending_action, action, stored)
        expected_approval = factory.approval_decided(pending_request, stored, action)
        if action_event.draft != expected_action or approval_event.draft != expected_approval:
            raise ApprovalBoundaryServiceError(
                "approval_decision_audit_mismatch",
                "approved leaf decision audit is not authoritative",
                run_id=request.run_id,
            )

    @staticmethod
    def _terminal_step_transitions(
        snapshot: _BoundarySnapshot,
        *,
        rejected_action_ids: frozenset[str],
        occurred_at: datetime,
        cancelling: bool,
    ) -> tuple[StepTransitionResult, ...]:
        action_by_step = {action.step_id: action for action in snapshot.actions}
        results: list[StepTransitionResult] = []
        for step in snapshot.plan_steps:
            action = action_by_step.get(step.id)
            if not cancelling and action is not None and action.id in rejected_action_ids:
                command = StepLifecycleCommand.REJECT
                reason = "approval_rejected"
            else:
                command = StepLifecycleCommand.CANCEL
                reason = "operator_cancelled" if cancelling else "sibling_approval_rejected"
            results.append(
                transition_step(
                    step,
                    command,
                    StepTerminalContext(reason),
                    occurred_at,
                )
            )
        return tuple(results)

    async def _load_current(
        self,
        unit_of_work: UnitOfWork,
        run_id: str,
    ) -> _BoundarySnapshot:
        try:
            selection = await unit_of_work.approvals.get_current_authorization_set(run_id)
        except ApprovalRepositoryConflict as exc:
            raise ApprovalBoundaryServiceError(
                exc.code,
                "current authorization set could not be validated",
                run_id=run_id,
            ) from None
        if selection is None:
            raise ApprovalBoundaryServiceError(
                "authorization_set_missing",
                "Run has no current authorization set",
                run_id=run_id,
            )
        authorization_set = selection.authorization_set
        run = await unit_of_work.runs.get(run_id)
        if run is None:
            raise ApprovalBoundaryServiceError(
                "run_not_found",
                "authorization set parent Run is missing",
                run_id=run_id,
            )
        try:
            requests = await unit_of_work.approvals.list_current_set(
                run_id,
                authorization_set.plan_hash,
                authorization_set.proposal_revision,
            )
            request_history = await unit_of_work.approvals.list_set_history(
                run_id,
                authorization_set.plan_hash,
                authorization_set.proposal_revision,
            )
            plan_steps = await unit_of_work.run_steps.validate_plan_for_execution(run_id)
        except RuntimeError as exc:
            raise ApprovalBoundaryServiceError(
                getattr(exc, "code", "authorization_set_corrupt"),
                "authorization set members could not be validated",
                run_id=run_id,
            ) from None
        request_by_action = {stored.request.action_id: stored for stored in requests}
        step_by_id = {step.id: step for step in plan_steps}
        actions: list[ExternalAction] = []
        steps: list[RunStep] = []
        for member in authorization_set.members:
            stored = request_by_action.get(member.action_id)
            action = await unit_of_work.external_actions.get(member.action_id)
            step = step_by_id.get(member.step_id)
            if (
                stored is None
                or action is None
                or step is None
                or stored.request.authorization_set_id != authorization_set.id
                or stored.request.action_hash != member.action_hash
                or stored.request.step_id != member.step_id
                or stored.request.step_key != member.step_key
                or action.action_hash != member.action_hash
                or action.run_id != run_id
                or action.step_id != member.step_id
                or step.run_id != run_id
                or step.key != member.step_key
                or step.plan_hash != authorization_set.plan_hash
            ):
                raise ApprovalBoundaryServiceError(
                    "authorization_set_member_mismatch",
                    "current authorization set does not exactly bind its durable members",
                    run_id=run_id,
                )
            actions.append(action)
            steps.append(step)
        if len(request_by_action) != len(authorization_set.members):
            raise ApprovalBoundaryServiceError(
                "authorization_set_member_mismatch",
                "current request leaves do not exactly cover the authorization set",
                run_id=run_id,
            )
        if run.approval_required is not True or {
            step.id for step in plan_steps if step.effect.value == "write"
        } != {member.step_id for member in authorization_set.members}:
            raise ApprovalBoundaryServiceError(
                "authorization_set_plan_mismatch",
                "authorization set does not exactly cover every planned write step",
                run_id=run_id,
            )
        ordered_requests = tuple(
            request_by_action[member.action_id] for member in authorization_set.members
        )
        return _BoundarySnapshot(
            selection=selection,
            run=run,
            requests=ordered_requests,
            request_history=request_history,
            actions=tuple(actions),
            member_steps=tuple(steps),
            plan_steps=tuple(plan_steps),
        )

    @staticmethod
    async def _append_release_audits(
        unit_of_work: UnitOfWork,
        snapshot: _BoundarySnapshot,
        released: AuthorizationSetReleaseResult,
        run_transition: RunTransitionResult,
        step_transitions: tuple[StepTransitionResult, ...],
        *,
        audit_context: AuditContext,
    ) -> None:
        factory = AuditEventFactory(audit_context)
        source_request_by_action = {
            stored.request.action_id: stored for stored in snapshot.requests
        }
        source_action_by_id = {action.id: action for action in snapshot.actions}
        target_request_by_action = {
            stored.request.action_id: stored for stored in released.requests
        }
        target_action_by_id = {action.id: action for action in released.actions}
        member_events: list[AuditEventDraft] = []
        for member in snapshot.selection.authorization_set.members:
            previous_request = source_request_by_action[member.action_id]
            previous_action = source_action_by_id[member.action_id]
            consumed = target_request_by_action[member.action_id]
            reserved = target_action_by_id[member.action_id]
            member_events.extend(
                (
                    factory.action_dispatch_reserved(
                        previous_action,
                        reserved,
                        consumed,
                    ),
                    factory.approval_consumed(
                        previous_request,
                        consumed,
                        reserved,
                    ),
                )
            )
        await unit_of_work.audits.append_many(
            (
                *member_events,
                *(
                    factory.step_transition(result.step, result.transition)
                    for result in step_transitions
                ),
                factory.run_transition(run_transition.run, run_transition.transition),
            )
        )

    @staticmethod
    async def _append_closure_audits(
        unit_of_work: UnitOfWork,
        snapshot: _BoundarySnapshot,
        closed: AuthorizationSetCloseResult,
        run_transition: RunTransitionResult,
        step_transitions: tuple[StepTransitionResult, ...],
        *,
        audit_context: AuditContext,
    ) -> None:
        factory = AuditEventFactory(audit_context)
        target_request_by_action = {stored.request.action_id: stored for stored in closed.requests}
        target_action_by_id = {action.id: action for action in closed.actions}
        member_events: list[AuditEventDraft] = []
        for previous_request, previous_action in zip(
            snapshot.requests,
            snapshot.actions,
            strict=True,
        ):
            action_id = previous_action.id
            terminal_request = target_request_by_action[action_id]
            cancelled = target_action_by_id[action_id]
            if previous_action != cancelled:
                member_events.append(
                    factory.action_cancelled(
                        previous_action,
                        cancelled,
                        terminal_request,
                    )
                )
            if previous_request != terminal_request:
                member_events.append(
                    factory.approval_superseded(
                        previous_request,
                        terminal_request,
                        cancelled,
                    )
                )
        await unit_of_work.audits.append_many(
            (
                *member_events,
                *(
                    factory.step_transition(result.step, result.transition)
                    for result in step_transitions
                ),
                factory.run_transition(run_transition.run, run_transition.transition),
            )
        )
