"""Pure exact-action approval proposal and immutable request binding."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    ExternalActionKeyMaterial,
    canonical_action_hash,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import ApprovalDecisionKind, ApprovalStatus
from marketing_agents.domain.validation import (
    require_digest,
    require_id,
    require_json_pointers,
    require_text,
    require_utc,
)


class ApprovalBindingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuthorizationSetStatus(StrEnum):
    """Lifecycle of one complete, immutable write-authorization epoch."""

    OPEN = "open"
    RELEASED = "released"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class AuthorizationSetMember:
    """One ordered action/step identity in a complete authorization set."""

    authorization_set_id: str
    ordinal: int
    run_id: str
    plan_hash: str
    proposal_revision: int
    action_id: str
    action_hash: str
    step_id: str
    step_key: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.authorization_set_id, "authorization set ID"),
            (self.run_id, "authorization set run ID"),
            (self.action_id, "authorization set action ID"),
            (self.step_id, "authorization set step ID"),
            (self.step_key, "authorization set step key"),
        ):
            require_id(value, name)
        require_digest(self.plan_hash, "authorization set plan hash")
        require_digest(self.action_hash, "authorization set action hash")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("authorization set member ordinal must be positive")
        if type(self.proposal_revision) is not int or self.proposal_revision < 1:
            raise ValueError("authorization set proposal revision must be positive")

    def hash_material(self) -> Mapping[str, object]:
        return {
            "ordinal": self.ordinal,
            "run_id": self.run_id,
            "plan_hash": self.plan_hash,
            "proposal_revision": self.proposal_revision,
            "action_id": self.action_id,
            "action_hash": self.action_hash,
            "step_id": self.step_id,
            "step_key": self.step_key,
        }


def authorization_set_membership_hash(
    authorization_set_id: str,
    members: tuple[AuthorizationSetMember, ...],
) -> str:
    """Hash the exact set identity and its stable, ordered membership."""

    require_id(authorization_set_id, "authorization set ID")
    if type(members) is not tuple or not members:
        raise ValueError("authorization set membership must be a nonempty immutable tuple")
    return hashlib.sha256(
        b"marketing-agents:authorization-set-membership:v1\x00"
        + canonical_json_bytes(
            {
                "authorization_set_id": authorization_set_id,
                "members": [member.hash_material() for member in members],
            }
        )
    ).hexdigest()


def authorization_set_release_hash(
    *,
    authorization_set_id: str,
    membership_hash: str,
    released_run_version: int,
    released_at: datetime,
    members: tuple[Mapping[str, object], ...],
) -> str:
    """Hash every durable one-time-use and reservation fact in a barrier release."""

    require_id(authorization_set_id, "authorization set ID")
    require_digest(membership_hash, "authorization set membership hash")
    require_utc(released_at, "authorization set release time")
    if type(released_run_version) is not int or released_run_version < 1:
        raise ValueError("released Run version must be positive")
    if type(members) is not tuple or not members:
        raise ValueError("authorization set release must contain every member")
    return hashlib.sha256(
        b"marketing-agents:authorization-set-release:v1\x00"
        + canonical_json_bytes(
            {
                "authorization_set_id": authorization_set_id,
                "membership_hash": membership_hash,
                "released_run_version": released_run_version,
                "released_at": released_at.isoformat(timespec="microseconds"),
                "members": members,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthorizationSet:
    """Integrity-checked complete set and its one-way barrier lifecycle."""

    id: str
    run_id: str
    plan_hash: str
    proposal_revision: int
    membership_hash: str
    members: tuple[AuthorizationSetMember, ...] = field(repr=False)
    status: AuthorizationSetStatus
    version: int
    opened_at: datetime
    updated_at: datetime
    release_hash: str | None = None
    released_at: datetime | None = None
    released_run_version: int | None = None
    terminal_reason_code: str | None = None
    superseded_by_set_id: str | None = None
    superseded_at: datetime | None = None

    @classmethod
    def open(
        cls,
        *,
        authorization_set_id: str,
        members: tuple[AuthorizationSetMember, ...],
        opened_at: datetime,
    ) -> AuthorizationSet:
        if not members:
            raise ValueError("authorization set cannot be empty")
        first = members[0]
        return cls(
            id=authorization_set_id,
            run_id=first.run_id,
            plan_hash=first.plan_hash,
            proposal_revision=first.proposal_revision,
            membership_hash=authorization_set_membership_hash(
                authorization_set_id,
                members,
            ),
            members=members,
            status=AuthorizationSetStatus.OPEN,
            version=1,
            opened_at=opened_at,
            updated_at=opened_at,
        )

    def __post_init__(self) -> None:
        require_id(self.id, "authorization set ID")
        require_id(self.run_id, "authorization set run ID")
        require_digest(self.plan_hash, "authorization set plan hash")
        require_digest(self.membership_hash, "authorization set membership hash")
        if type(self.proposal_revision) is not int or self.proposal_revision < 1:
            raise ValueError("authorization set proposal revision must be positive")
        if type(self.members) is not tuple or not self.members:
            raise ValueError("authorization set membership must be a nonempty tuple")
        if type(self.status) is not AuthorizationSetStatus:
            raise ValueError("authorization set status must use the exact enum")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("authorization set version must be positive")
        require_utc(self.opened_at, "authorization set open time")
        require_utc(self.updated_at, "authorization set update time")
        if self.updated_at < self.opened_at:
            raise ValueError("authorization set cannot update before it opens")
        expected_ordinals = tuple(range(1, len(self.members) + 1))
        if tuple(member.ordinal for member in self.members) != expected_ordinals:
            raise ValueError("authorization set members must have contiguous stable order")
        action_ids = {member.action_id for member in self.members}
        step_ids = {member.step_id for member in self.members}
        step_keys = {member.step_key for member in self.members}
        if any(len(values) != len(self.members) for values in (action_ids, step_ids, step_keys)):
            raise ValueError("authorization set action and step identities must be unique")
        if any(
            member.authorization_set_id != self.id
            or member.run_id != self.run_id
            or member.plan_hash != self.plan_hash
            or member.proposal_revision != self.proposal_revision
            for member in self.members
        ):
            raise ValueError("authorization set members must retain one exact epoch")
        if self.membership_hash != authorization_set_membership_hash(self.id, self.members):
            raise ApprovalBindingError(
                "authorization_set_hash_mismatch",
                "authorization set membership hash is not current",
            )
        if self.release_hash is not None:
            require_digest(self.release_hash, "authorization set release hash")
        if self.released_at is not None:
            require_utc(self.released_at, "authorization set release time")
        if self.superseded_at is not None:
            require_utc(self.superseded_at, "authorization set supersession time")
        if self.superseded_by_set_id is not None:
            require_id(self.superseded_by_set_id, "replacement authorization set ID")
            if self.superseded_by_set_id == self.id:
                raise ValueError("authorization set cannot supersede itself")
        release_fields = (
            self.release_hash,
            self.released_at,
            self.released_run_version,
        )
        if self.status is AuthorizationSetStatus.OPEN:
            if (
                self.version != 1
                or self.updated_at != self.opened_at
                or any(value is not None for value in release_fields)
                or self.terminal_reason_code is not None
                or self.superseded_by_set_id is not None
                or self.superseded_at is not None
            ):
                raise ValueError("open authorization set must retain pristine state")
            return
        if self.version != 2:
            raise ValueError("authorization set terminal mutation must be version two")
        if self.status is AuthorizationSetStatus.RELEASED:
            if (
                any(value is None for value in release_fields)
                or type(self.released_run_version) is not int
                or self.released_run_version < 1
                or self.released_at != self.updated_at
                or self.terminal_reason_code != "approval_barrier_satisfied"
                or self.superseded_by_set_id is not None
                or self.superseded_at is not None
            ):
                raise ValueError("released authorization set is incomplete")
            return
        if any(value is not None for value in release_fields):
            raise ValueError("unreleased authorization set cannot retain release evidence")
        expected_reason = {
            AuthorizationSetStatus.REJECTED: "approval_rejected",
            AuthorizationSetStatus.CANCELLED: "operator_cancelled",
            AuthorizationSetStatus.SUPERSEDED: "approval_set_superseded",
        }[self.status]
        if self.terminal_reason_code != expected_reason:
            raise ValueError("authorization set terminal reason is inconsistent")
        if self.status is AuthorizationSetStatus.SUPERSEDED:
            if self.superseded_by_set_id is None or self.superseded_at != self.updated_at:
                raise ValueError("superseded authorization set lacks replacement evidence")
        elif self.superseded_by_set_id is not None or self.superseded_at is not None:
            raise ValueError("terminal authorization set cannot retain supersession evidence")


@dataclass(frozen=True, slots=True)
class AuthorizationSetHead:
    """Run-owned pointer selecting the only current authorization-set epoch."""

    run_id: str
    current_set_id: str
    plan_hash: str
    proposal_revision: int
    membership_hash: str
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        require_id(self.run_id, "authorization set head run ID")
        require_id(self.current_set_id, "authorization set head current set ID")
        require_digest(self.plan_hash, "authorization set head plan hash")
        require_digest(self.membership_hash, "authorization set head membership hash")
        if type(self.proposal_revision) is not int or self.proposal_revision < 1:
            raise ValueError("authorization set head proposal revision must be positive")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("authorization set head version must be positive")
        require_utc(self.updated_at, "authorization set head update time")

    def assert_selects(self, authorization_set: AuthorizationSet) -> None:
        if (
            self.current_set_id != authorization_set.id
            or self.run_id != authorization_set.run_id
            or self.plan_hash != authorization_set.plan_hash
            or self.proposal_revision != authorization_set.proposal_revision
            or self.membership_hash != authorization_set.membership_hash
        ):
            raise ApprovalBindingError(
                "authorization_set_head_mismatch",
                "authorization set head does not select the exact set",
            )


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def approval_redaction_schema(fields: tuple[str, ...]) -> Mapping[str, Any]:
    """Build the approval projection schema from a sealed RunStep snapshot."""

    decoded_paths = require_json_pointers(fields, "approval redaction fields")
    root: dict[str, Any] = {"type": "object", "properties": {}}
    for tokens in decoded_paths:
        properties = root["properties"]
        for index, token in enumerate(tokens):
            if index == len(tokens) - 1:
                properties[token] = {"x-sensitive": True}
            else:
                child = properties.setdefault(token, {"type": "object", "properties": {}})
                properties = child["properties"]
    return cast(Mapping[str, Any], _deep_freeze(root))


def safe_approval_destination(binding_id: str) -> str:
    """Return the deterministic non-destination summary exposed to approvers."""

    require_id(binding_id, "approval binding ID")
    return f"configured destination via {binding_id}"


@dataclass(frozen=True, slots=True)
class ApprovalPolicySnapshot:
    policy_id: str
    required_roles: frozenset[str]
    required_scopes: frozenset[str]
    expires_after_seconds: int
    allow_self_approval: bool

    def __post_init__(self) -> None:
        require_id(self.policy_id, "approval policy ID")
        if (
            type(self.required_roles) is not frozenset
            or type(self.required_scopes) is not frozenset
        ):
            raise ValueError("approval authorities must be exact immutable sets")
        if not self.required_roles or not self.required_scopes:
            raise ValueError("approval policy must retain required roles and scopes")
        for value in (*self.required_roles, *self.required_scopes):
            if type(value) is not str:
                raise ValueError("approval authorities must be string identifiers")
            require_id(value, "approval authority")
        if (
            type(self.expires_after_seconds) is not int
            or not 60 <= self.expires_after_seconds <= 86_400
        ):
            raise ValueError("approval expiry must be finite from 60 through 86400 seconds")
        if type(self.allow_self_approval) is not bool:
            raise ValueError("approval self-approval setting must be an exact boolean")


@dataclass(frozen=True, slots=True)
class ProposedExternalAction:
    envelope: CanonicalExternalAction
    action_hash: str
    redacted_projection: Mapping[str, Any] = field(repr=False)

    @classmethod
    def create(
        cls,
        envelope: CanonicalExternalAction,
        *,
        redacted_destination: str,
        payload_schema: Mapping[str, Any],
    ) -> ProposedExternalAction:
        # Import only when constructing the projection. Eagerly importing the
        # security package here makes the framework-independent domain modules
        # depend on package import order through the admission digest exports.
        from marketing_agents.security.redaction import redact

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
        if not isinstance(self.redacted_projection, Mapping):
            raise ApprovalBindingError(
                "proposal_projection_shape", "approval projection must be a mapping"
            )
        projection = _deep_freeze(self.redacted_projection)
        if set(projection) != {
            "action_type",
            "capability_id",
            "connector_family",
            "binding_id",
            "destination",
            "payload",
        }:
            raise ApprovalBindingError(
                "proposal_projection_shape", "approval projection has an invalid shape"
            )
        for key, expected in (
            ("action_type", self.envelope.action_type),
            ("capability_id", self.envelope.capability_id),
            ("connector_family", self.envelope.connector_family),
            ("binding_id", self.envelope.binding_id),
        ):
            if projection[key] != expected:
                raise ApprovalBindingError(
                    "proposal_projection_mismatch",
                    "approval projection does not bind the exact action",
                )
        destination = projection["destination"]
        if type(destination) is not str or not destination or len(destination) > 300:
            raise ApprovalBindingError(
                "proposal_destination_invalid", "approval destination summary is invalid"
            )
        if not isinstance(projection["payload"], Mapping):
            raise ApprovalBindingError(
                "proposal_payload_projection_invalid",
                "approval payload projection must be a mapping",
            )
        object.__setattr__(self, "redacted_projection", projection)

    @property
    def key_material(self) -> ExternalActionKeyMaterial:
        """Expose stable inputs without deriving RUN-05 persistence keys here."""

        return self.envelope.key_material()


@dataclass(frozen=True, slots=True)
class ActionApprovalRequest:
    id: str
    generation: int
    action_id: str
    action_hash: str
    authorization_set_id: str
    run_id: str
    plan_hash: str
    proposal_revision: int
    step_id: str
    step_key: str
    template_id: str
    instance_id: str
    action_type: str
    capability_id: str
    connector_family: str
    binding_id: str
    semantic_action_hash: str
    redacted_destination: str
    redacted_projection: Mapping[str, Any] = field(repr=False)
    policy: ApprovalPolicySnapshot = field(repr=False)
    requested_by: str
    requested_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.policy) is not ApprovalPolicySnapshot:
            raise ValueError("approval request policy must use the exact snapshot type")
        for field_name in (
            "id",
            "action_id",
            "authorization_set_id",
            "run_id",
            "step_id",
            "step_key",
            "template_id",
            "instance_id",
            "action_type",
            "capability_id",
            "connector_family",
            "binding_id",
            "requested_by",
        ):
            require_id(getattr(self, field_name), field_name)
        require_digest(self.action_hash, "approval request action hash")
        require_digest(self.plan_hash, "approval request plan hash")
        require_digest(self.semantic_action_hash, "approval request semantic action hash")
        require_utc(self.requested_at, "approval request time")
        require_utc(self.expires_at, "approval request expiry")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("approval generation must be positive")
        if (
            not isinstance(self.proposal_revision, int)
            or isinstance(self.proposal_revision, bool)
            or self.proposal_revision < 1
        ):
            raise ValueError("proposal revision must be positive")
        if self.expires_at <= self.requested_at:
            raise ValueError("approval expiry must follow request time")
        if self.expires_at != self.requested_at + timedelta(
            seconds=self.policy.expires_after_seconds
        ):
            raise ValueError("approval expiry must exactly match the snapshotted policy TTL")
        if (
            type(self.redacted_destination) is not str
            or not self.redacted_destination
            or self.redacted_destination != self.redacted_destination.strip()
            or len(self.redacted_destination) > 300
        ):
            raise ApprovalBindingError(
                "approval_destination_invalid",
                "approval destination summary must be nonempty, trimmed, and bounded",
            )
        projection = _deep_freeze(self.redacted_projection)
        if not isinstance(projection, Mapping) or set(projection) != {
            "action_type",
            "capability_id",
            "connector_family",
            "binding_id",
            "destination",
            "payload",
        }:
            raise ApprovalBindingError(
                "approval_projection_shape", "approval request projection has an invalid shape"
            )
        if (
            projection.get("action_type") != self.action_type
            or projection.get("capability_id") != self.capability_id
            or projection.get("connector_family") != self.connector_family
            or projection.get("binding_id") != self.binding_id
            or projection.get("destination") != self.redacted_destination
            or not isinstance(projection.get("payload"), Mapping)
        ):
            raise ApprovalBindingError(
                "approval_destination_projection_mismatch",
                "approval request fields must match its safe projection",
            )
        object.__setattr__(self, "redacted_projection", projection)


_DECISION_REASON_CODES = frozenset({"approval_granted", "approval_rejected"})
_MAX_DECISION_REASON_LENGTH = 500


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Append-only decision facts; RUN-10 remains responsible for actor authorization."""

    id: str
    request_id: str
    action_id: str
    action_hash: str
    authorization_set_id: str
    run_id: str
    plan_hash: str
    proposal_revision: int
    step_id: str
    step_key: str
    actor_id: str = field(repr=False)
    authentication_method: str
    correlation_id: str = field(repr=False)
    decision: ApprovalDecisionKind
    authority_roles: frozenset[str] = field(repr=False)
    authority_scopes: frozenset[str] = field(repr=False)
    reason_code: str
    decided_at: datetime
    reason: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "approval decision ID"),
            (self.request_id, "approval request ID"),
            (self.action_id, "approval action ID"),
            (self.authorization_set_id, "approval authorization set ID"),
            (self.run_id, "approval run ID"),
            (self.step_id, "approval step ID"),
            (self.step_key, "approval step key"),
            (self.actor_id, "approval actor ID"),
            (self.authentication_method, "approval authentication method"),
            (self.correlation_id, "approval correlation ID"),
        ):
            require_id(value, name)
        require_digest(self.action_hash, "approval decision action hash")
        require_digest(self.plan_hash, "approval decision plan hash")
        if type(self.proposal_revision) is not int or self.proposal_revision < 1:
            raise ValueError("approval decision proposal revision must be positive")
        if type(self.decision) is not ApprovalDecisionKind:
            raise ValueError("approval decision must use the exact decision enum")
        for values, name in (
            (self.authority_roles, "approval decision roles"),
            (self.authority_scopes, "approval decision scopes"),
        ):
            if type(values) is not frozenset or any(type(value) is not str for value in values):
                raise ValueError(f"{name} must be an exact immutable string set")
            for value in values:
                require_id(value, name)
        expected_reason = (
            "approval_granted"
            if self.decision is ApprovalDecisionKind.APPROVE
            else "approval_rejected"
        )
        if self.reason_code != expected_reason or self.reason_code not in _DECISION_REASON_CODES:
            raise ValueError("approval decision reason must match the decision")
        if self.reason is not None:
            require_text(
                self.reason,
                "approval decision reason",
                maximum=_MAX_DECISION_REASON_LENGTH,
            )
            if any(
                ord(character) < 0x20
                or 0x7F <= ord(character) <= 0x9F
                or 0xD800 <= ord(character) <= 0xDFFF
                for character in self.reason
            ):
                raise ValueError("approval decision reason contains unsupported characters")
        require_utc(self.decided_at, "approval decision time")


@dataclass(frozen=True, slots=True)
class ApprovalUse:
    """Single-use authorization consumption evidence for future ORCH-08 composition."""

    id: str
    request_id: str
    decision_id: str
    action_id: str
    action_hash: str
    authorization_set_id: str
    run_id: str
    plan_hash: str
    proposal_revision: int
    step_id: str
    step_key: str
    reservation_id: str
    used_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "approval use ID"),
            (self.request_id, "approval request ID"),
            (self.decision_id, "approval decision ID"),
            (self.action_id, "approval action ID"),
            (self.authorization_set_id, "approval authorization set ID"),
            (self.run_id, "approval run ID"),
            (self.step_id, "approval step ID"),
            (self.step_key, "approval step key"),
            (self.reservation_id, "approval reservation ID"),
        ):
            require_id(value, name)
        require_digest(self.action_hash, "approval use action hash")
        require_digest(self.plan_hash, "approval use plan hash")
        if type(self.proposal_revision) is not int or self.proposal_revision < 1:
            raise ValueError("approval use proposal revision must be positive")
        require_utc(self.used_at, "approval use time")


@dataclass(frozen=True, slots=True)
class StoredActionApprovalRequest:
    """Canonical durable lifecycle around the immutable request leaf."""

    request: ActionApprovalRequest = field(repr=False)
    status: ApprovalStatus
    version: int
    updated_at: datetime
    decision: ApprovalDecision | None = field(default=None, repr=False)
    expired_at: datetime | None = None
    replacement_request_id: str | None = None
    renewed_at: datetime | None = None
    superseded_at: datetime | None = None
    superseded_reason_code: str | None = None
    use: ApprovalUse | None = field(default=None, repr=False)

    @classmethod
    def created(cls, request: ActionApprovalRequest) -> StoredActionApprovalRequest:
        if type(request) is not ActionApprovalRequest:
            raise ValueError("approval request must use the exact immutable contract")
        return cls(
            request=request,
            status=ApprovalStatus.PENDING,
            version=1,
            updated_at=request.requested_at,
        )

    def __post_init__(self) -> None:
        if type(self.request) is not ActionApprovalRequest:
            raise ValueError("stored approval request must use the exact request contract")
        if type(self.status) is not ApprovalStatus:
            raise ValueError("stored approval status must use the exact status enum")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("stored approval version must be positive")
        require_utc(self.updated_at, "stored approval update time")
        if self.updated_at < self.request.requested_at:
            raise ValueError("stored approval cannot update before it was requested")
        if self.decision is not None:
            if type(self.decision) is not ApprovalDecision:
                raise ValueError("stored approval decision must use the exact contract")
            assert_decision_binds_request(self.decision, self.request)
        if self.expired_at is not None:
            require_utc(self.expired_at, "approval expiration time")
        if self.replacement_request_id is not None:
            require_id(self.replacement_request_id, "replacement approval request ID")
            if self.replacement_request_id == self.request.id:
                raise ValueError("an approval request cannot replace itself")
        if self.renewed_at is not None:
            require_utc(self.renewed_at, "approval renewal time")
        if (self.replacement_request_id is None) != (self.renewed_at is None):
            raise ValueError("approval renewal request and time must be present together")
        if (self.superseded_at is None) != (self.superseded_reason_code is None):
            raise ValueError("approval supersession time and reason must be present together")
        if self.superseded_at is not None:
            require_utc(self.superseded_at, "approval supersession time")
            if self.superseded_at < self.request.requested_at:
                raise ValueError("approval cannot be superseded before it was requested")
        if self.superseded_reason_code is not None and self.superseded_reason_code not in {
            "approval_set_rejected",
            "approval_set_superseded",
            "run_cancelled",
        }:
            raise ValueError("approval supersession reason is not supported")
        if self.use is not None:
            if type(self.use) is not ApprovalUse:
                raise ValueError("approval use must use the exact immutable contract")
            assert_use_binds_request(self.use, self)
        if self.status is ApprovalStatus.PENDING:
            if (
                self.version != 1
                or self.updated_at != self.request.requested_at
                or any(
                    value is not None
                    for value in (
                        self.decision,
                        self.expired_at,
                        self.replacement_request_id,
                        self.renewed_at,
                        self.superseded_at,
                        self.superseded_reason_code,
                        self.use,
                    )
                )
            ):
                raise ValueError("pending approval must retain pristine creation state")
        elif self.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            expected = (
                ApprovalDecisionKind.APPROVE
                if self.status is ApprovalStatus.APPROVED
                else ApprovalDecisionKind.REJECT
            )
            if (
                self.decision is None
                or self.decision.decision is not expected
                or self.expired_at is not None
                or self.replacement_request_id is not None
                or self.renewed_at is not None
                or self.use is not None
                or self.superseded_at is not None
                or self.superseded_reason_code is not None
                or self.version != 2
                or self.updated_at != self.decision.decided_at
            ):
                raise ValueError("decided approval state is incomplete or contradictory")
        elif self.status is ApprovalStatus.EXPIRED:
            base_version = 3 if self.decision is not None else 2
            if (
                self.expired_at is None
                or self.expired_at < self.request.expires_at
                or self.use is not None
                or self.superseded_at is not None
                or self.superseded_reason_code is not None
                or (
                    self.decision is not None
                    and self.decision.decision is not ApprovalDecisionKind.APPROVE
                )
                or (
                    self.replacement_request_id is None
                    and (self.version != base_version or self.updated_at != self.expired_at)
                )
                or (
                    self.replacement_request_id is not None
                    and (
                        self.renewed_at is None
                        or self.renewed_at < self.expired_at
                        or self.version != base_version + 1
                        or self.updated_at != self.renewed_at
                    )
                )
            ):
                raise ValueError("expired approval state is incomplete or contradictory")
        elif self.status is ApprovalStatus.CONSUMED:
            if (
                self.decision is None
                or self.decision.decision is not ApprovalDecisionKind.APPROVE
                or self.use is None
                or self.expired_at is not None
                or self.replacement_request_id is not None
                or self.renewed_at is not None
                or self.superseded_at is not None
                or self.superseded_reason_code is not None
                or self.use.used_at >= self.request.expires_at
                or self.version != 3
                or self.updated_at != self.use.used_at
            ):
                raise ValueError("consumed approval must bind one unexpired approval use")
        elif self.status is ApprovalStatus.SUPERSEDED:
            base_version = 2 if self.decision is None else 3
            if (
                self.superseded_at is None
                or self.updated_at != self.superseded_at
                or self.version != base_version
                or self.expired_at is not None
                or self.replacement_request_id is not None
                or self.renewed_at is not None
                or self.use is not None
                or (
                    self.decision is not None
                    and self.decision.decision is not ApprovalDecisionKind.APPROVE
                )
            ):
                raise ValueError("superseded approval state is incomplete or contradictory")
        else:  # pragma: no cover - exact enum exhaustiveness
            raise AssertionError("unhandled approval status")


@dataclass(frozen=True, slots=True)
class ApprovalRenewal:
    """One expiry-only request-leaf replacement within the same set epoch."""

    expired: StoredActionApprovalRequest
    replacement: ActionApprovalRequest

    def __post_init__(self) -> None:
        if type(self.expired) is not StoredActionApprovalRequest:
            raise ValueError("approval renewal must retain the exact expired lifecycle type")
        if type(self.replacement) is not ActionApprovalRequest:
            raise ValueError("approval renewal must retain the exact replacement request type")
        old = self.expired.request
        new = self.replacement
        if (
            self.expired.status is not ApprovalStatus.EXPIRED
            or self.expired.replacement_request_id != new.id
            or self.expired.renewed_at != new.requested_at
            or new.generation != old.generation + 1
            or new.action_id != old.action_id
            or new.action_hash != old.action_hash
            or new.authorization_set_id != old.authorization_set_id
            or new.run_id != old.run_id
            or new.plan_hash != old.plan_hash
            or new.proposal_revision != old.proposal_revision
            or new.step_id != old.step_id
            or new.step_key != old.step_key
            or new.template_id != old.template_id
            or new.instance_id != old.instance_id
            or new.action_type != old.action_type
            or new.capability_id != old.capability_id
            or new.connector_family != old.connector_family
            or new.binding_id != old.binding_id
            or new.semantic_action_hash != old.semantic_action_hash
            or new.redacted_destination != old.redacted_destination
            or new.redacted_projection != old.redacted_projection
            or new.policy != old.policy
        ):
            raise ValueError("approval renewal must retain one exact action leaf and set epoch")


def expected_approval_projection(
    action: CanonicalExternalAction,
    request_redaction_fields: tuple[str, ...],
) -> Mapping[str, Any]:
    """Recompute the only projection allowed for a persisted planned action."""

    return ProposedExternalAction.create(
        action,
        redacted_destination=safe_approval_destination(action.binding_id),
        payload_schema=approval_redaction_schema(request_redaction_fields),
    ).redacted_projection


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
        plan_hash=envelope.plan_hash,
        proposal_revision=envelope.proposal_revision,
        step_id=envelope.step_id,
        step_key=envelope.step_key,
        template_id=envelope.template_id,
        instance_id=envelope.instance_id,
        action_type=envelope.action_type,
        capability_id=envelope.capability_id,
        connector_family=envelope.connector_family,
        binding_id=envelope.binding_id,
        semantic_action_hash=envelope.semantic_action_hash,
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
        (request.plan_hash == action.plan_hash, "approval_plan_mismatch"),
        (
            request.proposal_revision == action.proposal_revision,
            "approval_revision_mismatch",
        ),
        (request.step_id == action.step_id, "approval_step_mismatch"),
        (request.step_key == action.step_key, "approval_step_key_mismatch"),
        (request.template_id == action.template_id, "approval_template_mismatch"),
        (request.instance_id == action.instance_id, "approval_instance_mismatch"),
        (request.action_type == action.action_type, "approval_type_mismatch"),
        (request.capability_id == action.capability_id, "approval_capability_mismatch"),
        (
            request.connector_family == action.connector_family,
            "approval_connector_family_mismatch",
        ),
        (request.binding_id == action.binding_id, "approval_binding_mismatch"),
        (
            request.semantic_action_hash == action.semantic_action_hash,
            "approval_semantic_hash_mismatch",
        ),
    )
    for valid, code in checks:
        if not valid:
            raise ApprovalBindingError(code, "approval request does not bind this exact action")


def assert_decision_binds_request(
    decision: ApprovalDecision,
    request: ActionApprovalRequest,
) -> None:
    checks = (
        (decision.request_id == request.id, "approval_decision_request_mismatch"),
        (decision.action_id == request.action_id, "approval_decision_action_mismatch"),
        (decision.action_hash == request.action_hash, "approval_decision_hash_mismatch"),
        (
            decision.authorization_set_id == request.authorization_set_id,
            "approval_decision_set_mismatch",
        ),
        (decision.run_id == request.run_id, "approval_decision_run_mismatch"),
        (decision.plan_hash == request.plan_hash, "approval_decision_plan_mismatch"),
        (
            decision.proposal_revision == request.proposal_revision,
            "approval_decision_revision_mismatch",
        ),
        (decision.step_id == request.step_id, "approval_decision_step_mismatch"),
        (decision.step_key == request.step_key, "approval_decision_step_key_mismatch"),
        (
            request.requested_at <= decision.decided_at < request.expires_at,
            "approval_decision_time_invalid",
        ),
    )
    for valid, code in checks:
        if not valid:
            raise ApprovalBindingError(code, "approval decision does not bind this request")
    if not request.policy.allow_self_approval and decision.actor_id == request.requested_by:
        raise ApprovalBindingError(
            "approval_self_decision_forbidden",
            "approval policy forbids the requester from deciding the request",
        )
    if not request.policy.required_roles.issubset(
        decision.authority_roles
    ) or not request.policy.required_scopes.issubset(decision.authority_scopes):
        raise ApprovalBindingError(
            "approval_decision_authority_snapshot_mismatch",
            "approval decision does not retain the required authority snapshot",
        )


def assert_use_binds_request(
    use: ApprovalUse,
    stored: StoredActionApprovalRequest,
) -> None:
    request = stored.request
    decision = stored.decision
    checks = (
        (decision is not None, "approval_use_decision_missing"),
        (
            decision is not None and decision.decision is ApprovalDecisionKind.APPROVE,
            "approval_use_not_approved",
        ),
        (
            decision is not None and use.decision_id == decision.id,
            "approval_use_decision_mismatch",
        ),
        (use.request_id == request.id, "approval_use_request_mismatch"),
        (use.action_id == request.action_id, "approval_use_action_mismatch"),
        (use.action_hash == request.action_hash, "approval_use_hash_mismatch"),
        (
            use.authorization_set_id == request.authorization_set_id,
            "approval_use_set_mismatch",
        ),
        (use.run_id == request.run_id, "approval_use_run_mismatch"),
        (use.plan_hash == request.plan_hash, "approval_use_plan_mismatch"),
        (
            use.proposal_revision == request.proposal_revision,
            "approval_use_revision_mismatch",
        ),
        (use.step_id == request.step_id, "approval_use_step_mismatch"),
        (use.step_key == request.step_key, "approval_use_step_key_mismatch"),
        (
            decision is not None and decision.decided_at <= use.used_at < request.expires_at,
            "approval_use_time_invalid",
        ),
    )
    for valid, code in checks:
        if not valid:
            raise ApprovalBindingError(code, "approval use does not bind this approved request")
