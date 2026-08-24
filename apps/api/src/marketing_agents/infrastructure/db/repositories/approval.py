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
)
from marketing_agents.domain.action_hash import CanonicalExternalAction, canonical_action_hash
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalDecision,
    ApprovalPolicySnapshot,
    ApprovalRenewal,
    ApprovalUse,
    StoredActionApprovalRequest,
    assert_decision_binds_request,
    assert_request_binds_action,
    expected_approval_projection,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import ExternalAction, RunStep
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    Effect,
    ExternalActionState,
)
from marketing_agents.infrastructure.db.models.action import ExternalActionRecord
from marketing_agents.infrastructure.db.models.approval import (
    ApprovalDecisionRecord,
    ApprovalRequestRecord,
    ApprovalUseRecord,
)
from marketing_agents.infrastructure.db.repositories.action import (
    ExternalActionPersistenceConflict,
    SQLAlchemyExternalActionRepository,
)
from marketing_agents.infrastructure.db.repositories.step import SQLAlchemyRunStepRepository
from marketing_agents.security.approval_digest import (
    approval_decision_record_digest,
    approval_request_record_digest,
    approval_use_record_digest,
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


def _seal_request_record(record: ApprovalRequestRecord, key: DigestKey) -> None:
    record.integrity_digest = approval_request_record_digest(_request_record_material(record), key)


def _seal_decision_record(record: ApprovalDecisionRecord, key: DigestKey) -> None:
    record.integrity_digest = approval_decision_record_digest(
        _decision_record_material(record), key
    )


def _seal_use_record(record: ApprovalUseRecord, key: DigestKey) -> None:
    record.integrity_digest = approval_use_record_digest(_use_record_material(record), key)


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
            action.delivery_contract.timeout_seconds == step.timeout_seconds,
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
                if status in {ApprovalStatus.PENDING, ApprovalStatus.EXPIRED} and (
                    action.state is not ExternalActionState.AWAITING_APPROVAL
                    or action.reservation is not None
                ):
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
            return StoredActionApprovalRequest(
                request=request,
                status=status,
                version=record.version,
                updated_at=record.updated_at,
                decision=decision,
                expired_at=record.expired_at,
                replacement_request_id=record.replacement_request_id,
                renewed_at=record.renewed_at,
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
        record = await self._session.get(ApprovalRequestRecord, request_id)
        if record is None:
            return None
        stored = await self._hydrate(record)
        await self.list_current_set(
            stored.request.run_id,
            stored.request.plan_hash,
            stored.request.proposal_revision,
        )
        return stored

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
        actions = tuple(
            (
                await self._session.execute(
                    select(ExternalActionRecord)
                    .where(
                        ExternalActionRecord.run_id == run_id,
                        ExternalActionRecord.plan_hash == plan_hash,
                        ExternalActionRecord.proposal_revision == proposal_revision,
                    )
                    .order_by(ExternalActionRecord.step_key)
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

    async def add_initial_set_or_get(
        self,
        requests: tuple[ActionApprovalRequest, ...],
    ) -> ApprovalRequestSetInsertResult:
        self._validate_initial_set(requests)
        for request in requests:
            await self._validate_candidate(request)
        first = requests[0]
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
            return ApprovalRequestSetInsertResult(requests=ordered, inserted=False)
        try:
            async with self._session.begin_nested():
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
            return ApprovalRequestSetInsertResult(requests=ordered, inserted=False)
        self._session.expire_all()
        inserted = tuple(
            [cast(StoredActionApprovalRequest, await self.get(r.id)) for r in requests]
        )
        if any(stored is None for stored in inserted):  # pragma: no cover - cast guard
            raise ApprovalPersistenceConflict(
                "approval_request_corrupt", "inserted approval request could not be rehydrated"
            )
        return ApprovalRequestSetInsertResult(requests=inserted, inserted=True)

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
