"""Narrow domain-typed repository ports with no persistence-framework types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalDecision,
    ApprovalRenewal,
    ApprovalUse,
    AuthorizationSet,
    AuthorizationSetHead,
    AuthorizationSetStatus,
    StoredActionApprovalRequest,
    authorization_set_release_hash,
)
from marketing_agents.domain.audit import AuditEvent, AuditEventDraft
from marketing_agents.domain.entities import (
    ActionReservationSnapshot,
    ConnectorActionReceipt,
    ExternalAction,
    ExternalActionResultSnapshot,
    Run,
    RunPlanRoutingAssignment,
    RunPlanSelectedInstance,
    RunPlanSnapshot,
    RunStep,
    Schedule,
    ScheduleClaim,
    ScheduleOccurrence,
    WorkItem,
)
from marketing_agents.domain.enums import (
    ApprovalStatus,
    ExternalActionState,
    RunState,
    StepState,
)
from marketing_agents.domain.execution_control import (
    AttemptCompletionCommand,
    AttemptReservationCommand,
    DeliveryCallPermit,
    DeliveryCallReservationCommand,
    ExecutionAttempt,
    ExpiredAttemptRecoveryCommand,
    OperationExecutionPolicy,
    RateLimitWindow,
    RunExecutionControl,
    RunExecutionPolicy,
)
from marketing_agents.domain.provenance import ArtifactEnvelope
from marketing_agents.domain.run_lifecycle import RunStateTransition, RunTransitionResult
from marketing_agents.domain.runtime_policy import RateLimitScope
from marketing_agents.domain.step_lifecycle import (
    StepLifecycleCommand,
    StepStateTransition,
    StepTransitionResult,
)
from marketing_agents.domain.validation import require_digest, require_id


@dataclass(frozen=True, slots=True)
class WorkInsertResult:
    """Outcome of one atomic source-key insert-or-read operation."""

    work_item: WorkItem
    inserted: bool


class WorkRepository(Protocol):
    async def get(self, work_item_id: str) -> WorkItem | None: ...

    async def get_by_source_key(
        self, source: str, event_id: str, instance_id: str
    ) -> WorkItem | None: ...

    async def add(self, work_item: WorkItem) -> None: ...

    async def add_or_get(self, work_item: WorkItem) -> WorkInsertResult: ...


@dataclass(frozen=True, slots=True)
class ScheduleInsertResult:
    """Outcome of one initial schedule insert-or-exact-replay operation."""

    schedule: Schedule
    inserted: bool


@dataclass(frozen=True, slots=True)
class ScheduleOccurrenceInsertResult:
    """Outcome of one pending occurrence insert-or-authoritative-replay."""

    occurrence: ScheduleOccurrence
    inserted: bool


@dataclass(frozen=True, slots=True)
class ScheduleOccurrenceLinkResult:
    """Outcome of binding one occurrence to its WorkItem and primary Run."""

    occurrence: ScheduleOccurrence
    linked: bool


class ScheduleRepositoryConflict(RuntimeError):
    """Stable fail-closed schedule persistence or hydration conflict."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScheduleRepository(Protocol):
    async def get(self, schedule_id: str) -> Schedule | None: ...

    async def get_claim(self, schedule_id: str) -> ScheduleClaim | None: ...

    async def fence_claim(
        self,
        claim: ScheduleClaim,
        *,
        now: datetime,
    ) -> bool: ...

    async def get_occurrence(
        self,
        occurrence_id: str,
    ) -> ScheduleOccurrence | None: ...

    async def get_occurrence_by_schedule_due(
        self,
        schedule_id: str,
        scheduled_for_utc: datetime,
    ) -> ScheduleOccurrence | None: ...

    async def list_claimable_due(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Schedule, ...]: ...

    async def try_claim(
        self,
        *,
        schedule_id: str,
        expected_version: int,
        expected_due_at_utc: datetime,
        lease_owner: str,
        claimed_at_utc: datetime,
        lease_expires_at_utc: datetime,
    ) -> ScheduleClaim | None: ...

    async def add_or_get(self, schedule: Schedule) -> ScheduleInsertResult: ...

    async def add_occurrence_or_get(
        self,
        occurrence: ScheduleOccurrence,
    ) -> ScheduleOccurrenceInsertResult: ...

    async def mark_occurrence_enqueued(
        self,
        *,
        occurrence_id: str,
        work_item_id: str,
        run_id: str,
    ) -> ScheduleOccurrenceLinkResult: ...

    async def advance_and_release_claim(
        self,
        *,
        claim: ScheduleClaim,
        next_run_at_utc: datetime,
        completed_at_utc: datetime,
    ) -> Schedule | None: ...


@dataclass(frozen=True, slots=True)
class ArtifactInsertResult:
    """Outcome of one immutable artifact insert-or-exact-replay operation."""

    artifact: ArtifactEnvelope
    inserted: bool


class ArtifactRepositoryConflict(RuntimeError):
    """Stable fail-closed artifact persistence or hydration conflict."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactRepository(Protocol):
    async def get(self, artifact_id: str) -> ArtifactEnvelope | None: ...

    async def list_for_run(self, run_id: str) -> tuple[ArtifactEnvelope, ...]: ...

    async def add_or_get(self, artifact: ArtifactEnvelope) -> ArtifactInsertResult: ...


@dataclass(frozen=True, slots=True)
class RunInsertResult:
    """Outcome of atomic primary-Run receipt for one admitted WorkItem."""

    run: Run
    inserted: bool


class RunRepository(Protocol):
    async def get(self, run_id: str) -> Run | None: ...

    async def get_by_work_item_id(self, work_item_id: str) -> Run | None: ...

    async def add_received_or_get(
        self,
        run: Run,
        initial_transition: RunStateTransition,
    ) -> RunInsertResult: ...

    async def fence(
        self,
        *,
        run_id: str,
        expected_version: int,
        expected_state: RunState,
    ) -> bool: ...

    async def apply_transition(
        self,
        *,
        expected_version: int,
        expected_state: RunState,
        result: RunTransitionResult,
    ) -> bool: ...

    async def list_transitions(self, run_id: str) -> tuple[RunStateTransition, ...]: ...


@dataclass(frozen=True, slots=True)
class ExecutionControlInsertResult:
    control: RunExecutionControl
    operations: tuple[OperationExecutionPolicy, ...]
    inserted: bool


@dataclass(frozen=True, slots=True)
class ExecutionControlStartResult:
    control: RunExecutionControl
    started: bool


@dataclass(frozen=True, slots=True)
class ExecutionCancellationFenceResult:
    control: RunExecutionControl
    fenced: bool


@dataclass(frozen=True, slots=True)
class AttemptReservationResult:
    control: RunExecutionControl
    attempt: ExecutionAttempt
    rate_window: RateLimitWindow


@dataclass(frozen=True, slots=True)
class AttemptCompletionResult:
    attempt: ExecutionAttempt
    completed: bool

    @property
    def retry_not_before(self) -> datetime | None:
        return self.attempt.retry_not_before

    @property
    def terminal_reason_code(self) -> str | None:
        return self.attempt.terminal_reason_code


@dataclass(frozen=True, slots=True)
class DeliveryCallReservationResult:
    control: RunExecutionControl
    permit: DeliveryCallPermit
    rate_window: RateLimitWindow


class ExecutionControlRepositoryConflict(RuntimeError):
    """Stable fail-closed runtime-control persistence conflict."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        if retry_after_seconds is not None and not 1 <= retry_after_seconds <= 3_600:
            raise ValueError("retry-after seconds must be bounded")
        self.retry_after_seconds = retry_after_seconds


class ExecutionControlRepository(Protocol):
    async def initialize(
        self,
        policy: RunExecutionPolicy,
    ) -> ExecutionControlInsertResult: ...

    async def get(self, run_id: str) -> RunExecutionControl | None: ...

    async def get_operation(
        self,
        step_id: str,
        operation_key: str,
    ) -> OperationExecutionPolicy | None: ...

    async def start_execution(
        self,
        *,
        run_id: str,
        expected_control_version: int,
        started_at: datetime,
    ) -> ExecutionControlStartResult: ...

    async def request_cancel(
        self,
        *,
        run_id: str,
        expected_control_version: int,
        actor_digest: str,
        requested_at: datetime,
    ) -> ExecutionCancellationFenceResult: ...

    async def reserve_attempt(
        self,
        command: AttemptReservationCommand,
    ) -> AttemptReservationResult: ...

    async def complete_attempt(
        self,
        command: AttemptCompletionCommand,
    ) -> AttemptCompletionResult: ...

    async def recover_expired_attempt(
        self,
        command: ExpiredAttemptRecoveryCommand,
    ) -> AttemptCompletionResult: ...

    async def reserve_delivery_call(
        self,
        command: DeliveryCallReservationCommand,
    ) -> DeliveryCallReservationResult: ...

    async def get_attempt(self, attempt_id: str) -> ExecutionAttempt | None: ...

    async def list_attempts(
        self,
        step_id: str,
        operation_key: str,
    ) -> tuple[ExecutionAttempt, ...]: ...

    async def get_rate_window(
        self,
        scope: RateLimitScope,
        key: str,
        started_at: datetime,
    ) -> RateLimitWindow | None: ...


@dataclass(frozen=True, slots=True)
class ExternalActionSetInsertResult:
    """Atomic all-created or authoritative all-replayed action set."""

    actions: tuple[ExternalAction, ...]
    inserted: bool


class ReleaseCallMode(StrEnum):
    FIRST_CALL = "first_call"
    PROVIDER_RETRY = "provider_retry"


@dataclass(frozen=True, slots=True)
class ReleaseAuthority:
    """Exact committed barrier snapshot required by every dispatch mutation."""

    authorization_set_id: str
    membership_hash: str
    release_hash: str
    authorization_set_version: int
    head_version: int
    run_id: str
    released_run_version: int
    action_id: str
    action_hash: str
    step_id: str
    step_key: str
    released_step_version: int
    step_state: StepState
    step_version: int
    call_mode: ReleaseCallMode
    prior_started_attempt_number: int | None
    approval_request_id: str
    approval_decision_id: str
    approval_use_id: str
    reservation_id: str

    def __post_init__(self) -> None:
        for identifier, name in (
            (self.authorization_set_id, "release authorization set ID"),
            (self.run_id, "release Run ID"),
            (self.action_id, "release action ID"),
            (self.step_id, "release step ID"),
            (self.step_key, "release step key"),
            (self.approval_request_id, "release approval request ID"),
            (self.approval_decision_id, "release approval decision ID"),
            (self.approval_use_id, "release approval use ID"),
            (self.reservation_id, "release reservation ID"),
        ):
            require_id(identifier, name)
        for digest, name in (
            (self.membership_hash, "release membership hash"),
            (self.release_hash, "release hash"),
            (self.action_hash, "release action hash"),
        ):
            require_digest(digest, name)
        for version in (
            self.authorization_set_version,
            self.head_version,
            self.released_run_version,
            self.released_step_version,
            self.step_version,
        ):
            if type(version) is not int or version < 1:
                raise ValueError("release authority versions must be positive integers")
        if type(self.step_state) is not StepState or type(self.call_mode) is not ReleaseCallMode:
            raise ValueError("release authority must use exact state and call-mode enums")
        if self.call_mode is ReleaseCallMode.FIRST_CALL:
            if (
                self.step_state is not StepState.READY
                or self.step_version != self.released_step_version
                or self.prior_started_attempt_number is not None
            ):
                raise ValueError("first-call authority must retain the released READY step")
        else:
            prior_started_attempt_number = self.prior_started_attempt_number
            if (
                self.step_state is not StepState.EXECUTING
                or self.step_version != self.released_step_version + 1
                or type(prior_started_attempt_number) is not int
                or prior_started_attempt_number < 1
            ):
                raise ValueError("provider-retry authority requires exact started-attempt lineage")


@dataclass(frozen=True, slots=True)
class ActionCallStartResult:
    """Atomic action call marker plus reserved WRITE-step start."""

    action: ExternalAction
    step_transition: StepTransitionResult | None


class ExternalActionRepositoryConflict(RuntimeError):
    """Stable action hydration/mutation conflict exposed through the application port."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExternalActionRepository(Protocol):
    async def get(self, action_id: str) -> ExternalAction | None: ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> ExternalAction | None: ...

    async def list_plan_set(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> tuple[ExternalAction, ...]: ...

    async def list_run_plan(
        self,
        run_id: str,
        plan_hash: str,
    ) -> tuple[ExternalAction, ...]: ...

    async def add_proposed_set_or_get(
        self,
        actions: tuple[ExternalAction, ...],
    ) -> ExternalActionSetInsertResult: ...

    async def claim_reserved(
        self,
        *,
        action_id: str,
        expected_version: int,
        authority: ReleaseAuthority,
        lease_owner: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> ExternalAction | None: ...

    async def mark_call_started(
        self,
        *,
        action_id: str,
        expected_version: int,
        authority: ReleaseAuthority,
        lease_owner: str,
        attempt_number: int,
        started_at: datetime,
        call_deadline_at: datetime,
        step_transition: StepTransitionResult | None,
    ) -> ActionCallStartResult | None: ...

    async def cancel_unstarted_after_release(
        self,
        *,
        action_id: str,
        run_id: str,
        plan_hash: str,
        expected_version: int,
        occurred_at: datetime,
        reason_code: str = "operator_cancelled",
    ) -> ExternalAction | None: ...

    async def complete_succeeded(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        result: ExternalActionResultSnapshot,
    ) -> ExternalAction | None: ...

    async def complete_failed(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        reason_code: str,
        occurred_at: datetime,
    ) -> ExternalAction | None: ...

    async def mark_outcome_unknown(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        reason_code: str,
        occurred_at: datetime,
    ) -> ExternalAction | None: ...

    async def release_stale_for_retry(
        self,
        *,
        action_id: str,
        expected_version: int,
        attempt_number: int,
        occurred_at: datetime,
        conclusion: str,
    ) -> ExternalAction | None: ...

    async def fail_exhausted_stale_pre_call(
        self,
        *,
        action_id: str,
        expected_version: int,
        attempt_number: int,
        occurred_at: datetime,
        reason_code: str,
    ) -> ExternalAction | None: ...

    async def list_stale(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ExternalAction, ...]: ...


@dataclass(frozen=True, slots=True)
class ConnectorReceiptInsertResult:
    receipt: ConnectorActionReceipt
    inserted: bool


class ConnectorReceiptRepository(Protocol):
    async def get(
        self,
        connector_binding_id: str,
        idempotency_key: str,
    ) -> ConnectorActionReceipt | None: ...

    async def add_or_get(
        self,
        receipt: ConnectorActionReceipt,
    ) -> ConnectorReceiptInsertResult: ...


@dataclass(frozen=True, slots=True)
class ApprovalRequestSetInsertResult:
    requests: tuple[StoredActionApprovalRequest, ...]
    authorization_set: AuthorizationSet
    head: AuthorizationSetHead
    inserted: bool


@dataclass(frozen=True, slots=True)
class ApprovalSetReleaseMember:
    """Every exact source/target fact for one all-or-none barrier member."""

    request: StoredActionApprovalRequest
    action: ExternalAction
    step_transition: StepTransitionResult
    use: ApprovalUse
    reservation: ActionReservationSnapshot

    def __post_init__(self) -> None:
        request = self.request
        decision = request.decision
        transition = self.step_transition
        if (
            type(request) is not StoredActionApprovalRequest
            or request.status is not ApprovalStatus.APPROVED
            or decision is None
            or type(self.action) is not ExternalAction
            or self.action.state is not ExternalActionState.APPROVED
            or type(transition) is not StepTransitionResult
            or transition.transition.command is not StepLifecycleCommand.RELEASE_APPROVAL
            or transition.transition.previous_state is not StepState.AWAITING_APPROVAL
            or transition.step.state is not StepState.READY
            or type(self.use) is not ApprovalUse
            or type(self.reservation) is not ActionReservationSnapshot
        ):
            raise ValueError("approval release member requires approved exact source states")
        approval_request = request.request
        if (
            decision.authority_roles
            != approval_request.policy.required_roles | frozenset({"approver"})
            or decision.authority_scopes
            != approval_request.policy.required_scopes | frozenset({"approvals:decide"})
            or (
                not approval_request.policy.allow_self_approval
                and decision.actor_id == approval_request.requested_by
            )
            or decision.decided_at >= approval_request.expires_at
            or self.action.id != approval_request.action_id
            or self.action.action_hash != approval_request.action_hash
            or self.action.run_id != approval_request.run_id
            or self.action.step_id != approval_request.step_id
            or transition.step.id != approval_request.step_id
            or transition.step.run_id != approval_request.run_id
            or transition.step.key != approval_request.step_key
            or self.use.request_id != approval_request.id
            or self.use.decision_id != decision.id
            or self.use.action_id != approval_request.action_id
            or self.use.action_hash != approval_request.action_hash
            or self.use.authorization_set_id != approval_request.authorization_set_id
            or self.use.run_id != approval_request.run_id
            or self.use.plan_hash != approval_request.plan_hash
            or self.use.proposal_revision != approval_request.proposal_revision
            or self.use.step_id != approval_request.step_id
            or self.use.step_key != approval_request.step_key
            or self.reservation.reservation_id != self.use.reservation_id
            or self.reservation.authorization_set_id != approval_request.authorization_set_id
            or self.reservation.approval_request_id != approval_request.id
            or self.reservation.approval_decision_id != decision.id
            or self.reservation.action_hash != approval_request.action_hash
            or self.reservation.capability_id != self.action.envelope.capability_id
            or self.reservation.binding_id != self.action.envelope.binding_id
            or self.reservation.idempotency_key != self.action.idempotency_key
            or self.use.used_at != self.reservation.reserved_at
            or self.use.used_at != transition.transition.occurred_at
            or self.use.used_at < decision.decided_at
            or self.use.used_at >= approval_request.expires_at
        ):
            raise ValueError("approval release member does not bind one exact unexpired leaf")

    def release_hash_material(self) -> dict[str, object]:
        decision = self.request.decision
        assert decision is not None  # guaranteed by __post_init__
        return {
            "action_id": self.action.id,
            "action_hash": self.action.action_hash,
            "action_source_version": self.action.version,
            "request_id": self.request.request.id,
            "request_source_version": self.request.version,
            "decision_id": decision.id,
            "approval_use_id": self.use.id,
            "reservation_id": self.reservation.reservation_id,
            "step_id": self.step_transition.step.id,
            "released_step_version": self.step_transition.step.version,
        }


@dataclass(frozen=True, slots=True)
class AuthorizationSetReleaseCommand:
    """One complete current-set barrier transition staged in a single UoW."""

    authorization_set: AuthorizationSet
    head: AuthorizationSetHead
    run_transition: RunTransitionResult
    members: tuple[ApprovalSetReleaseMember, ...]
    released_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.authorization_set) is not AuthorizationSet
            or self.authorization_set.status is not AuthorizationSetStatus.OPEN
            or type(self.head) is not AuthorizationSetHead
            or type(self.run_transition) is not RunTransitionResult
            or type(self.members) is not tuple
            or not self.members
        ):
            raise ValueError("authorization release requires exact immutable contracts")
        self.head.assert_selects(self.authorization_set)
        transition = self.run_transition.transition
        if (
            transition.command.value != "release_approved_plan"
            or transition.previous_state is not RunState.AWAITING_APPROVAL
            or self.run_transition.run.state is not RunState.EXECUTING
            or self.run_transition.run.id != self.authorization_set.run_id
            or transition.occurred_at != self.released_at
        ):
            raise ValueError("authorization release requires the exact parent Run transition")
        expected = self.authorization_set.members
        if len(expected) != len(self.members):
            raise ValueError("authorization release must contain every set member")
        for set_member, release_member in zip(expected, self.members, strict=True):
            request = release_member.request.request
            if (
                set_member.action_id != release_member.action.id
                or set_member.action_hash != release_member.action.action_hash
                or set_member.step_id != release_member.step_transition.step.id
                or set_member.step_key != release_member.step_transition.step.key
                or set_member.authorization_set_id != request.authorization_set_id
                or set_member.run_id != request.run_id
                or set_member.plan_hash != request.plan_hash
                or set_member.proposal_revision != request.proposal_revision
            ):
                raise ValueError("authorization release differs from exact set membership")

    @property
    def release_hash(self) -> str:
        return authorization_set_release_hash(
            authorization_set_id=self.authorization_set.id,
            membership_hash=self.authorization_set.membership_hash,
            released_run_version=self.run_transition.run.version,
            released_at=self.released_at,
            members=tuple(member.release_hash_material() for member in self.members),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationSetReleaseResult:
    authorization_set: AuthorizationSet
    head: AuthorizationSetHead
    run: Run
    steps: tuple[RunStep, ...]
    actions: tuple[ExternalAction, ...]
    requests: tuple[StoredActionApprovalRequest, ...]
    inserted: bool


@dataclass(frozen=True, slots=True)
class CurrentAuthorizationSet:
    head: AuthorizationSetHead
    authorization_set: AuthorizationSet

    def __post_init__(self) -> None:
        self.head.assert_selects(self.authorization_set)


@dataclass(frozen=True, slots=True)
class AuthorizationSetCloseCommand:
    """Terminally close one unreleased current set with its parent/step results."""

    authorization_set: AuthorizationSet
    head: AuthorizationSetHead
    status: AuthorizationSetStatus
    run_transition: RunTransitionResult
    actions: tuple[ExternalAction, ...]
    requests: tuple[StoredActionApprovalRequest, ...]
    step_transitions: tuple[StepTransitionResult, ...]
    closed_at: datetime

    def __post_init__(self) -> None:
        if self.authorization_set.status is not AuthorizationSetStatus.OPEN or self.status not in {
            AuthorizationSetStatus.REJECTED,
            AuthorizationSetStatus.CANCELLED,
        }:
            raise ValueError("only an open authorization set can be terminally closed")
        self.head.assert_selects(self.authorization_set)
        expected_run_state = (
            RunState.REJECTED
            if self.status is AuthorizationSetStatus.REJECTED
            else RunState.CANCELLED
        )
        if (
            self.run_transition.run.id != self.authorization_set.run_id
            or self.run_transition.run.state is not expected_run_state
            or self.run_transition.transition.occurred_at != self.closed_at
            or type(self.actions) is not tuple
            or type(self.requests) is not tuple
            or type(self.step_transitions) is not tuple
        ):
            raise ValueError("authorization set closure has inconsistent parent state")
        expected_members = self.authorization_set.members
        if (
            {action.id for action in self.actions}
            != {member.action_id for member in expected_members}
            or {stored.request.action_id for stored in self.requests}
            != {member.action_id for member in expected_members}
            or not {member.step_id for member in expected_members}.issubset(
                {result.step.id for result in self.step_transitions}
            )
            or len({result.step.id for result in self.step_transitions})
            != len(self.step_transitions)
            or any(
                result.step.run_id != self.authorization_set.run_id
                or result.transition.occurred_at != self.closed_at
                for result in self.step_transitions
            )
        ):
            raise ValueError("authorization set closure must cover every exact set member")


@dataclass(frozen=True, slots=True)
class AuthorizationSetCloseResult:
    authorization_set: AuthorizationSet
    head: AuthorizationSetHead
    run: Run
    steps: tuple[RunStep, ...]
    actions: tuple[ExternalAction, ...]
    requests: tuple[StoredActionApprovalRequest, ...]


@dataclass(frozen=True, slots=True)
class ApprovalDecisionInsertResult:
    request: StoredActionApprovalRequest
    inserted: bool


class ApprovalRepositoryConflict(RuntimeError):
    """Stable approval mutation conflict exposed through the application port."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalRepository(Protocol):
    async def get(self, request_id: str) -> StoredActionApprovalRequest | None: ...

    async def list_current_set(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> tuple[StoredActionApprovalRequest, ...]: ...

    async def list_set_history(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> tuple[StoredActionApprovalRequest, ...]: ...

    async def get_current_authorization_set(
        self,
        run_id: str,
    ) -> CurrentAuthorizationSet | None: ...

    async def get_authorization_set_epoch(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> AuthorizationSet | None: ...

    async def add_initial_set_or_get(
        self,
        requests: tuple[ActionApprovalRequest, ...],
    ) -> ApprovalRequestSetInsertResult: ...

    async def record_decision(
        self,
        *,
        expected_version: int,
        expected_action_version: int,
        decision: ApprovalDecision,
    ) -> ApprovalDecisionInsertResult: ...

    async def release_current_set(
        self,
        command: AuthorizationSetReleaseCommand,
    ) -> AuthorizationSetReleaseResult: ...

    async def close_current_set(
        self,
        command: AuthorizationSetCloseCommand,
    ) -> AuthorizationSetCloseResult: ...

    async def get_release_authority(
        self,
        action_id: str,
    ) -> ReleaseAuthority | None: ...

    async def mark_expired(
        self,
        *,
        request_id: str,
        expected_version: int,
        expected_action_version: int,
        expired_at: datetime,
    ) -> StoredActionApprovalRequest: ...

    async def renew_expired(
        self,
        *,
        expected_version: int,
        expected_action_version: int,
        renewal: ApprovalRenewal,
    ) -> StoredActionApprovalRequest: ...


@dataclass(frozen=True, slots=True)
class RunStepPlanInsertResult:
    plan: RunPlanSnapshot
    steps: tuple[RunStep, ...]
    inserted: bool


class RunStepRepository(Protocol):
    async def get(self, step_id: str) -> RunStep | None: ...

    async def get_plan(self, run_id: str) -> RunPlanSnapshot | None: ...

    async def add_plan(
        self,
        plan: RunPlanSnapshot,
        selected_instances: tuple[RunPlanSelectedInstance, ...],
        assignments: tuple[RunPlanRoutingAssignment, ...],
        steps: tuple[RunStep, ...],
        initial_transitions: tuple[StepStateTransition, ...],
    ) -> RunStepPlanInsertResult: ...

    async def list_for_run(self, run_id: str) -> tuple[RunStep, ...]: ...

    async def validate_plan_for_execution(self, run_id: str) -> tuple[RunStep, ...]: ...

    async def apply_transition(
        self,
        *,
        expected_run_version: int,
        expected_run_state: RunState,
        expected_version: int,
        expected_state: StepState,
        result: StepTransitionResult,
    ) -> bool: ...

    async def list_transitions(self, step_id: str) -> tuple[StepStateTransition, ...]: ...


class AuditRepository(Protocol):
    async def append(self, event: AuditEventDraft) -> AuditEvent: ...

    async def append_many(self, events: tuple[AuditEventDraft, ...]) -> tuple[AuditEvent, ...]: ...

    async def append_global(self, event: AuditEventDraft) -> AuditEvent: ...

    async def append_global_many(
        self,
        events: tuple[AuditEventDraft, ...],
    ) -> tuple[AuditEvent, ...]: ...

    async def get(self, event_id: str) -> AuditEvent | None: ...

    async def get_attempt_event(self, run_id: str, attempt_id: str) -> AuditEvent | None: ...

    async def get_mutation_event(
        self,
        aggregate_type: str,
        aggregate_id: str,
        mutation_version: int,
    ) -> AuditEvent | None: ...

    async def list_run(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]: ...
