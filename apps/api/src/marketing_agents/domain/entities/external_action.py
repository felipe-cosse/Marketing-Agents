"""Persisted external-action aggregate and durable delivery snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from marketing_agents.domain.action_hash import CanonicalExternalAction
from marketing_agents.domain.action_idempotency import (
    derive_external_action_idempotency_key,
)
from marketing_agents.domain.approval import ApprovalPolicySnapshot, ProposedExternalAction
from marketing_agents.domain.enums import ExternalActionState
from marketing_agents.domain.validation import (
    frozen_json_mapping,
    require_digest,
    require_id,
    require_utc,
)

MAX_DELIVERY_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class ActionReservationSnapshot:
    """Immutable SAFE-02 inputs produced by the future approval barrier."""

    reservation_id: str
    authorization_set_id: str
    approval_request_id: str
    approval_decision_id: str
    action_hash: str
    capability_id: str
    binding_id: str
    idempotency_key: str
    reserved_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.reservation_id, "reservation ID"),
            (self.authorization_set_id, "authorization set ID"),
            (self.approval_request_id, "approval request ID"),
            (self.approval_decision_id, "approval decision ID"),
            (self.capability_id, "reservation capability ID"),
            (self.binding_id, "reservation binding ID"),
            (self.idempotency_key, "reservation idempotency key"),
        ):
            require_id(value, name)
        require_digest(self.action_hash, "reservation action hash")
        require_utc(self.reserved_at, "reservation time")
        if self.approval_request_id == self.approval_decision_id:
            raise ValueError("approval request and decision IDs must be distinct")


@dataclass(frozen=True, slots=True)
class DispatchLease:
    owner: str
    attempt_number: int
    claimed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        require_id(self.owner, "dispatch lease owner")
        if (
            not isinstance(self.attempt_number, int)
            or isinstance(self.attempt_number, bool)
            or self.attempt_number < 1
        ):
            raise ValueError("dispatch attempt number must be positive")
        require_utc(self.claimed_at, "dispatch claim time")
        require_utc(self.expires_at, "dispatch lease expiry")
        if self.expires_at <= self.claimed_at:
            raise ValueError("dispatch lease expiry must follow claim time")


@dataclass(frozen=True, slots=True)
class ExternalActionResultSnapshot:
    receipt_id: str
    status: str
    safe_metadata: Mapping[str, Any] = field(repr=False)
    completed_at: datetime

    def __post_init__(self) -> None:
        require_id(self.receipt_id, "connector receipt ID")
        require_id(self.status, "connector result status")
        require_utc(self.completed_at, "connector completion time")
        object.__setattr__(
            self,
            "safe_metadata",
            frozen_json_mapping(self.safe_metadata, "connector result safe metadata"),
        )


@dataclass(frozen=True, slots=True)
class ConnectorActionReceipt:
    """One durable deterministic connector effect receipt."""

    external_action_id: str
    connector_binding_id: str
    idempotency_key: str
    action_hash: str
    capability_id: str
    receipt_id: str
    status: str
    safe_metadata: Mapping[str, Any] = field(repr=False)
    created_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.external_action_id, "external action ID"),
            (self.connector_binding_id, "connector binding ID"),
            (self.idempotency_key, "connector receipt idempotency key"),
            (self.capability_id, "connector receipt capability ID"),
            (self.receipt_id, "connector receipt ID"),
            (self.status, "connector receipt status"),
        ):
            require_id(value, name)
        require_digest(self.action_hash, "connector receipt action hash")
        require_utc(self.created_at, "connector receipt creation time")
        object.__setattr__(
            self,
            "safe_metadata",
            frozen_json_mapping(self.safe_metadata, "connector receipt safe metadata"),
        )


@dataclass(frozen=True, slots=True)
class DeliveryContractSnapshot:
    """Trusted plan-time connector contract retained across registry drift."""

    capability_id: str
    connector_family: str
    binding_id: str
    binding_configuration_revision: int
    request_schema_id: str
    idempotency_support: Literal["required", "supported", "unavailable"]
    timeout_seconds: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.capability_id, "delivery capability ID"),
            (self.connector_family, "delivery connector family"),
            (self.binding_id, "delivery binding ID"),
            (self.request_schema_id, "delivery request schema ID"),
        ):
            require_id(value, name)
        if (
            not isinstance(self.binding_configuration_revision, int)
            or isinstance(self.binding_configuration_revision, bool)
            or self.binding_configuration_revision < 1
        ):
            raise ValueError("delivery binding configuration revision must be positive")
        if self.idempotency_support not in {"required", "supported", "unavailable"}:
            raise ValueError("unsupported delivery idempotency classification")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 120
        ):
            raise ValueError("delivery timeout must be from 1 through 120 seconds")


@dataclass(frozen=True, slots=True)
class ExternalAction:
    """Immutable exact proposal plus optimistic persisted delivery state."""

    proposal: ProposedExternalAction = field(repr=False)
    approval_policy: ApprovalPolicySnapshot = field(repr=False)
    delivery_contract: DeliveryContractSnapshot = field(repr=False)
    idempotency_key: str
    state: ExternalActionState
    created_at: datetime
    updated_at: datetime
    version: int = 1
    delivery_attempt_count: int = 0
    delivery_attempt_limit: int = 2
    reservation: ActionReservationSnapshot | None = field(default=None, repr=False)
    lease: DispatchLease | None = field(default=None, repr=False)
    call_started_at: datetime | None = None
    call_deadline_at: datetime | None = None
    result: ExternalActionResultSnapshot | None = field(default=None, repr=False)
    terminal_reason_code: str | None = None
    superseded_by_action_id: str | None = None
    superseded_at: datetime | None = None

    @classmethod
    def proposed(
        cls,
        proposal: ProposedExternalAction,
        approval_policy: ApprovalPolicySnapshot,
        delivery_contract: DeliveryContractSnapshot,
        created_at: datetime,
        *,
        delivery_attempt_limit: int = 2,
    ) -> ExternalAction:
        return cls(
            proposal=proposal,
            approval_policy=approval_policy,
            delivery_contract=delivery_contract,
            idempotency_key=derive_external_action_idempotency_key(proposal.key_material),
            state=ExternalActionState.PROPOSED,
            created_at=created_at,
            updated_at=created_at,
            delivery_attempt_limit=delivery_attempt_limit,
        )

    def __post_init__(self) -> None:
        require_id(self.idempotency_key, "external action idempotency key")
        if self.idempotency_key != derive_external_action_idempotency_key(
            self.proposal.key_material
        ):
            raise ValueError("external action idempotency key is not current")
        require_utc(self.created_at, "external action creation time")
        require_utc(self.updated_at, "external action update time")
        if self.updated_at < self.created_at:
            raise ValueError("external action update time cannot precede creation")
        for value, name in (
            (self.version, "external action version"),
            (self.delivery_attempt_limit, "delivery attempt limit"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be positive")
        if self.delivery_attempt_limit > MAX_DELIVERY_ATTEMPTS:
            raise ValueError(f"delivery attempt limit cannot exceed {MAX_DELIVERY_ATTEMPTS}")
        if (
            not isinstance(self.delivery_attempt_count, int)
            or isinstance(self.delivery_attempt_count, bool)
            or not 0 <= self.delivery_attempt_count <= self.delivery_attempt_limit
        ):
            raise ValueError("delivery attempt count is outside its persisted limit")
        envelope = self.proposal.envelope
        contract = self.delivery_contract
        if (
            contract.capability_id != envelope.capability_id
            or contract.connector_family != envelope.connector_family
            or contract.binding_id != envelope.binding_id
            or contract.request_schema_id != envelope.payload_schema_id
        ):
            raise ValueError("delivery contract does not bind the exact external action")
        if self.reservation is not None and (
            self.reservation.authorization_set_id != envelope.authorization_set_id
            or self.reservation.action_hash != self.proposal.action_hash
            or self.reservation.capability_id != envelope.capability_id
            or self.reservation.binding_id != envelope.binding_id
            or self.reservation.idempotency_key != self.idempotency_key
        ):
            raise ValueError("dispatch reservation does not bind the exact external action")
        if (self.call_started_at is None) != (self.call_deadline_at is None):
            raise ValueError("connector call start and deadline must be retained together")
        if self.call_started_at is not None and self.call_deadline_at is not None:
            require_utc(self.call_started_at, "connector call start time")
            require_utc(self.call_deadline_at, "connector call deadline")
            if not (
                self.call_started_at
                < self.call_deadline_at
                <= self.call_started_at + timedelta(seconds=self.delivery_contract.timeout_seconds)
            ):
                raise ValueError("connector call deadline exceeds its exact delivery authority")
        if self.state is ExternalActionState.DISPATCHING:
            if self.reservation is None or self.lease is None:
                raise ValueError("dispatching action requires reservation and lease")
            if self.delivery_attempt_count != self.lease.attempt_number:
                raise ValueError("dispatch lease attempt is not current")
            if self.call_started_at is not None and self.call_started_at < self.lease.claimed_at:
                raise ValueError("connector call cannot start before dispatch claim")
        elif (
            self.lease is not None
            or self.call_started_at is not None
            or self.call_deadline_at is not None
        ):
            raise ValueError("only a dispatching action may retain current call authority")
        post_approval_states = {
            ExternalActionState.DISPATCH_RESERVED,
            ExternalActionState.DISPATCHING,
            ExternalActionState.SUCCEEDED,
            ExternalActionState.FAILED,
            ExternalActionState.OUTCOME_UNKNOWN,
        }
        if self.state in post_approval_states and self.reservation is None:
            raise ValueError("post-approval action state requires a reservation")
        if (
            self.state
            in {
                ExternalActionState.PROPOSED,
                ExternalActionState.AWAITING_APPROVAL,
                ExternalActionState.APPROVED,
            }
            and self.reservation is not None
        ):
            raise ValueError("pre-dispatch action state cannot retain a reservation")
        if (
            self.state
            in {
                ExternalActionState.DISPATCHING,
                ExternalActionState.SUCCEEDED,
                ExternalActionState.FAILED,
                ExternalActionState.OUTCOME_UNKNOWN,
            }
            and self.delivery_attempt_count < 1
        ):
            raise ValueError("attempted external action state requires a delivery attempt")
        if self.state is ExternalActionState.SUCCEEDED:
            if self.result is None or self.terminal_reason_code is not None:
                raise ValueError("succeeded action requires only a connector result")
        elif self.result is not None:
            raise ValueError("only a succeeded action may retain a connector result")
        reason_required = self.state in {
            ExternalActionState.FAILED,
            ExternalActionState.REJECTED,
            ExternalActionState.CANCELLED,
            ExternalActionState.OUTCOME_UNKNOWN,
            ExternalActionState.SUPERSEDED,
        }
        if reason_required != (self.terminal_reason_code is not None):
            raise ValueError("terminal external action reason does not match its state")
        if self.terminal_reason_code is not None:
            require_id(self.terminal_reason_code, "external action terminal reason")
        is_superseded = self.state is ExternalActionState.SUPERSEDED
        if is_superseded != (self.superseded_by_action_id is not None):
            raise ValueError("superseded action must identify its replacement")
        if is_superseded != (self.superseded_at is not None):
            raise ValueError("superseded action must retain its replacement time")
        if self.superseded_by_action_id is not None:
            require_id(self.superseded_by_action_id, "replacement external action ID")
            if self.superseded_by_action_id == self.id:
                raise ValueError("an external action cannot supersede itself")
        if self.superseded_at is not None:
            require_utc(self.superseded_at, "external action supersession time")
            if self.superseded_at < self.created_at:
                raise ValueError("external action supersession cannot precede creation")

    @property
    def envelope(self) -> CanonicalExternalAction:
        return self.proposal.envelope

    @property
    def id(self) -> str:
        return self.envelope.action_id

    @property
    def run_id(self) -> str:
        return self.envelope.run_id

    @property
    def step_id(self) -> str:
        return self.envelope.step_id

    @property
    def connector_binding_id(self) -> str:
        return self.envelope.binding_id

    @property
    def action_hash(self) -> str:
        return self.proposal.action_hash
