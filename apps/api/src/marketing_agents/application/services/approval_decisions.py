"""Authenticated one-winner approval decisions with atomic audit witnesses."""

from __future__ import annotations

from dataclasses import dataclass, field

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.policies.approval_authorization import (
    ApprovalAuthorizationError,
    authorize_approval_decision,
    authorize_approval_principal,
)
from marketing_agents.application.ports.repositories import (
    ApprovalRepositoryConflict,
    ExternalActionRepositoryConflict,
)
from marketing_agents.application.services.approval_integrity import (
    ApprovalIntegrityError,
    validate_current_action,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.approval import ApprovalDecision, StoredActionApprovalRequest
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.validation import require_digest, require_id, require_text

MAX_APPROVAL_REASON_LENGTH = 500


class ApprovalDecisionServiceError(ValueError):
    """Stable, non-sensitive decision failure for later API problem mapping."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApprovalDecisionCommand:
    """Server-built command; actor, grants, action version, decision ID, and time are absent."""

    request_id: str
    expected_generation: int
    expected_action_hash: str
    decision: ApprovalDecisionKind
    correlation_id: str
    reason: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        require_id(self.request_id, "approval request ID")
        if type(self.expected_generation) is not int or self.expected_generation < 1:
            raise ValueError("approval expected generation must be a positive integer")
        require_digest(self.expected_action_hash, "approval expected action hash")
        if type(self.decision) is not ApprovalDecisionKind:
            raise ValueError("approval decision must use the exact decision enum")
        if self.reason is not None:
            require_text(
                self.reason,
                "approval decision reason",
                maximum=MAX_APPROVAL_REASON_LENGTH,
            )
        require_id(self.correlation_id, "approval correlation ID")


@dataclass(frozen=True, slots=True)
class AuthorizedApprovalDecision:
    request: StoredActionApprovalRequest
    action: ExternalAction
    decision: ApprovalDecision


class ApprovalDecisionService:
    """Derive trusted authority and atomically persist decision, action, and two audits."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
    ) -> None:
        self._dependencies = dependencies

    async def decide(
        self,
        command: ApprovalDecisionCommand,
        *,
        principal: AuthenticatedPrincipal,
    ) -> AuthorizedApprovalDecision:
        if type(command) is not ApprovalDecisionCommand:
            raise ApprovalDecisionServiceError(
                "approval_command_invalid",
                "approval decision command is invalid",
            )
        try:
            authorize_approval_principal(principal)
        except ApprovalAuthorizationError as exc:
            raise ApprovalDecisionServiceError(exc.code, str(exc)) from None
        async with self._dependencies.unit_of_work() as unit_of_work:
            try:
                current = await unit_of_work.approvals.get(command.request_id)
            except ApprovalRepositoryConflict:
                raise ApprovalDecisionServiceError(
                    "approval_record_corrupt",
                    "approval request could not be validated",
                ) from None
            if current is None:
                raise ApprovalDecisionServiceError(
                    "approval_request_missing",
                    "approval request does not exist",
                )
            request = current.request
            try:
                authority = authorize_approval_decision(principal, request)
            except ApprovalAuthorizationError as exc:
                raise ApprovalDecisionServiceError(exc.code, str(exc)) from None
            if request.generation != command.expected_generation:
                raise ApprovalDecisionServiceError(
                    "approval_generation_conflict",
                    "approval generation changed",
                )
            if current.status is not ApprovalStatus.PENDING or current.version != 1:
                raise ApprovalDecisionServiceError(
                    "approval_decision_conflict",
                    "approval request is no longer pending",
                )
            try:
                current_set = await unit_of_work.approvals.list_current_set(
                    request.run_id,
                    request.plan_hash,
                    request.proposal_revision,
                )
            except ApprovalRepositoryConflict:
                raise ApprovalDecisionServiceError(
                    "approval_record_corrupt",
                    "approval set could not be validated",
                ) from None
            matching_leaves = tuple(
                leaf
                for leaf in current_set
                if leaf.request.action_id == request.action_id and leaf.request.id == request.id
            )
            if len(matching_leaves) != 1 or matching_leaves[0] != current:
                raise ApprovalDecisionServiceError(
                    "approval_generation_conflict",
                    "approval request is not the unique current generation",
                )
            try:
                action = await unit_of_work.external_actions.get(request.action_id)
            except ExternalActionRepositoryConflict:
                raise ApprovalDecisionServiceError(
                    "approval_action_corrupt",
                    "approval action could not be validated",
                ) from None
            if action is None:
                raise ApprovalDecisionServiceError(
                    "approval_action_missing",
                    "approval action does not exist",
                )
            if (
                action.state is not ExternalActionState.AWAITING_APPROVAL
                or action.action_hash != request.action_hash
                or action.id != request.action_id
            ):
                raise ApprovalDecisionServiceError(
                    "approval_action_conflict",
                    "approval action is not awaiting this exact decision",
                )
            try:
                validate_current_action(
                    request,
                    action.envelope,
                    expected_client_hash=command.expected_action_hash,
                )
            except ApprovalIntegrityError as exc:
                code = (
                    "approval_hash_mismatch"
                    if exc.code == "expected_hash_mismatch"
                    else "approval_action_conflict"
                )
                raise ApprovalDecisionServiceError(
                    code,
                    "approval request no longer binds the expected action",
                ) from None
            decided_at = self._dependencies.utc_now()
            if decided_at >= request.expires_at:
                raise ApprovalDecisionServiceError(
                    "approval_expired",
                    "approval request has expired",
                )
            reason_code = (
                "approval_granted"
                if command.decision is ApprovalDecisionKind.APPROVE
                else "approval_rejected"
            )
            decision = ApprovalDecision(
                id=self._dependencies.new_id("approval-decision"),
                request_id=request.id,
                action_id=request.action_id,
                action_hash=request.action_hash,
                authorization_set_id=request.authorization_set_id,
                run_id=request.run_id,
                plan_hash=request.plan_hash,
                proposal_revision=request.proposal_revision,
                step_id=request.step_id,
                step_key=request.step_key,
                actor_id=authority.actor_id,
                authentication_method=authority.authentication_method.value,
                correlation_id=command.correlation_id,
                decision=command.decision,
                authority_roles=authority.matched_roles,
                authority_scopes=authority.matched_scopes,
                reason_code=reason_code,
                decided_at=decided_at,
            )
            try:
                inserted = await unit_of_work.approvals.record_decision(
                    expected_version=current.version,
                    expected_action_version=action.version,
                    decision=decision,
                )
            except ApprovalRepositoryConflict:
                raise ApprovalDecisionServiceError(
                    "approval_decision_conflict",
                    "another decision won or the approval changed",
                ) from None
            if not inserted.inserted:
                raise ApprovalDecisionServiceError(
                    "approval_decision_conflict",
                    "approval decision was already recorded",
                )
            decided_request = inserted.request
            try:
                decided_action = await unit_of_work.external_actions.get(action.id)
            except ExternalActionRepositoryConflict:
                raise ApprovalDecisionServiceError(
                    "approval_action_corrupt",
                    "decided approval action could not be validated",
                ) from None
            if decided_action is None:
                raise ApprovalDecisionServiceError(
                    "approval_action_missing",
                    "decided approval action disappeared",
                )
            audit_context = AuditContext.authenticated_user(
                authority.actor_id,
                authentication_method=authority.authentication_method.value,
                correlation_id=command.correlation_id,
            )
            factory = AuditEventFactory(audit_context)
            await unit_of_work.audits.append_many(
                (
                    factory.action_decided(action, decided_action, decided_request),
                    factory.approval_decided(current, decided_request, decided_action),
                )
            )
            await unit_of_work.commit()
            return AuthorizedApprovalDecision(
                request=decided_request,
                action=decided_action,
                decision=decision,
            )
