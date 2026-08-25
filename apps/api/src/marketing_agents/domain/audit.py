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
_RUNTIME_CONTROL_DENIAL_ID_DOMAIN = b"marketing-agents:audit-runtime-control-denial-id:v1\x00"
_RUNTIME_CONTROL_DENIAL_ID_PREFIX = "runtime-control-denial-v1:"
RUNTIME_CONTROL_DENIAL_CODES = frozenset(
    {
        "adapter_contract_drift",
        "adapter_contract_invalid",
        "adapter_contract_unavailable",
        "attempt_completion_conflict",
        "attempt_completion_time_invalid",
        "attempt_id_conflict",
        "attempt_in_progress",
        "attempt_missing",
        "attempt_not_retryable",
        "attempt_reservation_conflict",
        "attempt_time_invalid",
        "attempts_exhausted",
        "cancellation_conflict",
        "cancellation_time_invalid",
        "cancelled",
        "deadline_exceeded",
        "delivery_action_fence_invalid",
        "delivery_contract_drift",
        "delivery_contract_invalid",
        "delivery_contract_unavailable",
        "delivery_reservation_conflict",
        "delivery_step_fence_invalid",
        "delivery_time_invalid",
        "execution_control_integrity_corrupt",
        "execution_control_missing",
        "execution_not_started",
        "execution_plan_missing",
        "execution_policy_binding_invalid",
        "execution_policy_conflict",
        "execution_policy_source_corrupt",
        "execution_policy_time_invalid",
        "execution_run_missing",
        "execution_start_conflict",
        "execution_start_time_invalid",
        "input_field_too_large",
        "input_payload_too_large",
        "model_budget_exhausted",
        "model_output_tokens_exceeded",
        "model_output_tokens_invalid",
        "operation_policy_missing",
        "output_payload_too_large",
        "output_schema_invalid",
        "permanent_failure",
        "rate_limit_conflict",
        "rate_limit_exhausted",
        "retry_deadline_exceeded",
        "retry_not_ready",
        "run_cancelled",
        "run_not_executing",
        "stale_execution_control",
        "step_fence_invalid",
        "tool_budget_exhausted",
    }
)
TERMINAL_RUNTIME_CONTROL_DENIAL_CODES = frozenset(
    {
        "adapter_contract_drift",
        "adapter_contract_invalid",
        "adapter_contract_unavailable",
        "attempts_exhausted",
        "deadline_exceeded",
        "delivery_contract_drift",
        "delivery_contract_invalid",
        "delivery_contract_unavailable",
        "input_field_too_large",
        "input_payload_too_large",
        "model_budget_exhausted",
        "model_output_tokens_exceeded",
        "model_output_tokens_invalid",
        "output_payload_too_large",
        "output_schema_invalid",
        "retry_deadline_exceeded",
        "tool_budget_exhausted",
    }
)
_EVENT_AGGREGATES = {
    "schedule.occurrence_created": "schedule_occurrence",
    "schedule.misfire_skipped": "schedule_occurrence",
    "schedule.misfire_run_once": "schedule_occurrence",
    "schedule.next_occurrence_persisted": "schedule",
    "run.received": "run",
    "run.transitioned": "run",
    "run.transition_rejected": "run_attempt",
    "run.plan_recorded": "run",
    "step.recorded": "step",
    "step.transitioned": "step",
    "attempt.reserved": "execution_attempt",
    "attempt.completed": "execution_attempt",
    "artifact.persisted": "artifact",
    "action.proposed": "external_action",
    "action.awaiting_approval": "external_action",
    "action.approved": "external_action",
    "action.rejected": "external_action",
    "action.dispatch_reserved": "external_action",
    "action.dispatch_claimed": "external_action",
    "action.call_started": "external_action",
    "action.retry_released": "external_action",
    "action.succeeded": "external_action",
    "action.failed": "external_action",
    "action.outcome_unknown": "external_action",
    "action.receipt_reconciled": "external_action",
    "action.cancelled": "external_action",
    "connector.receipt_committed": "connector_receipt",
    "approval.requested": "approval_request",
    "approval.approved": "approval_request",
    "approval.rejected": "approval_request",
    "approval.consumed": "approval_request",
    "approval.superseded": "approval_request",
    "approval.expired": "approval_request",
    "approval.renewed": "approval_request",
    "runtime.control_denied": "runtime_control_denial",
}
_EVENT_OUTCOMES = {
    **{event_type: "accepted" for event_type in _EVENT_AGGREGATES},
    "run.transition_rejected": "rejected",
    "runtime.control_denied": "rejected",
    "connector.receipt_committed": "observed",
}
_EVENT_REQUIRED_METADATA: Mapping[str, frozenset[str]] = {
    "schedule.occurrence_created": frozenset(
        {
            "next_run_at_utc",
            "recurrence_version",
            "scheduled_for_utc",
            "work_admitted",
        }
    ),
    "schedule.misfire_skipped": frozenset(
        {
            "first_missed_at_utc",
            "last_missed_at_utc",
            "missed_count",
            "next_run_at_utc",
            "recurrence_version",
            "scheduled_for_utc",
            "work_admitted",
        }
    ),
    "schedule.misfire_run_once": frozenset(
        {
            "first_missed_at_utc",
            "last_missed_at_utc",
            "missed_count",
            "next_run_at_utc",
            "recurrence_version",
            "scheduled_for_utc",
            "work_admitted",
        }
    ),
    "schedule.next_occurrence_persisted": frozenset(
        {
            "disposition",
            "last_scheduled_at_utc",
            "next_run_at_utc",
            "occurrence_id",
            "previous_next_run_at_utc",
        }
    ),
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
    "attempt.reserved": frozenset(
        {
            "attempt_kind",
            "attempt_number",
            "input_classification",
            "input_schema_id",
            "operation_key",
        }
    ),
    "attempt.completed": frozenset(
        {
            "attempt_kind",
            "attempt_number",
            "attempt_outcome",
            "operation_key",
        }
    ),
    "artifact.persisted": frozenset(
        {
            "data_classification",
            "output_schema_hash",
            "output_schema_id",
            "output_schema_version",
        }
    ),
    "action.proposed": frozenset({"idempotency_support"}),
    "action.awaiting_approval": frozenset({"idempotency_support"}),
    "action.approved": frozenset({"idempotency_support"}),
    "action.rejected": frozenset({"idempotency_support"}),
    "action.dispatch_reserved": frozenset(
        {
            "approval_use_id",
            "approval_set_id",
            "idempotency_support",
            "reservation_id",
        }
    ),
    "action.dispatch_claimed": frozenset({"idempotency_support"}),
    "action.call_started": frozenset({"idempotency_support"}),
    "action.retry_released": frozenset({"conclusion", "idempotency_support"}),
    "action.succeeded": frozenset({"conclusion", "connector_status", "idempotency_support"}),
    "action.failed": frozenset({"conclusion", "idempotency_support"}),
    "action.outcome_unknown": frozenset({"conclusion", "idempotency_support"}),
    "action.receipt_reconciled": frozenset(
        {"conclusion", "connector_status", "idempotency_support"}
    ),
    "action.cancelled": frozenset(
        {"approval_set_id", "approval_status", "closure_reason", "idempotency_support"}
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
    "approval.approved": frozenset(
        {
            "action_state",
            "action_version",
            "decision",
            "generation",
            "policy_id",
            "proposal_revision",
            "status",
        }
    ),
    "approval.rejected": frozenset(
        {
            "action_state",
            "action_version",
            "decision",
            "generation",
            "policy_id",
            "proposal_revision",
            "status",
        }
    ),
    "approval.consumed": frozenset(
        {
            "action_state",
            "action_version",
            "approval_use_id",
            "approval_set_id",
            "generation",
            "policy_id",
            "proposal_revision",
            "reservation_id",
            "status",
        }
    ),
    "approval.superseded": frozenset(
        {
            "action_state",
            "action_version",
            "approval_set_id",
            "generation",
            "policy_id",
            "proposal_revision",
            "status",
            "supersession_reason",
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
    "runtime.control_denied": frozenset({"denial_code", "operation_key"}),
}
_EVENT_OPTIONAL_METADATA: Mapping[str, frozenset[str]] = {
    "attempt.completed": frozenset({"safe_error_code"}),
    "runtime.control_denied": frozenset({"retry_after_seconds"}),
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
        "approval_barrier_released",
        "approval_consumed",
        "approval_expired",
        "approval_granted",
        "approval_renewed",
        "approval_rejected",
        "approval_requested",
        "approval_set_rejected",
        "approval_set_superseded",
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
        "reserved_write_started",
        "run_cancelled",
        "run_cancelled_after_call_start",
        "runtime_control_denied",
        "runtime_control_denied_after_call_start",
        "sibling_approval_rejected",
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


def _runtime_control_denial_aggregate_id(
    *,
    actor_id: str,
    actor_source: str,
    correlation_id: str,
    run_id: str,
    step_id: str,
    action_id: str | None,
    operation_key: str,
    denial_code: str,
) -> str:
    """Derive one replay-stable identity without retaining raw actor context."""

    for value, name in (
        (actor_id, "runtime-control denial actor ID"),
        (actor_source, "runtime-control denial actor source"),
        (correlation_id, "runtime-control denial correlation ID"),
        (run_id, "runtime-control denial Run ID"),
        (step_id, "runtime-control denial step ID"),
        (operation_key, "runtime-control denial operation key"),
        (denial_code, "runtime-control denial code"),
    ):
        require_id(value, name)
    if action_id is not None:
        require_id(action_id, "runtime-control denial action ID")
    if denial_code not in RUNTIME_CONTROL_DENIAL_CODES:
        raise ValueError("runtime-control denial code is not allowlisted")
    identity = {
        "action_id": action_id,
        "actor_id": actor_id,
        "actor_source": actor_source,
        "correlation_id": correlation_id,
        "denial_code": denial_code,
        "operation_key": operation_key,
        "run_id": run_id,
        "step_id": step_id,
    }
    return (
        _RUNTIME_CONTROL_DENIAL_ID_PREFIX
        + hashlib.sha256(
            _RUNTIME_CONTROL_DENIAL_ID_DOMAIN + canonical_json_bytes(identity)
        ).hexdigest()
    )


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


_AUTH_METHODS_BY_SOURCE: Mapping[AuditActorSource, frozenset[str]] = {
    AuditActorSource.SYSTEM: frozenset({"internal"}),
    AuditActorSource.WORKER: frozenset({"internal"}),
    AuditActorSource.USER: frozenset({"local_session", "local_fixed", "bearer"}),
    AuditActorSource.CONNECTOR: frozenset({"connector_registry"}),
}


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
        if self.auth_method not in _AUTH_METHODS_BY_SOURCE[self.actor_source]:
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

    @classmethod
    def authenticated_user(
        cls,
        actor_id: str,
        *,
        authentication_method: str,
        correlation_id: str,
    ) -> AuditContext:
        if authentication_method not in {"local_fixed", "bearer"}:
            raise ValueError("human audit authentication method is unsupported")
        return cls(
            actor_id,
            AuditActorSource.USER,
            authentication_method,
            correlation_id,
            _seal=_AUDIT_CONTEXT_SEAL,
        )

    def binds_authenticated_user(
        self,
        *,
        actor_id: str,
        authentication_method: str,
        correlation_id: str,
    ) -> bool:
        self.verify_integrity()
        return (
            self.actor_source is AuditActorSource.USER
            and self.auth_method == authentication_method
            and self.actor_id == _pseudonym(_ACTOR_PSEUDONYM_DOMAIN, actor_id, "audit-actor-v1")
            and self.correlation_id
            == _pseudonym(
                _CORRELATION_PSEUDONYM_DOMAIN,
                correlation_id,
                "audit-correlation-v1",
            )
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
    run_id: str | None
    schedule_id: str | None
    occurrence_id: str | None
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
        run_id: str | None,
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
        schedule_id: str | None = None,
        occurrence_id: str | None = None,
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
            "schedule_id": schedule_id,
            "occurrence_id": occurrence_id,
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
            (self.aggregate_id, "audit aggregate ID"),
            (self.actor_id, "audit actor ID"),
            (self.auth_method, "audit authentication method"),
            (self.correlation_id, "audit correlation ID"),
        )
        for required_identifier, name in identifiers:
            require_id(required_identifier, name)
        scheduler_aggregate = self.aggregate_type in {"schedule", "schedule_occurrence"}
        if scheduler_aggregate:
            if self.run_id is not None:
                raise ValueError("scheduler audit events must use the global timeline")
        elif self.run_id is None:
            raise ValueError("non-scheduler audit events require one Run timeline")
        else:
            require_id(self.run_id, "audit run ID")
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
        if self.aggregate_type == "schedule" and self.aggregate_id != self.schedule_id:
            raise ValueError("schedule audit aggregate does not match its schedule link")
        if self.aggregate_type == "schedule_occurrence" and (
            self.aggregate_id != self.occurrence_id or self.schedule_id is None
        ):
            raise ValueError("schedule occurrence audit requires its occurrence and schedule links")
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
        if self.aggregate_type == "execution_attempt" and (
            self.aggregate_id != self.attempt_id or self.step_id is None
        ):
            raise ValueError("attempt audit aggregate requires its attempt and step links")
        if self.aggregate_type == "artifact" and (
            self.aggregate_id != self.artifact_id or self.step_id is None or self.attempt_id is None
        ):
            raise ValueError("artifact audit aggregate requires artifact, attempt, and step links")
        rejection_observation_fields = (
            self.attempted_command,
            self.expected_version,
            self.observed_version,
            self.observed_state,
            self.requested_state,
        )
        if self.aggregate_type == "run_attempt":
            if (
                self.event_type != "run.transition_rejected"
                or self.aggregate_id != self.attempt_id
                or self.attempt_id is None
                or self.mutation_version is not None
                or self.transition_sequence is not None
                or self.previous_state is not None
                or self.new_state is not None
                or self.outcome is not AuditOutcome.REJECTED
                or any(value is None for value in rejection_observation_fields)
                or self.step_id is not None
                or self.action_id is not None
                or self.action_attempt_number is not None
                or self.receipt_id is not None
            ):
                raise ValueError("rejected run attempt has an invalid nonmutation shape")
        elif any(value is not None for value in rejection_observation_fields):
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
        elif self.aggregate_type == "schedule_occurrence":
            if (
                self.schedule_id is None
                or self.occurrence_id is None
                or self.step_id is not None
                or self.action_id is not None
                or self.action_attempt_number is not None
                or self.receipt_id is not None
                or self.approval_request_id is not None
                or self.approval_decision_id is not None
                or self.artifact_id is not None
                or self.attempt_id is not None
                or self.transition_sequence is not None
                or self.previous_state is not None
                or self.new_state is not None
                or self.reason_code is not None
                or self.mutation_version != 1
                or self.outcome is not AuditOutcome.ACCEPTED
            ):
                raise ValueError("schedule occurrence audit has invalid subject links")
        elif self.aggregate_type == "schedule":
            if (
                self.event_type != "schedule.next_occurrence_persisted"
                or self.schedule_id is None
                or self.occurrence_id is None
                or self.step_id is not None
                or self.action_id is not None
                or self.action_attempt_number is not None
                or self.receipt_id is not None
                or self.approval_request_id is not None
                or self.approval_decision_id is not None
                or self.artifact_id is not None
                or self.attempt_id is not None
                or self.transition_sequence is not None
                or self.previous_state is not None
                or self.new_state is not None
                or self.reason_code is not None
                or self.outcome is not AuditOutcome.ACCEPTED
            ):
                raise ValueError("schedule audit has invalid subject links")
        elif self.aggregate_type == "step":
            if (
                self.step_id is None
                or self.action_id is not None
                or self.receipt_id is not None
                or self.transition_sequence is None
            ):
                raise ValueError("step audit event has invalid subject links")
        elif self.aggregate_type == "execution_attempt":
            if (
                self.step_id is None
                or self.attempt_id is None
                or self.action_id is not None
                or self.action_attempt_number is not None
                or self.receipt_id is not None
                or self.approval_request_id is not None
                or self.approval_decision_id is not None
                or self.transition_sequence is not None
            ):
                raise ValueError("attempt audit event has invalid subject links")
        elif self.aggregate_type == "artifact":
            if (
                self.step_id is None
                or self.attempt_id is None
                or self.artifact_id is None
                or self.action_id is not None
                or self.action_attempt_number is not None
                or self.receipt_id is not None
                or self.approval_request_id is not None
                or self.approval_decision_id is not None
                or self.transition_sequence is not None
            ):
                raise ValueError("artifact audit event has invalid subject links")
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
            request_event = self.event_type in {
                "action.approved",
                "action.rejected",
                "action.dispatch_reserved",
                "action.cancelled",
            }
            required_decision_event = self.event_type in {
                "action.approved",
                "action.rejected",
                "action.dispatch_reserved",
            }
            if request_event != (self.approval_request_id is not None):
                raise ValueError("action approval-request link does not match its event type")
            if required_decision_event != (self.approval_decision_id is not None) and not (
                self.event_type == "action.cancelled" and self.approval_decision_id is not None
            ):
                raise ValueError("action approval-decision link does not match its event type")
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
            or (
                self.event_type != "approval.superseded"
                and (
                    self.event_type
                    in {"approval.approved", "approval.rejected", "approval.consumed"}
                )
                != (self.approval_decision_id is not None)
            )
            or self.artifact_id is not None
            or self.attempt_id is not None
            or self.transition_sequence is not None
        ):
            raise ValueError("approval audit event has invalid subject links")
        elif self.aggregate_type == "runtime_control_denial" and (
            self.event_type != "runtime.control_denied"
            or self.step_id is None
            or self.action_attempt_number is not None
            or self.receipt_id is not None
            or self.approval_request_id is not None
            or self.approval_decision_id is not None
            or self.artifact_id is not None
            or self.attempt_id is not None
            or self.transition_sequence is not None
            or self.previous_state is not None
            or self.new_state is not None
            or self.reason_code is not None
            or self.mutation_version is not None
            or self.outcome is not AuditOutcome.REJECTED
        ):
            raise ValueError("runtime-control denial has an invalid nonmutation shape")
        if not scheduler_aggregate and (
            self.schedule_id is not None or self.occurrence_id is not None
        ):
            raise ValueError("only scheduler audit events may retain scheduler links")
        if type(self.actor_source) is not AuditActorSource:
            raise ValueError("audit actor source is invalid")
        _require_pseudonym(self.actor_id, "audit-actor-v1", "audit actor ID")
        _require_pseudonym(
            self.correlation_id,
            "audit-correlation-v1",
            "audit correlation ID",
        )
        if self.auth_method not in _AUTH_METHODS_BY_SOURCE[self.actor_source]:
            raise ValueError("audit actor source and authentication method disagree")
        if type(self.safe_metadata) is not SealedAuditMetadata:
            raise ValueError("audit metadata must use the validator-issued contract")
        self.safe_metadata.verify_integrity()
        actual_metadata = set(self.safe_metadata.values)
        required_metadata = _EVENT_REQUIRED_METADATA[self.event_type]
        optional_metadata = _EVENT_OPTIONAL_METADATA.get(self.event_type, frozenset())
        if not required_metadata <= actual_metadata or not actual_metadata <= (
            required_metadata | optional_metadata
        ):
            raise ValueError("audit event is missing its exact typed safe metadata")
        require_utc(self.occurred_at, "audit event time")
        if self.safe_metadata.expires_at <= self.occurred_at:
            raise ValueError("audit metadata must expire after the event time")
        optional_identifiers = (
            (self.schedule_id, "audit schedule ID"),
            (self.occurrence_id, "audit occurrence ID"),
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
    payload: dict[str, Any] = {
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
    if draft.schedule_id is not None:
        payload["schedule_id"] = draft.schedule_id
    if draft.occurrence_id is not None:
        payload["occurrence_id"] = draft.occurrence_id
    return payload


def _validate_event_semantics(draft: AuditEventDraft) -> None:
    attempt_link_event = draft.event_type in {
        "run.transition_rejected",
        "attempt.reserved",
        "attempt.completed",
        "artifact.persisted",
    }
    if attempt_link_event != (draft.attempt_id is not None):
        raise ValueError("attempt link does not match its event family")
    artifact_link_event = draft.event_type == "artifact.persisted" or (
        draft.event_type == "attempt.completed" and draft.new_state == "succeeded"
    )
    if artifact_link_event != (draft.artifact_id is not None):
        raise ValueError("artifact link does not match its event family")
    required_decision_witness = draft.event_type in {
        "action.approved",
        "action.rejected",
        "action.dispatch_reserved",
        "approval.approved",
        "approval.rejected",
        "approval.consumed",
    }
    optional_decision_witness = draft.event_type in {
        "action.cancelled",
        "approval.superseded",
    }
    if required_decision_witness != (draft.approval_decision_id is not None) and not (
        optional_decision_witness and draft.approval_decision_id is not None
    ):
        raise ValueError("approval decision link does not match its event family")
    approval_link = draft.aggregate_type == "approval_request" or draft.event_type in {
        "action.approved",
        "action.rejected",
        "action.dispatch_reserved",
        "action.cancelled",
    }
    if approval_link != (draft.approval_request_id is not None):
        raise ValueError("approval request link does not match its event family")
    if draft.reason_code is not None and draft.reason_code not in _SAFE_AUDIT_REASONS:
        raise ValueError("audit reason code is not an allowlisted operational code")
    metadata = draft.safe_metadata.values
    if draft.event_type in {
        "schedule.occurrence_created",
        "schedule.misfire_skipped",
        "schedule.misfire_run_once",
    }:
        scheduled_for_utc = datetime.fromisoformat(metadata["scheduled_for_utc"])
        next_run_at_utc = datetime.fromisoformat(metadata["next_run_at_utc"])
        if next_run_at_utc <= scheduled_for_utc:
            raise ValueError("schedule occurrence audit next run must follow its due time")
        expected_work_admitted = draft.event_type != "schedule.misfire_skipped"
        if metadata["work_admitted"] is not expected_work_admitted:
            raise ValueError("schedule occurrence audit work admission is inconsistent")
        if draft.event_type == "schedule.occurrence_created" and any(
            field_name in metadata
            for field_name in (
                "first_missed_at_utc",
                "last_missed_at_utc",
                "missed_count",
            )
        ):
            raise ValueError("on-time schedule audit must not retain missed-range facts")
        if draft.event_type != "schedule.occurrence_created":
            first_missed_at_utc = datetime.fromisoformat(metadata["first_missed_at_utc"])
            last_missed_at_utc = datetime.fromisoformat(metadata["last_missed_at_utc"])
            if (
                first_missed_at_utc != scheduled_for_utc
                or last_missed_at_utc < first_missed_at_utc
                or last_missed_at_utc >= next_run_at_utc
                or (metadata["missed_count"] == 1) != (last_missed_at_utc == first_missed_at_utc)
            ):
                raise ValueError("schedule misfire audit range is inconsistent")
    elif draft.event_type == "schedule.next_occurrence_persisted":
        if metadata["occurrence_id"] != draft.occurrence_id:
            raise ValueError("schedule advancement audit occurrence identity does not match")
        if metadata["disposition"] not in {"on_time", "skip", "run_once"}:
            raise ValueError("schedule advancement audit disposition is unsupported")
        previous_next_run_at_utc = datetime.fromisoformat(metadata["previous_next_run_at_utc"])
        last_scheduled_at_utc = datetime.fromisoformat(metadata["last_scheduled_at_utc"])
        next_run_at_utc = datetime.fromisoformat(metadata["next_run_at_utc"])
        if (
            previous_next_run_at_utc != last_scheduled_at_utc
            or next_run_at_utc <= last_scheduled_at_utc
        ):
            raise ValueError("schedule advancement audit projection is inconsistent")
    if draft.event_type == "runtime.control_denied":
        if draft.run_id is None:  # pragma: no cover - rejected by the draft shape
            raise AssertionError("runtime-control audit Run identity disappeared")
        expected_aggregate_id = _runtime_control_denial_aggregate_id(
            actor_id=draft.actor_id,
            actor_source=draft.actor_source.value,
            correlation_id=draft.correlation_id,
            run_id=draft.run_id,
            step_id=draft.step_id or "",
            action_id=draft.action_id,
            operation_key=metadata["operation_key"],
            denial_code=metadata["denial_code"],
        )
        if draft.aggregate_id != expected_aggregate_id:
            raise ValueError("runtime-control denial aggregate identity does not match")
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
    elif draft.event_type == "attempt.reserved":
        if (
            draft.mutation_version != 1
            or draft.previous_state is not None
            or draft.new_state != "reserved"
            or draft.reason_code is not None
        ):
            raise ValueError("attempt reservation audit has an invalid initial state")
    elif draft.event_type == "attempt.completed":
        safe_error_present = "safe_error_code" in metadata
        succeeded = draft.new_state == "succeeded"
        if (
            draft.mutation_version != 2
            or draft.previous_state != "reserved"
            or draft.new_state
            not in {"succeeded", "transient_failure", "permanent_failure", "cancelled"}
            or metadata["attempt_outcome"] != draft.new_state
            or succeeded != (draft.artifact_id is not None)
            or safe_error_present == succeeded
            or draft.reason_code is not None
        ):
            raise ValueError("attempt completion audit has an invalid terminal state")
    elif draft.event_type == "artifact.persisted":
        if (
            draft.mutation_version != 1
            or draft.previous_state is not None
            or draft.new_state != "persisted"
            or draft.reason_code is not None
        ):
            raise ValueError("artifact persistence audit has an invalid initial state")
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
            "release_approval": frozenset({("awaiting_approval", "ready")}),
            "start": frozenset({("ready", "executing")}),
            "start_reserved_write": frozenset({("ready", "executing")}),
            "succeed": frozenset({("executing", "succeeded")}),
            "fail": frozenset({("ready", "failed"), ("executing", "failed")}),
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
            "release_approval": "approval_barrier_released",
            "start": "step_execution_started",
            "start_reserved_write": "reserved_write_started",
            "succeed": "step_succeeded",
        }
        fixed_step_reason = fixed_step_reasons.get(command)
        if fixed_step_reason is not None and draft.reason_code != fixed_step_reason:
            raise ValueError("step transition audit reason disagrees with its command")
    elif draft.aggregate_type == "approval_request":
        expected_action_state = {
            "approval.requested": "awaiting_approval",
            "approval.approved": "approved",
            "approval.rejected": "rejected",
            "approval.consumed": "dispatch_reserved",
            "approval.superseded": "cancelled",
            "approval.expired": "awaiting_approval",
            "approval.renewed": "awaiting_approval",
        }[draft.event_type]
        if metadata["action_state"] != expected_action_state:
            raise ValueError("approval audit does not bind the exact action state")
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
        elif draft.event_type in {"approval.approved", "approval.rejected"}:
            expected_status = "approved" if draft.event_type == "approval.approved" else "rejected"
            expected_decision = "approve" if draft.event_type == "approval.approved" else "reject"
            expected_reason = (
                "approval_granted"
                if draft.event_type == "approval.approved"
                else "approval_rejected"
            )
            if (
                draft.mutation_version != 2
                or draft.previous_state != "pending"
                or draft.new_state != expected_status
                or draft.reason_code != expected_reason
                or metadata["decision"] != expected_decision
            ):
                raise ValueError("approval decision audit has an invalid lifecycle shape")
        elif draft.event_type == "approval.consumed":
            if (
                draft.mutation_version != 3
                or draft.previous_state != "approved"
                or draft.new_state != "consumed"
                or draft.reason_code != "approval_consumed"
            ):
                raise ValueError("approval consumption audit has an invalid lifecycle shape")
        elif draft.event_type == "approval.superseded":
            expected_version = 3 if draft.previous_state == "approved" else 2
            expected_decision_link = draft.previous_state == "approved"
            if (
                draft.previous_state not in {"pending", "approved"}
                or draft.mutation_version != expected_version
                or draft.new_state != "superseded"
                or draft.reason_code not in {"approval_set_rejected", "run_cancelled"}
                or (draft.approval_decision_id is not None) != expected_decision_link
                or metadata["supersession_reason"] != draft.reason_code
            ):
                raise ValueError("approval supersession audit has an invalid lifecycle shape")
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
            "action.approved": ("awaiting_approval", "approved", False, False),
            "action.rejected": ("awaiting_approval", "rejected", False, False),
            "action.dispatch_reserved": ("approved", "dispatch_reserved", False, False),
            "action.dispatch_claimed": ("dispatch_reserved", "dispatching", True, False),
            "action.call_started": ("dispatching", "dispatching", True, False),
            "action.retry_released": ("dispatching", "dispatch_reserved", True, False),
            "action.succeeded": ("dispatching", "succeeded", True, True),
            "action.failed": ("dispatching", "failed", True, False),
            "action.outcome_unknown": ("dispatching", "outcome_unknown", True, False),
            "action.receipt_reconciled": ("dispatching", "succeeded", True, True),
            "action.cancelled": (None, "cancelled", False, False),
        }
        previous, current, requires_attempt, requires_receipt = action_shapes[draft.event_type]
        allowed_previous = (
            {"proposed", "approved"}
            if draft.event_type == "action.awaiting_approval"
            else {"awaiting_approval", "approved", "dispatch_reserved", "dispatching"}
            if draft.event_type == "action.cancelled"
            else {previous}
        )
        if draft.event_type == "action.cancelled" and draft.previous_state == "dispatching":
            requires_attempt = True
        if (
            draft.previous_state not in allowed_previous
            or draft.new_state != current
            or requires_attempt != (draft.action_attempt_number is not None)
            or requires_receipt != (draft.receipt_id is not None)
            or (draft.event_type == "action.proposed" and draft.mutation_version != 1)
            or (draft.event_type != "action.proposed" and draft.mutation_version == 1)
            or (
                draft.event_type in {"action.approved", "action.rejected"}
                and (
                    draft.mutation_version is None
                    or draft.mutation_version < 3
                    or draft.mutation_version % 2 != 1
                )
            )
        ):
            raise ValueError("action audit does not match its exact mutation shape")
        expected_conclusions = {
            "action.proposed": None,
            "action.awaiting_approval": None,
            "action.approved": None,
            "action.rejected": None,
            "action.dispatch_reserved": None,
            "action.dispatch_claimed": None,
            "action.call_started": None,
            "action.retry_released": {"pre_call_expired", "provider_retry"},
            "action.succeeded": {"succeeded"},
            "action.failed": {"failed"},
            "action.outcome_unknown": {"outcome_unknown"},
            "action.receipt_reconciled": {"receipt_reconciled"},
            "action.cancelled": None,
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
            "action.approved",
            "action.rejected",
            "action.dispatch_reserved",
            "action.cancelled",
            "action.failed",
            "action.outcome_unknown",
        }
        if reason_required != (draft.reason_code is not None):
            raise ValueError("action audit terminal reason does not match its event")
        expected_approval_reason = {
            "action.awaiting_approval": {"approval_requested", "approval_expired"},
            "action.approved": {"approval_granted"},
            "action.rejected": {"approval_rejected"},
            "action.dispatch_reserved": {"approval_consumed"},
            "action.cancelled": {
                "operator_cancelled",
                "runtime_control_denied",
                "sibling_approval_rejected",
            },
        }.get(draft.event_type)
        if (
            expected_approval_reason is not None
            and draft.reason_code not in expected_approval_reason
        ):
            raise ValueError("action approval audit reason disagrees with its transition")
        if draft.event_type == "action.dispatch_reserved" and (
            draft.mutation_version is None
            or draft.mutation_version < 4
            or draft.mutation_version % 2 != 0
            or metadata["approval_set_id"] is None
            or metadata["approval_use_id"] is None
            or metadata["reservation_id"] is None
        ):
            raise ValueError("action reservation audit has an invalid barrier shape")
        if draft.event_type == "action.cancelled":
            approval_status = metadata["approval_status"]
            cancel_expected_decision_link: bool | None = (
                draft.previous_state == "approved" if approval_status == "superseded" else None
            )
            # Provider retries add claim, call-start, and release mutations, so
            # released work does not retain one stable version parity. The
            # factory already proves the exact previous-to-cancelled increment.
            valid_version = draft.mutation_version is not None and (
                (
                    draft.previous_state == "approved"
                    and draft.mutation_version >= 4
                    and draft.mutation_version % 2 == 0
                )
                or (
                    draft.previous_state == "awaiting_approval"
                    and draft.mutation_version >= 3
                    and draft.mutation_version % 2 == 1
                )
                or (
                    approval_status == "released"
                    and draft.previous_state == "dispatch_reserved"
                    and draft.mutation_version >= 5
                )
                or (
                    approval_status == "released"
                    and draft.previous_state == "dispatching"
                    and draft.mutation_version >= 6
                )
            )
            if (
                not valid_version
                or approval_status not in {"superseded", "expired", "released"}
                or (
                    cancel_expected_decision_link is not None
                    and (draft.approval_decision_id is not None) != cancel_expected_decision_link
                )
                or (
                    approval_status == "released"
                    and (
                        draft.approval_decision_id is None
                        or draft.reason_code not in {"operator_cancelled", "runtime_control_denied"}
                    )
                )
                or (
                    approval_status == "expired"
                    and (
                        draft.previous_state != "awaiting_approval"
                        or draft.reason_code
                        not in {"operator_cancelled", "sibling_approval_rejected"}
                    )
                )
                or metadata["closure_reason"] != draft.reason_code
            ):
                raise ValueError("action cancellation audit has an invalid closure shape")


def _event_fingerprint(draft: AuditEventDraft) -> str:
    return hashlib.sha256(
        _EVENT_FINGERPRINT_DOMAIN + canonical_json_bytes(_draft_fingerprint_payload(draft))
    ).hexdigest()


def _issue_audit_event_draft(**values: Any) -> AuditEventDraft:
    """Narrow issuance seam consumed only by the application audit factory."""

    return AuditEventDraft(**values, _seal=_AUDIT_EVENT_SEAL)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Persisted event with a global identity and optional stable per-Run order."""

    draft: AuditEventDraft
    global_sequence: int
    run_sequence: int | None

    def __post_init__(self) -> None:
        if type(self.draft) is not AuditEventDraft:
            raise ValueError("persisted audit event requires the exact draft contract")
        self.draft.verify_integrity()
        if (
            not isinstance(self.global_sequence, int)
            or isinstance(self.global_sequence, bool)
            or self.global_sequence < 1
        ):
            raise ValueError("internal audit row identity must be positive")
        if (self.draft.run_id is None) != (self.run_sequence is None):
            raise ValueError("audit Run identity and sequence must be present together")
        if self.run_sequence is not None and (
            not isinstance(self.run_sequence, int)
            or isinstance(self.run_sequence, bool)
            or self.run_sequence < 1
        ):
            raise ValueError("run audit sequence must be positive")

    def __getattr__(self, name: str) -> Any:
        """Expose immutable draft fields without duplicating the persisted contract."""

        return getattr(self.draft, name)
