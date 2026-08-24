"""Atomic action-plus-approval registration and exact expiry renewal facade."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime

from marketing_agents.application.orchestration.dependencies import (
    OrchestrationDependencies,
)
from marketing_agents.application.orchestration.effect_planner import EffectPlan
from marketing_agents.application.policies.approval_authorization import (
    APPROVAL_DECIDE_SCOPE,
    APPROVER_ROLE,
)
from marketing_agents.application.ports.repositories import ApprovalRepositoryConflict
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services.approval_integrity import (
    ApprovalIntegrityError,
    renew_expired_request,
)
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.application.services.external_action_registration import (
    ExternalActionRegistrationDisposition,
    ExternalActionRegistrationService,
    RegisteredExternalAction,
    RegisteredExternalActionSet,
)
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalRenewal,
    AuthorizationSet,
    AuthorizationSetHead,
    StoredActionApprovalRequest,
)
from marketing_agents.domain.audit import AuditContext, AuditEventDraft
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
)
from marketing_agents.domain.validation import require_digest, require_id


class ApprovalRecordServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RegisteredApprovalSet:
    actions: RegisteredExternalActionSet
    requests: tuple[StoredActionApprovalRequest, ...]


@dataclass(frozen=True, slots=True)
class RenewedApprovalRequest:
    expired: StoredActionApprovalRequest
    replacement: StoredActionApprovalRequest


def _approval_semantics(request: ActionApprovalRequest) -> tuple[object, ...]:
    return (
        request.run_id,
        request.plan_hash,
        request.proposal_revision,
        request.step_key,
        request.template_id,
        request.instance_id,
        request.action_type,
        request.capability_id,
        request.connector_family,
        request.binding_id,
        request.semantic_action_hash,
        request.redacted_destination,
        request.redacted_projection,
        request.policy,
        request.requested_by,
    )


def _require_authorization_set_binding(
    plan: EffectPlan,
    authorization_set: AuthorizationSet,
    head: AuthorizationSetHead,
    stored_requests: tuple[StoredActionApprovalRequest, ...],
) -> None:
    """Require the head-selected complete epoch; this verifier never creates or repairs it."""

    try:
        head.assert_selects(authorization_set)
    except ValueError:
        raise ApprovalRecordServiceError(
            "authorization_set_head_mismatch",
            "authorization set head does not select the exact persisted epoch",
        ) from None
    expected_by_action = {stored.request.action_id: stored.request for stored in stored_requests}
    members_by_action = {member.action_id: member for member in authorization_set.members}
    first = stored_requests[0].request
    if (
        authorization_set.id != first.authorization_set_id
        or authorization_set.run_id != plan.run_id
        or authorization_set.plan_hash != plan.plan_hash
        or authorization_set.proposal_revision != first.proposal_revision
        or set(members_by_action) != set(expected_by_action)
        or any(
            member.action_hash != expected_by_action[action_id].action_hash
            or member.step_id != expected_by_action[action_id].step_id
            or member.step_key != expected_by_action[action_id].step_key
            or member.authorization_set_id != expected_by_action[action_id].authorization_set_id
            for action_id, member in members_by_action.items()
        )
    ):
        raise ApprovalRecordServiceError(
            "authorization_set_binding_mismatch",
            "authorization set does not exactly bind the planned write set",
        )


async def _mutation_draft(
    unit_of_work: UnitOfWork,
    *,
    aggregate_type: str,
    aggregate_id: str,
    mutation_version: int,
) -> AuditEventDraft:
    event = await unit_of_work.audits.get_mutation_event(
        aggregate_type,
        aggregate_id,
        mutation_version,
    )
    if event is None:
        raise ApprovalRecordServiceError(
            "approval_audit_missing",
            "authoritative approval lifecycle audit witness is missing",
        )
    return event.draft


async def _require_complete_run_timeline(
    unit_of_work: UnitOfWork,
    run_id: str,
) -> None:
    after_sequence = 0
    while True:
        page = await unit_of_work.audits.list_run(
            run_id,
            after_sequence=after_sequence,
            limit=100,
        )
        if not page:
            return
        after_sequence = page[-1].run_sequence
        if len(page) < 100:
            return


async def _require_initial_action_audits(
    unit_of_work: UnitOfWork,
    action: ExternalAction,
) -> None:
    expected = (
        (1, "action.proposed", None, "proposed", None),
        (
            2,
            "action.awaiting_approval",
            "proposed",
            "awaiting_approval",
            "approval_requested",
        ),
    )
    for version, event_type, previous_state, new_state, reason_code in expected:
        draft = await _mutation_draft(
            unit_of_work,
            aggregate_type="external_action",
            aggregate_id=action.id,
            mutation_version=version,
        )
        if (
            draft.event_type != event_type
            or draft.run_id != action.run_id
            or draft.step_id != action.step_id
            or draft.action_id != action.id
            or draft.previous_state != previous_state
            or draft.new_state != new_state
            or draft.reason_code != reason_code
            or draft.occurred_at != action.created_at
            or draft.safe_metadata.values
            != {"idempotency_support": action.delivery_contract.idempotency_support}
        ):
            raise ApprovalRecordServiceError(
                "approval_audit_conflict",
                "stored action approval audit differs from the authoritative action",
            )


async def _require_request_audit(
    unit_of_work: UnitOfWork,
    stored: StoredActionApprovalRequest,
    *,
    expected_action_version: int | None = None,
) -> None:
    request = stored.request
    draft = await _mutation_draft(
        unit_of_work,
        aggregate_type="approval_request",
        aggregate_id=request.id,
        mutation_version=1,
    )
    metadata = dict(draft.safe_metadata.values)
    action_version = metadata.pop("action_version", None)
    expected_metadata = {
        "action_state": "awaiting_approval",
        "generation": request.generation,
        "policy_id": request.policy.policy_id,
        "proposal_revision": request.proposal_revision,
        "status": "pending",
    }
    if (
        type(action_version) is not int
        or action_version < 2
        or (request.generation == 1 and action_version != 2)
        or (expected_action_version is not None and action_version != expected_action_version)
        or metadata != expected_metadata
        or draft.event_type != "approval.requested"
        or draft.run_id != request.run_id
        or draft.step_id != request.step_id
        or draft.action_id != request.action_id
        or draft.approval_request_id != request.id
        or draft.previous_state is not None
        or draft.new_state != "pending"
        or draft.reason_code != "approval_requested"
        or draft.occurred_at != request.requested_at
    ):
        raise ApprovalRecordServiceError(
            "approval_audit_conflict",
            "stored approval-request audit differs from its immutable request",
        )


async def _require_action_reopened_audit(
    unit_of_work: UnitOfWork,
    action: ExternalAction,
    *,
    mutation_version: int,
    occurred_at: datetime,
) -> None:
    draft = await _mutation_draft(
        unit_of_work,
        aggregate_type="external_action",
        aggregate_id=action.id,
        mutation_version=mutation_version,
    )
    if (
        draft.event_type != "action.awaiting_approval"
        or draft.run_id != action.run_id
        or draft.step_id != action.step_id
        or draft.action_id != action.id
        or draft.previous_state != "approved"
        or draft.new_state != "awaiting_approval"
        or draft.reason_code != "approval_expired"
        or draft.occurred_at != occurred_at
        or draft.safe_metadata.values
        != {"idempotency_support": action.delivery_contract.idempotency_support}
    ):
        raise ApprovalRecordServiceError(
            "approval_audit_conflict",
            "stored action expiry audit differs from the authoritative action",
        )


async def _require_expiry_audit(
    unit_of_work: UnitOfWork,
    expired: StoredActionApprovalRequest,
) -> tuple[int, datetime]:
    if expired.expired_at is None:
        raise ApprovalRecordServiceError(
            "approval_renewal_corrupt", "expired approval lost its expiration time"
        )
    expiry_version = (
        expired.version - 1 if expired.replacement_request_id is not None else expired.version
    )
    draft = await _mutation_draft(
        unit_of_work,
        aggregate_type="approval_request",
        aggregate_id=expired.request.id,
        mutation_version=expiry_version,
    )
    metadata = dict(draft.safe_metadata.values)
    action_version = metadata.pop("action_version", None)
    request = expired.request
    if (
        type(action_version) is not int
        or action_version < 2
        or metadata
        != {
            "action_state": "awaiting_approval",
            "generation": request.generation,
            "policy_id": request.policy.policy_id,
            "proposal_revision": request.proposal_revision,
            "status": "expired",
        }
        or draft.event_type != "approval.expired"
        or draft.previous_state != ("approved" if expired.decision is not None else "pending")
        or draft.new_state != "expired"
        or draft.reason_code != "approval_expired"
        or draft.occurred_at != expired.expired_at
    ):
        raise ApprovalRecordServiceError(
            "approval_audit_conflict",
            "stored approval-expiry audit differs from its lifecycle",
        )
    return action_version, expired.expired_at


async def _require_renewal_audit(
    unit_of_work: UnitOfWork,
    expired: StoredActionApprovalRequest,
    *,
    expected_action_version: int,
) -> None:
    if expired.replacement_request_id is None or expired.renewed_at is None:
        raise ApprovalRecordServiceError(
            "approval_renewal_corrupt", "renewed approval lost its replacement link"
        )
    draft = await _mutation_draft(
        unit_of_work,
        aggregate_type="approval_request",
        aggregate_id=expired.request.id,
        mutation_version=expired.version,
    )
    metadata = dict(draft.safe_metadata.values)
    action_version = metadata.pop("action_version", None)
    request = expired.request
    if (
        type(action_version) is not int
        or action_version != expected_action_version
        or metadata
        != {
            "action_state": "awaiting_approval",
            "generation": request.generation,
            "policy_id": request.policy.policy_id,
            "proposal_revision": request.proposal_revision,
            "replacement_request_id": expired.replacement_request_id,
            "status": "expired",
        }
        or draft.event_type != "approval.renewed"
        or draft.previous_state != "expired"
        or draft.new_state != "expired"
        or draft.reason_code != "approval_renewed"
        or draft.occurred_at != expired.renewed_at
    ):
        raise ApprovalRecordServiceError(
            "approval_audit_conflict",
            "stored approval-renewal audit differs from its lifecycle",
        )


async def _require_decision_audits(
    unit_of_work: UnitOfWork,
    stored: StoredActionApprovalRequest,
    action: ExternalAction,
    *,
    action_version: int,
) -> None:
    decision = stored.decision
    if decision is None:
        raise ApprovalRecordServiceError(
            "approval_decision_corrupt",
            "approval history expected a missing decision",
        )
    request = stored.request
    expected_status = (
        ApprovalStatus.APPROVED
        if decision.decision is ApprovalDecisionKind.APPROVE
        else ApprovalStatus.REJECTED
    )
    expected_action_state = (
        ExternalActionState.APPROVED
        if expected_status is ApprovalStatus.APPROVED
        else ExternalActionState.REJECTED
    )
    if (
        decision.authentication_method not in {"local_fixed", "bearer"}
        or decision.authority_roles != request.policy.required_roles | frozenset({APPROVER_ROLE})
        or decision.authority_scopes
        != request.policy.required_scopes | frozenset({APPROVAL_DECIDE_SCOPE})
        or (not request.policy.allow_self_approval and decision.actor_id == request.requested_by)
    ):
        raise ApprovalRecordServiceError(
            "approval_authority_snapshot_mismatch",
            "approval history lost its exact authorized human decision",
        )
    decided_request = StoredActionApprovalRequest(
        request=request,
        status=expected_status,
        version=2,
        updated_at=decision.decided_at,
        decision=decision,
    )
    decided_action = replace(
        action,
        state=expected_action_state,
        updated_at=decision.decided_at,
        version=action_version,
        delivery_attempt_count=0,
        reservation=None,
        lease=None,
        call_started_at=None,
        call_deadline_at=None,
        result=None,
        terminal_reason_code=(
            None if expected_action_state is ExternalActionState.APPROVED else "approval_rejected"
        ),
        superseded_by_action_id=None,
        superseded_at=None,
    )
    pending_action = replace(
        decided_action,
        state=ExternalActionState.AWAITING_APPROVAL,
        updated_at=request.requested_at,
        version=action_version - 1,
        terminal_reason_code=None,
    )
    context = AuditContext.authenticated_user(
        decision.actor_id,
        authentication_method=decision.authentication_method,
        correlation_id=decision.correlation_id,
    )
    factory = AuditEventFactory(context)
    expected_action = factory.action_decided(
        pending_action,
        decided_action,
        decided_request,
    )
    expected_approval = factory.approval_decided(
        StoredActionApprovalRequest.created(request),
        decided_request,
        decided_action,
    )
    action_draft = await _mutation_draft(
        unit_of_work,
        aggregate_type="external_action",
        aggregate_id=action.id,
        mutation_version=action_version,
    )
    approval_draft = await _mutation_draft(
        unit_of_work,
        aggregate_type="approval_request",
        aggregate_id=request.id,
        mutation_version=2,
    )
    if action_draft != expected_action or approval_draft != expected_approval:
        raise ApprovalRecordServiceError(
            "approval_audit_conflict",
            "stored approval decision audits differ from their exact lifecycle",
        )


async def require_complete_member_history(
    unit_of_work: UnitOfWork,
    histories: tuple[StoredActionApprovalRequest, ...],
    actions: tuple[ExternalAction, ...],
) -> None:
    """Require initial registration and every renewal generation audit witness."""

    if histories:
        await _require_complete_run_timeline(
            unit_of_work,
            histories[0].request.run_id,
        )
    grouped: dict[str, list[StoredActionApprovalRequest]] = defaultdict(list)
    for stored in histories:
        grouped[stored.request.action_id].append(stored)
    action_by_id = {action.id: action for action in actions}
    if not grouped or set(grouped) != set(action_by_id):
        raise ApprovalRecordServiceError(
            "approval_history_membership_mismatch",
            "approval history does not exactly cover the authorization members",
        )
    for action_id, action in action_by_id.items():
        chain = sorted(grouped[action_id], key=lambda stored: stored.request.generation)
        if [stored.request.generation for stored in chain] != list(range(1, len(chain) + 1)):
            raise ApprovalRecordServiceError(
                "approval_history_generation_gap",
                "approval history generations are not contiguous",
            )
        await _require_initial_action_audits(unit_of_work, action)
        expected_action_version = 2
        for index, stored in enumerate(chain):
            await _require_request_audit(
                unit_of_work,
                stored,
                expected_action_version=expected_action_version,
            )
            if stored.decision is not None:
                expected_action_version += 1
                await _require_decision_audits(
                    unit_of_work,
                    stored,
                    action,
                    action_version=expected_action_version,
                )
            if stored.expired_at is not None:
                expiry_action_version, expiry_time = await _require_expiry_audit(
                    unit_of_work,
                    stored,
                )
                expected_expiry_action_version = expected_action_version + (
                    1 if stored.decision is not None else 0
                )
                if expiry_action_version != expected_expiry_action_version:
                    raise ApprovalRecordServiceError(
                        "approval_history_action_version_mismatch",
                        "approval expiry history disagrees with its action generation",
                    )
                if stored.decision is not None:
                    await _require_action_reopened_audit(
                        unit_of_work,
                        action,
                        mutation_version=expiry_action_version,
                        occurred_at=expiry_time,
                    )
                expected_action_version = expiry_action_version
            if stored.replacement_request_id is not None:
                if (
                    index + 1 >= len(chain)
                    or chain[index + 1].request.id != stored.replacement_request_id
                ):
                    raise ApprovalRecordServiceError(
                        "approval_history_replacement_missing",
                        "approval renewal history lost its exact replacement request",
                    )
                await _require_renewal_audit(
                    unit_of_work,
                    stored,
                    expected_action_version=expected_action_version,
                )
            elif index + 1 != len(chain):
                raise ApprovalRecordServiceError(
                    "approval_history_replacement_mismatch",
                    "approval history contains an unlinked later generation",
                )


class ApprovalRecordService:
    """Compose RUN-05 action persistence with a complete approval request set."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        *,
        delivery_attempt_limit: int = 2,
    ) -> None:
        self._dependencies = dependencies
        self._actions = ExternalActionRegistrationService(
            dependencies,
            delivery_attempt_limit=delivery_attempt_limit,
        )

    async def register_plan(
        self,
        plan: EffectPlan,
        *,
        audit_context: AuditContext,
    ) -> RegisteredApprovalSet:
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self.register_plan_in_uow(
                unit_of_work,
                plan,
                audit_context=audit_context,
            )
            await unit_of_work.commit()
            return result

    async def register_plan_in_uow(
        self,
        unit_of_work: UnitOfWork,
        plan: EffectPlan,
        *,
        audit_context: AuditContext,
    ) -> RegisteredApprovalSet:
        registered = await self._actions.register_plan_actions_in_uow(unit_of_work, plan)
        proposed_actions = tuple(item.action for item in registered.actions)
        if registered.disposition is ExternalActionRegistrationDisposition.CREATED:
            inserted = await unit_of_work.approvals.add_initial_set_or_get(plan.approval_requests)
            if not inserted.inserted:
                raise ApprovalRecordServiceError(
                    "approval_create_race",
                    "new actions unexpectedly resolved to an existing approval set",
                )
            requests = inserted.requests
            authorization_set = inserted.authorization_set
            authorization_head = inserted.head
        else:
            try:
                selection = await unit_of_work.approvals.get_current_authorization_set(plan.run_id)
                epoch = await unit_of_work.approvals.get_authorization_set_epoch(
                    plan.run_id,
                    plan.plan_hash,
                    plan.proposed_actions[0].envelope.proposal_revision,
                )
                requests = await unit_of_work.approvals.list_current_set(
                    plan.run_id,
                    plan.plan_hash,
                    plan.proposed_actions[0].envelope.proposal_revision,
                )
            except ApprovalRepositoryConflict:
                raise
            except RuntimeError as exc:
                raise ApprovalRecordServiceError(
                    getattr(exc, "code", "approval_set_corrupt"),
                    "authorization set replay could not be validated",
                ) from None
            if selection is None or epoch is None or selection.authorization_set != epoch:
                raise ApprovalRecordServiceError(
                    "authorization_set_head_missing",
                    "replayed approval set lacks its exact current head and epoch",
                )
            authorization_set = selection.authorization_set
            authorization_head = selection.head
            candidate_by_step = {request.step_key: request for request in plan.approval_requests}
            stored_by_step = {stored.request.step_key: stored for stored in requests}
            actions_by_step = {
                item.action.envelope.step_key: item.action for item in registered.actions
            }
            if (
                set(candidate_by_step) != set(stored_by_step)
                or set(candidate_by_step) != set(actions_by_step)
                or any(
                    _approval_semantics(candidate_by_step[step_key])
                    != _approval_semantics(stored_by_step[step_key].request)
                    or stored_by_step[step_key].request.action_id != actions_by_step[step_key].id
                    or stored_by_step[step_key].request.action_hash
                    != actions_by_step[step_key].action_hash
                    for step_key in candidate_by_step
                )
            ):
                raise ApprovalRecordServiceError(
                    "approval_replay_conflict",
                    "replayed plan differs from the authoritative approval request set",
                )
        _require_authorization_set_binding(
            plan,
            authorization_set,
            authorization_head,
            tuple(requests),
        )
        reloaded_actions = await unit_of_work.external_actions.list_plan_set(
            plan.run_id,
            plan.plan_hash,
            plan.proposed_actions[0].envelope.proposal_revision,
        )
        reloaded_by_step = {action.envelope.step_key: action for action in reloaded_actions}
        action_order = tuple(item.action.envelope.step_key for item in registered.actions)
        if set(reloaded_by_step) != set(action_order):
            raise ApprovalRecordServiceError(
                "partial_action_set", "registered actions disappeared before commit"
            )
        registered = RegisteredExternalActionSet(
            actions=tuple(
                RegisteredExternalAction(reloaded_by_step[step_key]) for step_key in action_order
            ),
            disposition=registered.disposition,
        )
        stored_by_action = {stored.request.action_id: stored for stored in requests}
        if set(stored_by_action) != {item.action.id for item in registered.actions}:
            raise ApprovalRecordServiceError(
                "partial_approval_set",
                "approval request set does not exactly cover registered actions",
            )
        ordered_requests = tuple(stored_by_action[item.action.id] for item in registered.actions)
        factory = AuditEventFactory(audit_context)
        if registered.disposition is ExternalActionRegistrationDisposition.CREATED:
            proposed_by_step = {action.envelope.step_key: action for action in proposed_actions}
            events: list[AuditEventDraft] = []
            for item, stored in zip(
                registered.actions,
                ordered_requests,
                strict=True,
            ):
                action = item.action
                proposed = proposed_by_step[action.envelope.step_key]
                events.extend(
                    (
                        factory.action_proposed(proposed),
                        factory.action_awaiting_approval(
                            action,
                            previous_state=ExternalActionState.PROPOSED,
                        ),
                        factory.approval_requested(stored, action),
                    )
                )
            await unit_of_work.audits.append_many(tuple(events))
        else:
            await _require_complete_run_timeline(unit_of_work, plan.run_id)
            for item, stored in zip(
                registered.actions,
                ordered_requests,
                strict=True,
            ):
                await _require_initial_action_audits(unit_of_work, item.action)
                await _require_request_audit(unit_of_work, stored)
        return RegisteredApprovalSet(actions=registered, requests=ordered_requests)

    async def mark_expired(
        self,
        *,
        request_id: str,
        expected_version: int,
        audit_context: AuditContext,
    ) -> StoredActionApprovalRequest:
        self._validate_expiry_identity(request_id, expected_version)
        async with self._dependencies.unit_of_work() as unit_of_work:
            expired = await self.mark_expired_in_uow(
                unit_of_work,
                request_id=request_id,
                expected_version=expected_version,
                audit_context=audit_context,
            )
            await unit_of_work.commit()
            return expired

    async def mark_expired_in_uow(
        self,
        unit_of_work: UnitOfWork,
        *,
        request_id: str,
        expected_version: int,
        audit_context: AuditContext,
        expired_at: datetime | None = None,
    ) -> StoredActionApprovalRequest:
        """Expire one leaf without committing so a boundary check can stay atomic."""

        self._validate_expiry_identity(request_id, expected_version)
        current = await unit_of_work.approvals.get(request_id)
        if current is None:
            raise ApprovalRecordServiceError(
                "approval_request_missing", "approval expiration request does not exist"
            )
        if current.status is ApprovalStatus.EXPIRED:
            await _require_complete_run_timeline(
                unit_of_work,
                current.request.run_id,
            )
            if expected_version not in {current.version, current.version - 1}:
                raise ApprovalRecordServiceError(
                    "approval_expiration_conflict",
                    "expiration replay does not match the original approval version",
                )
            action = await unit_of_work.external_actions.get(current.request.action_id)
            if action is None:
                raise ApprovalRecordServiceError(
                    "approval_action_missing",
                    "expired approval lost its exact action",
                )
            if current.decision is not None:
                action_version, recorded_expiry = await _require_expiry_audit(
                    unit_of_work,
                    current,
                )
                await _require_action_reopened_audit(
                    unit_of_work,
                    action,
                    mutation_version=action_version,
                    occurred_at=recorded_expiry,
                )
            else:
                await _require_expiry_audit(unit_of_work, current)
            return current
        action = await unit_of_work.external_actions.get(current.request.action_id)
        if action is None:
            raise ApprovalRecordServiceError(
                "approval_action_missing", "approval expiration lost its exact action"
            )
        expiration_time = expired_at or self._dependencies.utc_now()
        expired = await unit_of_work.approvals.mark_expired(
            request_id=request_id,
            expected_version=expected_version,
            expected_action_version=action.version,
            expired_at=expiration_time,
        )
        updated_action = await unit_of_work.external_actions.get(action.id)
        if updated_action is None:
            raise ApprovalRecordServiceError(
                "approval_action_missing", "expired approval action disappeared"
            )
        factory = AuditEventFactory(audit_context)
        events: list[AuditEventDraft] = []
        if current.status is ApprovalStatus.APPROVED:
            events.append(
                factory.action_awaiting_approval(
                    updated_action,
                    previous_state=ExternalActionState.APPROVED,
                )
            )
        events.append(factory.approval_expired(current, expired, updated_action))
        await unit_of_work.audits.append_many(tuple(events))
        return expired

    @staticmethod
    def _validate_expiry_identity(request_id: str, expected_version: int) -> None:
        if type(expected_version) is not int or expected_version < 1:
            raise ApprovalRecordServiceError(
                "approval_version_invalid",
                "expected approval version must be a positive integer",
            )
        try:
            require_id(request_id, "approval request ID")
        except (TypeError, ValueError):
            raise ApprovalRecordServiceError(
                "approval_request_invalid", "approval request ID is invalid"
            ) from None

    async def renew_expired(
        self,
        *,
        request_id: str,
        expected_version: int,
        expected_action_hash: str,
        audit_context: AuditContext,
    ) -> RenewedApprovalRequest:
        if type(expected_version) is not int or expected_version < 1:
            raise ApprovalRecordServiceError(
                "approval_version_invalid",
                "expected approval version must be a positive integer",
            )
        try:
            require_id(request_id, "approval request ID")
            require_digest(expected_action_hash, "approval action hash")
        except (TypeError, ValueError):
            raise ApprovalRecordServiceError(
                "approval_request_invalid", "approval renewal identity is invalid"
            ) from None
        async with self._dependencies.unit_of_work() as unit_of_work:
            current = await unit_of_work.approvals.get(request_id)
            if current is None:
                raise ApprovalRecordServiceError(
                    "approval_request_missing", "approval renewal request does not exist"
                )
            action = await unit_of_work.external_actions.get(current.request.action_id)
            if action is None:
                raise ApprovalRecordServiceError(
                    "approval_action_missing", "approval renewal lost its exact action"
                )
            if expected_action_hash != current.request.action_hash:
                raise ApprovalRecordServiceError(
                    "expected_hash_mismatch",
                    "client expected hash is not the current approval hash",
                )
            if current.replacement_request_id is not None:
                await _require_complete_run_timeline(
                    unit_of_work,
                    current.request.run_id,
                )
                if expected_version not in {current.version - 1, current.version - 2}:
                    raise ApprovalRecordServiceError(
                        "approval_renewal_conflict",
                        "renewal replay does not match the original approval version",
                    )
                replacement = await unit_of_work.approvals.get(current.replacement_request_id)
                if replacement is None:
                    raise ApprovalRecordServiceError(
                        "approval_renewal_corrupt",
                        "linked approval replacement is missing",
                    )
                try:
                    ApprovalRenewal(
                        expired=current,
                        replacement=replacement.request,
                    )
                except ValueError:
                    raise ApprovalRecordServiceError(
                        "approval_renewal_corrupt",
                        "linked approval replacement is not the exact renewal leaf",
                    ) from None
                action_version, expired_at = await _require_expiry_audit(unit_of_work, current)
                if current.decision is not None:
                    await _require_action_reopened_audit(
                        unit_of_work,
                        action,
                        mutation_version=action_version,
                        occurred_at=expired_at,
                    )
                await _require_renewal_audit(
                    unit_of_work,
                    current,
                    expected_action_version=action_version,
                )
                await _require_request_audit(
                    unit_of_work,
                    replacement,
                    expected_action_version=action_version,
                )
                return RenewedApprovalRequest(
                    expired=current,
                    replacement=replacement,
                )
            try:
                renewal = renew_expired_request(
                    current=current,
                    replacement_request_id=self._dependencies.new_id("approval-request"),
                    exact_action=action.proposal,
                    now=self._dependencies.utc_now(),
                    expected_client_hash=expected_action_hash,
                )
            except ApprovalIntegrityError as exc:
                raise ApprovalRecordServiceError(exc.code, str(exc)) from None
            expired = await unit_of_work.approvals.renew_expired(
                expected_version=expected_version,
                expected_action_version=action.version,
                renewal=renewal,
            )
            replacement = await unit_of_work.approvals.get(renewal.replacement.id)
            if replacement is None:
                raise ApprovalRecordServiceError(
                    "approval_renewal_corrupt",
                    "approval replacement disappeared before commit",
                )
            updated_action = await unit_of_work.external_actions.get(action.id)
            if updated_action is None:
                raise ApprovalRecordServiceError(
                    "approval_action_missing", "renewed approval action disappeared"
                )
            factory = AuditEventFactory(audit_context)
            events: list[AuditEventDraft] = []
            if current.status is ApprovalStatus.APPROVED:
                events.append(
                    factory.action_awaiting_approval(
                        updated_action,
                        previous_state=ExternalActionState.APPROVED,
                    )
                )
            if current.status is ApprovalStatus.EXPIRED:
                await _require_expiry_audit(unit_of_work, current)
                expiry = current
            else:
                if renewal.expired.expired_at is None:
                    raise ApprovalRecordServiceError(
                        "approval_renewal_corrupt",
                        "approval renewal lost its expiration time",
                    )
                expiry = StoredActionApprovalRequest(
                    request=current.request,
                    status=ApprovalStatus.EXPIRED,
                    version=current.version + 1,
                    updated_at=renewal.expired.expired_at,
                    decision=current.decision,
                    expired_at=renewal.expired.expired_at,
                )
                events.append(factory.approval_expired(current, expiry, updated_action))
            events.extend(
                (
                    factory.approval_requested(replacement, updated_action),
                    factory.approval_renewed(expiry, renewal, updated_action),
                )
            )
            await unit_of_work.audits.append_many(tuple(events))
            await unit_of_work.commit()
            return RenewedApprovalRequest(expired=expired, replacement=replacement)
