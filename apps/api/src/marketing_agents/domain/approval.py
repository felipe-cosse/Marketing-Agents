"""Pure exact-action approval proposal and immutable request binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from marketing_agents.domain.action_hash import CanonicalExternalAction, canonical_action_hash
from marketing_agents.domain.entities._validation import require_digest, require_id, require_utc
from marketing_agents.domain.enums import ApprovalStatus
from marketing_agents.security.redaction import redact


class ApprovalBindingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ApprovalPolicySnapshot:
    policy_id: str
    required_roles: frozenset[str]
    required_scopes: frozenset[str]
    expires_after_seconds: int
    allow_self_approval: bool

    def __post_init__(self) -> None:
        require_id(self.policy_id, "approval policy ID")
        if not self.required_roles or not self.required_scopes:
            raise ValueError("approval policy must retain required roles and scopes")
        for value in (*self.required_roles, *self.required_scopes):
            require_id(value, "approval authority")
        if not 60 <= self.expires_after_seconds <= 86_400:
            raise ValueError("approval expiry must be finite from 60 through 86400 seconds")


@dataclass(frozen=True, slots=True)
class ProposedExternalAction:
    envelope: CanonicalExternalAction
    action_hash: str
    redacted_projection: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        envelope: CanonicalExternalAction,
        *,
        redacted_destination: str,
        payload_schema: Mapping[str, Any],
    ) -> ProposedExternalAction:
        if not redacted_destination or redacted_destination != redacted_destination.strip():
            raise ValueError("redacted destination must be a nonempty bounded summary")
        if len(redacted_destination) > 300:
            raise ValueError("redacted destination is too long")
        projection = {
            "action_type": envelope.action_type,
            "capability_id": envelope.capability_id,
            "connector_family": envelope.connector_family,
            "binding_id": envelope.binding_id,
            "destination": redacted_destination,
            "payload": redact(envelope.minimized_payload, schema=payload_schema),
        }
        return cls(
            envelope=envelope,
            action_hash=canonical_action_hash(envelope),
            redacted_projection=_deep_freeze(projection),
        )

    def __post_init__(self) -> None:
        require_digest(self.action_hash, "proposed action hash")
        if self.action_hash != canonical_action_hash(self.envelope):
            raise ApprovalBindingError("proposal_hash_mismatch", "proposal hash is not current")
        object.__setattr__(self, "redacted_projection", _deep_freeze(self.redacted_projection))


@dataclass(frozen=True, slots=True)
class ActionApprovalRequest:
    id: str
    generation: int
    action_id: str
    action_hash: str
    authorization_set_id: str
    run_id: str
    step_id: str
    template_id: str
    instance_id: str
    action_type: str
    capability_id: str
    binding_id: str
    redacted_destination: str
    redacted_projection: Mapping[str, Any]
    policy: ApprovalPolicySnapshot
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "action_id",
            "authorization_set_id",
            "run_id",
            "step_id",
            "template_id",
            "instance_id",
            "capability_id",
            "binding_id",
            "requested_by",
        ):
            require_id(getattr(self, field_name), field_name)
        require_digest(self.action_hash, "approval request action hash")
        require_utc(self.requested_at, "approval request time")
        require_utc(self.expires_at, "approval request expiry")
        if self.generation < 1:
            raise ValueError("approval generation must be positive")
        if self.expires_at <= self.requested_at:
            raise ValueError("approval expiry must follow request time")
        if self.status is not ApprovalStatus.PENDING:
            raise ValueError("a new exact-action request must begin pending")
        object.__setattr__(self, "redacted_projection", _deep_freeze(self.redacted_projection))


def request_approval(
    *,
    request_id: str,
    proposed_action: ProposedExternalAction,
    policy: ApprovalPolicySnapshot,
    requested_by: str,
    requested_at: datetime,
    generation: int = 1,
) -> ActionApprovalRequest:
    envelope = proposed_action.envelope
    require_utc(requested_at, "approval request time")
    return ActionApprovalRequest(
        id=request_id,
        generation=generation,
        action_id=envelope.action_id,
        action_hash=proposed_action.action_hash,
        authorization_set_id=envelope.authorization_set_id,
        run_id=envelope.run_id,
        step_id=envelope.step_id,
        template_id=envelope.template_id,
        instance_id=envelope.instance_id,
        action_type=envelope.action_type,
        capability_id=envelope.capability_id,
        binding_id=envelope.binding_id,
        redacted_destination=str(proposed_action.redacted_projection["destination"]),
        redacted_projection=proposed_action.redacted_projection,
        policy=policy,
        requested_by=requested_by,
        requested_at=requested_at,
        expires_at=requested_at + timedelta(seconds=policy.expires_after_seconds),
    )


def assert_request_binds_action(
    request: ActionApprovalRequest, action: CanonicalExternalAction
) -> None:
    checks = (
        (request.action_id == action.action_id, "approval_action_mismatch"),
        (request.action_hash == canonical_action_hash(action), "approval_hash_mismatch"),
        (
            request.authorization_set_id == action.authorization_set_id,
            "approval_set_mismatch",
        ),
        (request.run_id == action.run_id, "approval_run_mismatch"),
        (request.step_id == action.step_id, "approval_step_mismatch"),
        (request.template_id == action.template_id, "approval_template_mismatch"),
        (request.instance_id == action.instance_id, "approval_instance_mismatch"),
        (request.action_type == action.action_type, "approval_type_mismatch"),
        (request.capability_id == action.capability_id, "approval_capability_mismatch"),
        (request.binding_id == action.binding_id, "approval_binding_mismatch"),
    )
    for valid, code in checks:
        if not valid:
            raise ApprovalBindingError(code, "approval request does not bind this exact action")
