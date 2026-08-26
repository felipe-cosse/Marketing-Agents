"""SQLAlchemy persistence for exact approval request sets and one-winner decisions."""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import (
    ApprovalDecisionInsertResult,
    ApprovalRepositoryConflict,
    ApprovalRequestSetInsertResult,
    AuthorizationSetCloseCommand,
    AuthorizationSetCloseResult,
    AuthorizationSetReleaseCommand,
    AuthorizationSetReleaseResult,
    CurrentAuthorizationSet,
    ReleaseAuthority,
    ReleaseCallMode,
)
from marketing_agents.domain.action_hash import CanonicalExternalAction, canonical_action_hash
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalDecision,
    ApprovalPolicySnapshot,
    ApprovalRenewal,
    ApprovalUse,
    AuthorizationSet,
    AuthorizationSetHead,
    AuthorizationSetMember,
    AuthorizationSetStatus,
    StoredActionApprovalRequest,
    assert_decision_binds_request,
    assert_request_binds_action,
    authorization_set_release_hash,
    expected_approval_projection,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import ExternalAction, RunStep
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    Effect,
    ExternalActionState,
    RunState,
    StepState,
)
from marketing_agents.domain.runtime_policy import effective_call_timeout_seconds
from marketing_agents.domain.validation import require_id, require_utc
from marketing_agents.infrastructure.db.models.action import (
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
)
from marketing_agents.infrastructure.db.models.approval import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ApprovalUseRecord,
    AuthorizationSetHeadRecord,
    AuthorizationSetMemberRecord,
    AuthorizationSetRecord,
)
from marketing_agents.infrastructure.db.models.run import RunRecord, RunStateTransitionRecord
from marketing_agents.infrastructure.db.models.step import (
    RunStepDependencyRecord,
    RunStepRecord,
    RunStepStateTransitionRecord,
)
from marketing_agents.infrastructure.db.repositories.action import (
    ExternalActionPersistenceConflict,
    SQLAlchemyExternalActionRepository,
)
from marketing_agents.infrastructure.db.repositories.run import SQLAlchemyRunRepository
from marketing_agents.infrastructure.db.repositories.step import SQLAlchemyRunStepRepository
from marketing_agents.security.approval_digest import (
    approval_decision_record_digest,
    approval_request_record_digest,
    approval_use_record_digest,
    authorization_set_head_record_digest,
    authorization_set_member_record_digest,
    authorization_set_record_digest,
)
from marketing_agents.security.digest_key import DigestKey


class ApprovalPersistenceConflict(ApprovalRepositoryConflict):
    """Raised when persisted approval authority is missing, partial, or contradictory."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class _ApprovalCASLost(RuntimeError):
    pass


def _is_sqlite_busy(session: AsyncSession, exc: OperationalError) -> bool:
    return bool(
        session.get_bind().dialect.name == "sqlite"
        and getattr(exc.orig, "sqlite_errorcode", None)
        in {sqlite3.SQLITE_BUSY, getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517)}
    )


def _plain_json(value: object) -> object:
    return json.loads(canonical_json_bytes(value))


def _strict_strings(value: object, name: str) -> tuple[str, ...]:
    if (
        type(value) is not list
        or any(type(item) is not str for item in value)
        or len(set(value)) != len(value)
    ):
        raise ApprovalPersistenceConflict(
            "approval_snapshot_corrupt", f"persisted {name} must be a unique string array"
        )
    return tuple(cast(list[str], value))


def _integrity_time(value: datetime | None, name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ApprovalPersistenceConflict(
            "approval_integrity_corrupt", f"persisted {name} is not an exact UTC timestamp"
        )
    return value.isoformat(timespec="microseconds")


def _request_record_material(record: ApprovalRequestRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "action_id": record.action_id,
        "action_hash": record.action_hash,
        "authorization_set_id": record.authorization_set_id,
        "run_id": record.run_id,
        "plan_hash": record.plan_hash,
        "proposal_revision": record.proposal_revision,
        "generation": record.generation,
        "step_id": record.step_id,
        "step_key": record.step_key,
        "template_id": record.template_id,
        "instance_id": record.instance_id,
        "action_type": record.action_type,
        "capability_id": record.capability_id,
        "connector_family": record.connector_family,
        "binding_id": record.binding_id,
        "semantic_action_hash": record.semantic_action_hash,
        "redacted_destination": record.redacted_destination,
        "redacted_projection": record.redacted_projection,
        "policy_id": record.policy_id,
        "required_roles": record.required_roles,
        "required_scopes": record.required_scopes,
        "expires_after_seconds": record.expires_after_seconds,
        "allow_self_approval": record.allow_self_approval,
        "requested_by": record.requested_by,
        "requested_at": _integrity_time(record.requested_at, "request time"),
        "expires_at": _integrity_time(record.expires_at, "request expiry"),
        "status": record.status,
        "version": record.version,
        "updated_at": _integrity_time(record.updated_at, "request update time"),
        "expired_at": _integrity_time(record.expired_at, "request expiration time"),
        "replacement_request_id": record.replacement_request_id,
        "renewed_at": _integrity_time(record.renewed_at, "request renewal time"),
        "superseded_at": _integrity_time(
            record.superseded_at,
            "request supersession time",
        ),
        "superseded_reason_code": record.superseded_reason_code,
    }


def _decision_record_material(record: ApprovalDecisionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "request_id": record.request_id,
        "action_id": record.action_id,
        "action_hash": record.action_hash,
        "authorization_set_id": record.authorization_set_id,
        "run_id": record.run_id,
        "plan_hash": record.plan_hash,
        "proposal_revision": record.proposal_revision,
        "step_id": record.step_id,
        "step_key": record.step_key,
        "actor_id": record.actor_id,
        "authentication_method": record.authentication_method,
        "correlation_id": record.correlation_id,
        "decision": record.decision,
        "authority_roles": record.authority_roles,
        "authority_scopes": record.authority_scopes,
        "reason_code": record.reason_code,
        "reason": record.reason,
        "decided_at": _integrity_time(record.decided_at, "decision time"),
    }


def _use_record_material(record: ApprovalUseRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "request_id": record.request_id,
        "decision_id": record.decision_id,
        "action_id": record.action_id,
        "action_hash": record.action_hash,
        "authorization_set_id": record.authorization_set_id,
        "run_id": record.run_id,
        "plan_hash": record.plan_hash,
        "proposal_revision": record.proposal_revision,
        "step_id": record.step_id,
        "step_key": record.step_key,
        "reservation_id": record.reservation_id,
        "used_at": _integrity_time(record.used_at, "approval use time"),
    }


def _set_record_material(record: AuthorizationSetRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "run_id": record.run_id,
        "plan_hash": record.plan_hash,
        "proposal_revision": record.proposal_revision,
        "membership_hash": record.membership_hash,
        "member_count": record.member_count,
        "status": record.status,
        "version": record.version,
        "opened_at": _integrity_time(record.opened_at, "authorization set open time"),
        "updated_at": _integrity_time(record.updated_at, "authorization set update time"),
        "release_hash": record.release_hash,
        "released_at": _integrity_time(record.released_at, "authorization set release time"),
        "released_run_version": record.released_run_version,
        "terminal_reason_code": record.terminal_reason_code,
        "superseded_by_set_id": record.superseded_by_set_id,
        "superseded_at": _integrity_time(
            record.superseded_at,
            "authorization set supersession time",
        ),
    }


def _set_head_record_material(record: AuthorizationSetHeadRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "current_set_id": record.current_set_id,
        "plan_hash": record.plan_hash,
        "proposal_revision": record.proposal_revision,
        "membership_hash": record.membership_hash,
        "version": record.version,
        "updated_at": _integrity_time(record.updated_at, "authorization set head update time"),
    }


def _set_member_record_material(record: AuthorizationSetMemberRecord) -> dict[str, Any]:
    return {
        "authorization_set_id": record.authorization_set_id,
        "ordinal": record.ordinal,
        "run_id": record.run_id,
        "plan_hash": record.plan_hash,
        "proposal_revision": record.proposal_revision,
        "membership_hash": record.membership_hash,
        "action_id": record.action_id,
        "action_hash": record.action_hash,
        "step_id": record.step_id,
        "step_key": record.step_key,
        "approval_request_id": record.approval_request_id,
        "approval_decision_id": record.approval_decision_id,
        "approval_use_id": record.approval_use_id,
        "reservation_id": record.reservation_id,
        "released_action_source_version": record.released_action_source_version,
        "released_request_source_version": record.released_request_source_version,
        "released_step_version": record.released_step_version,
        "released_at": _integrity_time(record.released_at, "set member release time"),
    }


def _seal_request_record(record: ApprovalRequestRecord, key: DigestKey) -> None:
    record.integrity_digest = approval_request_record_digest(_request_record_material(record), key)


def _seal_decision_record(record: ApprovalDecisionRecord, key: DigestKey) -> None:
    record.integrity_digest = approval_decision_record_digest(
        _decision_record_material(record), key
    )


def _seal_use_record(record: ApprovalUseRecord, key: DigestKey) -> None:
    record.integrity_digest = approval_use_record_digest(_use_record_material(record), key)


def _seal_set_record(record: AuthorizationSetRecord, key: DigestKey) -> None:
    record.integrity_digest = authorization_set_record_digest(
        _set_record_material(record),
        key,
    )


def _seal_set_head_record(record: AuthorizationSetHeadRecord, key: DigestKey) -> None:
    record.integrity_digest = authorization_set_head_record_digest(
        _set_head_record_material(record),
        key,
    )


def _seal_set_member_record(record: AuthorizationSetMemberRecord, key: DigestKey) -> None:
    record.integrity_digest = authorization_set_member_record_digest(
        _set_member_record_material(record),
        key,
    )


def _verify_digest(actual: object, expected: str, record_name: str) -> None:
    if type(actual) is not str or not hmac.compare_digest(actual, expected):
        raise ApprovalPersistenceConflict(
            "approval_integrity_corrupt",
            f"persisted {record_name} failed its keyed corruption-detection digest",
        )


def _verify_request_record(record: ApprovalRequestRecord, key: DigestKey) -> None:
    _verify_digest(
        record.integrity_digest,
        approval_request_record_digest(_request_record_material(record), key),
        "approval request",
    )


def _verify_decision_record(record: ApprovalDecisionRecord, key: DigestKey) -> None:
    _verify_digest(
        record.integrity_digest,
        approval_decision_record_digest(_decision_record_material(record), key),
        "approval decision",
    )


def _verify_use_record(record: ApprovalUseRecord, key: DigestKey) -> None:
    _verify_digest(
        record.integrity_digest,
        approval_use_record_digest(_use_record_material(record), key),
        "approval use",
    )


def _verify_set_record(record: AuthorizationSetRecord, key: DigestKey) -> None:
    _verify_digest(
        record.integrity_digest,
        authorization_set_record_digest(_set_record_material(record), key),
        "authorization set",
    )


def _verify_set_head_record(record: AuthorizationSetHeadRecord, key: DigestKey) -> None:
    _verify_digest(
        record.integrity_digest,
        authorization_set_head_record_digest(_set_head_record_material(record), key),
        "authorization set head",
    )


def _verify_set_member_record(record: AuthorizationSetMemberRecord, key: DigestKey) -> None:
    _verify_digest(
        record.integrity_digest,
        authorization_set_member_record_digest(_set_member_record_material(record), key),
        "authorization set member",
    )


def _request_to_record(
    stored: StoredActionApprovalRequest,
    key: DigestKey,
) -> ApprovalRequestRecord:
    request = stored.request
    record = ApprovalRequestRecord(
        id=request.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        generation=request.generation,
        step_id=request.step_id,
        step_key=request.step_key,
        template_id=request.template_id,
        instance_id=request.instance_id,
        action_type=request.action_type,
        capability_id=request.capability_id,
        connector_family=request.connector_family,
        binding_id=request.binding_id,
        semantic_action_hash=request.semantic_action_hash,
        redacted_destination=request.redacted_destination,
        redacted_projection=cast(dict[str, Any], _plain_json(request.redacted_projection)),
        policy_id=request.policy.policy_id,
        required_roles=sorted(request.policy.required_roles),
        required_scopes=sorted(request.policy.required_scopes),
        expires_after_seconds=request.policy.expires_after_seconds,
        allow_self_approval=request.policy.allow_self_approval,
        requested_by=request.requested_by,
        requested_at=request.requested_at,
        expires_at=request.expires_at,
        status=stored.status.value,
        version=stored.version,
        updated_at=stored.updated_at,
        expired_at=stored.expired_at,
        replacement_request_id=stored.replacement_request_id,
        renewed_at=stored.renewed_at,
        superseded_at=stored.superseded_at,
        superseded_reason_code=stored.superseded_reason_code,
    )
    _seal_request_record(record, key)
    return record


def _request_update_values(
    stored: StoredActionApprovalRequest,
    key: DigestKey,
) -> dict[str, object]:
    record = _request_to_record(stored, key)
    return {
        "status": record.status,
        "version": record.version,
        "updated_at": record.updated_at,
        "expired_at": record.expired_at,
        "replacement_request_id": record.replacement_request_id,
        "renewed_at": record.renewed_at,
        "superseded_at": record.superseded_at,
        "superseded_reason_code": record.superseded_reason_code,
        "integrity_digest": record.integrity_digest,
    }


def _decision_to_record(
    decision: ApprovalDecision,
    key: DigestKey,
) -> ApprovalDecisionRecord:
    record = ApprovalDecisionRecord(
        id=decision.id,
        request_id=decision.request_id,
        action_id=decision.action_id,
        action_hash=decision.action_hash,
        authorization_set_id=decision.authorization_set_id,
        run_id=decision.run_id,
        plan_hash=decision.plan_hash,
        proposal_revision=decision.proposal_revision,
        step_id=decision.step_id,
        step_key=decision.step_key,
        actor_id=decision.actor_id,
        authentication_method=decision.authentication_method,
        correlation_id=decision.correlation_id,
        decision=decision.decision.value,
        authority_roles=sorted(decision.authority_roles),
        authority_scopes=sorted(decision.authority_scopes),
        reason_code=decision.reason_code,
        reason=decision.reason,
        decided_at=decision.decided_at,
    )
    _seal_decision_record(record, key)
    return record


def _use_to_record(use: ApprovalUse, key: DigestKey) -> ApprovalUseRecord:
    record = ApprovalUseRecord(
        id=use.id,
        request_id=use.request_id,
        decision_id=use.decision_id,
        action_id=use.action_id,
        action_hash=use.action_hash,
        authorization_set_id=use.authorization_set_id,
        run_id=use.run_id,
        plan_hash=use.plan_hash,
        proposal_revision=use.proposal_revision,
        step_id=use.step_id,
        step_key=use.step_key,
        reservation_id=use.reservation_id,
        used_at=use.used_at,
    )
    _seal_use_record(record, key)
    return record


def _set_to_record(
    authorization_set: AuthorizationSet,
    key: DigestKey,
) -> AuthorizationSetRecord:
    record = AuthorizationSetRecord(
        id=authorization_set.id,
        run_id=authorization_set.run_id,
        plan_hash=authorization_set.plan_hash,
        proposal_revision=authorization_set.proposal_revision,
        membership_hash=authorization_set.membership_hash,
        member_count=len(authorization_set.members),
        status=authorization_set.status.value,
        version=authorization_set.version,
        opened_at=authorization_set.opened_at,
        updated_at=authorization_set.updated_at,
        release_hash=authorization_set.release_hash,
        released_at=authorization_set.released_at,
        released_run_version=authorization_set.released_run_version,
        terminal_reason_code=authorization_set.terminal_reason_code,
        superseded_by_set_id=authorization_set.superseded_by_set_id,
        superseded_at=authorization_set.superseded_at,
    )
    _seal_set_record(record, key)
    return record


def _head_to_record(head: AuthorizationSetHead, key: DigestKey) -> AuthorizationSetHeadRecord:
    record = AuthorizationSetHeadRecord(
        run_id=head.run_id,
        current_set_id=head.current_set_id,
        plan_hash=head.plan_hash,
        proposal_revision=head.proposal_revision,
        membership_hash=head.membership_hash,
        version=head.version,
        updated_at=head.updated_at,
    )
    _seal_set_head_record(record, key)
    return record


def _member_to_record(
    member: AuthorizationSetMember,
    membership_hash: str,
    key: DigestKey,
) -> AuthorizationSetMemberRecord:
    record = AuthorizationSetMemberRecord(
        authorization_set_id=member.authorization_set_id,
        ordinal=member.ordinal,
        run_id=member.run_id,
        plan_hash=member.plan_hash,
        proposal_revision=member.proposal_revision,
        membership_hash=membership_hash,
        action_id=member.action_id,
        action_hash=member.action_hash,
        step_id=member.step_id,
        step_key=member.step_key,
        approval_request_id=None,
        approval_decision_id=None,
        approval_use_id=None,
        reservation_id=None,
        released_action_source_version=None,
        released_request_source_version=None,
        released_step_version=None,
        released_at=None,
    )
    _seal_set_member_record(record, key)
    return record


def _member_from_record(
    record: AuthorizationSetMemberRecord,
    key: DigestKey,
) -> AuthorizationSetMember:
    _verify_set_member_record(record, key)
    return AuthorizationSetMember(
        authorization_set_id=record.authorization_set_id,
        ordinal=record.ordinal,
        run_id=record.run_id,
        plan_hash=record.plan_hash,
        proposal_revision=record.proposal_revision,
        action_id=record.action_id,
        action_hash=record.action_hash,
        step_id=record.step_id,
        step_key=record.step_key,
    )


def _head_from_record(
    record: AuthorizationSetHeadRecord,
    key: DigestKey,
) -> AuthorizationSetHead:
    _verify_set_head_record(record, key)
    return AuthorizationSetHead(
        run_id=record.run_id,
        current_set_id=record.current_set_id,
        plan_hash=record.plan_hash,
        proposal_revision=record.proposal_revision,
        membership_hash=record.membership_hash,
        version=record.version,
        updated_at=record.updated_at,
    )


def _decision_from_record(record: ApprovalDecisionRecord, key: DigestKey) -> ApprovalDecision:
    _verify_decision_record(record, key)
    return ApprovalDecision(
        id=record.id,
        request_id=record.request_id,
        action_id=record.action_id,
        action_hash=record.action_hash,
        authorization_set_id=record.authorization_set_id,
        run_id=record.run_id,
        plan_hash=record.plan_hash,
        proposal_revision=record.proposal_revision,
        step_id=record.step_id,
        step_key=record.step_key,
        actor_id=record.actor_id,
        authentication_method=record.authentication_method,
        correlation_id=record.correlation_id,
        decision=ApprovalDecisionKind(record.decision),
        authority_roles=frozenset(_strict_strings(record.authority_roles, "decision roles")),
        authority_scopes=frozenset(_strict_strings(record.authority_scopes, "decision scopes")),
        reason_code=record.reason_code,
        decided_at=record.decided_at,
        reason=record.reason,
    )


def _use_from_record(record: ApprovalUseRecord, key: DigestKey) -> ApprovalUse:
    _verify_use_record(record, key)
    return ApprovalUse(
        id=record.id,
        request_id=record.request_id,
        decision_id=record.decision_id,
        action_id=record.action_id,
        action_hash=record.action_hash,
        authorization_set_id=record.authorization_set_id,
        run_id=record.run_id,
        plan_hash=record.plan_hash,
        proposal_revision=record.proposal_revision,
        step_id=record.step_id,
        step_key=record.step_key,
        reservation_id=record.reservation_id,
        used_at=record.used_at,
    )


def _same_json(left: object, right: object) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


class SQLAlchemyApprovalRepository:
    def __init__(self, session: AsyncSession, integrity_key: DigestKey) -> None:
        if type(integrity_key) is not DigestKey:
            raise ValueError("approval repository requires the exact persistent digest key")
        self._session = session
        self._integrity_key = integrity_key
        self._validated_steps: dict[str, dict[str, RunStep]] = {}

    async def _steps_for_run(self, run_id: str) -> dict[str, RunStep]:
        cached = self._validated_steps.get(run_id)
        if cached is not None:
            return cached
        steps = await SQLAlchemyRunStepRepository(self._session).validate_plan_for_execution(run_id)
        result = {step.id: step for step in steps}
        self._validated_steps[run_id] = result
        return result

    async def _hydrate_authorization_set(
        self,
        record: AuthorizationSetRecord,
    ) -> AuthorizationSet:
        _verify_set_record(record, self._integrity_key)
        member_records = tuple(
            (
                await self._session.execute(
                    select(AuthorizationSetMemberRecord)
                    .where(AuthorizationSetMemberRecord.authorization_set_id == record.id)
                    .order_by(AuthorizationSetMemberRecord.ordinal)
                )
            ).scalars()
        )
        if len(member_records) != record.member_count:
            raise ApprovalPersistenceConflict(
                "authorization_set_partial",
                "authorization set member count differs from its sealed record",
            )
        try:
            members = tuple(
                _member_from_record(member, self._integrity_key) for member in member_records
            )
            authorization_set = AuthorizationSet(
                id=record.id,
                run_id=record.run_id,
                plan_hash=record.plan_hash,
                proposal_revision=record.proposal_revision,
                membership_hash=record.membership_hash,
                members=members,
                status=AuthorizationSetStatus(record.status),
                version=record.version,
                opened_at=record.opened_at,
                updated_at=record.updated_at,
                release_hash=record.release_hash,
                released_at=record.released_at,
                released_run_version=record.released_run_version,
                terminal_reason_code=record.terminal_reason_code,
                superseded_by_set_id=record.superseded_by_set_id,
                superseded_at=record.superseded_at,
            )
        except (KeyError, TypeError, ValueError):
            raise ApprovalPersistenceConflict(
                "authorization_set_corrupt",
                "authorization set failed exact aggregate hydration",
            ) from None

        steps = await self._steps_for_run(record.run_id)
        ordered_writes = tuple(
            sorted(
                (step for step in steps.values() if step.effect is Effect.WRITE),
                key=lambda step: step.ordinal,
            )
        )
        if tuple((step.id, step.key) for step in ordered_writes) != tuple(
            (member.step_id, member.step_key) for member in members
        ):
            raise ApprovalPersistenceConflict(
                "authorization_set_membership_corrupt",
                "authorization set does not retain canonical complete WRITE-step order",
            )

        release_material: list[dict[str, object]] = []
        if authorization_set.status is AuthorizationSetStatus.RELEASED:
            assert authorization_set.released_at is not None
            assert authorization_set.released_run_version is not None
            run_record = await self._session.get(RunRecord, authorization_set.run_id)
            run_transition = await self._session.get(
                RunStateTransitionRecord,
                (
                    authorization_set.run_id,
                    authorization_set.released_run_version,
                ),
            )
            if (
                run_record is None
                or run_record.version < authorization_set.released_run_version
                or run_transition is None
                or run_transition.command != "release_approved_plan"
                or run_transition.previous_state != RunState.AWAITING_APPROVAL.value
                or run_transition.new_state != RunState.EXECUTING.value
                or run_transition.occurred_at != authorization_set.released_at
                or run_transition.resulting_version != authorization_set.released_run_version
            ):
                raise ApprovalPersistenceConflict(
                    "authorization_set_release_corrupt",
                    "released authorization set lost its exact Run transition",
                )
        for member_record in member_records:
            release_values = (
                member_record.approval_request_id,
                member_record.approval_decision_id,
                member_record.approval_use_id,
                member_record.reservation_id,
                member_record.released_action_source_version,
                member_record.released_request_source_version,
                member_record.released_step_version,
                member_record.released_at,
            )
            if authorization_set.status is AuthorizationSetStatus.RELEASED:
                if any(value is None for value in release_values):
                    raise ApprovalPersistenceConflict(
                        "authorization_set_release_partial",
                        "released authorization set has an incomplete member projection",
                    )
                use_record = await self._session.get(
                    ApprovalUseRecord,
                    cast(str, member_record.approval_use_id),
                )
                action_record = await self._session.get(
                    ExternalActionRecord,
                    member_record.action_id,
                )
                request_record = await self._session.get(
                    ApprovalRequestRecord,
                    cast(str, member_record.approval_request_id),
                )
                decision_record = await self._session.get(
                    ApprovalDecisionRecord,
                    cast(str, member_record.approval_decision_id),
                )
                step_record = await self._session.get(RunStepRecord, member_record.step_id)
                step_transition = await self._session.get(
                    RunStepStateTransitionRecord,
                    (
                        member_record.step_id,
                        cast(int, member_record.released_step_version),
                    ),
                )
                if (
                    use_record is None
                    or action_record is None
                    or request_record is None
                    or decision_record is None
                    or step_record is None
                    or step_transition is None
                ):
                    raise ApprovalPersistenceConflict(
                        "authorization_set_release_corrupt",
                        "released authorization member lost exact lifecycle evidence",
                    )
                _verify_use_record(use_record, self._integrity_key)
                _verify_request_record(request_record, self._integrity_key)
                _verify_decision_record(decision_record, self._integrity_key)
                expected_roles = frozenset(
                    _strict_strings(request_record.required_roles, "request roles")
                ) | frozenset({"approver"})
                expected_scopes = frozenset(
                    _strict_strings(request_record.required_scopes, "request scopes")
                ) | frozenset({"approvals:decide"})
                decision_roles = frozenset(
                    _strict_strings(decision_record.authority_roles, "decision roles")
                )
                decision_scopes = frozenset(
                    _strict_strings(decision_record.authority_scopes, "decision scopes")
                )
                if (
                    use_record.request_id != member_record.approval_request_id
                    or use_record.decision_id != member_record.approval_decision_id
                    or use_record.action_id != member_record.action_id
                    or use_record.action_hash != member_record.action_hash
                    or use_record.authorization_set_id != member_record.authorization_set_id
                    or use_record.run_id != member_record.run_id
                    or use_record.plan_hash != member_record.plan_hash
                    or use_record.proposal_revision != member_record.proposal_revision
                    or use_record.step_id != member_record.step_id
                    or use_record.step_key != member_record.step_key
                    or use_record.reservation_id != member_record.reservation_id
                    or use_record.used_at != member_record.released_at
                    or member_record.released_at != authorization_set.released_at
                    or request_record.status != ApprovalStatus.CONSUMED.value
                    or request_record.version
                    != cast(int, member_record.released_request_source_version) + 1
                    or request_record.updated_at != authorization_set.released_at
                    or request_record.expires_at <= authorization_set.released_at
                    or decision_record.id != member_record.approval_decision_id
                    or decision_record.request_id != member_record.approval_request_id
                    or decision_record.decision != ApprovalDecisionKind.APPROVE.value
                    or decision_record.authentication_method not in {"local_fixed", "bearer"}
                    or decision_roles != expected_roles
                    or decision_scopes != expected_scopes
                    or (
                        not request_record.allow_self_approval
                        and decision_record.actor_id == request_record.requested_by
                    )
                    or decision_record.decided_at > authorization_set.released_at
                    or action_record.version
                    < cast(int, member_record.released_action_source_version) + 1
                    or action_record.reservation_id != member_record.reservation_id
                    or action_record.reservation_authorization_set_id
                    != member_record.authorization_set_id
                    or action_record.approval_request_id != member_record.approval_request_id
                    or action_record.approval_decision_id != member_record.approval_decision_id
                    or step_record.version < cast(int, member_record.released_step_version)
                    or step_transition.command != "release_approval"
                    or step_transition.previous_state != StepState.AWAITING_APPROVAL.value
                    or step_transition.new_state != StepState.READY.value
                    or step_transition.occurred_at != authorization_set.released_at
                    or step_transition.resulting_version != member_record.released_step_version
                ):
                    raise ApprovalPersistenceConflict(
                        "authorization_set_release_corrupt",
                        "released member use and action reservation disagree",
                    )
                release_material.append(
                    {
                        "action_id": member_record.action_id,
                        "action_hash": member_record.action_hash,
                        "action_source_version": cast(
                            int, member_record.released_action_source_version
                        ),
                        "request_id": member_record.approval_request_id,
                        "request_source_version": cast(
                            int, member_record.released_request_source_version
                        ),
                        "decision_id": member_record.approval_decision_id,
                        "approval_use_id": cast(str, member_record.approval_use_id),
                        "reservation_id": member_record.reservation_id,
                        "step_id": member_record.step_id,
                        "released_step_version": member_record.released_step_version,
                    }
                )
            elif any(value is not None for value in release_values):
                raise ApprovalPersistenceConflict(
                    "authorization_set_release_corrupt",
                    "unreleased authorization set retains member release authority",
                )
        if authorization_set.status is AuthorizationSetStatus.RELEASED:
            assert authorization_set.released_at is not None
            assert authorization_set.released_run_version is not None
            expected_release_hash = authorization_set_release_hash(
                authorization_set_id=authorization_set.id,
                membership_hash=authorization_set.membership_hash,
                released_run_version=authorization_set.released_run_version,
                released_at=authorization_set.released_at,
                members=tuple(release_material),
            )
            if not hmac.compare_digest(
                cast(str, authorization_set.release_hash),
                expected_release_hash,
            ):
                raise ApprovalPersistenceConflict(
                    "authorization_set_release_hash_mismatch",
                    "authorization set release hash is not current",
                )
        return authorization_set

    async def get_authorization_set_epoch(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> AuthorizationSet | None:
        statement = select(AuthorizationSetRecord).where(
            AuthorizationSetRecord.run_id == run_id,
            AuthorizationSetRecord.plan_hash == plan_hash,
            AuthorizationSetRecord.proposal_revision == proposal_revision,
        )
        record = (await self._session.execute(statement)).scalar_one_or_none()
        return None if record is None else await self._hydrate_authorization_set(record)

    async def get_current_authorization_set(
        self,
        run_id: str,
    ) -> CurrentAuthorizationSet | None:
        record = await self._session.get(AuthorizationSetHeadRecord, run_id)
        if record is None:
            orphan = (
                await self._session.execute(
                    select(AuthorizationSetRecord.id).where(AuthorizationSetRecord.run_id == run_id)
                )
            ).first()
            if orphan is not None:
                raise ApprovalPersistenceConflict(
                    "authorization_set_head_missing",
                    "authorization set exists without its current head",
                )
            return None
        head = _head_from_record(record, self._integrity_key)
        set_record = await self._session.get(AuthorizationSetRecord, head.current_set_id)
        if set_record is None:
            raise ApprovalPersistenceConflict(
                "authorization_set_missing",
                "authorization set head points at a missing epoch",
            )
        authorization_set = await self._hydrate_authorization_set(set_record)
        try:
            head.assert_selects(authorization_set)
        except ValueError:
            raise ApprovalPersistenceConflict(
                "authorization_set_head_corrupt",
                "authorization set head differs from the selected epoch",
            ) from None
        return CurrentAuthorizationSet(head=head, authorization_set=authorization_set)

    async def _authority(
        self, action_id: str, step_id: str
    ) -> tuple[ExternalAction, RunStep, CanonicalExternalAction, ApprovalPolicySnapshot]:
        try:
            action = await SQLAlchemyExternalActionRepository(self._session).get(action_id)
        except (ExternalActionPersistenceConflict, KeyError, TypeError, ValueError):
            raise ApprovalPersistenceConflict(
                "approval_authority_corrupt",
                "approval action failed exact aggregate hydration",
            ) from None
        if action is None:
            raise ApprovalPersistenceConflict(
                "approval_authority_missing",
                "approval request lost its authoritative action or RunStep",
            )
        step = (await self._steps_for_run(action.run_id)).get(step_id)
        if action is None or step is None:
            raise ApprovalPersistenceConflict(
                "approval_authority_missing",
                "approval request lost its authoritative action or RunStep",
            )
        try:
            envelope = action.envelope
            redaction_fields = step.request_redaction_fields
            expected_projection = expected_approval_projection(envelope, redaction_fields)
            policy = action.approval_policy
        except ApprovalPersistenceConflict:
            raise
        except (KeyError, TypeError, ValueError):
            raise ApprovalPersistenceConflict(
                "approval_authority_corrupt",
                "approval authority failed exact persisted hydration",
            ) from None
        scalar_checks = (
            action.id == envelope.action_id,
            action.run_id == envelope.run_id == step.run_id,
            action.step_id == envelope.step_id == step.id,
            envelope.step_key == step.key,
            envelope.plan_hash == step.plan_hash,
            action.action_hash == canonical_action_hash(envelope),
            envelope.capability_id == step.capability_id,
            envelope.connector_family == step.connector_family,
            action.connector_binding_id == envelope.binding_id == step.binding_id,
            action.delivery_contract.request_schema_id
            == envelope.payload_schema_id
            == step.request_schema_id,
            action.delivery_contract.binding_configuration_revision
            == step.binding_configuration_revision
            == step.configuration_revision,
            action.delivery_contract.idempotency_support == step.idempotency_support,
            action.delivery_contract.timeout_seconds
            == effective_call_timeout_seconds(step.runtime_policy, step.timeout_seconds),
            step.template_id == envelope.template_id,
            step.selected_instance_id == envelope.instance_id,
            step.approval_policy_id == policy.policy_id,
            tuple(sorted(policy.required_roles)) == tuple(step.approval_required_roles),
            tuple(sorted(policy.required_scopes)) == tuple(step.approval_required_scopes),
            policy.expires_after_seconds == step.approval_expires_after_seconds,
            policy.allow_self_approval == step.approval_allow_self_approval,
            _same_json(action.proposal.redacted_projection, expected_projection),
        )
        if not all(scalar_checks):
            raise ApprovalPersistenceConflict(
                "approval_authority_corrupt",
                "action, step, policy, or safe projection authority is inconsistent",
            )
        return action, step, envelope, policy

    async def _hydrate_unchecked(
        self, record: ApprovalRequestRecord
    ) -> StoredActionApprovalRequest:
        _verify_request_record(record, self._integrity_key)
        action, step, envelope, policy = await self._authority(record.action_id, record.step_id)
        record_policy = ApprovalPolicySnapshot(
            policy_id=record.policy_id,
            required_roles=frozenset(_strict_strings(record.required_roles, "request roles")),
            required_scopes=frozenset(_strict_strings(record.required_scopes, "request scopes")),
            expires_after_seconds=record.expires_after_seconds,
            allow_self_approval=record.allow_self_approval,
        )
        if record_policy != policy:
            raise ApprovalPersistenceConflict(
                "approval_policy_corrupt", "request policy differs from action/step authority"
            )
        exact_scalars = (
            record.action_id == action.id == envelope.action_id,
            record.action_hash == action.action_hash == canonical_action_hash(envelope),
            record.authorization_set_id == envelope.authorization_set_id,
            record.run_id == action.run_id == envelope.run_id,
            record.plan_hash == envelope.plan_hash,
            record.proposal_revision == envelope.proposal_revision,
            record.step_id == step.id == envelope.step_id,
            record.step_key == step.key == envelope.step_key,
            record.template_id == step.template_id == envelope.template_id,
            record.instance_id == step.selected_instance_id == envelope.instance_id,
            record.action_type == envelope.action_type,
            record.capability_id == envelope.capability_id,
            record.connector_family == envelope.connector_family,
            record.binding_id == action.connector_binding_id == envelope.binding_id,
            record.semantic_action_hash == envelope.semantic_action_hash,
            record.redacted_destination
            == expected_approval_projection(
                envelope,
                step.request_redaction_fields,
            )["destination"],
            _same_json(record.redacted_projection, action.proposal.redacted_projection),
            record.generation != 1 or record.requested_at == action.created_at,
        )
        if not all(exact_scalars):
            raise ApprovalPersistenceConflict(
                "approval_request_corrupt",
                "persisted request differs from its exact action and RunStep authority",
            )
        request = ActionApprovalRequest(
            id=record.id,
            generation=record.generation,
            action_id=record.action_id,
            action_hash=record.action_hash,
            authorization_set_id=record.authorization_set_id,
            run_id=record.run_id,
            plan_hash=record.plan_hash,
            proposal_revision=record.proposal_revision,
            step_id=record.step_id,
            step_key=record.step_key,
            template_id=record.template_id,
            instance_id=record.instance_id,
            action_type=record.action_type,
            capability_id=record.capability_id,
            connector_family=record.connector_family,
            binding_id=record.binding_id,
            semantic_action_hash=record.semantic_action_hash,
            redacted_destination=record.redacted_destination,
            redacted_projection=record.redacted_projection,
            policy=record_policy,
            requested_by=record.requested_by,
            requested_at=record.requested_at,
            expires_at=record.expires_at,
        )
        decision_record = (
            await self._session.execute(
                select(ApprovalDecisionRecord).where(ApprovalDecisionRecord.request_id == record.id)
            )
        ).scalar_one_or_none()
        use_record = (
            await self._session.execute(
                select(ApprovalUseRecord).where(ApprovalUseRecord.request_id == record.id)
            )
        ).scalar_one_or_none()
        try:
            decision = (
                None
                if decision_record is None
                else _decision_from_record(decision_record, self._integrity_key)
            )
            use = None if use_record is None else _use_from_record(use_record, self._integrity_key)
            status = ApprovalStatus(record.status)
            if use is not None and (
                action.reservation is None
                or use.reservation_id != action.reservation.reservation_id
                or action.reservation.authorization_set_id != request.authorization_set_id
                or action.reservation.approval_request_id != request.id
                or action.reservation.approval_decision_id != use.decision_id
                or action.reservation.action_hash != request.action_hash
                or action.reservation.capability_id != request.capability_id
                or action.reservation.binding_id != request.binding_id
                or action.reservation.idempotency_key != action.idempotency_key
                or use.used_at != action.reservation.reserved_at
            ):
                raise ValueError("approval use does not match its action reservation")
            if record.replacement_request_id is None:
                if status in {ApprovalStatus.PENDING, ApprovalStatus.EXPIRED}:
                    waiting = (
                        action.state is ExternalActionState.AWAITING_APPROVAL
                        and action.reservation is None
                    )
                    terminal_expiry = False
                    if (
                        status is ApprovalStatus.EXPIRED
                        and action.state is ExternalActionState.CANCELLED
                        and action.reservation is None
                    ):
                        set_record = await self._session.get(
                            AuthorizationSetRecord,
                            record.authorization_set_id,
                        )
                        if set_record is not None:
                            _verify_set_record(set_record, self._integrity_key)
                            expected_reason = {
                                AuthorizationSetStatus.REJECTED.value: "sibling_approval_rejected",
                                AuthorizationSetStatus.CANCELLED.value: "operator_cancelled",
                            }.get(set_record.status)
                            terminal_expiry = (
                                expected_reason is not None
                                and action.terminal_reason_code == expected_reason
                                and action.updated_at == set_record.updated_at
                            )
                    if not waiting and not terminal_expiry:
                        raise ValueError("current approval wait state disagrees with its action")
                if status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED} and (
                    decision is None
                    or action.state.value != status.value
                    or action.updated_at != decision.decided_at
                    or action.reservation is not None
                ):
                    raise ValueError("current approval decision disagrees with its action")
                if status is ApprovalStatus.CONSUMED and (
                    use is None
                    or action.reservation is None
                    or action.state
                    not in {
                        ExternalActionState.DISPATCH_RESERVED,
                        ExternalActionState.DISPATCHING,
                        ExternalActionState.SUCCEEDED,
                        ExternalActionState.FAILED,
                        ExternalActionState.CANCELLED,
                        ExternalActionState.OUTCOME_UNKNOWN,
                    }
                ):
                    raise ValueError("consumed approval lost its exact action reservation")
                if status is ApprovalStatus.SUPERSEDED:
                    superseded_reason_code = record.superseded_reason_code
                    if superseded_reason_code is None:
                        raise ValueError("superseded approval lost its terminal reason")
                    expected_action_state = {
                        "approval_set_rejected": ExternalActionState.CANCELLED,
                        "approval_set_superseded": ExternalActionState.SUPERSEDED,
                        "run_cancelled": ExternalActionState.CANCELLED,
                    }.get(superseded_reason_code)
                    expected_terminal_reason = {
                        "approval_set_rejected": "sibling_approval_rejected",
                        "approval_set_superseded": "approval_set_superseded",
                        "run_cancelled": "operator_cancelled",
                    }.get(superseded_reason_code)
                    if (
                        expected_action_state is None
                        or expected_terminal_reason is None
                        or action.state is not expected_action_state
                        or action.terminal_reason_code != expected_terminal_reason
                        or action.updated_at != record.superseded_at
                        or action.reservation is not None
                    ):
                        raise ValueError("superseded approval disagrees with its terminal action")
            return StoredActionApprovalRequest(
                request=request,
                status=status,
                version=record.version,
                updated_at=record.updated_at,
                decision=decision,
                expired_at=record.expired_at,
                replacement_request_id=record.replacement_request_id,
                renewed_at=record.renewed_at,
                superseded_at=record.superseded_at,
                superseded_reason_code=record.superseded_reason_code,
                use=use,
            )
        except (TypeError, ValueError):
            raise ApprovalPersistenceConflict(
                "approval_request_corrupt", "persisted approval lifecycle is invalid"
            ) from None

    async def _hydrate(self, record: ApprovalRequestRecord) -> StoredActionApprovalRequest:
        try:
            return await self._hydrate_unchecked(record)
        except ApprovalPersistenceConflict:
            raise
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ApprovalPersistenceConflict(
                "approval_request_corrupt", "persisted approval request failed hydration"
            ) from None

    async def get(self, request_id: str) -> StoredActionApprovalRequest | None:
        stored = await self.get_inspectable(request_id)
        if stored is None:
            return None
        await self.list_current_set(
            stored.request.run_id,
            stored.request.plan_hash,
            stored.request.proposal_revision,
        )
        return stored

    async def get_inspectable(
        self,
        request_id: str,
    ) -> StoredActionApprovalRequest | None:
        """Hydrate one immutable resource without requiring its epoch to remain current."""

        require_id(request_id, "approval request ID")
        record = await self._session.get(ApprovalRequestRecord, request_id)
        if record is None:
            return None
        return await self._hydrate(record)

    async def list_requests(
        self,
        *,
        status: ApprovalStatus | None,
        run_id: str | None,
        action_id: str | None,
        before_requested_at: datetime | None,
        before_request_id: str | None,
        limit: int,
    ) -> tuple[StoredActionApprovalRequest, ...]:
        """Return one stable keyset page after validating every selected aggregate."""

        if status is not None and type(status) is not ApprovalStatus:
            raise ValueError("approval status filter must use the exact enum")
        for value, name in ((run_id, "approval run filter"), (action_id, "approval action filter")):
            if value is not None:
                require_id(value, name)
        if (before_requested_at is None) != (before_request_id is None):
            raise ValueError("approval cursor boundary must be complete")
        if before_requested_at is not None:
            require_utc(before_requested_at, "approval cursor time")
            require_id(cast(str, before_request_id), "approval cursor request ID")
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("approval page limit must be from 1 through 101")

        statement = select(ApprovalRequestRecord)
        if status is not None:
            statement = statement.where(ApprovalRequestRecord.status == status.value)
        if run_id is not None:
            statement = statement.where(ApprovalRequestRecord.run_id == run_id)
        if action_id is not None:
            statement = statement.where(ApprovalRequestRecord.action_id == action_id)
        if before_requested_at is not None:
            assert before_request_id is not None
            statement = statement.where(
                (ApprovalRequestRecord.requested_at < before_requested_at)
                | (
                    (ApprovalRequestRecord.requested_at == before_requested_at)
                    & (ApprovalRequestRecord.id < before_request_id)
                )
            )
        statement = statement.order_by(
            ApprovalRequestRecord.requested_at.desc(),
            ApprovalRequestRecord.id.desc(),
        ).limit(limit)
        records = tuple((await self._session.execute(statement)).scalars())
        return tuple([await self._hydrate(record) for record in records])

    async def list_for_action(
        self,
        action_id: str,
    ) -> tuple[StoredActionApprovalRequest, ...]:
        """Return the exact validated generation chain for one current-set action."""

        require_id(action_id, "approval action ID")
        statement = (
            select(ApprovalRequestRecord)
            .where(ApprovalRequestRecord.action_id == action_id)
            .order_by(ApprovalRequestRecord.generation)
        )
        records = tuple((await self._session.execute(statement)).scalars())
        if not records:
            return ()
        first = records[0]
        history = await self.list_set_history(
            first.run_id,
            first.plan_hash,
            first.proposal_revision,
        )
        chain = tuple(item for item in history if item.request.action_id == action_id)
        if tuple(item.request.id for item in chain) != tuple(record.id for record in records):
            raise ApprovalPersistenceConflict(
                "approval_generation_chain_corrupt",
                "approval action history differs from its validated authorization set",
            )
        return chain

    async def _all_for_set(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> tuple[StoredActionApprovalRequest, ...]:
        statement = (
            select(ApprovalRequestRecord)
            .where(
                ApprovalRequestRecord.run_id == run_id,
                ApprovalRequestRecord.plan_hash == plan_hash,
                ApprovalRequestRecord.proposal_revision == proposal_revision,
            )
            .order_by(ApprovalRequestRecord.step_key, ApprovalRequestRecord.generation)
        )
        records = tuple((await self._session.execute(statement)).scalars())
        return tuple([await self._hydrate(record) for record in records])

    async def list_current_set(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> tuple[StoredActionApprovalRequest, ...]:
        selected = await self.get_current_authorization_set(run_id)
        if selected is not None and (
            selected.authorization_set.plan_hash != plan_hash
            or selected.authorization_set.proposal_revision != proposal_revision
        ):
            raise ApprovalPersistenceConflict(
                "authorization_set_epoch_stale",
                "caller-selected approval epoch is not the current authorization set",
            )
        actions = tuple(
            (
                await self._session.execute(
                    select(ExternalActionRecord)
                    .join(
                        RunStepRecord,
                        RunStepRecord.id == ExternalActionRecord.step_id,
                    )
                    .where(
                        ExternalActionRecord.run_id == run_id,
                        ExternalActionRecord.plan_hash == plan_hash,
                        ExternalActionRecord.proposal_revision == proposal_revision,
                    )
                    .order_by(RunStepRecord.ordinal)
                )
            ).scalars()
        )
        requests = await self._all_for_set(run_id, plan_hash, proposal_revision)
        validated_steps = await self._steps_for_run(run_id)
        write_step_ids = {
            step.id for step in validated_steps.values() if step.effect is Effect.WRITE
        }
        if {action.step_id for action in actions} != write_step_ids:
            raise ApprovalPersistenceConflict(
                "incomplete_action_set",
                "approval actions must exactly cover every validated write step",
            )
        if not actions:
            if requests:
                raise ApprovalPersistenceConflict(
                    "approval_action_set_missing",
                    "approval requests exist without their authoritative action set",
                )
            return ()
        if len({action.authorization_set_id for action in actions}) != 1:
            raise ApprovalPersistenceConflict(
                "authorization_set_corrupt",
                "one plan revision must retain one authoritative authorization set",
            )
        if selected is None:
            raise ApprovalPersistenceConflict(
                "authorization_set_head_missing",
                "approval action set has no current authorization-set head",
            )
        if tuple(action.id for action in actions) != tuple(
            member.action_id for member in selected.authorization_set.members
        ):
            raise ApprovalPersistenceConflict(
                "authorization_set_membership_corrupt",
                "approval action set differs from the current exact membership",
            )
        grouped: dict[str, list[StoredActionApprovalRequest]] = defaultdict(list)
        for request in requests:
            grouped[request.request.action_id].append(request)
        if set(grouped) != {action.id for action in actions}:
            raise ApprovalPersistenceConflict(
                "partial_approval_set",
                "approval request set is missing or has unexpected action members",
            )
        current: list[StoredActionApprovalRequest] = []
        for action in actions:
            chain = grouped[action.id]
            if [leaf.request.generation for leaf in chain] != list(range(1, len(chain) + 1)):
                raise ApprovalPersistenceConflict(
                    "approval_generation_gap", "approval request generations are not contiguous"
                )
            for old, new in pairwise(chain):
                try:
                    ApprovalRenewal(expired=old, replacement=new.request)
                except ValueError:
                    raise ApprovalPersistenceConflict(
                        "approval_renewal_chain_corrupt",
                        "approval renewal chain mixes action or set authority",
                    ) from None
            if chain[-1].replacement_request_id is not None:
                raise ApprovalPersistenceConflict(
                    "approval_renewal_chain_corrupt",
                    "current approval generation points at a missing replacement",
                )
            expected_action_version = 2
            for leaf in chain:
                if leaf.decision is not None:
                    expected_action_version += 1
                    if leaf.expired_at is not None:
                        expected_action_version += 1
            if chain[-1].status is ApprovalStatus.SUPERSEDED:
                expected_action_version += 1
            if chain[-1].status is ApprovalStatus.EXPIRED and selected.authorization_set.status in {
                AuthorizationSetStatus.REJECTED,
                AuthorizationSetStatus.CANCELLED,
            }:
                expected_action_version += 1
            if (
                chain[-1].status is not ApprovalStatus.CONSUMED
                and action.version != expected_action_version
            ):
                raise ApprovalPersistenceConflict(
                    "approval_action_lifecycle_corrupt",
                    "current approval generation and action version disagree",
                )
            if (
                chain[-1].status is ApprovalStatus.CONSUMED
                and action.version < expected_action_version + 1
            ):
                raise ApprovalPersistenceConflict(
                    "approval_action_lifecycle_corrupt",
                    "consumed approval action predates its reservation",
                )
            current.append(chain[-1])
        return tuple(current)

    async def list_set_history(
        self,
        run_id: str,
        plan_hash: str,
        proposal_revision: int,
    ) -> tuple[StoredActionApprovalRequest, ...]:
        """Return every structurally validated generation in the current set epoch."""

        await self.list_current_set(run_id, plan_hash, proposal_revision)
        return await self._all_for_set(run_id, plan_hash, proposal_revision)

    @staticmethod
    def _validate_initial_set(requests: tuple[ActionApprovalRequest, ...]) -> None:
        if type(requests) is not tuple or not requests:
            raise ValueError("initial approval request set must be a nonempty tuple")
        if any(type(request) is not ActionApprovalRequest for request in requests):
            raise ValueError("approval request set members must use the exact contract")
        first = requests[0]
        action_ids: set[str] = set()
        step_keys: set[str] = set()
        request_ids: set[str] = set()
        for request in requests:
            if (
                request.generation != 1
                or request.run_id != first.run_id
                or request.plan_hash != first.plan_hash
                or request.proposal_revision != first.proposal_revision
                or request.authorization_set_id != first.authorization_set_id
            ):
                raise ValueError("initial approval request set scope must be uniform")
            if (
                request.action_id in action_ids
                or request.step_key in step_keys
                or request.id in request_ids
            ):
                raise ValueError("initial approval request set identities must be unique")
            action_ids.add(request.action_id)
            step_keys.add(request.step_key)
            request_ids.add(request.id)

    async def _validate_candidate(self, request: ActionApprovalRequest) -> None:
        action, step, envelope, policy = await self._authority(request.action_id, request.step_id)
        expected = expected_approval_projection(
            envelope,
            step.request_redaction_fields,
        )
        try:
            assert_request_binds_action(request, envelope)
        except ValueError:
            raise ApprovalPersistenceConflict(
                "approval_candidate_mismatch",
                "approval candidate does not bind the authoritative exact action",
            ) from None
        if (
            request.action_hash != action.action_hash
            or request.authorization_set_id != envelope.authorization_set_id
            or request.run_id != action.run_id
            or request.plan_hash != envelope.plan_hash
            or request.proposal_revision != envelope.proposal_revision
            or request.step_key != envelope.step_key
            or request.step_id != action.step_id
            or request.template_id != envelope.template_id
            or request.instance_id != envelope.instance_id
            or request.action_type != envelope.action_type
            or request.capability_id != envelope.capability_id
            or request.connector_family != envelope.connector_family
            or request.binding_id != action.connector_binding_id
            or request.semantic_action_hash != envelope.semantic_action_hash
            or request.policy != policy
            or not _same_json(request.redacted_projection, expected)
            or request.redacted_destination != expected["destination"]
            or (request.generation == 1 and request.requested_at != action.created_at)
        ):
            raise ApprovalPersistenceConflict(
                "approval_candidate_mismatch",
                "approval candidate does not bind authoritative action/step snapshots",
            )

    async def _require_current_leaf(self, current: StoredActionApprovalRequest) -> None:
        current_set = await self.list_current_set(
            current.request.run_id,
            current.request.plan_hash,
            current.request.proposal_revision,
        )
        by_action = {stored.request.action_id: stored for stored in current_set}
        authoritative = by_action.get(current.request.action_id)
        if authoritative is None or authoritative.request.id != current.request.id:
            raise ApprovalPersistenceConflict(
                "approval_generation_stale",
                "approval mutation requires the current request generation",
            )

    @staticmethod
    def _replay_projection(request: ActionApprovalRequest) -> tuple[object, ...]:
        return (
            request.action_id,
            request.action_hash,
            request.authorization_set_id,
            request.run_id,
            request.plan_hash,
            request.proposal_revision,
            request.step_id,
            request.step_key,
            request.template_id,
            request.instance_id,
            request.action_type,
            request.capability_id,
            request.connector_family,
            request.binding_id,
            request.semantic_action_hash,
            canonical_json_bytes(request.redacted_projection),
            request.policy,
            request.requested_by,
        )

    async def _build_initial_authorization_set(
        self,
        requests: tuple[ActionApprovalRequest, ...],
    ) -> tuple[AuthorizationSet, AuthorizationSetHead]:
        first = requests[0]
        if any(request.requested_at != first.requested_at for request in requests):
            raise ApprovalPersistenceConflict(
                "authorization_set_time_mismatch",
                "all initial authorization members must open at one exact instant",
            )
        steps = await self._steps_for_run(first.run_id)
        ordered_writes = tuple(
            sorted(
                (step for step in steps.values() if step.effect is Effect.WRITE),
                key=lambda step: step.ordinal,
            )
        )
        by_step = {request.step_id: request for request in requests}
        if set(by_step) != {step.id for step in ordered_writes}:
            raise ApprovalPersistenceConflict(
                "authorization_set_membership_mismatch",
                "authorization set must exactly cover canonical persisted WRITE steps",
            )
        members = tuple(
            AuthorizationSetMember(
                authorization_set_id=first.authorization_set_id,
                ordinal=index,
                run_id=first.run_id,
                plan_hash=first.plan_hash,
                proposal_revision=first.proposal_revision,
                action_id=by_step[step.id].action_id,
                action_hash=by_step[step.id].action_hash,
                step_id=step.id,
                step_key=step.key,
            )
            for index, step in enumerate(ordered_writes, start=1)
        )
        authorization_set = AuthorizationSet.open(
            authorization_set_id=first.authorization_set_id,
            members=members,
            opened_at=first.requested_at,
        )
        head = AuthorizationSetHead(
            run_id=first.run_id,
            current_set_id=authorization_set.id,
            plan_hash=authorization_set.plan_hash,
            proposal_revision=authorization_set.proposal_revision,
            membership_hash=authorization_set.membership_hash,
            version=1,
            updated_at=authorization_set.opened_at,
        )
        return authorization_set, head

    async def add_initial_set_or_get(
        self,
        requests: tuple[ActionApprovalRequest, ...],
    ) -> ApprovalRequestSetInsertResult:
        self._validate_initial_set(requests)
        for request in requests:
            await self._validate_candidate(request)
        first = requests[0]
        authorization_set, head = await self._build_initial_authorization_set(requests)
        action_rows = tuple(
            (
                await self._session.execute(
                    select(ExternalActionRecord).where(
                        ExternalActionRecord.run_id == first.run_id,
                        ExternalActionRecord.plan_hash == first.plan_hash,
                        ExternalActionRecord.proposal_revision == first.proposal_revision,
                    )
                )
            ).scalars()
        )
        if {row.id for row in action_rows} != {request.action_id for request in requests} or {
            row.authorization_set_id for row in action_rows
        } != {first.authorization_set_id}:
            raise ApprovalPersistenceConflict(
                "partial_approval_set",
                "initial requests must exactly cover the authoritative action set",
            )
        validated_steps = await self._steps_for_run(first.run_id)
        write_step_ids = {
            step.id for step in validated_steps.values() if step.effect is Effect.WRITE
        }
        if {row.step_id for row in action_rows} != write_step_ids or any(
            row.reservation_id is not None or row.delivery_attempt_count != 0 for row in action_rows
        ):
            raise ApprovalPersistenceConflict(
                "incomplete_action_set",
                "initial approvals require every planned write action without delivery state",
            )
        proposed = all(
            row.state == ExternalActionState.PROPOSED.value and row.version == 1
            for row in action_rows
        )
        awaiting = all(
            row.state == ExternalActionState.AWAITING_APPROVAL.value and row.version == 2
            for row in action_rows
        )
        if not proposed and not awaiting:
            raise ApprovalPersistenceConflict(
                "approval_action_state_conflict",
                "approval registration found a partial or unexpected action lifecycle",
            )
        if awaiting:
            existing = await self.list_current_set(
                first.run_id, first.plan_hash, first.proposal_revision
            )
            by_action = {stored.request.action_id: stored for stored in existing}
            if len(existing) != len(requests) or set(by_action) != {
                request.action_id for request in requests
            }:
                raise ApprovalPersistenceConflict(
                    "partial_approval_set",
                    "approval replay found a partial or unexpected request set",
                )
            ordered = tuple(by_action[request.action_id] for request in requests)
            if any(
                self._replay_projection(candidate) != self._replay_projection(stored.request)
                for candidate, stored in zip(requests, ordered, strict=True)
            ):
                raise ApprovalPersistenceConflict(
                    "approval_request_collision",
                    "approval replay differs from authoritative stored semantics",
                )
            current = await self.get_current_authorization_set(first.run_id)
            if current is None or current.authorization_set != authorization_set:
                raise ApprovalPersistenceConflict(
                    "authorization_set_replay_conflict",
                    "approval replay differs from its current authorization set",
                )
            return ApprovalRequestSetInsertResult(
                requests=ordered,
                authorization_set=current.authorization_set,
                head=current.head,
                inserted=False,
            )
        try:
            async with self._session.begin_nested():
                self._session.add(_set_to_record(authorization_set, self._integrity_key))
                await self._session.flush()
                self._session.add_all(
                    [
                        _member_to_record(
                            member,
                            authorization_set.membership_hash,
                            self._integrity_key,
                        )
                        for member in authorization_set.members
                    ]
                )
                await self._session.flush()
                self._session.add(_head_to_record(head, self._integrity_key))
                self._session.add_all(
                    [
                        _request_to_record(
                            StoredActionApprovalRequest.created(request),
                            self._integrity_key,
                        )
                        for request in requests
                    ]
                )
                await self._session.flush()
                for request in requests:
                    statement = (
                        update(ExternalActionRecord)
                        .where(
                            ExternalActionRecord.id == request.action_id,
                            ExternalActionRecord.action_hash == request.action_hash,
                            ExternalActionRecord.state == ExternalActionState.PROPOSED.value,
                            ExternalActionRecord.version == 1,
                            ExternalActionRecord.reservation_id.is_(None),
                            ExternalActionRecord.delivery_attempt_count == 0,
                        )
                        .values(
                            state=ExternalActionState.AWAITING_APPROVAL.value,
                            version=2,
                            updated_at=request.requested_at,
                        )
                        .returning(ExternalActionRecord.id)
                        .execution_options(synchronize_session=False)
                    )
                    if (await self._session.execute(statement)).scalar_one_or_none() is None:
                        raise _ApprovalCASLost
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                raise ApprovalPersistenceConflict(
                    "approval_create_race", "approval creation lost a concurrent writer race"
                ) from None
            raise
        except (IntegrityError, _ApprovalCASLost):
            self._session.expire_all()
            existing = await self.list_current_set(
                first.run_id, first.plan_hash, first.proposal_revision
            )
            by_action = {stored.request.action_id: stored for stored in existing}
            if len(existing) != len(requests) or set(by_action) != {
                request.action_id for request in requests
            }:
                raise ApprovalPersistenceConflict(
                    "partial_approval_set",
                    "approval replay found a partial or unexpected request set",
                ) from None
            ordered = tuple(by_action[request.action_id] for request in requests)
            if any(
                self._replay_projection(candidate) != self._replay_projection(stored.request)
                for candidate, stored in zip(requests, ordered, strict=True)
            ):
                raise ApprovalPersistenceConflict(
                    "approval_request_collision",
                    "approval replay differs from authoritative stored semantics",
                ) from None
            current = await self.get_current_authorization_set(first.run_id)
            if current is None:
                raise ApprovalPersistenceConflict(
                    "authorization_set_head_missing",
                    "approval replay lost its current authorization set",
                ) from None
            return ApprovalRequestSetInsertResult(
                requests=ordered,
                authorization_set=current.authorization_set,
                head=current.head,
                inserted=False,
            )
        self._session.expire_all()
        inserted = tuple(
            [cast(StoredActionApprovalRequest, await self.get(r.id)) for r in requests]
        )
        if any(stored is None for stored in inserted):  # pragma: no cover - cast guard
            raise ApprovalPersistenceConflict(
                "approval_request_corrupt", "inserted approval request could not be rehydrated"
            )
        current = await self.get_current_authorization_set(first.run_id)
        if current is None:
            raise ApprovalPersistenceConflict(
                "authorization_set_head_missing",
                "inserted approval set lost its current authorization head",
            )
        return ApprovalRequestSetInsertResult(
            requests=inserted,
            authorization_set=current.authorization_set,
            head=current.head,
            inserted=True,
        )

    async def record_decision(
        self,
        *,
        expected_version: int,
        expected_action_version: int,
        decision: ApprovalDecision,
    ) -> ApprovalDecisionInsertResult:
        if type(decision) is not ApprovalDecision:
            raise ValueError("approval decision must use the exact immutable contract")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected approval version must be a positive integer")
        if type(expected_action_version) is not int or expected_action_version < 1:
            raise ValueError("expected action version must be a positive integer")
        current = await self.get(decision.request_id)
        if current is None:
            raise ApprovalPersistenceConflict(
                "approval_request_missing", "approval decision request does not exist"
            )
        await self._require_current_leaf(current)
        try:
            assert_decision_binds_request(decision, current.request)
            target = StoredActionApprovalRequest(
                request=current.request,
                status=(
                    ApprovalStatus.APPROVED
                    if decision.decision is ApprovalDecisionKind.APPROVE
                    else ApprovalStatus.REJECTED
                ),
                version=expected_version + 1,
                updated_at=decision.decided_at,
                decision=decision,
            )
        except ValueError:
            raise ApprovalPersistenceConflict(
                "approval_decision_invalid",
                "approval decision does not satisfy the exact request policy",
            ) from None
        if (
            current.version != expected_version
            or current.status is not ApprovalStatus.PENDING
            or decision.decided_at >= current.request.expires_at
        ):
            existing_record = (
                await self._session.execute(
                    select(ApprovalDecisionRecord).where(
                        ApprovalDecisionRecord.request_id == decision.request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                existing_record is not None
                and _decision_from_record(existing_record, self._integrity_key) == decision
            ):
                return ApprovalDecisionInsertResult(request=current, inserted=False)
            raise ApprovalPersistenceConflict(
                "approval_decision_conflict",
                "approval request is expired, decided, or changed concurrently",
            )
        try:
            async with self._session.begin_nested():
                self._session.add(_decision_to_record(decision, self._integrity_key))
                await self._session.flush()
                action_state = (
                    ExternalActionState.APPROVED
                    if decision.decision is ApprovalDecisionKind.APPROVE
                    else ExternalActionState.REJECTED
                )
                action_statement = (
                    update(ExternalActionRecord)
                    .where(
                        ExternalActionRecord.id == decision.action_id,
                        ExternalActionRecord.action_hash == decision.action_hash,
                        ExternalActionRecord.version == expected_action_version,
                        ExternalActionRecord.state == ExternalActionState.AWAITING_APPROVAL.value,
                    )
                    .values(
                        state=action_state.value,
                        version=expected_action_version + 1,
                        updated_at=decision.decided_at,
                        terminal_reason_code=(
                            None
                            if action_state is ExternalActionState.APPROVED
                            else "approval_rejected"
                        ),
                    )
                    .returning(ExternalActionRecord.id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(action_statement)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
                statement = (
                    update(ApprovalRequestRecord)
                    .where(
                        ApprovalRequestRecord.id == decision.request_id,
                        ApprovalRequestRecord.version == expected_version,
                        ApprovalRequestRecord.status == ApprovalStatus.PENDING.value,
                        ApprovalRequestRecord.expires_at > decision.decided_at,
                    )
                    .values(**_request_update_values(target, self._integrity_key))
                    .returning(ApprovalRequestRecord.id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(statement)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                raise ApprovalPersistenceConflict(
                    "approval_decision_conflict",
                    "another decision raced the approval request",
                ) from None
            raise
        except (IntegrityError, _ApprovalCASLost):
            self._session.expire_all()
            authoritative = await self.get(decision.request_id)
            existing_record = (
                await self._session.execute(
                    select(ApprovalDecisionRecord).where(
                        ApprovalDecisionRecord.request_id == decision.request_id
                    )
                )
            ).scalar_one_or_none()
            if (
                authoritative is not None
                and existing_record is not None
                and _decision_from_record(existing_record, self._integrity_key) == decision
            ):
                return ApprovalDecisionInsertResult(request=authoritative, inserted=False)
            raise ApprovalPersistenceConflict(
                "approval_decision_conflict",
                "another decision won the approval request",
            ) from None
        self._session.expire_all()
        updated_request = await self.get(decision.request_id)
        if updated_request is None:
            raise ApprovalPersistenceConflict(
                "approval_request_corrupt", "decided approval disappeared after persistence"
            )
        return ApprovalDecisionInsertResult(request=updated_request, inserted=True)

    async def release_current_set(
        self,
        command: AuthorizationSetReleaseCommand,
    ) -> AuthorizationSetReleaseResult:
        if type(command) is not AuthorizationSetReleaseCommand:
            raise ValueError("authorization release must use the exact command contract")
        selected = await self.get_current_authorization_set(command.authorization_set.run_id)
        if (
            selected is None
            or selected.head != command.head
            or selected.authorization_set != command.authorization_set
            or selected.authorization_set.status is not AuthorizationSetStatus.OPEN
        ):
            raise ApprovalPersistenceConflict(
                "authorization_release_conflict",
                "authorization set changed before barrier release",
            )
        current_requests = await self.list_current_set(
            command.authorization_set.run_id,
            command.authorization_set.plan_hash,
            command.authorization_set.proposal_revision,
        )
        by_action = {stored.request.action_id: stored for stored in current_requests}
        if set(by_action) != {member.action.id for member in command.members}:
            raise ApprovalPersistenceConflict(
                "authorization_release_partial",
                "authorization release does not cover the complete current set",
            )
        for release_member in command.members:
            authoritative = by_action.get(release_member.action.id)
            if authoritative != release_member.request:
                raise ApprovalPersistenceConflict(
                    "authorization_release_stale",
                    "approval leaf changed before barrier release",
                )
            action = await SQLAlchemyExternalActionRepository(self._session).get(
                release_member.action.id
            )
            if action != release_member.action:
                raise ApprovalPersistenceConflict(
                    "authorization_release_stale",
                    "external action changed before barrier release",
                )

        target_set = AuthorizationSet(
            id=command.authorization_set.id,
            run_id=command.authorization_set.run_id,
            plan_hash=command.authorization_set.plan_hash,
            proposal_revision=command.authorization_set.proposal_revision,
            membership_hash=command.authorization_set.membership_hash,
            members=command.authorization_set.members,
            status=AuthorizationSetStatus.RELEASED,
            version=command.authorization_set.version + 1,
            opened_at=command.authorization_set.opened_at,
            updated_at=command.released_at,
            release_hash=command.release_hash,
            released_at=command.released_at,
            released_run_version=command.run_transition.run.version,
            terminal_reason_code="approval_barrier_satisfied",
        )
        try:
            async with self._session.begin_nested():
                run_applied = await SQLAlchemyRunRepository(self._session).apply_transition(
                    expected_version=command.run_transition.transition.expected_version,
                    expected_state=RunState.AWAITING_APPROVAL,
                    result=command.run_transition,
                )
                if not run_applied:
                    raise _ApprovalCASLost
                for set_member, release_member in zip(
                    command.authorization_set.members,
                    command.members,
                    strict=True,
                ):
                    request = release_member.request
                    action = release_member.action
                    decision = request.decision
                    assert decision is not None
                    step_result = release_member.step_transition
                    step_applied = await SQLAlchemyRunStepRepository(
                        self._session
                    ).apply_transition(
                        expected_run_version=command.run_transition.run.version,
                        expected_run_state=RunState.EXECUTING,
                        expected_version=step_result.transition.expected_version,
                        expected_state=StepState.AWAITING_APPROVAL,
                        result=step_result,
                    )
                    if not step_applied:
                        raise _ApprovalCASLost
                    reservation = release_member.reservation
                    action_statement = (
                        update(ExternalActionRecord)
                        .where(
                            ExternalActionRecord.id == action.id,
                            ExternalActionRecord.action_hash == action.action_hash,
                            ExternalActionRecord.authorization_set_id
                            == command.authorization_set.id,
                            ExternalActionRecord.run_id == command.authorization_set.run_id,
                            ExternalActionRecord.plan_hash == command.authorization_set.plan_hash,
                            ExternalActionRecord.proposal_revision
                            == command.authorization_set.proposal_revision,
                            ExternalActionRecord.step_id == set_member.step_id,
                            ExternalActionRecord.step_key == set_member.step_key,
                            ExternalActionRecord.version == action.version,
                            ExternalActionRecord.state == ExternalActionState.APPROVED.value,
                            ExternalActionRecord.reservation_id.is_(None),
                            ExternalActionRecord.delivery_attempt_count == 0,
                        )
                        .values(
                            state=ExternalActionState.DISPATCH_RESERVED.value,
                            reservation_id=reservation.reservation_id,
                            reservation_authorization_set_id=reservation.authorization_set_id,
                            approval_request_id=reservation.approval_request_id,
                            approval_decision_id=reservation.approval_decision_id,
                            reservation_action_hash=reservation.action_hash,
                            reservation_capability_id=reservation.capability_id,
                            reservation_binding_id=reservation.binding_id,
                            reservation_idempotency_key=reservation.idempotency_key,
                            reserved_at=reservation.reserved_at,
                            updated_at=command.released_at,
                            version=action.version + 1,
                        )
                        .returning(ExternalActionRecord.id)
                        .execution_options(synchronize_session=False)
                    )
                    if (await self._session.execute(action_statement)).scalar_one_or_none() is None:
                        raise _ApprovalCASLost
                    self._session.add(_use_to_record(release_member.use, self._integrity_key))
                    await self._session.flush()
                    target_request = StoredActionApprovalRequest(
                        request=request.request,
                        status=ApprovalStatus.CONSUMED,
                        version=request.version + 1,
                        updated_at=command.released_at,
                        decision=decision,
                        use=release_member.use,
                    )
                    request_statement = (
                        update(ApprovalRequestRecord)
                        .where(
                            ApprovalRequestRecord.id == request.request.id,
                            ApprovalRequestRecord.action_id == action.id,
                            ApprovalRequestRecord.action_hash == action.action_hash,
                            ApprovalRequestRecord.authorization_set_id
                            == command.authorization_set.id,
                            ApprovalRequestRecord.version == request.version,
                            ApprovalRequestRecord.status == ApprovalStatus.APPROVED.value,
                            ApprovalRequestRecord.expires_at > command.released_at,
                            ApprovalRequestRecord.replacement_request_id.is_(None),
                        )
                        .values(
                            **_request_update_values(
                                target_request,
                                self._integrity_key,
                            )
                        )
                        .returning(ApprovalRequestRecord.id)
                        .execution_options(synchronize_session=False)
                    )
                    if (
                        await self._session.execute(request_statement)
                    ).scalar_one_or_none() is None:
                        raise _ApprovalCASLost

                    released_member_record = AuthorizationSetMemberRecord(
                        authorization_set_id=set_member.authorization_set_id,
                        ordinal=set_member.ordinal,
                        run_id=set_member.run_id,
                        plan_hash=set_member.plan_hash,
                        proposal_revision=set_member.proposal_revision,
                        membership_hash=command.authorization_set.membership_hash,
                        action_id=set_member.action_id,
                        action_hash=set_member.action_hash,
                        step_id=set_member.step_id,
                        step_key=set_member.step_key,
                        approval_request_id=request.request.id,
                        approval_decision_id=decision.id,
                        approval_use_id=release_member.use.id,
                        reservation_id=reservation.reservation_id,
                        released_action_source_version=action.version,
                        released_request_source_version=request.version,
                        released_step_version=step_result.step.version,
                        released_at=command.released_at,
                    )
                    _seal_set_member_record(
                        released_member_record,
                        self._integrity_key,
                    )
                    member_statement = (
                        update(AuthorizationSetMemberRecord)
                        .where(
                            AuthorizationSetMemberRecord.authorization_set_id
                            == set_member.authorization_set_id,
                            AuthorizationSetMemberRecord.ordinal == set_member.ordinal,
                            AuthorizationSetMemberRecord.membership_hash
                            == command.authorization_set.membership_hash,
                            AuthorizationSetMemberRecord.action_id == set_member.action_id,
                            AuthorizationSetMemberRecord.action_hash == set_member.action_hash,
                            AuthorizationSetMemberRecord.approval_use_id.is_(None),
                            AuthorizationSetMemberRecord.released_at.is_(None),
                        )
                        .values(
                            approval_request_id=released_member_record.approval_request_id,
                            approval_decision_id=released_member_record.approval_decision_id,
                            approval_use_id=released_member_record.approval_use_id,
                            reservation_id=released_member_record.reservation_id,
                            released_action_source_version=(
                                released_member_record.released_action_source_version
                            ),
                            released_request_source_version=(
                                released_member_record.released_request_source_version
                            ),
                            released_step_version=released_member_record.released_step_version,
                            released_at=released_member_record.released_at,
                            integrity_digest=released_member_record.integrity_digest,
                        )
                        .returning(AuthorizationSetMemberRecord.ordinal)
                        .execution_options(synchronize_session=False)
                    )
                    if (await self._session.execute(member_statement)).scalar_one_or_none() is None:
                        raise _ApprovalCASLost

                head_fence = (
                    update(AuthorizationSetHeadRecord)
                    .where(
                        AuthorizationSetHeadRecord.run_id == command.head.run_id,
                        AuthorizationSetHeadRecord.current_set_id == command.head.current_set_id,
                        AuthorizationSetHeadRecord.membership_hash == command.head.membership_hash,
                        AuthorizationSetHeadRecord.version == command.head.version,
                    )
                    .values(version=AuthorizationSetHeadRecord.version)
                    .returning(AuthorizationSetHeadRecord.run_id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(head_fence)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
                target_record = _set_to_record(target_set, self._integrity_key)
                set_statement = (
                    update(AuthorizationSetRecord)
                    .where(
                        AuthorizationSetRecord.id == command.authorization_set.id,
                        AuthorizationSetRecord.membership_hash
                        == command.authorization_set.membership_hash,
                        AuthorizationSetRecord.version == command.authorization_set.version,
                        AuthorizationSetRecord.status == AuthorizationSetStatus.OPEN.value,
                        AuthorizationSetRecord.release_hash.is_(None),
                    )
                    .values(
                        status=target_record.status,
                        version=target_record.version,
                        updated_at=target_record.updated_at,
                        release_hash=target_record.release_hash,
                        released_at=target_record.released_at,
                        released_run_version=target_record.released_run_version,
                        terminal_reason_code=target_record.terminal_reason_code,
                        integrity_digest=target_record.integrity_digest,
                    )
                    .returning(AuthorizationSetRecord.id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(set_statement)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                raise ApprovalPersistenceConflict(
                    "authorization_release_conflict",
                    "another worker raced authorization barrier release",
                ) from None
            raise
        except (IntegrityError, _ApprovalCASLost):
            raise ApprovalPersistenceConflict(
                "authorization_release_conflict",
                "authorization barrier release lost an atomic CAS",
            ) from None

        self._session.expire_all()
        self._validated_steps.pop(command.authorization_set.run_id, None)
        current = await self.get_current_authorization_set(command.authorization_set.run_id)
        if (
            current is None
            or current.authorization_set.status is not AuthorizationSetStatus.RELEASED
            or current.authorization_set.release_hash != command.release_hash
        ):
            raise ApprovalPersistenceConflict(
                "authorization_release_corrupt",
                "released authorization set failed exact rehydration",
            )
        run = await SQLAlchemyRunRepository(self._session).get(command.authorization_set.run_id)
        if run is None:
            raise ApprovalPersistenceConflict(
                "authorization_release_corrupt",
                "released authorization set lost its Run",
            )
        steps_by_id = {
            step.id: step
            for step in await SQLAlchemyRunStepRepository(self._session).list_for_run(run.id)
        }
        actions: list[ExternalAction] = []
        requests: list[StoredActionApprovalRequest] = []
        for member in current.authorization_set.members:
            action = await SQLAlchemyExternalActionRepository(self._session).get(member.action_id)
            stored_request = await self.get(by_action[member.action_id].request.id)
            if action is None or stored_request is None or member.step_id not in steps_by_id:
                raise ApprovalPersistenceConflict(
                    "authorization_release_corrupt",
                    "released member disappeared after persistence",
                )
            actions.append(action)
            requests.append(stored_request)
        return AuthorizationSetReleaseResult(
            authorization_set=current.authorization_set,
            head=current.head,
            run=run,
            steps=tuple(
                steps_by_id[member.step_id] for member in current.authorization_set.members
            ),
            actions=tuple(actions),
            requests=tuple(requests),
            inserted=True,
        )

    async def get_release_authority(self, action_id: str) -> ReleaseAuthority | None:
        action_record = await self._session.get(ExternalActionRecord, action_id)
        if action_record is None:
            return None
        selected = await self.get_current_authorization_set(action_record.run_id)
        if (
            selected is None
            or selected.authorization_set.status is not AuthorizationSetStatus.RELEASED
            or selected.authorization_set.id != action_record.authorization_set_id
            or selected.authorization_set.release_hash is None
            or selected.authorization_set.released_run_version is None
        ):
            return None
        member_records = tuple(
            (
                await self._session.execute(
                    select(AuthorizationSetMemberRecord).where(
                        AuthorizationSetMemberRecord.authorization_set_id
                        == selected.authorization_set.id,
                        AuthorizationSetMemberRecord.action_id == action_id,
                    )
                )
            ).scalars()
        )
        if len(member_records) != 1:
            return None
        member = member_records[0]
        release_values = (
            member.approval_request_id,
            member.approval_decision_id,
            member.approval_use_id,
            member.reservation_id,
            member.released_step_version,
            member.released_at,
        )
        if any(value is None for value in release_values):
            return None
        run_record = await self._session.get(RunRecord, action_record.run_id)
        step_record = await self._session.get(RunStepRecord, member.step_id)
        request_record = await self._session.get(
            ApprovalRequestRecord,
            cast(str, member.approval_request_id),
        )
        use_record = await self._session.get(
            ApprovalUseRecord,
            cast(str, member.approval_use_id),
        )
        if (
            run_record is None
            or step_record is None
            or request_record is None
            or use_record is None
            or run_record.state != RunState.EXECUTING.value
            or run_record.version != selected.authorization_set.released_run_version
            or request_record.status != ApprovalStatus.CONSUMED.value
            or request_record.id != member.approval_request_id
            or use_record.id != member.approval_use_id
            or use_record.request_id != member.approval_request_id
            or use_record.decision_id != member.approval_decision_id
            or use_record.reservation_id != member.reservation_id
            or action_record.state
            not in {
                ExternalActionState.DISPATCH_RESERVED.value,
                ExternalActionState.DISPATCHING.value,
            }
            or action_record.reservation_id != member.reservation_id
            or action_record.reservation_authorization_set_id != selected.authorization_set.id
            or action_record.approval_request_id != member.approval_request_id
            or action_record.approval_decision_id != member.approval_decision_id
            or action_record.reservation_action_hash != action_record.action_hash
            or action_record.action_hash != member.action_hash
            or action_record.step_id != member.step_id
            or action_record.step_key != member.step_key
        ):
            return None
        initial_step = (
            step_record.state == StepState.READY.value
            and step_record.version == member.released_step_version
        )
        retry_step = (
            step_record.state == StepState.EXECUTING.value
            and step_record.version == cast(int, member.released_step_version) + 1
        )
        if not initial_step and not retry_step:
            return None
        attempts = tuple(
            (
                await self._session.execute(
                    select(ExternalActionDispatchAttemptRecord)
                    .where(ExternalActionDispatchAttemptRecord.external_action_id == action_id)
                    .order_by(ExternalActionDispatchAttemptRecord.attempt_number)
                )
            ).scalars()
        )
        if tuple(attempt.attempt_number for attempt in attempts) != tuple(
            range(1, action_record.delivery_attempt_count + 1)
        ):
            return None
        if action_record.state == ExternalActionState.DISPATCHING.value:
            current_attempt = attempts[-1] if attempts else None
            if (
                current_attempt is None
                or current_attempt.attempt_number != action_record.dispatch_attempt_number
                or current_attempt.idempotency_support != action_record.idempotency_support
                or current_attempt.lease_owner != action_record.dispatch_lease_owner
                or current_attempt.claimed_at != action_record.dispatch_claimed_at
                or current_attempt.lease_expires_at != action_record.dispatch_lease_expires_at
                or current_attempt.call_started_at is not None
                or current_attempt.completed_at is not None
                or current_attempt.conclusion is not None
            ):
                return None
            predecessors = attempts[:-1]
        else:
            predecessors = attempts
        started_numbers: list[int] = []
        for attempt in predecessors:
            if (
                attempt.idempotency_support != action_record.idempotency_support
                or attempt.completed_at is None
                or attempt.conclusion is None
            ):
                return None
            if attempt.call_started_at is None:
                if attempt.conclusion != "pre_call_expired":
                    return None
            elif attempt.conclusion == "provider_retry":
                started_numbers.append(attempt.attempt_number)
            else:
                return None
        if not started_numbers:
            if not initial_step:
                return None
            call_mode = ReleaseCallMode.FIRST_CALL
            prior_started_attempt_number = None
        else:
            start_transition = await self._session.get(
                RunStepStateTransitionRecord,
                (member.step_id, cast(int, member.released_step_version) + 1),
            )
            if (
                not retry_step
                or action_record.idempotency_support not in {"required", "supported"}
                or start_transition is None
                or start_transition.command != "start_reserved_write"
                or start_transition.previous_state != StepState.READY.value
                or start_transition.new_state != StepState.EXECUTING.value
                or start_transition.expected_version != member.released_step_version
                or start_transition.occurred_at
                != next(
                    attempt.call_started_at
                    for attempt in predecessors
                    if attempt.call_started_at is not None
                )
            ):
                return None
            call_mode = ReleaseCallMode.PROVIDER_RETRY
            prior_started_attempt_number = started_numbers[-1]
        unsatisfied_dependency = (
            await self._session.execute(
                select(RunStepRecord.id)
                .join(
                    RunStepDependencyRecord,
                    (RunStepRecord.run_id == RunStepDependencyRecord.run_id)
                    & (RunStepRecord.key == RunStepDependencyRecord.dependency_key),
                )
                .where(
                    RunStepDependencyRecord.step_id == member.step_id,
                    RunStepRecord.state != StepState.SUCCEEDED.value,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if unsatisfied_dependency is not None:
            return None
        return ReleaseAuthority(
            authorization_set_id=selected.authorization_set.id,
            membership_hash=selected.authorization_set.membership_hash,
            release_hash=selected.authorization_set.release_hash,
            authorization_set_version=selected.authorization_set.version,
            head_version=selected.head.version,
            run_id=selected.authorization_set.run_id,
            released_run_version=selected.authorization_set.released_run_version,
            action_id=member.action_id,
            action_hash=member.action_hash,
            step_id=member.step_id,
            step_key=member.step_key,
            released_step_version=cast(int, member.released_step_version),
            step_state=StepState(step_record.state),
            step_version=step_record.version,
            call_mode=call_mode,
            prior_started_attempt_number=prior_started_attempt_number,
            approval_request_id=member.approval_request_id,
            approval_decision_id=member.approval_decision_id,
            approval_use_id=member.approval_use_id,
            reservation_id=member.reservation_id,
        )

    async def close_current_set(
        self,
        command: AuthorizationSetCloseCommand,
    ) -> AuthorizationSetCloseResult:
        if type(command) is not AuthorizationSetCloseCommand:
            raise ValueError("authorization closure must use the exact command contract")
        selected = await self.get_current_authorization_set(command.authorization_set.run_id)
        if (
            selected is None
            or selected.head != command.head
            or selected.authorization_set != command.authorization_set
            or selected.authorization_set.status is not AuthorizationSetStatus.OPEN
        ):
            raise ApprovalPersistenceConflict(
                "authorization_close_conflict",
                "authorization set changed before terminal closure",
            )
        current_requests = await self.list_current_set(
            command.authorization_set.run_id,
            command.authorization_set.plan_hash,
            command.authorization_set.proposal_revision,
        )
        requests_by_action = {stored.request.action_id: stored for stored in current_requests}
        actions_by_id = {action.id: action for action in command.actions}
        action_repository = SQLAlchemyExternalActionRepository(self._session)
        persisted_actions: dict[str, ExternalAction | None] = {}
        for action_id in actions_by_id:
            persisted_actions[action_id] = await action_repository.get(action_id)
        if (
            set(requests_by_action)
            != {member.action_id for member in command.authorization_set.members}
            or set(actions_by_id) != set(requests_by_action)
            or any(
                persisted_actions[action_id] != action
                for action_id, action in actions_by_id.items()
            )
            or any(
                requests_by_action[stored.request.action_id] != stored
                for stored in command.requests
            )
        ):
            raise ApprovalPersistenceConflict(
                "authorization_close_stale",
                "authorization closure sources differ from the current exact set",
            )
        stored_steps = await SQLAlchemyRunStepRepository(self._session).list_for_run(
            command.authorization_set.run_id
        )
        mutable_step_ids = {
            step.id
            for step in stored_steps
            if step.state
            in {
                StepState.PENDING,
                StepState.READY,
                StepState.AWAITING_APPROVAL,
            }
        }
        transitions_by_id = {result.step.id: result for result in command.step_transitions}
        if set(transitions_by_id) != mutable_step_ids:
            raise ApprovalPersistenceConflict(
                "authorization_close_partial",
                "authorization closure must terminalize every mutable plan step",
            )
        target_reason = (
            "approval_rejected"
            if command.status is AuthorizationSetStatus.REJECTED
            else "operator_cancelled"
        )
        target_set = AuthorizationSet(
            id=command.authorization_set.id,
            run_id=command.authorization_set.run_id,
            plan_hash=command.authorization_set.plan_hash,
            proposal_revision=command.authorization_set.proposal_revision,
            membership_hash=command.authorization_set.membership_hash,
            members=command.authorization_set.members,
            status=command.status,
            version=command.authorization_set.version + 1,
            opened_at=command.authorization_set.opened_at,
            updated_at=command.closed_at,
            terminal_reason_code=target_reason,
        )
        expected_run_state = command.run_transition.transition.previous_state
        if expected_run_state is None:
            raise ValueError("authorization closure requires an existing Run state")
        try:
            async with self._session.begin_nested():
                if not await SQLAlchemyRunRepository(self._session).apply_transition(
                    expected_version=command.run_transition.transition.expected_version,
                    expected_state=expected_run_state,
                    result=command.run_transition,
                ):
                    raise _ApprovalCASLost
                for source_step in stored_steps:
                    result = transitions_by_id.get(source_step.id)
                    if result is None:
                        continue
                    if not await SQLAlchemyRunStepRepository(self._session).apply_transition(
                        expected_run_version=command.run_transition.run.version,
                        expected_run_state=command.run_transition.run.state,
                        expected_version=source_step.version,
                        expected_state=source_step.state,
                        result=result,
                    ):
                        raise _ApprovalCASLost
                for member in command.authorization_set.members:
                    source_request = requests_by_action[member.action_id]
                    source_action = actions_by_id[member.action_id]
                    if (
                        command.status is AuthorizationSetStatus.REJECTED
                        and source_request.status is ApprovalStatus.REJECTED
                        and source_action.state is ExternalActionState.REJECTED
                    ):
                        fence = (
                            update(ExternalActionRecord)
                            .where(
                                ExternalActionRecord.id == source_action.id,
                                ExternalActionRecord.version == source_action.version,
                                ExternalActionRecord.state == ExternalActionState.REJECTED.value,
                                ExternalActionRecord.authorization_set_id
                                == command.authorization_set.id,
                            )
                            .values(version=ExternalActionRecord.version)
                            .returning(ExternalActionRecord.id)
                            .execution_options(synchronize_session=False)
                        )
                        if (await self._session.execute(fence)).scalar_one_or_none() is None:
                            raise _ApprovalCASLost
                        continue
                    expired_source = (
                        source_request.status is ApprovalStatus.EXPIRED
                        and source_action.state is ExternalActionState.AWAITING_APPROVAL
                    )
                    if (
                        not expired_source
                        and source_request.status
                        not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                    ) or source_action.state not in {
                        ExternalActionState.AWAITING_APPROVAL,
                        ExternalActionState.APPROVED,
                    }:
                        raise _ApprovalCASLost
                    target_action_state = ExternalActionState.CANCELLED
                    target_action_reason = (
                        "sibling_approval_rejected"
                        if command.status is AuthorizationSetStatus.REJECTED
                        else "operator_cancelled"
                    )
                    action_update = (
                        update(ExternalActionRecord)
                        .where(
                            ExternalActionRecord.id == source_action.id,
                            ExternalActionRecord.action_hash == source_action.action_hash,
                            ExternalActionRecord.authorization_set_id
                            == command.authorization_set.id,
                            ExternalActionRecord.version == source_action.version,
                            ExternalActionRecord.state == source_action.state.value,
                            ExternalActionRecord.reservation_id.is_(None),
                        )
                        .values(
                            state=target_action_state.value,
                            version=source_action.version + 1,
                            updated_at=command.closed_at,
                            terminal_reason_code=target_action_reason,
                        )
                        .returning(ExternalActionRecord.id)
                        .execution_options(synchronize_session=False)
                    )
                    if (await self._session.execute(action_update)).scalar_one_or_none() is None:
                        raise _ApprovalCASLost
                    if expired_source:
                        request_fence = (
                            update(ApprovalRequestRecord)
                            .where(
                                ApprovalRequestRecord.id == source_request.request.id,
                                ApprovalRequestRecord.version == source_request.version,
                                ApprovalRequestRecord.status == ApprovalStatus.EXPIRED.value,
                                ApprovalRequestRecord.authorization_set_id
                                == command.authorization_set.id,
                                ApprovalRequestRecord.replacement_request_id.is_(None),
                            )
                            .values(version=ApprovalRequestRecord.version)
                            .returning(ApprovalRequestRecord.id)
                            .execution_options(synchronize_session=False)
                        )
                        if (
                            await self._session.execute(request_fence)
                        ).scalar_one_or_none() is None:
                            raise _ApprovalCASLost
                        continue
                    target_request = StoredActionApprovalRequest(
                        request=source_request.request,
                        status=ApprovalStatus.SUPERSEDED,
                        version=source_request.version + 1,
                        updated_at=command.closed_at,
                        decision=source_request.decision,
                        superseded_at=command.closed_at,
                        superseded_reason_code=(
                            "approval_set_rejected"
                            if command.status is AuthorizationSetStatus.REJECTED
                            else "run_cancelled"
                        ),
                    )
                    request_update = (
                        update(ApprovalRequestRecord)
                        .where(
                            ApprovalRequestRecord.id == source_request.request.id,
                            ApprovalRequestRecord.version == source_request.version,
                            ApprovalRequestRecord.status == source_request.status.value,
                            ApprovalRequestRecord.authorization_set_id
                            == command.authorization_set.id,
                            ApprovalRequestRecord.replacement_request_id.is_(None),
                        )
                        .values(
                            **_request_update_values(
                                target_request,
                                self._integrity_key,
                            )
                        )
                        .returning(ApprovalRequestRecord.id)
                        .execution_options(synchronize_session=False)
                    )
                    if (await self._session.execute(request_update)).scalar_one_or_none() is None:
                        raise _ApprovalCASLost
                head_fence = (
                    update(AuthorizationSetHeadRecord)
                    .where(
                        AuthorizationSetHeadRecord.run_id == command.head.run_id,
                        AuthorizationSetHeadRecord.current_set_id == command.head.current_set_id,
                        AuthorizationSetHeadRecord.membership_hash == command.head.membership_hash,
                        AuthorizationSetHeadRecord.version == command.head.version,
                    )
                    .values(version=AuthorizationSetHeadRecord.version)
                    .returning(AuthorizationSetHeadRecord.run_id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(head_fence)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
                target_record = _set_to_record(target_set, self._integrity_key)
                set_update = (
                    update(AuthorizationSetRecord)
                    .where(
                        AuthorizationSetRecord.id == command.authorization_set.id,
                        AuthorizationSetRecord.version == command.authorization_set.version,
                        AuthorizationSetRecord.status == AuthorizationSetStatus.OPEN.value,
                        AuthorizationSetRecord.membership_hash
                        == command.authorization_set.membership_hash,
                    )
                    .values(
                        status=target_record.status,
                        version=target_record.version,
                        updated_at=target_record.updated_at,
                        terminal_reason_code=target_record.terminal_reason_code,
                        integrity_digest=target_record.integrity_digest,
                    )
                    .returning(AuthorizationSetRecord.id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(set_update)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                raise ApprovalPersistenceConflict(
                    "authorization_close_conflict",
                    "another worker raced authorization-set closure",
                ) from None
            raise
        except (IntegrityError, _ApprovalCASLost):
            raise ApprovalPersistenceConflict(
                "authorization_close_conflict",
                "authorization-set closure lost an atomic CAS",
            ) from None
        self._session.expire_all()
        self._validated_steps.pop(command.authorization_set.run_id, None)
        current = await self.get_current_authorization_set(command.authorization_set.run_id)
        run = await SQLAlchemyRunRepository(self._session).get(command.authorization_set.run_id)
        if current is None or run is None or current.authorization_set.status is not command.status:
            raise ApprovalPersistenceConflict(
                "authorization_close_corrupt",
                "closed authorization set failed exact rehydration",
            )
        steps = await SQLAlchemyRunStepRepository(self._session).list_for_run(run.id)
        actions: list[ExternalAction] = []
        requests: list[StoredActionApprovalRequest] = []
        for member in current.authorization_set.members:
            action = await SQLAlchemyExternalActionRepository(self._session).get(member.action_id)
            request_id = requests_by_action[member.action_id].request.id
            request = await self.get(request_id)
            if action is None or request is None:
                raise ApprovalPersistenceConflict(
                    "authorization_close_corrupt",
                    "closed authorization member disappeared",
                )
            actions.append(action)
            requests.append(request)
        return AuthorizationSetCloseResult(
            authorization_set=current.authorization_set,
            head=current.head,
            run=run,
            steps=steps,
            actions=tuple(actions),
            requests=tuple(requests),
        )

    async def mark_expired(
        self,
        *,
        request_id: str,
        expected_version: int,
        expected_action_version: int,
        expired_at: datetime,
    ) -> StoredActionApprovalRequest:
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected approval version must be a positive integer")
        if type(expected_action_version) is not int or expected_action_version < 1:
            raise ValueError("expected action version must be a positive integer")
        current = await self.get(request_id)
        if current is None:
            raise ApprovalPersistenceConflict(
                "approval_request_missing", "approval expiration request does not exist"
            )
        await self._require_current_leaf(current)
        if current.status is ApprovalStatus.EXPIRED:
            if current.expired_at == expired_at:
                return current
            raise ApprovalPersistenceConflict(
                "approval_expiration_conflict", "approval was already expired differently"
            )
        if (
            current.version != expected_version
            or current.status not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
            or expired_at < current.request.expires_at
        ):
            raise ApprovalPersistenceConflict(
                "approval_expiration_conflict",
                "approval is not the expected renewable request at its expiry",
            )
        target = StoredActionApprovalRequest(
            request=current.request,
            status=ApprovalStatus.EXPIRED,
            version=expected_version + 1,
            updated_at=expired_at,
            decision=current.decision,
            expired_at=expired_at,
        )
        source_action_state = (
            ExternalActionState.APPROVED
            if current.status is ApprovalStatus.APPROVED
            else ExternalActionState.AWAITING_APPROVAL
        )
        try:
            async with self._session.begin_nested():
                action_values: dict[str, object] = {
                    "version": expected_action_version,
                }
                if source_action_state is ExternalActionState.APPROVED:
                    action_values = {
                        "state": ExternalActionState.AWAITING_APPROVAL.value,
                        "version": expected_action_version + 1,
                        "updated_at": expired_at,
                        "terminal_reason_code": None,
                    }
                action_statement = (
                    update(ExternalActionRecord)
                    .where(
                        ExternalActionRecord.id == current.request.action_id,
                        ExternalActionRecord.action_hash == current.request.action_hash,
                        ExternalActionRecord.version == expected_action_version,
                        ExternalActionRecord.state == source_action_state.value,
                    )
                    .values(**action_values)
                    .returning(ExternalActionRecord.id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(action_statement)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
                statement = (
                    update(ApprovalRequestRecord)
                    .where(
                        ApprovalRequestRecord.id == request_id,
                        ApprovalRequestRecord.version == expected_version,
                        ApprovalRequestRecord.status == current.status.value,
                        ApprovalRequestRecord.replacement_request_id.is_(None),
                    )
                    .values(**_request_update_values(target, self._integrity_key))
                    .returning(ApprovalRequestRecord.id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(statement)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                raise ApprovalPersistenceConflict(
                    "approval_expiration_conflict",
                    "another lifecycle writer raced approval expiration",
                ) from None
            raise
        except (IntegrityError, _ApprovalCASLost):
            self._session.expire_all()
            authoritative = await self.get(request_id)
            if authoritative == target:
                return authoritative
            raise ApprovalPersistenceConflict(
                "approval_expiration_conflict",
                "another approval lifecycle mutation won expiration",
            ) from None
        self._session.expire_all()
        updated_request = await self.get(request_id)
        if updated_request is None:
            raise ApprovalPersistenceConflict(
                "approval_request_corrupt", "expired approval disappeared after persistence"
            )
        return updated_request

    async def renew_expired(
        self,
        *,
        expected_version: int,
        expected_action_version: int,
        renewal: ApprovalRenewal,
    ) -> StoredActionApprovalRequest:
        if type(renewal) is not ApprovalRenewal:
            raise ValueError("approval renewal must use the exact immutable contract")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected approval version must be a positive integer")
        if type(expected_action_version) is not int or expected_action_version < 1:
            raise ValueError("expected action version must be a positive integer")
        old = renewal.expired
        replacement = renewal.replacement
        if old.request.id == replacement.id:
            raise ValueError("approval renewal identities must be distinct")
        await self._validate_candidate(replacement)
        current = await self.get(old.request.id)
        if current is None:
            raise ApprovalPersistenceConflict(
                "approval_request_missing", "approval renewal source does not exist"
            )
        await self._require_current_leaf(current)
        expected_expired_at = (
            current.expired_at
            if current.status is ApprovalStatus.EXPIRED
            else replacement.requested_at
        )
        expected_transition = StoredActionApprovalRequest(
            request=current.request,
            status=ApprovalStatus.EXPIRED,
            version=current.version + (1 if current.status is ApprovalStatus.EXPIRED else 2),
            updated_at=replacement.requested_at,
            decision=current.decision,
            expired_at=expected_expired_at,
            replacement_request_id=replacement.id,
            renewed_at=replacement.requested_at,
        )
        if old != expected_transition:
            raise ApprovalPersistenceConflict(
                "approval_renewal_invalid",
                "approval renewal does not exactly follow the current lifecycle",
            )
        if (
            current.version != expected_version
            or current.request != old.request
            or current.status
            not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED, ApprovalStatus.EXPIRED}
            or current.replacement_request_id is not None
        ):
            if (
                current.status is ApprovalStatus.EXPIRED
                and current.replacement_request_id == replacement.id
            ):
                existing = await self.get(replacement.id)
                if existing is not None and existing.request == replacement:
                    return current
            raise ApprovalPersistenceConflict(
                "approval_renewal_conflict",
                "approval request is not the expected renewable generation",
            )
        try:
            async with self._session.begin_nested():
                self._session.add(
                    _request_to_record(
                        StoredActionApprovalRequest.created(replacement),
                        self._integrity_key,
                    )
                )
                await self._session.flush()
                action = await self._session.get(ExternalActionRecord, replacement.action_id)
                if action is None:
                    raise _ApprovalCASLost
                approved_source = current.status is ApprovalStatus.APPROVED
                expected_action_state = (
                    ExternalActionState.APPROVED
                    if approved_source
                    else ExternalActionState.AWAITING_APPROVAL
                )
                if (
                    action.version != expected_action_version
                    or action.state != expected_action_state.value
                ):
                    raise _ApprovalCASLost
                if approved_source:
                    action_statement = (
                        update(ExternalActionRecord)
                        .where(
                            ExternalActionRecord.id == replacement.action_id,
                            ExternalActionRecord.action_hash == replacement.action_hash,
                            ExternalActionRecord.version == expected_action_version,
                            ExternalActionRecord.state == ExternalActionState.APPROVED.value,
                        )
                        .values(
                            state=ExternalActionState.AWAITING_APPROVAL.value,
                            version=expected_action_version + 1,
                            updated_at=replacement.requested_at,
                            terminal_reason_code=None,
                        )
                        .returning(ExternalActionRecord.id)
                        .execution_options(synchronize_session=False)
                    )
                    if (await self._session.execute(action_statement)).scalar_one_or_none() is None:
                        raise _ApprovalCASLost
                else:
                    fence_statement = (
                        update(ExternalActionRecord)
                        .where(
                            ExternalActionRecord.id == replacement.action_id,
                            ExternalActionRecord.action_hash == replacement.action_hash,
                            ExternalActionRecord.version == expected_action_version,
                            ExternalActionRecord.state
                            == ExternalActionState.AWAITING_APPROVAL.value,
                        )
                        .values(version=expected_action_version)
                        .returning(ExternalActionRecord.id)
                        .execution_options(synchronize_session=False)
                    )
                    if (await self._session.execute(fence_statement)).scalar_one_or_none() is None:
                        raise _ApprovalCASLost
                statement = (
                    update(ApprovalRequestRecord)
                    .where(
                        ApprovalRequestRecord.id == old.request.id,
                        ApprovalRequestRecord.version == expected_version,
                        ApprovalRequestRecord.status == current.status.value,
                        ApprovalRequestRecord.replacement_request_id.is_(None),
                    )
                    .values(**_request_update_values(old, self._integrity_key))
                    .returning(ApprovalRequestRecord.id)
                    .execution_options(synchronize_session=False)
                )
                if (await self._session.execute(statement)).scalar_one_or_none() is None:
                    raise _ApprovalCASLost
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                raise ApprovalPersistenceConflict(
                    "approval_renewal_conflict",
                    "another renewal raced the approval request",
                ) from None
            raise
        except (IntegrityError, _ApprovalCASLost):
            self._session.expire_all()
            authoritative = await self.get(old.request.id)
            existing = await self.get(replacement.id)
            if (
                authoritative is not None
                and authoritative.status is ApprovalStatus.EXPIRED
                and authoritative.replacement_request_id == replacement.id
                and existing is not None
                and existing.request == replacement
            ):
                return authoritative
            raise ApprovalPersistenceConflict(
                "approval_renewal_conflict", "another renewal won the approval request"
            ) from None
        self._session.expire_all()
        updated_request = await self.get(old.request.id)
        if updated_request is None:
            raise ApprovalPersistenceConflict(
                "approval_request_corrupt", "renewed approval disappeared after persistence"
            )
        return updated_request
