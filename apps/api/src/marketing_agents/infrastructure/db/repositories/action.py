"""SQLAlchemy persistence for exact external actions and connector receipts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import (
    ActionCallStartResult,
    ConnectorReceiptInsertResult,
    ExternalActionRepositoryConflict,
    ExternalActionSetInsertResult,
    ReleaseAuthority,
    ReleaseCallMode,
)
from marketing_agents.domain.action_hash import CanonicalExternalAction
from marketing_agents.domain.approval import ApprovalPolicySnapshot, ProposedExternalAction
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import (
    ActionReservationSnapshot,
    ConnectorActionReceipt,
    DeliveryContractSnapshot,
    DispatchLease,
    ExternalAction,
    ExternalActionResultSnapshot,
)
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState
from marketing_agents.domain.step_lifecycle import StepLifecycleCommand, StepTransitionResult
from marketing_agents.domain.validation import require_digest, require_id, require_utc
from marketing_agents.infrastructure.db.models.action import (
    ConnectorActionReceiptRecord,
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
)
from marketing_agents.infrastructure.db.models.approval import (
    ApprovalRequestRecord,
    ApprovalUseRecord,
    AuthorizationSetHeadRecord,
    AuthorizationSetMemberRecord,
    AuthorizationSetRecord,
)
from marketing_agents.infrastructure.db.models.execution_control import (
    RunExecutionControlRecord,
)
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.infrastructure.db.models.step import (
    RunStepDependencyRecord,
    RunStepRecord,
)
from marketing_agents.infrastructure.db.repositories.step import SQLAlchemyRunStepRepository


class ExternalActionPersistenceConflict(ExternalActionRepositoryConflict):
    """Raised when a stable key resolves to incompatible persisted semantics."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class _ActionCASLost(RuntimeError):
    pass


def _plain_json(value: object) -> object:
    return json.loads(canonical_json_bytes(value))


def _policy_projection(policy: ApprovalPolicySnapshot) -> dict[str, object]:
    return {
        "policy_id": policy.policy_id,
        "required_roles": sorted(policy.required_roles),
        "required_scopes": sorted(policy.required_scopes),
        "expires_after_seconds": policy.expires_after_seconds,
        "allow_self_approval": policy.allow_self_approval,
    }


def _policy_from_json(value: object) -> ApprovalPolicySnapshot:
    if type(value) is not dict or set(value) != {
        "policy_id",
        "required_roles",
        "required_scopes",
        "expires_after_seconds",
        "allow_self_approval",
    }:
        raise ExternalActionPersistenceConflict(
            "approval_policy_corrupt", "persisted approval policy has an invalid shape"
        )
    policy_json = cast(dict[str, object], value)
    policy_id = policy_json["policy_id"]
    roles = policy_json["required_roles"]
    scopes = policy_json["required_scopes"]
    expiry = policy_json["expires_after_seconds"]
    allow_self = policy_json["allow_self_approval"]
    if (
        type(policy_id) is not str
        or type(roles) is not list
        or type(scopes) is not list
        or any(type(item) is not str for item in (*roles, *scopes))
        or len(set(roles)) != len(roles)
        or len(set(scopes)) != len(scopes)
        or type(expiry) is not int
        or type(allow_self) is not bool
    ):
        raise ExternalActionPersistenceConflict(
            "approval_policy_corrupt", "persisted approval policy types are invalid"
        )
    return ApprovalPolicySnapshot(
        policy_id=policy_id,
        required_roles=frozenset(cast(list[str], roles)),
        required_scopes=frozenset(cast(list[str], scopes)),
        expires_after_seconds=expiry,
        allow_self_approval=allow_self,
    )


def _to_record(action: ExternalAction) -> ExternalActionRecord:
    envelope = action.envelope
    reservation = action.reservation
    lease = action.lease
    result = action.result
    return ExternalActionRecord(
        id=action.id,
        run_id=envelope.run_id,
        step_id=envelope.step_id,
        authorization_set_id=envelope.authorization_set_id,
        plan_hash=envelope.plan_hash,
        proposal_revision=envelope.proposal_revision,
        step_key=envelope.step_key,
        action_type=envelope.action_type,
        capability_id=envelope.capability_id,
        connector_family=envelope.connector_family,
        connector_binding_id=envelope.binding_id,
        semantic_action_hash=envelope.semantic_action_hash,
        action_hash=action.action_hash,
        idempotency_key=action.idempotency_key,
        canonical_envelope=cast(
            dict[str, object], _plain_json(envelope.authorization_projection())
        ),
        redacted_projection=cast(
            dict[str, object], _plain_json(action.proposal.redacted_projection)
        ),
        approval_policy_snapshot=_policy_projection(action.approval_policy),
        binding_configuration_revision=(action.delivery_contract.binding_configuration_revision),
        request_schema_id=action.delivery_contract.request_schema_id,
        idempotency_support=action.delivery_contract.idempotency_support,
        timeout_seconds=action.delivery_contract.timeout_seconds,
        state=action.state.value,
        created_at=action.created_at,
        updated_at=action.updated_at,
        version=action.version,
        delivery_attempt_count=action.delivery_attempt_count,
        delivery_attempt_limit=action.delivery_attempt_limit,
        reservation_id=None if reservation is None else reservation.reservation_id,
        reservation_authorization_set_id=(
            None if reservation is None else reservation.authorization_set_id
        ),
        approval_request_id=(None if reservation is None else reservation.approval_request_id),
        approval_decision_id=(None if reservation is None else reservation.approval_decision_id),
        reservation_action_hash=(None if reservation is None else reservation.action_hash),
        reservation_capability_id=(None if reservation is None else reservation.capability_id),
        reservation_binding_id=(None if reservation is None else reservation.binding_id),
        reservation_idempotency_key=(None if reservation is None else reservation.idempotency_key),
        reserved_at=None if reservation is None else reservation.reserved_at,
        dispatch_lease_owner=None if lease is None else lease.owner,
        dispatch_attempt_number=None if lease is None else lease.attempt_number,
        dispatch_claimed_at=None if lease is None else lease.claimed_at,
        dispatch_lease_expires_at=None if lease is None else lease.expires_at,
        connector_call_started_at=action.call_started_at,
        connector_call_deadline_at=action.call_deadline_at,
        connector_receipt_id=None if result is None else result.receipt_id,
        connector_result_status=None if result is None else result.status,
        connector_safe_metadata=(
            None if result is None else cast(dict[str, object], _plain_json(result.safe_metadata))
        ),
        completed_at=None if result is None else result.completed_at,
        terminal_reason_code=action.terminal_reason_code,
        superseded_by_action_id=action.superseded_by_action_id,
        superseded_at=action.superseded_at,
    )


def _to_domain(record: ExternalActionRecord) -> ExternalAction:
    envelope = CanonicalExternalAction.model_validate(record.canonical_envelope, strict=True)
    duplicated_identity = (
        (record.id, envelope.action_id),
        (record.run_id, envelope.run_id),
        (record.step_id, envelope.step_id),
        (record.authorization_set_id, envelope.authorization_set_id),
        (record.plan_hash, envelope.plan_hash),
        (record.proposal_revision, envelope.proposal_revision),
        (record.step_key, envelope.step_key),
        (record.action_type, envelope.action_type),
        (record.capability_id, envelope.capability_id),
        (record.connector_family, envelope.connector_family),
        (record.connector_binding_id, envelope.binding_id),
        (record.semantic_action_hash, envelope.semantic_action_hash),
        (record.request_schema_id, envelope.payload_schema_id),
    )
    if any(stored != canonical for stored, canonical in duplicated_identity):
        raise ExternalActionPersistenceConflict(
            "action_identity_corrupt",
            "external action scalar identity differs from its canonical envelope",
        )
    policy = _policy_from_json(record.approval_policy_snapshot)
    proposal = ProposedExternalAction(
        envelope=envelope,
        action_hash=record.action_hash,
        redacted_projection=record.redacted_projection,
    )
    contract = DeliveryContractSnapshot(
        capability_id=record.capability_id,
        connector_family=record.connector_family,
        binding_id=record.connector_binding_id,
        binding_configuration_revision=record.binding_configuration_revision,
        request_schema_id=record.request_schema_id,
        idempotency_support=cast(
            Literal["required", "supported", "unavailable"],
            record.idempotency_support,
        ),
        timeout_seconds=record.timeout_seconds,
    )
    reservation = None
    if record.reservation_id is not None:
        reservation = ActionReservationSnapshot(
            reservation_id=record.reservation_id,
            authorization_set_id=cast(str, record.reservation_authorization_set_id),
            approval_request_id=cast(str, record.approval_request_id),
            approval_decision_id=cast(str, record.approval_decision_id),
            action_hash=cast(str, record.reservation_action_hash),
            capability_id=cast(str, record.reservation_capability_id),
            binding_id=cast(str, record.reservation_binding_id),
            idempotency_key=cast(str, record.reservation_idempotency_key),
            reserved_at=cast(datetime, record.reserved_at),
        )
    lease = None
    if record.dispatch_lease_owner is not None:
        lease = DispatchLease(
            owner=record.dispatch_lease_owner,
            attempt_number=cast(int, record.dispatch_attempt_number),
            claimed_at=cast(datetime, record.dispatch_claimed_at),
            expires_at=cast(datetime, record.dispatch_lease_expires_at),
        )
    result = None
    if record.connector_receipt_id is not None:
        result = ExternalActionResultSnapshot(
            receipt_id=record.connector_receipt_id,
            status=cast(str, record.connector_result_status),
            safe_metadata=cast(dict[str, object], record.connector_safe_metadata),
            completed_at=cast(datetime, record.completed_at),
        )
    return ExternalAction(
        proposal=proposal,
        approval_policy=policy,
        delivery_contract=contract,
        idempotency_key=record.idempotency_key,
        state=ExternalActionState(record.state),
        created_at=record.created_at,
        updated_at=record.updated_at,
        version=record.version,
        delivery_attempt_count=record.delivery_attempt_count,
        delivery_attempt_limit=record.delivery_attempt_limit,
        reservation=reservation,
        lease=lease,
        call_started_at=record.connector_call_started_at,
        call_deadline_at=record.connector_call_deadline_at,
        result=result,
        terminal_reason_code=record.terminal_reason_code,
        superseded_by_action_id=record.superseded_by_action_id,
        superseded_at=record.superseded_at,
    )


def _replay_projection(action: ExternalAction) -> bytes:
    envelope = action.envelope
    return canonical_json_bytes(
        {
            "key_material": asdict(envelope.key_material()),
            "semantic_action": envelope.semantic_action().semantic_projection(),
            "redacted_projection": action.proposal.redacted_projection,
            "approval_policy": _policy_projection(action.approval_policy),
            "delivery_contract": asdict(action.delivery_contract),
            "delivery_attempt_limit": action.delivery_attempt_limit,
        }
    )


def _is_sqlite_busy(session: AsyncSession, exc: OperationalError) -> bool:
    return bool(
        session.get_bind().dialect.name == "sqlite"
        and getattr(exc.orig, "sqlite_errorcode", None)
        in {sqlite3.SQLITE_BUSY, getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517)}
    )


class SQLAlchemyExternalActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _validated_action(self, record: ExternalActionRecord) -> ExternalAction:
        action = _to_domain(record)
        if action.state is not ExternalActionState.DISPATCHING:
            return action

        lease = action.lease
        if lease is None:  # pragma: no cover - guarded by ExternalAction hydration
            raise ExternalActionPersistenceConflict(
                "dispatch_attempt_corrupt",
                "dispatching action does not retain an exact current attempt",
            )
        statement = (
            select(ExternalActionDispatchAttemptRecord)
            .where(
                ExternalActionDispatchAttemptRecord.external_action_id == action.id,
                ExternalActionDispatchAttemptRecord.attempt_number == lease.attempt_number,
            )
            .execution_options(populate_existing=True)
        )
        attempt = (await self._session.execute(statement)).scalar_one_or_none()
        if (
            attempt is None
            or attempt.idempotency_support != action.delivery_contract.idempotency_support
            or attempt.lease_owner != lease.owner
            or attempt.claimed_at != lease.claimed_at
            or attempt.lease_expires_at != lease.expires_at
            or attempt.call_started_at != action.call_started_at
            or attempt.call_deadline_at != action.call_deadline_at
            or attempt.completed_at is not None
            or attempt.conclusion is not None
            or attempt.reason_code is not None
            or attempt.connector_receipt_id is not None
        ):
            raise ExternalActionPersistenceConflict(
                "dispatch_attempt_corrupt",
                "dispatching action does not retain an exact current attempt",
            )
        return action

    async def get(self, action_id: str) -> ExternalAction | None:
        record = await self._session.get(ExternalActionRecord, action_id)
        return None if record is None else await self._validated_action(record)

    async def get_by_idempotency_key(self, idempotency_key: str) -> ExternalAction | None:
        statement = select(ExternalActionRecord).where(
            ExternalActionRecord.idempotency_key == idempotency_key
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else await self._validated_action(record)

    async def list_plan_set(
        self, run_id: str, plan_hash: str, proposal_revision: int
    ) -> tuple[ExternalAction, ...]:
        statement = (
            select(ExternalActionRecord)
            .where(
                ExternalActionRecord.run_id == run_id,
                ExternalActionRecord.plan_hash == plan_hash,
                ExternalActionRecord.proposal_revision == proposal_revision,
            )
            .order_by(ExternalActionRecord.step_key)
        )
        rows = tuple((await self._session.execute(statement)).scalars())
        return tuple([await self._validated_action(row) for row in rows])

    async def list_run_plan(
        self,
        run_id: str,
        plan_hash: str,
    ) -> tuple[ExternalAction, ...]:
        statement = (
            select(ExternalActionRecord)
            .where(
                ExternalActionRecord.run_id == run_id,
                ExternalActionRecord.plan_hash == plan_hash,
            )
            .order_by(
                ExternalActionRecord.proposal_revision,
                ExternalActionRecord.step_key,
            )
        )
        rows = tuple((await self._session.execute(statement)).scalars())
        return tuple([await self._validated_action(row) for row in rows])

    async def add_proposed_set_or_get(
        self, actions: tuple[ExternalAction, ...]
    ) -> ExternalActionSetInsertResult:
        self._validate_new_set(actions)
        try:
            async with self._session.begin_nested():
                self._session.add_all([_to_record(action) for action in actions])
                await self._session.flush()
        except IntegrityError:
            first = actions[0]
            existing = await self.list_plan_set(
                first.run_id,
                first.envelope.plan_hash,
                first.envelope.proposal_revision,
            )
            if not existing:
                raise
            return self._validate_replay(actions, existing)
        return ExternalActionSetInsertResult(actions=actions, inserted=True)

    @staticmethod
    def _validate_new_set(actions: tuple[ExternalAction, ...]) -> None:
        if not actions:
            raise ValueError("external action set cannot be empty")
        first = actions[0].envelope
        step_keys: set[str] = set()
        action_ids: set[str] = set()
        for action in actions:
            envelope = action.envelope
            if action.state is not ExternalActionState.PROPOSED or action.version != 1:
                raise ValueError("new external action set must contain version-one proposals")
            if action.reservation is not None or action.delivery_attempt_count != 0:
                raise ValueError("new external action set cannot contain delivery state")
            if (
                envelope.run_id != first.run_id
                or envelope.plan_hash != first.plan_hash
                or envelope.proposal_revision != first.proposal_revision
                or envelope.authorization_set_id != first.authorization_set_id
            ):
                raise ValueError("external action set scope must be uniform")
            if envelope.step_key in step_keys or action.id in action_ids:
                raise ValueError("external action set identities must be unique")
            step_keys.add(envelope.step_key)
            action_ids.add(action.id)

    @staticmethod
    def _validate_replay(
        requested: tuple[ExternalAction, ...], existing: tuple[ExternalAction, ...]
    ) -> ExternalActionSetInsertResult:
        by_step = {action.envelope.step_key: action for action in existing}
        if len(existing) != len(requested) or set(by_step) != {
            action.envelope.step_key for action in requested
        }:
            raise ExternalActionPersistenceConflict(
                "partial_action_set",
                "persisted action set is incomplete or has unexpected members",
            ) from None
        ordered = tuple(by_step[action.envelope.step_key] for action in requested)
        if any(
            stored.idempotency_key != candidate.idempotency_key
            or _replay_projection(stored) != _replay_projection(candidate)
            for candidate, stored in zip(requested, ordered, strict=True)
        ):
            raise ExternalActionPersistenceConflict(
                "action_key_collision",
                "persisted action key maps to different stable semantics",
            ) from None
        stored_set_ids = {action.envelope.authorization_set_id for action in ordered}
        if len(stored_set_ids) != 1:
            raise ExternalActionPersistenceConflict(
                "authorization_set_corrupt",
                "persisted plan revision does not retain one authoritative action set",
            ) from None
        return ExternalActionSetInsertResult(actions=ordered, inserted=False)

    @staticmethod
    def _release_fence(authority: ReleaseAuthority) -> Any:
        dependency_step = RunStepRecord.__table__.alias("authorization_dependency_step")
        history_limit = func.coalesce(
            ExternalActionRecord.dispatch_attempt_number,
            ExternalActionRecord.delivery_attempt_count + 1,
        )
        unsatisfied_dependency = (
            select(dependency_step.c.id)
            .select_from(
                RunStepDependencyRecord.__table__.join(
                    dependency_step,
                    and_(
                        dependency_step.c.run_id == RunStepDependencyRecord.run_id,
                        dependency_step.c.key == RunStepDependencyRecord.dependency_key,
                    ),
                )
            )
            .where(
                RunStepDependencyRecord.step_id == authority.step_id,
                dependency_step.c.state != StepState.SUCCEEDED.value,
            )
            .exists()
        )
        attempt_count = (
            select(func.count(ExternalActionDispatchAttemptRecord.attempt_number))
            .where(ExternalActionDispatchAttemptRecord.external_action_id == authority.action_id)
            .scalar_subquery()
        )
        maximum_attempt = (
            select(func.coalesce(func.max(ExternalActionDispatchAttemptRecord.attempt_number), 0))
            .where(ExternalActionDispatchAttemptRecord.external_action_id == authority.action_id)
            .scalar_subquery()
        )
        invalid_historical_attempt = (
            select(ExternalActionDispatchAttemptRecord.external_action_id)
            .where(
                ExternalActionDispatchAttemptRecord.external_action_id == authority.action_id,
                ExternalActionDispatchAttemptRecord.attempt_number < history_limit,
                or_(
                    ExternalActionDispatchAttemptRecord.idempotency_support
                    != ExternalActionRecord.idempotency_support,
                    ExternalActionDispatchAttemptRecord.completed_at.is_(None),
                    ExternalActionDispatchAttemptRecord.conclusion.is_(None),
                    and_(
                        ExternalActionDispatchAttemptRecord.call_started_at.is_(None),
                        ExternalActionDispatchAttemptRecord.conclusion != "pre_call_expired",
                    ),
                    and_(
                        ExternalActionDispatchAttemptRecord.call_started_at.is_not(None),
                        ExternalActionDispatchAttemptRecord.conclusion != "provider_retry",
                    ),
                ),
            )
            .correlate(ExternalActionRecord)
            .exists()
        )
        any_started_attempt = (
            select(ExternalActionDispatchAttemptRecord.external_action_id)
            .where(
                ExternalActionDispatchAttemptRecord.external_action_id == authority.action_id,
                ExternalActionDispatchAttemptRecord.attempt_number < history_limit,
                ExternalActionDispatchAttemptRecord.call_started_at.is_not(None),
            )
            .correlate(ExternalActionRecord)
            .exists()
        )
        prior_provider_retry = (
            select(ExternalActionDispatchAttemptRecord.external_action_id)
            .where(
                ExternalActionDispatchAttemptRecord.external_action_id == authority.action_id,
                ExternalActionDispatchAttemptRecord.attempt_number
                == authority.prior_started_attempt_number,
                ExternalActionDispatchAttemptRecord.attempt_number < history_limit,
                ExternalActionDispatchAttemptRecord.idempotency_support
                == ExternalActionRecord.idempotency_support,
                ExternalActionDispatchAttemptRecord.call_started_at.is_not(None),
                ExternalActionDispatchAttemptRecord.completed_at.is_not(None),
                ExternalActionDispatchAttemptRecord.conclusion == "provider_retry",
            )
            .correlate(ExternalActionRecord)
            .exists()
        )
        if authority.call_mode is ReleaseCallMode.FIRST_CALL:
            call_mode_fence = ~any_started_attempt
        elif authority.call_mode is ReleaseCallMode.PROVIDER_RETRY:
            prior_started_attempt_number = authority.prior_started_attempt_number
            if prior_started_attempt_number is None:  # pragma: no cover - domain guard
                return ExternalActionRecord.id.is_(None)
            started_after_prior = (
                select(ExternalActionDispatchAttemptRecord.external_action_id)
                .where(
                    ExternalActionDispatchAttemptRecord.external_action_id == authority.action_id,
                    ExternalActionDispatchAttemptRecord.attempt_number < history_limit,
                    ExternalActionDispatchAttemptRecord.attempt_number
                    > prior_started_attempt_number,
                    ExternalActionDispatchAttemptRecord.call_started_at.is_not(None),
                )
                .correlate(ExternalActionRecord)
                .exists()
            )
            call_mode_fence = (
                prior_provider_retry
                & ~started_after_prior
                & ExternalActionRecord.idempotency_support.in_(("required", "supported"))
            )
        else:
            call_mode_fence = ExternalActionRecord.id.is_(None)
        return (
            select(AuthorizationSetHeadRecord.run_id)
            .select_from(
                AuthorizationSetHeadRecord.__table__.join(
                    AuthorizationSetRecord.__table__,
                    and_(
                        AuthorizationSetRecord.id == AuthorizationSetHeadRecord.current_set_id,
                        AuthorizationSetRecord.run_id == AuthorizationSetHeadRecord.run_id,
                        AuthorizationSetRecord.plan_hash == AuthorizationSetHeadRecord.plan_hash,
                        AuthorizationSetRecord.proposal_revision
                        == AuthorizationSetHeadRecord.proposal_revision,
                        AuthorizationSetRecord.membership_hash
                        == AuthorizationSetHeadRecord.membership_hash,
                    ),
                )
                .join(
                    AuthorizationSetMemberRecord.__table__,
                    AuthorizationSetMemberRecord.authorization_set_id == AuthorizationSetRecord.id,
                )
                .join(
                    ApprovalUseRecord.__table__,
                    ApprovalUseRecord.id == AuthorizationSetMemberRecord.approval_use_id,
                )
                .join(
                    ApprovalRequestRecord.__table__,
                    ApprovalRequestRecord.id == AuthorizationSetMemberRecord.approval_request_id,
                )
                .join(RunRecord.__table__, RunRecord.id == AuthorizationSetRecord.run_id)
                .join(
                    RunStepRecord.__table__,
                    RunStepRecord.id == AuthorizationSetMemberRecord.step_id,
                )
            )
            .where(
                AuthorizationSetHeadRecord.run_id == authority.run_id,
                AuthorizationSetHeadRecord.current_set_id == authority.authorization_set_id,
                AuthorizationSetHeadRecord.membership_hash == authority.membership_hash,
                AuthorizationSetHeadRecord.version == authority.head_version,
                AuthorizationSetRecord.status == "released",
                AuthorizationSetRecord.version == authority.authorization_set_version,
                AuthorizationSetRecord.membership_hash == authority.membership_hash,
                AuthorizationSetRecord.release_hash == authority.release_hash,
                AuthorizationSetRecord.released_run_version == authority.released_run_version,
                AuthorizationSetMemberRecord.action_id == authority.action_id,
                AuthorizationSetMemberRecord.action_hash == authority.action_hash,
                AuthorizationSetMemberRecord.step_id == authority.step_id,
                AuthorizationSetMemberRecord.step_key == authority.step_key,
                AuthorizationSetMemberRecord.released_step_version
                == authority.released_step_version,
                AuthorizationSetMemberRecord.approval_request_id == authority.approval_request_id,
                AuthorizationSetMemberRecord.approval_decision_id == authority.approval_decision_id,
                AuthorizationSetMemberRecord.approval_use_id == authority.approval_use_id,
                AuthorizationSetMemberRecord.reservation_id == authority.reservation_id,
                AuthorizationSetMemberRecord.released_at == AuthorizationSetRecord.released_at,
                ApprovalUseRecord.request_id == authority.approval_request_id,
                ApprovalUseRecord.decision_id == authority.approval_decision_id,
                ApprovalUseRecord.action_id == authority.action_id,
                ApprovalUseRecord.action_hash == authority.action_hash,
                ApprovalUseRecord.authorization_set_id == authority.authorization_set_id,
                ApprovalUseRecord.run_id == authority.run_id,
                ApprovalUseRecord.step_id == authority.step_id,
                ApprovalUseRecord.step_key == authority.step_key,
                ApprovalUseRecord.reservation_id == authority.reservation_id,
                ApprovalUseRecord.used_at == AuthorizationSetRecord.released_at,
                ApprovalRequestRecord.action_id == authority.action_id,
                ApprovalRequestRecord.action_hash == authority.action_hash,
                ApprovalRequestRecord.authorization_set_id == authority.authorization_set_id,
                ApprovalRequestRecord.run_id == authority.run_id,
                ApprovalRequestRecord.step_id == authority.step_id,
                ApprovalRequestRecord.step_key == authority.step_key,
                ApprovalRequestRecord.status == "consumed",
                RunRecord.state == RunState.EXECUTING.value,
                RunRecord.version == authority.released_run_version,
                RunRecord.approval_required.is_(True),
                RunStepRecord.run_id == authority.run_id,
                RunStepRecord.key == authority.step_key,
                RunStepRecord.effect == "write",
                RunStepRecord.state == authority.step_state.value,
                RunStepRecord.version == authority.step_version,
                ExternalActionRecord.id == authority.action_id,
                ExternalActionRecord.action_hash == authority.action_hash,
                ExternalActionRecord.authorization_set_id == authority.authorization_set_id,
                ExternalActionRecord.run_id == authority.run_id,
                ExternalActionRecord.step_id == authority.step_id,
                ExternalActionRecord.step_key == authority.step_key,
                ExternalActionRecord.reservation_id == authority.reservation_id,
                ExternalActionRecord.reservation_authorization_set_id
                == authority.authorization_set_id,
                ExternalActionRecord.approval_request_id == authority.approval_request_id,
                ExternalActionRecord.approval_decision_id == authority.approval_decision_id,
                attempt_count == ExternalActionRecord.delivery_attempt_count,
                maximum_attempt == ExternalActionRecord.delivery_attempt_count,
                ~invalid_historical_attempt,
                call_mode_fence,
                ~unsatisfied_dependency,
            )
            .exists()
        )

    async def _updated(self, statement: Any) -> ExternalAction | None:
        try:
            result = await self._session.execute(statement)
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                return None
            raise
        action_id = result.scalar_one_or_none()
        if action_id is None:
            return None
        await self._session.flush()
        fresh = (
            await self._session.execute(
                select(ExternalActionRecord)
                .where(ExternalActionRecord.id == action_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return _to_domain(fresh)

    async def claim_reserved(
        self,
        *,
        action_id: str,
        expected_version: int,
        authority: ReleaseAuthority,
        lease_owner: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> ExternalAction | None:
        current = await self.get(action_id)
        if (
            current is None
            or type(authority) is not ReleaseAuthority
            or authority.action_id != action_id
            or authority.action_hash != current.action_hash
            or (
                authority.call_mode is ReleaseCallMode.FIRST_CALL
                and (
                    authority.step_state is not StepState.READY
                    or authority.step_version != authority.released_step_version
                    or authority.prior_started_attempt_number is not None
                )
            )
            or (
                authority.call_mode is ReleaseCallMode.PROVIDER_RETRY
                and (
                    authority.step_state is not StepState.EXECUTING
                    or authority.step_version != authority.released_step_version + 1
                    or authority.prior_started_attempt_number is None
                )
            )
            or authority.call_mode
            not in {ReleaseCallMode.FIRST_CALL, ReleaseCallMode.PROVIDER_RETRY}
        ):
            return None
        attempt = current.delivery_attempt_count + 1
        if attempt > current.delivery_attempt_limit:
            return None
        statement = (
            update(ExternalActionRecord)
            .where(
                ExternalActionRecord.id == action_id,
                ExternalActionRecord.version == expected_version,
                ExternalActionRecord.state == ExternalActionState.DISPATCH_RESERVED.value,
                ExternalActionRecord.delivery_attempt_count
                < (ExternalActionRecord.delivery_attempt_limit),
                self._release_fence(authority),
            )
            .values(
                state=ExternalActionState.DISPATCHING.value,
                dispatch_lease_owner=lease_owner,
                dispatch_attempt_number=attempt,
                dispatch_claimed_at=claimed_at,
                dispatch_lease_expires_at=lease_expires_at,
                delivery_attempt_count=attempt,
                updated_at=claimed_at,
                version=expected_version + 1,
            )
            .returning(ExternalActionRecord.id)
            .execution_options(synchronize_session=False)
        )
        claimed = await self._updated(statement)
        if claimed is not None:
            self._session.add(
                ExternalActionDispatchAttemptRecord(
                    external_action_id=action_id,
                    attempt_number=attempt,
                    idempotency_support=current.delivery_contract.idempotency_support,
                    lease_owner=lease_owner,
                    claimed_at=claimed_at,
                    lease_expires_at=lease_expires_at,
                )
            )
            await self._session.flush()
        return claimed

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
    ) -> ActionCallStartResult | None:
        require_utc(started_at, "connector call start time")
        require_utc(call_deadline_at, "connector call deadline")
        current = await self.get(action_id)
        initial_start = authority.call_mode is ReleaseCallMode.FIRST_CALL
        retry_start = authority.call_mode is ReleaseCallMode.PROVIDER_RETRY
        if (
            current is None
            or not (
                started_at
                < call_deadline_at
                <= started_at + timedelta(seconds=current.delivery_contract.timeout_seconds)
            )
            or type(authority) is not ReleaseAuthority
            or authority.action_id != action_id
            or (not initial_start and not retry_start)
            or (
                initial_start
                and (
                    type(step_transition) is not StepTransitionResult
                    or step_transition.transition.command
                    is not StepLifecycleCommand.START_RESERVED_WRITE
                    or step_transition.transition.previous_state is not StepState.READY
                    or step_transition.step.state is not StepState.EXECUTING
                    or step_transition.step.id != authority.step_id
                    or step_transition.step.run_id != authority.run_id
                    or step_transition.step.key != authority.step_key
                    or step_transition.transition.expected_version
                    != authority.released_step_version
                    or step_transition.transition.occurred_at != started_at
                )
            )
            or (
                retry_start
                and (
                    step_transition is not None
                    or authority.step_state is not StepState.EXECUTING
                    or authority.step_version != authority.released_step_version + 1
                    or authority.prior_started_attempt_number is None
                )
            )
        ):
            return None
        lease = current.lease
        if lease is None or lease.owner != lease_owner or lease.attempt_number != attempt_number:
            return None
        current_attempt_fence = (
            select(ExternalActionDispatchAttemptRecord.external_action_id)
            .where(
                ExternalActionDispatchAttemptRecord.external_action_id == action_id,
                ExternalActionDispatchAttemptRecord.attempt_number == attempt_number,
                ExternalActionDispatchAttemptRecord.idempotency_support
                == ExternalActionRecord.idempotency_support,
                ExternalActionDispatchAttemptRecord.lease_owner == lease_owner,
                ExternalActionDispatchAttemptRecord.claimed_at
                == ExternalActionRecord.dispatch_claimed_at,
                ExternalActionDispatchAttemptRecord.lease_expires_at
                == ExternalActionRecord.dispatch_lease_expires_at,
                ExternalActionDispatchAttemptRecord.call_started_at.is_(None),
                ExternalActionDispatchAttemptRecord.call_deadline_at.is_(None),
                ExternalActionDispatchAttemptRecord.completed_at.is_(None),
                ExternalActionDispatchAttemptRecord.conclusion.is_(None),
            )
            .correlate(ExternalActionRecord)
            .exists()
        )
        try:
            async with self._session.begin_nested():
                statement = (
                    update(ExternalActionRecord)
                    .where(
                        ExternalActionRecord.id == action_id,
                        ExternalActionRecord.version == expected_version,
                        ExternalActionRecord.state == ExternalActionState.DISPATCHING.value,
                        ExternalActionRecord.dispatch_lease_owner == lease_owner,
                        ExternalActionRecord.dispatch_attempt_number == attempt_number,
                        ExternalActionRecord.dispatch_lease_expires_at > started_at,
                        ExternalActionRecord.connector_call_started_at.is_(None),
                        ExternalActionRecord.connector_call_deadline_at.is_(None),
                        self._release_fence(authority),
                        current_attempt_fence,
                    )
                    .values(
                        connector_call_started_at=started_at,
                        connector_call_deadline_at=call_deadline_at,
                        updated_at=started_at,
                        version=expected_version + 1,
                    )
                    .returning(ExternalActionRecord.id)
                    .execution_options(synchronize_session=False)
                )
                marked = await self._updated(statement)
                if marked is None:
                    raise _ActionCASLost
                if step_transition is not None:
                    step_applied = await SQLAlchemyRunStepRepository(
                        self._session
                    ).apply_transition(
                        expected_run_version=authority.released_run_version,
                        expected_run_state=RunState.EXECUTING,
                        expected_version=authority.released_step_version,
                        expected_state=StepState.READY,
                        result=step_transition,
                    )
                    if not step_applied:
                        raise _ActionCASLost
                attempt_update = await self._session.execute(
                    update(ExternalActionDispatchAttemptRecord)
                    .where(
                        ExternalActionDispatchAttemptRecord.external_action_id == action_id,
                        ExternalActionDispatchAttemptRecord.attempt_number == attempt_number,
                        ExternalActionDispatchAttemptRecord.idempotency_support
                        == current.delivery_contract.idempotency_support,
                        ExternalActionDispatchAttemptRecord.lease_owner == lease_owner,
                        ExternalActionDispatchAttemptRecord.claimed_at == lease.claimed_at,
                        ExternalActionDispatchAttemptRecord.lease_expires_at == lease.expires_at,
                        ExternalActionDispatchAttemptRecord.call_started_at.is_(None),
                        ExternalActionDispatchAttemptRecord.call_deadline_at.is_(None),
                        ExternalActionDispatchAttemptRecord.completed_at.is_(None),
                        ExternalActionDispatchAttemptRecord.conclusion.is_(None),
                    )
                    .values(
                        call_started_at=started_at,
                        call_deadline_at=call_deadline_at,
                    )
                    .returning(ExternalActionDispatchAttemptRecord.external_action_id)
                    .execution_options(synchronize_session=False)
                )
                if attempt_update.scalar_one_or_none() is None:
                    raise _ActionCASLost
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                return None
            raise
        except _ActionCASLost:
            return None
        return ActionCallStartResult(
            action=marked,
            step_transition=step_transition,
        )

    async def cancel_unstarted_after_release(
        self,
        *,
        action_id: str,
        run_id: str,
        plan_hash: str,
        expected_version: int,
        occurred_at: datetime,
        reason_code: str = "operator_cancelled",
    ) -> ExternalAction | None:
        """Cancel released work only while no connector call has started."""

        require_id(action_id, "runtime-cancel action ID")
        require_id(run_id, "runtime-cancel Run ID")
        require_digest(plan_hash, "runtime-cancel plan hash")
        require_id(reason_code, "runtime-cancel reason code")
        if reason_code not in {
            "operator_cancelled",
            "parent_run_failed",
            "runtime_control_denied",
        }:
            raise ValueError("runtime-cancel reason code is not allowlisted")
        require_utc(occurred_at, "runtime-cancel time")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("runtime-cancel expected version must be positive")
        current = await self.get(action_id)
        if (
            current is None
            or current.run_id != run_id
            or current.envelope.plan_hash != plan_hash
            or current.version != expected_version
            or current.reservation is None
            or current.updated_at > occurred_at
            or current.state
            not in {
                ExternalActionState.DISPATCH_RESERVED,
                ExternalActionState.DISPATCHING,
            }
            or current.call_started_at is not None
        ):
            return None
        prior_attempt_number = (
            current.delivery_attempt_count
            if current.state is ExternalActionState.DISPATCHING
            else None
        )
        statement = (
            update(ExternalActionRecord)
            .where(
                ExternalActionRecord.id == action_id,
                ExternalActionRecord.run_id == run_id,
                ExternalActionRecord.plan_hash == plan_hash,
                ExternalActionRecord.version == expected_version,
                ExternalActionRecord.state == current.state.value,
                ExternalActionRecord.reservation_id == current.reservation.reservation_id,
                ExternalActionRecord.connector_call_started_at.is_(None),
            )
            .values(
                state=ExternalActionState.CANCELLED.value,
                dispatch_lease_owner=None,
                dispatch_attempt_number=None,
                dispatch_claimed_at=None,
                dispatch_lease_expires_at=None,
                connector_call_started_at=None,
                connector_call_deadline_at=None,
                terminal_reason_code=reason_code,
                updated_at=occurred_at,
                version=expected_version + 1,
            )
            .returning(ExternalActionRecord.id)
            .execution_options(synchronize_session=False)
        )
        try:
            async with self._session.begin_nested():
                cancelled = await self._updated(statement)
                if cancelled is None:
                    raise _ActionCASLost
                if prior_attempt_number is not None:
                    attempt_update = await self._session.execute(
                        update(ExternalActionDispatchAttemptRecord)
                        .where(
                            ExternalActionDispatchAttemptRecord.external_action_id == action_id,
                            ExternalActionDispatchAttemptRecord.attempt_number
                            == prior_attempt_number,
                            ExternalActionDispatchAttemptRecord.call_started_at.is_(None),
                            ExternalActionDispatchAttemptRecord.completed_at.is_(None),
                            ExternalActionDispatchAttemptRecord.conclusion.is_(None),
                        )
                        .values(
                            completed_at=occurred_at,
                            conclusion="cancelled",
                            reason_code=reason_code,
                        )
                        .returning(ExternalActionDispatchAttemptRecord.external_action_id)
                        .execution_options(synchronize_session=False)
                    )
                    if attempt_update.scalar_one_or_none() is None:
                        raise _ActionCASLost
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                return None
            raise
        except _ActionCASLost:
            return None
        return cancelled

    async def complete_succeeded(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        result: ExternalActionResultSnapshot,
    ) -> ExternalAction | None:
        receipt_exists = (
            select(ConnectorActionReceiptRecord.external_action_id)
            .where(
                ConnectorActionReceiptRecord.external_action_id == action_id,
                ConnectorActionReceiptRecord.receipt_id == result.receipt_id,
                ConnectorActionReceiptRecord.status == result.status,
                ConnectorActionReceiptRecord.connector_binding_id
                == ExternalActionRecord.connector_binding_id,
                ConnectorActionReceiptRecord.idempotency_key
                == ExternalActionRecord.idempotency_key,
                ConnectorActionReceiptRecord.action_hash == ExternalActionRecord.action_hash,
                ConnectorActionReceiptRecord.capability_id == ExternalActionRecord.capability_id,
            )
            .exists()
        )
        return await self._complete(
            action_id=action_id,
            expected_version=expected_version,
            lease_owner=lease_owner,
            attempt_number=attempt_number,
            occurred_at=result.completed_at,
            state=ExternalActionState.SUCCEEDED,
            reason_code=None,
            result=result,
            extra_predicate=receipt_exists,
        )

    async def complete_failed(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        reason_code: str,
        occurred_at: datetime,
    ) -> ExternalAction | None:
        return await self._complete(
            action_id=action_id,
            expected_version=expected_version,
            lease_owner=lease_owner,
            attempt_number=attempt_number,
            occurred_at=occurred_at,
            state=ExternalActionState.FAILED,
            reason_code=reason_code,
            result=None,
            extra_predicate=None,
        )

    async def mark_outcome_unknown(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        reason_code: str,
        occurred_at: datetime,
    ) -> ExternalAction | None:
        return await self._complete(
            action_id=action_id,
            expected_version=expected_version,
            lease_owner=lease_owner,
            attempt_number=attempt_number,
            occurred_at=occurred_at,
            state=ExternalActionState.OUTCOME_UNKNOWN,
            reason_code=reason_code,
            result=None,
            extra_predicate=None,
        )

    async def _complete(
        self,
        *,
        action_id: str,
        expected_version: int,
        lease_owner: str,
        attempt_number: int,
        occurred_at: datetime,
        state: ExternalActionState,
        reason_code: str | None,
        result: ExternalActionResultSnapshot | None,
        extra_predicate: Any | None,
    ) -> ExternalAction | None:
        predicates: list[Any] = [
            ExternalActionRecord.id == action_id,
            ExternalActionRecord.version == expected_version,
            ExternalActionRecord.state == ExternalActionState.DISPATCHING.value,
            ExternalActionRecord.dispatch_lease_owner == lease_owner,
            ExternalActionRecord.dispatch_attempt_number == attempt_number,
            ExternalActionRecord.connector_call_started_at.is_not(None),
        ]
        if extra_predicate is not None:
            predicates.append(extra_predicate)
        statement = (
            update(ExternalActionRecord)
            .where(*predicates)
            .values(
                state=state.value,
                dispatch_lease_owner=None,
                dispatch_attempt_number=None,
                dispatch_claimed_at=None,
                dispatch_lease_expires_at=None,
                connector_call_started_at=None,
                connector_call_deadline_at=None,
                connector_receipt_id=None if result is None else result.receipt_id,
                connector_result_status=None if result is None else result.status,
                connector_safe_metadata=(
                    None
                    if result is None
                    else cast(dict[str, object], _plain_json(result.safe_metadata))
                ),
                completed_at=None if result is None else result.completed_at,
                terminal_reason_code=reason_code,
                updated_at=occurred_at,
                version=expected_version + 1,
            )
            .returning(ExternalActionRecord.id)
            .execution_options(synchronize_session=False)
        )
        completed = await self._updated(statement)
        if completed is not None:
            await self._session.execute(
                update(ExternalActionDispatchAttemptRecord)
                .where(
                    ExternalActionDispatchAttemptRecord.external_action_id == action_id,
                    ExternalActionDispatchAttemptRecord.attempt_number == attempt_number,
                )
                .values(
                    completed_at=occurred_at,
                    conclusion=state.value,
                    reason_code=reason_code,
                    connector_receipt_id=None if result is None else result.receipt_id,
                )
            )
        return completed

    async def release_stale_for_retry(
        self,
        *,
        action_id: str,
        expected_version: int,
        attempt_number: int,
        occurred_at: datetime,
        conclusion: str,
    ) -> ExternalAction | None:
        if conclusion not in {"pre_call_expired", "provider_retry"}:
            raise ValueError("unsupported retry release conclusion")
        if conclusion == "pre_call_expired":
            recovery_predicates = [
                ExternalActionRecord.connector_call_started_at.is_(None),
                ExternalActionRecord.dispatch_lease_expires_at <= occurred_at,
                ExternalActionRecord.delivery_attempt_count
                < ExternalActionRecord.delivery_attempt_limit,
            ]
        else:
            recovery_predicates = [
                ExternalActionRecord.connector_call_started_at.is_not(None),
                ExternalActionRecord.connector_call_deadline_at <= occurred_at,
                ExternalActionRecord.idempotency_support.in_(("required", "supported")),
                ExternalActionRecord.delivery_attempt_count
                < ExternalActionRecord.delivery_attempt_limit,
            ]
        statement = (
            update(ExternalActionRecord)
            .where(
                ExternalActionRecord.id == action_id,
                ExternalActionRecord.version == expected_version,
                ExternalActionRecord.state == ExternalActionState.DISPATCHING.value,
                ExternalActionRecord.dispatch_attempt_number == attempt_number,
                # Recovery and cancellation must contend in this same write:
                # a stale snapshot cannot re-arm work beneath a cancelled Run.
                exists(
                    select(RunRecord.id).where(
                        RunRecord.id == ExternalActionRecord.run_id,
                        RunRecord.state == RunState.EXECUTING.value,
                    )
                ),
                exists(
                    select(RunExecutionControlRecord.run_id).where(
                        RunExecutionControlRecord.run_id == ExternalActionRecord.run_id,
                        RunExecutionControlRecord.policy_hash == ExternalActionRecord.plan_hash,
                        RunExecutionControlRecord.started_at.is_not(None),
                        RunExecutionControlRecord.cancel_requested_at.is_(None),
                    )
                ),
                *recovery_predicates,
            )
            .values(
                state=ExternalActionState.DISPATCH_RESERVED.value,
                dispatch_lease_owner=None,
                dispatch_attempt_number=None,
                dispatch_claimed_at=None,
                dispatch_lease_expires_at=None,
                connector_call_started_at=None,
                connector_call_deadline_at=None,
                updated_at=occurred_at,
                version=expected_version + 1,
            )
            .returning(ExternalActionRecord.id)
            .execution_options(synchronize_session=False)
        )
        released = await self._updated(statement)
        if released is not None:
            await self._session.execute(
                update(ExternalActionDispatchAttemptRecord)
                .where(
                    ExternalActionDispatchAttemptRecord.external_action_id == action_id,
                    ExternalActionDispatchAttemptRecord.attempt_number == attempt_number,
                )
                .values(
                    completed_at=occurred_at,
                    conclusion=conclusion,
                    reason_code=conclusion,
                )
            )
        return released

    async def fail_exhausted_stale_pre_call(
        self,
        *,
        action_id: str,
        expected_version: int,
        attempt_number: int,
        occurred_at: datetime,
        reason_code: str,
    ) -> ExternalAction | None:
        statement = (
            update(ExternalActionRecord)
            .where(
                ExternalActionRecord.id == action_id,
                ExternalActionRecord.version == expected_version,
                ExternalActionRecord.state == ExternalActionState.DISPATCHING.value,
                ExternalActionRecord.dispatch_attempt_number == attempt_number,
                ExternalActionRecord.dispatch_lease_expires_at <= occurred_at,
                ExternalActionRecord.connector_call_started_at.is_(None),
                ExternalActionRecord.delivery_attempt_count
                >= ExternalActionRecord.delivery_attempt_limit,
            )
            .values(
                state=ExternalActionState.FAILED.value,
                dispatch_lease_owner=None,
                dispatch_attempt_number=None,
                dispatch_claimed_at=None,
                dispatch_lease_expires_at=None,
                connector_call_started_at=None,
                connector_call_deadline_at=None,
                terminal_reason_code=reason_code,
                updated_at=occurred_at,
                version=expected_version + 1,
            )
            .returning(ExternalActionRecord.id)
            .execution_options(synchronize_session=False)
        )
        failed = await self._updated(statement)
        if failed is not None:
            await self._session.execute(
                update(ExternalActionDispatchAttemptRecord)
                .where(
                    ExternalActionDispatchAttemptRecord.external_action_id == action_id,
                    ExternalActionDispatchAttemptRecord.attempt_number == attempt_number,
                )
                .values(
                    completed_at=occurred_at,
                    conclusion="failed",
                    reason_code=reason_code,
                )
            )
        return failed

    async def list_stale(self, *, now: datetime, limit: int) -> tuple[ExternalAction, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("stale action limit must be from 1 through 1000")
        statement = (
            select(ExternalActionRecord)
            .where(
                ExternalActionRecord.state == ExternalActionState.DISPATCHING.value,
                or_(
                    and_(
                        ExternalActionRecord.connector_call_started_at.is_(None),
                        ExternalActionRecord.dispatch_lease_expires_at <= now,
                    ),
                    and_(
                        ExternalActionRecord.connector_call_started_at.is_not(None),
                        ExternalActionRecord.connector_call_deadline_at <= now,
                    ),
                ),
            )
            .order_by(
                ExternalActionRecord.dispatch_lease_expires_at,
                ExternalActionRecord.id,
            )
            .limit(limit)
        )
        rows = tuple((await self._session.execute(statement)).scalars())
        return tuple([await self._validated_action(row) for row in rows])


def _receipt_to_domain(record: ConnectorActionReceiptRecord) -> ConnectorActionReceipt:
    return ConnectorActionReceipt(
        external_action_id=record.external_action_id,
        connector_binding_id=record.connector_binding_id,
        idempotency_key=record.idempotency_key,
        action_hash=record.action_hash,
        capability_id=record.capability_id,
        receipt_id=record.receipt_id,
        status=record.status,
        safe_metadata=record.safe_metadata,
        created_at=record.created_at,
    )


class SQLAlchemyConnectorReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, connector_binding_id: str, idempotency_key: str
    ) -> ConnectorActionReceipt | None:
        record = await self._session.get(
            ConnectorActionReceiptRecord,
            (connector_binding_id, idempotency_key),
        )
        return None if record is None else _receipt_to_domain(record)

    async def add_or_get(self, receipt: ConnectorActionReceipt) -> ConnectorReceiptInsertResult:
        try:
            async with self._session.begin_nested():
                self._session.add(
                    ConnectorActionReceiptRecord(
                        connector_binding_id=receipt.connector_binding_id,
                        idempotency_key=receipt.idempotency_key,
                        external_action_id=receipt.external_action_id,
                        action_hash=receipt.action_hash,
                        capability_id=receipt.capability_id,
                        receipt_id=receipt.receipt_id,
                        status=receipt.status,
                        safe_metadata=cast(dict[str, object], _plain_json(receipt.safe_metadata)),
                        created_at=receipt.created_at,
                    )
                )
                await self._session.flush()
        except IntegrityError:
            existing = await self.get(receipt.connector_binding_id, receipt.idempotency_key)
            if existing is None:
                statement = select(ConnectorActionReceiptRecord).where(
                    ConnectorActionReceiptRecord.external_action_id == receipt.external_action_id
                )
                row = (await self._session.execute(statement)).scalar_one_or_none()
                existing = None if row is None else _receipt_to_domain(row)
            if existing is None:
                raise
            if (
                existing.external_action_id != receipt.external_action_id
                or existing.connector_binding_id != receipt.connector_binding_id
                or existing.idempotency_key != receipt.idempotency_key
                or existing.action_hash != receipt.action_hash
                or existing.capability_id != receipt.capability_id
                or existing.receipt_id != receipt.receipt_id
                or existing.status != receipt.status
                or canonical_json_bytes(existing.safe_metadata)
                != canonical_json_bytes(receipt.safe_metadata)
            ):
                raise ExternalActionPersistenceConflict(
                    "connector_receipt_collision",
                    "connector idempotency key maps to another exact action",
                ) from None
            return ConnectorReceiptInsertResult(existing, inserted=False)
        return ConnectorReceiptInsertResult(receipt, inserted=True)
