"""Framework-independent, integrity-sealed audit timeline contracts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.validation import (
    frozen_json_mapping,
    require_id,
    require_text,
    require_utc,
)

_AUDIT_METADATA_SEAL = object()
_AUDIT_EVENT_SEAL = object()
_AUDIT_CONTEXT_SEAL = object()
_METADATA_FINGERPRINT_DOMAIN = b"marketing-agents:audit-metadata:v1\x00"
_EVENT_FINGERPRINT_DOMAIN = b"marketing-agents:audit-event-draft:v1\x00"
_CONTEXT_FINGERPRINT_DOMAIN = b"marketing-agents:audit-context:v1\x00"
_ACTOR_PSEUDONYM_DOMAIN = b"marketing-agents:audit-actor:v1\x00"
_CORRELATION_PSEUDONYM_DOMAIN = b"marketing-agents:audit-correlation:v1\x00"
_EVENT_AGGREGATES = {
    "run.received": "run",
    "run.transitioned": "run",
    "run.transition_rejected": "run_attempt",
    "run.plan_recorded": "run",
    "step.recorded": "step",
    "step.transitioned": "step",
    "action.proposed": "external_action",
    "action.awaiting_approval": "external_action",
    "action.dispatch_claimed": "external_action",
    "action.call_started": "external_action",
    "action.retry_released": "external_action",
    "action.succeeded": "external_action",
    "action.failed": "external_action",
    "action.outcome_unknown": "external_action",
    "action.receipt_reconciled": "external_action",
    "connector.receipt_committed": "connector_receipt",
    "approval.requested": "approval_request",
    "approval.expired": "approval_request",
    "approval.renewed": "approval_request",
}
_EVENT_OUTCOMES = {
    **{event_type: "accepted" for event_type in _EVENT_AGGREGATES},
    "run.transition_rejected": "rejected",
    "connector.receipt_committed": "observed",
}
_EVENT_REQUIRED_METADATA: Mapping[str, frozenset[str]] = {
    "run.received": frozenset({"command", "catalog_content_hash"}),
    "run.transitioned": frozenset({"command"}),
    "run.transition_rejected": frozenset({"command"}),
    "run.plan_recorded": frozenset(
        {
            "command",
            "catalog_content_hash",
            "graph_hash",
            "plan_hash",
            "routing_hash",
            "step_count",
            "workflow_definition_hash",
            "workflow_id",
            "workflow_version",
        }
    ),
    "step.recorded": frozenset(
        {
            "catalog_content_hash",
            "configuration_revision",
            "graph_hash",
            "ordinal",
            "plan_hash",
            "routing_hash",
            "step_count",
            "step_kind",
            "template_id",
            "terminal_result",
            "workflow_definition_hash",
            "workflow_id",
            "workflow_version",
        }
    ),
    "step.transitioned": frozenset(
        {
            "command",
            "configuration_revision",
            "ordinal",
            "step_kind",
            "template_id",
            "terminal_result",
        }
    ),
    "action.proposed": frozenset({"idempotency_support"}),
    "action.awaiting_approval": frozenset({"idempotency_support"}),
    "action.dispatch_claimed": frozenset({"idempotency_support"}),
    "action.call_started": frozenset({"idempotency_support"}),
    "action.retry_released": frozenset({"conclusion", "idempotency_support"}),
    "action.succeeded": frozenset({"conclusion", "connector_status", "idempotency_support"}),
    "action.failed": frozenset({"conclusion", "idempotency_support"}),
    "action.outcome_unknown": frozenset({"conclusion", "idempotency_support"}),
    "action.receipt_reconciled": frozenset(
        {"conclusion", "connector_status", "idempotency_support"}
    ),
    "connector.receipt_committed": frozenset({"connector_status"}),
    "approval.requested": frozenset(
        {
            "action_state",
            "action_version",
            "generation",
            "policy_id",
            "proposal_revision",
            "status",
        }
    ),
    "approval.expired": frozenset(
        {
            "action_state",
            "action_version",
            "generation",
            "policy_id",
            "proposal_revision",
            "status",
        }
    ),
    "approval.renewed": frozenset(
        {
            "action_state",
            "action_version",
            "generation",
            "policy_id",
            "proposal_revision",
            "replacement_request_id",
            "status",
        }
    ),
}
_RUN_COMMANDS = frozenset(
    {
        "activate_plan",
        "cancel",
        "complete",
        "fail",
        "mark_validated",
        "receive",
        "record_plan",
        "reject_approval",
        "release_approved_plan",
    }
)
_RUN_STATES = frozenset(
    {
        "received",
        "validated",
        "planned",
        "awaiting_approval",
        "executing",
        "completed",
        "failed",
        "rejected",
        "cancelled",
    }
)
_SAFE_AUDIT_REASONS = frozenset(
    {
        "approval_barrier_incomplete",
        "approval_barrier_satisfied",
        "approval_expired",
        "approval_renewed",
        "approval_rejected",
        "approval_requested",
        "approval_rejection_mismatch",
        "connector_delivery_uncertain",
        "connector_request_rejected",
        "connector_timeout",
        "execution_completed",
        "execution_incomplete",
        "failure_phase_mismatch",
        "input_validated",
        "invalid_cancellation_effects",
        "invalid_transition",
        "non_monotonic_time",
        "operator_cancelled",
        "plan_recorded",
        "plan_step_recorded",
        "pre_call_attempts_exhausted",
        "read_only_plan_released",
        "stale_delivery_outcome_unknown",
        "stale_run_version",
        "step_approval_required",
        "step_dependencies_satisfied",
        "step_execution_started",
        "step_succeeded",
        "terminal_state_immutable",
        "unclassified_failure",
        "work_admitted",
        "write_plan_requires_approval",
    }
)


def normalize_audit_reason_code(value: str | None) -> str | None:
    """Map arbitrary domain/provider reason strings to a bounded safe skeleton code."""

    if value is None or value in _SAFE_AUDIT_REASONS:
        return value
    return "unclassified_failure"


_SENSITIVE_SKELETON = re.compile(
    r"(?:@|bearer|password|secret|signature|token|api[-_.]?key)", re.IGNORECASE
)


class AuditActorSource(StrEnum):
    """Stable provenance class for a timeline actor."""

    SYSTEM = "system"
    USER = "user"
    WORKER = "worker"
    CONNECTOR = "connector"


class AuditOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    OBSERVED = "observed"


def _require_safe_skeleton(value: str, field_name: str) -> None:
    require_id(value, field_name)
    if _SENSITIVE_SKELETON.search(value) is not None:
        raise ValueError(f"{field_name} contains forbidden sensitive material")


@dataclass(frozen=True, slots=True, init=False)
class AuditContext:
    """Authenticated or internal actor context carried across one mutation boundary."""

    actor_id: str
    actor_source: AuditActorSource
    auth_method: str
    correlation_id: str
    issuance_fingerprint: str = field(repr=False)

    def __init__(
        self,
        actor_id: str,
        actor_source: AuditActorSource,
        auth_method: str,
        correlation_id: str,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _AUDIT_CONTEXT_SEAL:
            raise ValueError("audit context must be issued by a trusted server factory")
        pseudonymous_actor = _pseudonym(_ACTOR_PSEUDONYM_DOMAIN, actor_id, "audit-actor-v1")
        pseudonymous_correlation = _pseudonym(
            _CORRELATION_PSEUDONYM_DOMAIN,
            correlation_id,
            "audit-correlation-v1",
        )
        object.__setattr__(self, "actor_id", pseudonymous_actor)
        object.__setattr__(self, "actor_source", actor_source)
        object.__setattr__(self, "auth_method", auth_method)
        object.__setattr__(self, "correlation_id", pseudonymous_correlation)
        self.__post_init__()
        object.__setattr__(self, "issuance_fingerprint", _context_fingerprint(self))

    def __post_init__(self) -> None:
        if type(self.actor_source) is not AuditActorSource:
            raise ValueError("audit actor source is invalid")
        _require_pseudonym(self.actor_id, "audit-actor-v1", "audit actor ID")
        _require_pseudonym(
            self.correlation_id,
            "audit-correlation-v1",
            "audit correlation ID",
        )
        expected_auth = {
            AuditActorSource.SYSTEM: "internal",
            AuditActorSource.WORKER: "internal",
            AuditActorSource.USER: "local_session",
            AuditActorSource.CONNECTOR: "connector_registry",
        }[self.actor_source]
        if self.auth_method != expected_auth:
            raise ValueError("audit actor source and authentication method disagree")

    def verify_integrity(self) -> None:
        self.__post_init__()
        if self.issuance_fingerprint != _context_fingerprint(self):
            raise ValueError("audit context changed after trusted issuance")

    @classmethod
    def system(cls, actor_id: str, *, correlation_id: str) -> AuditContext:
        return cls(
            actor_id=actor_id,
            actor_source=AuditActorSource.SYSTEM,
            auth_method="internal",
            correlation_id=correlation_id,
            _seal=_AUDIT_CONTEXT_SEAL,
        )

    @classmethod
    def worker(cls, actor_id: str, *, correlation_id: str) -> AuditContext:
        return cls(
            actor_id,
            AuditActorSource.WORKER,
            "internal",
            correlation_id,
            _seal=_AUDIT_CONTEXT_SEAL,
        )

    @classmethod
    def connector(cls, actor_id: str, *, correlation_id: str) -> AuditContext:
        return cls(
            actor_id,
            AuditActorSource.CONNECTOR,
            "connector_registry",
            correlation_id,
            _seal=_AUDIT_CONTEXT_SEAL,
        )

    @classmethod
    def local_user(cls, actor_id: str, *, correlation_id: str) -> AuditContext:
        return cls(
            actor_id,
            AuditActorSource.USER,
            "local_session",
            correlation_id,
            _seal=_AUDIT_CONTEXT_SEAL,
        )


def _pseudonym(domain: bytes, value: str, prefix: str) -> str:
    require_text(value, "audit pseudonym source", maximum=1_000)
    return prefix + ":" + hashlib.sha256(domain + value.encode("utf-8")).hexdigest()


def _require_pseudonym(value: str, prefix: str, field_name: str) -> None:
    expected_prefix = prefix + ":"
    if not value.startswith(expected_prefix):
        raise ValueError(f"{field_name} must use the trusted pseudonym domain")
    digest = value.removeprefix(expected_prefix)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{field_name} pseudonym digest is invalid")


def _context_fingerprint(context: AuditContext) -> str:
    payload = {
        "actor_id": context.actor_id,
        "actor_source": context.actor_source.value,
        "auth_method": context.auth_method,
        "correlation_id": context.correlation_id,
    }
    return hashlib.sha256(_CONTEXT_FINGERPRINT_DOMAIN + canonical_json_bytes(payload)).hexdigest()


def _metadata_fingerprint(
    values: Mapping[str, Any],
    classification: DataClassification,
    expires_at: datetime,
) -> str:
    payload = {
        "classification": classification.value,
        "expires_at": expires_at.isoformat(),
        "values": values,
    }
    return hashlib.sha256(_METADATA_FINGERPRINT_DOMAIN + canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class SealedAuditMetadata:
    """Validator-issued bounded JSON with post-issuance mutation detection."""

    values: Mapping[str, Any] = field(repr=False)
    classification: DataClassification
    expires_at: datetime
    issuance_fingerprint: str = field(repr=False)

    def __init__(
        self,
        values: Mapping[str, Any],
        classification: DataClassification,
        expires_at: datetime,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _AUDIT_METADATA_SEAL:
            raise ValueError("audit metadata must be issued by the central validator")
        if type(classification) is not DataClassification:
            raise ValueError("audit metadata classification is invalid")
        if classification is DataClassification.SECRET:
            raise ValueError("secret audit metadata is never retainable")
        require_utc(expires_at, "audit metadata expiry")
        frozen = frozen_json_mapping(values, "audit safe metadata")
        object.__setattr__(self, "values", frozen)
        object.__setattr__(self, "classification", classification)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "issuance_fingerprint",
            _metadata_fingerprint(frozen, classification, expires_at),
        )

    def verify_integrity(self) -> None:
        if self.issuance_fingerprint != _metadata_fingerprint(
            self.values,
            self.classification,
            self.expires_at,
        ):
            raise ValueError("audit metadata changed after validation")


def _issue_sealed_audit_metadata(
    values: Mapping[str, Any],
    classification: DataClassification,
    expires_at: datetime,
) -> SealedAuditMetadata:
    """Narrow issuance seam consumed only by ``security.audit_metadata``."""

    return SealedAuditMetadata(
        values,
        classification,
        expires_at,
        _seal=_AUDIT_METADATA_SEAL,
    )


@dataclass(frozen=True, slots=True, init=False)
class AuditEventDraft:
    """Factory-issued event appended in the same UoW as its mutation."""

    id: str
    schema_version: int
    run_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    outcome: AuditOutcome
    actor_id: str
    actor_source: AuditActorSource
    auth_method: str
    correlation_id: str
    safe_metadata: SealedAuditMetadata = field(repr=False)
    occurred_at: datetime
    step_id: str | None
    action_id: str | None
    action_attempt_number: int | None
    receipt_id: str | None
    approval_request_id: str | None
    approval_decision_id: str | None
    artifact_id: str | None
    attempt_id: str | None
    attempted_command: str | None
    expected_version: int | None
    observed_version: int | None
    observed_state: str | None
    requested_state: str | None
    mutation_version: int | None
    transition_sequence: int | None
    previous_state: str | None
    new_state: str | None
    reason_code: str | None
    issuance_fingerprint: str = field(repr=False)

    def __init__(
        self,
        *,
        id: str,
        run_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        outcome: AuditOutcome,
        actor_id: str,
        actor_source: AuditActorSource,
        auth_method: str,
        correlation_id: str,
        safe_metadata: SealedAuditMetadata,
        occurred_at: datetime,
        step_id: str | None = None,
        action_id: str | None = None,
        action_attempt_number: int | None = None,
        receipt_id: str | None = None,
        approval_request_id: str | None = None,
        approval_decision_id: str | None = None,
        artifact_id: str | None = None,
        attempt_id: str | None = None,
        attempted_command: str | None = None,
        expected_version: int | None = None,
        observed_version: int | None = None,
        observed_state: str | None = None,
        requested_state: str | None = None,
        mutation_version: int | None = None,
        transition_sequence: int | None = None,
        previous_state: str | None = None,
        new_state: str | None = None,
        reason_code: str | None = None,
        _seal: object,
    ) -> None:
        if _seal is not _AUDIT_EVENT_SEAL:
            raise ValueError("audit event drafts must be issued by the typed factory")
        values = {
            "id": id,
            "schema_version": 1,
            "run_id": run_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "outcome": outcome,
            "actor_id": actor_id,
            "actor_source": actor_source,
            "auth_method": auth_method,
            "correlation_id": correlation_id,
            "safe_metadata": safe_metadata,
            "occurred_at": occurred_at,
            "step_id": step_id,
            "action_id": action_id,
            "action_attempt_number": action_attempt_number,
            "receipt_id": receipt_id,
            "approval_request_id": approval_request_id,
            "approval_decision_id": approval_decision_id,
            "artifact_id": artifact_id,
            "attempt_id": attempt_id,
            "attempted_command": attempted_command,
            "expected_version": expected_version,
            "observed_version": observed_version,
            "observed_state": observed_state,
            "requested_state": requested_state,
            "mutation_version": mutation_version,
            "transition_sequence": transition_sequence,
            "previous_state": previous_state,
            "new_state": new_state,
            "reason_code": reason_code,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        self._validate()
        object.__setattr__(self, "issuance_fingerprint", _event_fingerprint(self))

    def _validate(self) -> None:
        identifiers = (
            (self.id, "audit event ID"),
            (self.run_id, "audit run ID"),
            (self.aggregate_id, "audit aggregate ID"),
            (self.actor_id, "audit actor ID"),
            (self.auth_method, "audit authentication method"),
            (self.correlation_id, "audit correlation ID"),
        )
        for required_identifier, name in identifiers:
            require_id(required_identifier, name)
        if self.schema_version != 1:
            raise ValueError("audit event schema version is unsupported")
        require_text(self.event_type, "audit event type", maximum=120)
        require_text(self.aggregate_type, "audit aggregate type", maximum=40)
        if type(self.outcome) is not AuditOutcome:
            raise ValueError("audit outcome must use the exact enum")
        if _EVENT_AGGREGATES.get(self.event_type) != self.aggregate_type:
            raise ValueError("audit event type does not match its aggregate")
        if _EVENT_OUTCOMES.get(self.event_type) != self.outcome.value:
            raise ValueError("audit event type does not match its outcome")
        if self.aggregate_type == "run" and self.aggregate_id != self.run_id:
            raise ValueError("run audit aggregate does not match its timeline")
        if self.aggregate_type == "step" and self.aggregate_id != self.step_id:
            raise ValueError("step audit aggregate does not match its step link")
        if self.aggregate_type == "external_action" and self.aggregate_id != self.action_id:
            raise ValueError("action audit aggregate does not match its action link")
        if self.aggregate_type == "connector_receipt" and (
            self.aggregate_id != self.receipt_id or self.action_id is None
        ):
            raise ValueError("receipt audit aggregate requires its receipt and action links")
        if self.aggregate_type == "approval_request" and (
            self.aggregate_id != self.approval_request_id
            or self.action_id is None
            or self.step_id is None
        ):
            raise ValueError(
                "approval audit aggregate requires its request, action, and step links"
            )
        rejection_fields = (
            self.attempt_id,
            self.attempted_command,
            self.expected_version,
            self.observed_version,
            self.observed_state,
        )
        if self.aggregate_type == "run_attempt":
            if (
                self.event_type != "run.transition_rejected"
                or self.aggregate_id != self.attempt_id
                or self.mutation_version is not None
                or self.transition_sequence is not None
                or self.previous_state is not None
                or self.new_state is not None
                or self.outcome is not AuditOutcome.REJECTED
                or any(value is None for value in rejection_fields)
                or self.step_id is not None
                or self.action_id is not None
                or self.action_attempt_number is not None
                or self.receipt_id is not None
            ):
                raise ValueError("rejected run attempt has an invalid nonmutation shape")
        elif any(value is not None for value in (*rejection_fields, self.requested_state)):
            raise ValueError("only rejected attempts may retain attempt observations")
        if self.outcome in {AuditOutcome.ACCEPTED, AuditOutcome.OBSERVED}:
            if self.mutation_version is None:
                raise ValueError("accepted and observed audit events require a mutation coordinate")
        elif self.mutation_version is not None:
            raise ValueError("rejected audit attempts cannot consume a mutation coordinate")
        if self.aggregate_type == "run":
            if (
                self.step_id is not None
                or self.action_id is not None
                or self.receipt_id is not None
                or self.transition_sequence is None
            ):
                raise ValueError("run audit event has invalid subject links")
        elif self.aggregate_type == "step":
            if (
                self.step_id is None
                or self.action_id is not None
                or self.receipt_id is not None
                or self.transition_sequence is None
            ):
                raise ValueError("step audit event has invalid subject links")
        elif self.aggregate_type == "external_action":
            if (
                self.step_id is None
                or self.action_id is None
                or self.transition_sequence is not None
            ):
                raise ValueError("action audit event has invalid subject links")
            success_event = self.event_type in {
                "action.succeeded",
                "action.receipt_reconciled",
            }
            if success_event != (self.receipt_id is not None):
                raise ValueError("action receipt link does not match its event type")
        elif self.aggregate_type == "connector_receipt" and (
            self.step_id is None
            or self.action_id is None
            or self.action_attempt_number is None
            or self.receipt_id is None
            or self.mutation_version != 1
            or self.outcome is not AuditOutcome.OBSERVED
        ):
            raise ValueError("connector receipt audit has invalid subject links")
        elif self.aggregate_type == "approval_request" and (
            self.step_id is None
            or self.action_id is None
            or self.action_attempt_number is not None
            or self.receipt_id is not None
            or self.approval_request_id is None
            or self.approval_decision_id is not None
            or self.artifact_id is not None
            or self.attempt_id is not None
            or self.transition_sequence is not None
        ):
            raise ValueError("approval audit event has invalid subject links")
        if type(self.actor_source) is not AuditActorSource:
            raise ValueError("audit actor source is invalid")
        _require_pseudonym(self.actor_id, "audit-actor-v1", "audit actor ID")
        _require_pseudonym(
            self.correlation_id,
            "audit-correlation-v1",
            "audit correlation ID",
        )
        expected_auth = {
            AuditActorSource.SYSTEM: "internal",
            AuditActorSource.WORKER: "internal",
            AuditActorSource.USER: "local_session",
            AuditActorSource.CONNECTOR: "connector_registry",
        }[self.actor_source]
        if self.auth_method != expected_auth:
            raise ValueError("audit actor source and authentication method disagree")
        if type(self.safe_metadata) is not SealedAuditMetadata:
            raise ValueError("audit metadata must use the validator-issued contract")
        self.safe_metadata.verify_integrity()
        if set(self.safe_metadata.values) != _EVENT_REQUIRED_METADATA[self.event_type]:
            raise ValueError("audit event is missing its exact typed safe metadata")
        require_utc(self.occurred_at, "audit event time")
        if self.safe_metadata.expires_at <= self.occurred_at:
            raise ValueError("audit metadata must expire after the event time")
        optional_identifiers = (
            (self.step_id, "audit step ID"),
            (self.action_id, "audit action ID"),
            (self.receipt_id, "audit receipt ID"),
            (self.approval_request_id, "audit approval request ID"),
            (self.approval_decision_id, "audit approval decision ID"),
            (self.artifact_id, "audit artifact ID"),
            (self.reason_code, "audit reason code"),
            (self.attempt_id, "audit attempt ID"),
            (self.attempted_command, "audit attempted command"),
            (self.observed_state, "audit observed state"),
            (self.requested_state, "audit requested state"),
        )
        for optional_identifier, name in optional_identifiers:
            if optional_identifier is not None:
                require_id(optional_identifier, name)
        if self.expected_version is not None and (
            not isinstance(self.expected_version, int)
            or isinstance(self.expected_version, bool)
            or self.expected_version < 0
        ):
            raise ValueError("audit expected version must be a nonnegative integer")
        if self.observed_version is not None and (
            not isinstance(self.observed_version, int)
            or isinstance(self.observed_version, bool)
            or self.observed_version < 1
        ):
            raise ValueError("audit observed version must be a positive integer")
        optional_positive_integers = (
            (self.action_attempt_number, "audit action attempt"),
            (self.mutation_version, "audit mutation version"),
            (self.transition_sequence, "audit transition sequence"),
        )
        for number, name in optional_positive_integers:
            if number is not None and (
                not isinstance(number, int) or isinstance(number, bool) or number < 1
            ):
                raise ValueError(f"{name} must be a positive integer")
        if self.action_attempt_number is not None and self.action_id is None:
            raise ValueError("audit action attempt requires an action ID")
        if self.transition_sequence is not None and self.mutation_version is None:
            raise ValueError("audit transition sequence requires a mutation version")
        has_state = self.previous_state is not None or self.new_state is not None
        if has_state and self.mutation_version is None:
            raise ValueError("audit state changes require a mutation version")
        if self.aggregate_type in {"run", "step"} and has_state:
            if self.transition_sequence != self.mutation_version:
                raise ValueError("lifecycle audit sequence must equal its mutation version")
        elif self.transition_sequence is not None:
            raise ValueError("only run and step audit events may link transitions")
        if self.previous_state is None and self.new_state is not None:
            if self.mutation_version != 1:
                raise ValueError("only an initial transition may omit its previous state")
            if self.aggregate_type in {"run", "step"} and self.transition_sequence != 1:
                raise ValueError("initial run and step events require transition sequence one")
            if self.aggregate_type not in {"run", "step"} and self.transition_sequence is not None:
                raise ValueError("non-lifecycle initial events cannot link a transition")
            require_id(self.new_state, "audit initial state")
        elif (self.previous_state is None) != (self.new_state is None):
            raise ValueError("audit state change must retain both previous and new state")
        elif self.previous_state is not None:
            require_id(self.previous_state, "audit previous state")
            if self.new_state is None:  # pragma: no cover - guarded immediately above
                raise AssertionError("audit new state disappeared")
            require_id(self.new_state, "audit new state")
            if self.previous_state == self.new_state and self.event_type not in {
                "action.call_started",
                "approval.renewed",
            }:
                raise ValueError("audit state change must change state")
        _validate_event_semantics(self)

    def verify_integrity(self) -> None:
        self._validate()
        if self.issuance_fingerprint != _event_fingerprint(self):
            raise ValueError("audit event draft changed after validation")


def _draft_fingerprint_payload(draft: AuditEventDraft) -> Mapping[str, Any]:
    return {
        "action_attempt_number": draft.action_attempt_number,
        "action_id": draft.action_id,
        "actor_id": draft.actor_id,
        "actor_source": draft.actor_source.value,
        "aggregate_id": draft.aggregate_id,
        "aggregate_type": draft.aggregate_type,
        "outcome": draft.outcome.value,
        "auth_method": draft.auth_method,
        "correlation_id": draft.correlation_id,
        "event_type": draft.event_type,
        "id": draft.id,
        "schema_version": draft.schema_version,
        "metadata_classification": draft.safe_metadata.classification.value,
        "metadata_expires_at": draft.safe_metadata.expires_at.isoformat(),
        "metadata_fingerprint": draft.safe_metadata.issuance_fingerprint,
        "mutation_version": draft.mutation_version,
        "new_state": draft.new_state,
        "occurred_at": draft.occurred_at.isoformat(),
        "previous_state": draft.previous_state,
        "reason_code": draft.reason_code,
        "receipt_id": draft.receipt_id,
        "approval_request_id": draft.approval_request_id,
        "approval_decision_id": draft.approval_decision_id,
        "artifact_id": draft.artifact_id,
        "attempt_id": draft.attempt_id,
        "attempted_command": draft.attempted_command,
        "expected_version": draft.expected_version,
        "observed_version": draft.observed_version,
        "observed_state": draft.observed_state,
        "requested_state": draft.requested_state,
        "run_id": draft.run_id,
        "step_id": draft.step_id,
        "transition_sequence": draft.transition_sequence,
    }


def _validate_event_semantics(draft: AuditEventDraft) -> None:
    if draft.approval_decision_id is not None or draft.artifact_id is not None:
        raise ValueError("future audit subject links are not valid for current event families")
    if (draft.aggregate_type == "approval_request") != (draft.approval_request_id is not None):
        raise ValueError("approval request link does not match its event family")
    if draft.reason_code is not None and draft.reason_code not in _SAFE_AUDIT_REASONS:
        raise ValueError("audit reason code is not an allowlisted operational code")
    metadata = draft.safe_metadata.values
    if draft.aggregate_type in {"run", "step"} and (
        draft.new_state is None or draft.reason_code is None
    ):
        raise ValueError("lifecycle audit requires its state and safe reason")
    if draft.event_type == "run.received":
        if (
            draft.mutation_version != 1
            or draft.transition_sequence != 1
            or draft.previous_state is not None
            or draft.new_state != "received"
            or draft.reason_code != "work_admitted"
            or metadata["command"] != "receive"
        ):
            raise ValueError("run received audit does not match the initial transition")
    elif draft.event_type == "run.plan_recorded":
        if (
            draft.mutation_version is None
            or draft.mutation_version <= 1
            or draft.previous_state != "validated"
            or draft.new_state != "planned"
            or draft.reason_code != "plan_recorded"
            or metadata["command"] != "record_plan"
        ):
            raise ValueError("plan audit does not match the record-plan transition")
    elif draft.event_type == "run.transitioned":
        command = metadata["command"]
        if command not in _RUN_COMMANDS - {"receive", "record_plan"}:
            raise ValueError("run transition audit command is invalid")
        if draft.mutation_version is None or draft.mutation_version <= 1:
            raise ValueError("run transition audit must follow initial receipt")
        allowed_edges: Mapping[str, frozenset[tuple[str, str]]] = {
            "mark_validated": frozenset({("received", "validated")}),
            "activate_plan": frozenset(
                {("planned", "awaiting_approval"), ("planned", "executing")}
            ),
            "release_approved_plan": frozenset({("awaiting_approval", "executing")}),
            "reject_approval": frozenset({("awaiting_approval", "rejected")}),
            "complete": frozenset({("executing", "completed")}),
            "fail": frozenset(
                {
                    ("received", "failed"),
                    ("validated", "failed"),
                    ("planned", "failed"),
                    ("awaiting_approval", "failed"),
                    ("executing", "failed"),
                }
            ),
            "cancel": frozenset(
                {
                    ("received", "cancelled"),
                    ("validated", "cancelled"),
                    ("planned", "cancelled"),
                    ("awaiting_approval", "cancelled"),
                    ("executing", "cancelled"),
                }
            ),
        }
        if (draft.previous_state, draft.new_state) not in allowed_edges[command]:
            raise ValueError("run transition audit command and states disagree")
        fixed_run_reasons = {
            ("mark_validated", "validated"): "input_validated",
            ("activate_plan", "awaiting_approval"): "write_plan_requires_approval",
            ("activate_plan", "executing"): "read_only_plan_released",
            ("release_approved_plan", "executing"): "approval_barrier_satisfied",
            ("reject_approval", "rejected"): "approval_rejected",
            ("complete", "completed"): "execution_completed",
        }
        fixed_reason = (
            None
            if draft.new_state is None
            else fixed_run_reasons.get((str(command), draft.new_state))
        )
        if fixed_reason is not None and draft.reason_code != fixed_reason:
            raise ValueError("run transition audit reason disagrees with its command")
    elif draft.event_type == "run.transition_rejected":
        rejection_reasons = {
            "approval_barrier_incomplete",
            "approval_rejection_mismatch",
            "execution_incomplete",
            "failure_phase_mismatch",
            "invalid_cancellation_effects",
            "invalid_transition",
            "non_monotonic_time",
            "stale_run_version",
            "terminal_state_immutable",
        }
        requested_by_command: Mapping[str, frozenset[str]] = {
            "receive": frozenset({"received"}),
            "mark_validated": frozenset({"validated"}),
            "record_plan": frozenset({"planned"}),
            "activate_plan": frozenset({"awaiting_approval", "executing"}),
            "release_approved_plan": frozenset({"executing"}),
            "reject_approval": frozenset({"rejected"}),
            "complete": frozenset({"completed"}),
            "fail": frozenset({"failed"}),
            "cancel": frozenset({"cancelled"}),
        }
        common_reasons = {
            "invalid_transition",
            "non_monotonic_time",
            "stale_run_version",
            "terminal_state_immutable",
        }
        specific_reason = (
            None
            if draft.attempted_command is None
            else {
                "release_approved_plan": "approval_barrier_incomplete",
                "reject_approval": "approval_rejection_mismatch",
                "complete": "execution_incomplete",
                "fail": "failure_phase_mismatch",
                "cancel": "invalid_cancellation_effects",
            }.get(draft.attempted_command)
        )
        allowed_reasons = common_reasons | ({specific_reason} if specific_reason else set())
        if (
            draft.reason_code is None
            or draft.reason_code not in rejection_reasons
            or draft.attempted_command not in _RUN_COMMANDS
            or metadata["command"] != draft.attempted_command
            or draft.observed_state not in _RUN_STATES
            or (draft.requested_state is not None and draft.requested_state not in _RUN_STATES)
            or draft.requested_state
            not in requested_by_command.get(draft.attempted_command or "", frozenset())
            or draft.reason_code not in allowed_reasons
        ):
            raise ValueError("rejected run audit has an invalid observation")
    elif draft.event_type == "step.recorded":
        if (
            draft.mutation_version != 1
            or draft.transition_sequence != 1
            or draft.previous_state is not None
            or draft.new_state != "pending"
            or draft.reason_code != "plan_step_recorded"
        ):
            raise ValueError("step recorded audit does not match initial pending state")
    elif draft.event_type == "step.transitioned":
        if draft.mutation_version is None or draft.mutation_version <= 1:
            raise ValueError("step transition audit must follow initial persistence")
        step_edges: Mapping[str, frozenset[tuple[str, str]]] = {
            "mark_ready": frozenset({("pending", "ready")}),
            "wait_for_approval": frozenset({("pending", "awaiting_approval")}),
            "start": frozenset({("ready", "executing")}),
            "succeed": frozenset({("executing", "succeeded")}),
            "fail": frozenset({("executing", "failed")}),
            "reject": frozenset({("awaiting_approval", "rejected")}),
            "cancel": frozenset(
                {
                    ("pending", "cancelled"),
                    ("ready", "cancelled"),
                    ("awaiting_approval", "cancelled"),
                }
            ),
            "skip": frozenset(
                {
                    ("pending", "skipped"),
                    ("ready", "skipped"),
                    ("awaiting_approval", "skipped"),
                }
            ),
        }
        command = metadata["command"]
        if (
            command not in step_edges
            or (
                draft.previous_state,
                draft.new_state,
            )
            not in step_edges[command]
        ):
            raise ValueError("step transition audit command and states disagree")
        fixed_step_reasons = {
            "mark_ready": "step_dependencies_satisfied",
            "wait_for_approval": "step_approval_required",
            "start": "step_execution_started",
            "succeed": "step_succeeded",
        }
        fixed_step_reason = fixed_step_reasons.get(command)
        if fixed_step_reason is not None and draft.reason_code != fixed_step_reason:
            raise ValueError("step transition audit reason disagrees with its command")
    elif draft.aggregate_type == "approval_request":
        if metadata["action_state"] != "awaiting_approval":
            raise ValueError("approval audit must bind the action approval-wait state")
        if metadata["status"] != draft.new_state:
            raise ValueError("approval audit metadata status disagrees with its state")
        if draft.event_type == "approval.requested":
            if (
                draft.mutation_version != 1
                or draft.previous_state is not None
                or draft.new_state != "pending"
                or draft.reason_code != "approval_requested"
                or metadata["generation"] < 1
            ):
                raise ValueError("approval request audit has an invalid initial shape")
        elif draft.event_type == "approval.expired":
            if (
                draft.mutation_version is None
                or draft.mutation_version < 2
                or draft.previous_state not in {"pending", "approved"}
                or draft.new_state != "expired"
                or draft.reason_code != "approval_expired"
            ):
                raise ValueError("approval expiry audit has an invalid lifecycle shape")
        elif draft.event_type == "approval.renewed" and (
            draft.mutation_version is None
            or draft.mutation_version < 3
            or draft.previous_state != "expired"
            or draft.new_state != "expired"
            or draft.reason_code != "approval_renewed"
            or metadata["replacement_request_id"] == draft.approval_request_id
        ):
            raise ValueError("approval renewal audit has an invalid lifecycle shape")
    elif draft.aggregate_type == "external_action":
        action_shapes: Mapping[str, tuple[str | None, str, bool, bool]] = {
            "action.proposed": (None, "proposed", False, False),
            "action.awaiting_approval": (None, "awaiting_approval", False, False),
            "action.dispatch_claimed": ("dispatch_reserved", "dispatching", True, False),
            "action.call_started": ("dispatching", "dispatching", True, False),
            "action.retry_released": ("dispatching", "dispatch_reserved", True, False),
            "action.succeeded": ("dispatching", "succeeded", True, True),
            "action.failed": ("dispatching", "failed", True, False),
            "action.outcome_unknown": ("dispatching", "outcome_unknown", True, False),
            "action.receipt_reconciled": ("dispatching", "succeeded", True, True),
        }
        previous, current, requires_attempt, requires_receipt = action_shapes[draft.event_type]
        allowed_previous = (
            {"proposed", "approved"}
            if draft.event_type == "action.awaiting_approval"
            else {previous}
        )
        if (
            draft.previous_state not in allowed_previous
            or draft.new_state != current
            or requires_attempt != (draft.action_attempt_number is not None)
            or requires_receipt != (draft.receipt_id is not None)
            or (draft.event_type == "action.proposed" and draft.mutation_version != 1)
            or (draft.event_type != "action.proposed" and draft.mutation_version == 1)
        ):
            raise ValueError("action audit does not match its exact mutation shape")
        expected_conclusions = {
            "action.proposed": None,
            "action.awaiting_approval": None,
            "action.dispatch_claimed": None,
            "action.call_started": None,
            "action.retry_released": {"pre_call_expired", "provider_retry"},
            "action.succeeded": {"succeeded"},
            "action.failed": {"failed"},
            "action.outcome_unknown": {"outcome_unknown"},
            "action.receipt_reconciled": {"receipt_reconciled"},
        }
        expected_conclusion = expected_conclusions[draft.event_type]
        actual_conclusion = metadata.get("conclusion")
        if expected_conclusion is None:
            if actual_conclusion is not None:
                raise ValueError("action audit conclusion is invalid for its event")
        elif actual_conclusion not in expected_conclusion:
            raise ValueError("action audit conclusion is invalid for its event")
        reason_required = draft.event_type in {
            "action.awaiting_approval",
            "action.failed",
            "action.outcome_unknown",
        }
        if reason_required != (draft.reason_code is not None):
            raise ValueError("action audit terminal reason does not match its event")
        expected_approval_reason = {
            "action.awaiting_approval": {"approval_requested", "approval_expired"},
        }.get(draft.event_type)
        if (
            expected_approval_reason is not None
            and draft.reason_code not in expected_approval_reason
        ):
            raise ValueError("action approval audit reason disagrees with its transition")


def _event_fingerprint(draft: AuditEventDraft) -> str:
    return hashlib.sha256(
        _EVENT_FINGERPRINT_DOMAIN + canonical_json_bytes(_draft_fingerprint_payload(draft))
    ).hexdigest()


def _issue_audit_event_draft(**values: Any) -> AuditEventDraft:
    """Narrow issuance seam consumed only by the application audit factory."""

    return AuditEventDraft(**values, _seal=_AUDIT_EVENT_SEAL)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Persisted event with an internal row identity and stable per-Run order."""

    draft: AuditEventDraft
    global_sequence: int
    run_sequence: int

    def __post_init__(self) -> None:
        if type(self.draft) is not AuditEventDraft:
            raise ValueError("persisted audit event requires the exact draft contract")
        self.draft.verify_integrity()
        for number, name in (
            (self.global_sequence, "internal audit row identity"),
            (self.run_sequence, "run audit sequence"),
        ):
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise ValueError(f"{name} must be positive")

    def __getattr__(self, name: str) -> Any:
        """Expose immutable draft fields without duplicating the persisted contract."""

        return getattr(self.draft, name)
