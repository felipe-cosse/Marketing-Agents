"""Event-specific, SAFE-07-redacted audit metadata issuance."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from marketing_agents.domain.audit import (
    RUNTIME_CONTROL_DENIAL_CODES,
    SealedAuditMetadata,
    _issue_sealed_audit_metadata,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.execution_control import SAFE_ATTEMPT_ERROR_CODES
from marketing_agents.domain.retention import RetentionCategory, RetentionPolicy
from marketing_agents.domain.validation import require_digest, require_id, require_utc

from .redaction import redact

MAX_AUDIT_METADATA_BYTES = 8_192
MAX_AUDIT_METADATA_DEPTH = 8
MAX_AUDIT_METADATA_KEYS = 64
MAX_AUDIT_METADATA_ARRAY = 256

_RUN_FIELDS = frozenset({"command"})
_PLAN_FIELDS = frozenset(
    {
        "catalog_content_hash",
        "graph_hash",
        "plan_hash",
        "routing_hash",
        "step_count",
        "workflow_definition_hash",
        "workflow_id",
        "workflow_version",
    }
)
_STEP_FIELDS = frozenset(
    {
        "configuration_revision",
        "ordinal",
        "step_kind",
        "template_id",
        "terminal_result",
    }
)
_ACTION_FIELDS = frozenset({"conclusion", "connector_status", "idempotency_support"})
_ATTEMPT_IDENTITY_FIELDS = frozenset({"attempt_kind", "attempt_number", "operation_key"})
_EVENT_FIELDS: Mapping[str, frozenset[str]] = {
    "run.received": _RUN_FIELDS | frozenset({"catalog_content_hash"}),
    "run.transitioned": _RUN_FIELDS,
    "run.transition_rejected": _RUN_FIELDS,
    "run.plan_recorded": _RUN_FIELDS | _PLAN_FIELDS,
    "step.recorded": _STEP_FIELDS | _PLAN_FIELDS,
    "step.transitioned": _RUN_FIELDS | _STEP_FIELDS,
    "attempt.reserved": _ATTEMPT_IDENTITY_FIELDS
    | frozenset({"input_classification", "input_schema_id"}),
    "attempt.completed": _ATTEMPT_IDENTITY_FIELDS
    | frozenset({"attempt_outcome", "safe_error_code"}),
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
    "action.retry_released": _ACTION_FIELDS,
    "action.succeeded": _ACTION_FIELDS,
    "action.failed": _ACTION_FIELDS,
    "action.outcome_unknown": _ACTION_FIELDS,
    "action.receipt_reconciled": _ACTION_FIELDS,
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
    "runtime.control_denied": frozenset({"denial_code", "operation_key", "retry_after_seconds"}),
}
_DIGEST_FIELDS = frozenset({"graph_hash", "plan_hash", "routing_hash", "workflow_definition_hash"})
_POSITIVE_INTEGER_FIELDS = frozenset(
    {
        "action_version",
        "attempt_number",
        "configuration_revision",
        "generation",
        "ordinal",
        "proposal_revision",
        "retry_after_seconds",
        "step_count",
        "workflow_version",
    }
)
_BOOLEAN_FIELDS = frozenset({"terminal_result"})
_CONNECTOR_STATUSES = frozenset(
    {"accepted", "completed", "mock_committed", "mock_succeeded", "succeeded"}
)
_IDEMPOTENCY_SUPPORT = frozenset({"required", "supported", "unavailable"})
_APPROVAL_STATUSES = frozenset(
    {"pending", "approved", "rejected", "expired", "consumed", "superseded"}
)
_APPROVAL_ACTION_STATES = frozenset(
    {"awaiting_approval", "approved", "rejected", "dispatch_reserved", "cancelled"}
)
_APPROVAL_DECISIONS = frozenset({"approve", "reject"})
_CLOSURE_REASONS = frozenset(
    {"operator_cancelled", "runtime_control_denied", "sibling_approval_rejected"}
)
_SUPERSESSION_REASONS = frozenset(
    {"approval_set_rejected", "approval_set_superseded", "run_cancelled"}
)
_COMMANDS = frozenset(
    {
        "activate_plan",
        "cancel",
        "complete",
        "fail",
        "initialize",
        "mark_ready",
        "mark_validated",
        "receive",
        "record_plan",
        "release_approval",
        "release_approved_plan",
        "reject",
        "reject_approval",
        "skip",
        "start",
        "start_reserved_write",
        "succeed",
        "wait_for_approval",
    }
)
_CONCLUSIONS = frozenset(
    {
        "failed",
        "outcome_unknown",
        "pre_call_expired",
        "provider_retry",
        "receipt_reconciled",
        "succeeded",
    }
)
_ATTEMPT_KINDS = frozenset({"model", "tool"})
_ATTEMPT_OUTCOMES = frozenset({"succeeded", "transient_failure", "permanent_failure", "cancelled"})
_DATA_CLASSIFICATIONS = frozenset(item.value for item in DataClassification)


class AuditMetadataError(ValueError):
    """Stable fail-closed error before timeline sequence allocation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _measure(value: Any, *, depth: int = 1) -> tuple[int, int]:
    if depth > MAX_AUDIT_METADATA_DEPTH:
        raise AuditMetadataError("metadata_too_deep", "audit metadata nesting is too deep")
    if isinstance(value, Mapping):
        if len(value) > MAX_AUDIT_METADATA_KEYS:
            raise AuditMetadataError("metadata_too_many_keys", "audit metadata has too many keys")
        keys = len(value)
        array_items = 0
        for item in value.values():
            child_keys, child_items = _measure(item, depth=depth + 1)
            keys += child_keys
            array_items += child_items
        if keys > MAX_AUDIT_METADATA_KEYS:
            raise AuditMetadataError("metadata_too_many_keys", "audit metadata has too many keys")
        return keys, array_items
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_AUDIT_METADATA_ARRAY:
            raise AuditMetadataError(
                "metadata_array_too_large", "audit metadata array is too large"
            )
        keys = 0
        array_items = len(value)
        for item in value:
            child_keys, child_items = _measure(item, depth=depth + 1)
            keys += child_keys
            array_items += child_items
        if array_items > MAX_AUDIT_METADATA_ARRAY:
            raise AuditMetadataError(
                "metadata_array_too_large", "audit metadata array is too large"
            )
        return keys, array_items
    return 0, 0


def _validate_catalog_hash(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.startswith("catalog-sha256-v1:"):
        raise ValueError(f"{field_name} must use the catalog hash version")
    require_digest(value.removeprefix("catalog-sha256-v1:"), field_name)


def _validate_schema_hash(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.startswith("schema-sha256-v1:"):
        raise ValueError(f"{field_name} must use the schema hash version")
    require_digest(value.removeprefix("schema-sha256-v1:"), field_name)


def _validate_positive_integer(value: Any, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_typed_value(field_name: str, value: Any) -> None:
    validator: Callable[[Any, str], None]
    if field_name in _DIGEST_FIELDS:
        validator = require_digest
    elif field_name == "catalog_content_hash":
        validator = _validate_catalog_hash
    elif field_name == "output_schema_hash":
        validator = _validate_schema_hash
    elif field_name in _POSITIVE_INTEGER_FIELDS:
        validator = _validate_positive_integer
    elif field_name in _BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise AuditMetadataError(
                "metadata_value_invalid", f"audit metadata {field_name} must be boolean"
            )
        return
    else:
        validator = require_id
    try:
        validator(value, f"audit metadata {field_name}")
    except (TypeError, ValueError) as exc:
        raise AuditMetadataError(
            "metadata_value_invalid",
            f"audit metadata {field_name} has an invalid safe value",
        ) from exc
    if field_name == "connector_status" and value not in _CONNECTOR_STATUSES:
        raise AuditMetadataError(
            "metadata_value_invalid", "connector status is not an internal safe code"
        )
    if field_name == "idempotency_support" and value not in _IDEMPOTENCY_SUPPORT:
        raise AuditMetadataError(
            "metadata_value_invalid", "idempotency support is not a safe persisted class"
        )
    if field_name == "command" and value not in _COMMANDS:
        raise AuditMetadataError(
            "metadata_value_invalid", "lifecycle command is not an allowlisted value"
        )
    if field_name == "conclusion" and value not in _CONCLUSIONS:
        raise AuditMetadataError(
            "metadata_value_invalid", "action conclusion is not an allowlisted value"
        )
    if field_name == "denial_code" and value not in RUNTIME_CONTROL_DENIAL_CODES:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "runtime-control denial is not an allowlisted safe code",
        )
    if field_name == "safe_error_code" and value not in SAFE_ATTEMPT_ERROR_CODES:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "attempt error is not an allowlisted safe code",
        )
    if field_name == "attempt_kind" and value not in _ATTEMPT_KINDS:
        raise AuditMetadataError(
            "metadata_value_invalid", "attempt kind is not an allowlisted safe code"
        )
    if field_name == "attempt_outcome" and value not in _ATTEMPT_OUTCOMES:
        raise AuditMetadataError(
            "metadata_value_invalid", "attempt outcome is not an allowlisted safe code"
        )
    if field_name in {"input_classification", "data_classification"} and (
        value not in _DATA_CLASSIFICATIONS
    ):
        raise AuditMetadataError(
            "metadata_value_invalid",
            "data classification is not an allowlisted safe code",
        )
    if field_name == "retry_after_seconds" and value > 3_600:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "runtime-control retry-after exceeds its safe bound",
        )
    if field_name == "status" and value not in _APPROVAL_STATUSES:
        raise AuditMetadataError(
            "metadata_value_invalid", "approval status is not an allowlisted safe code"
        )
    if field_name == "action_state" and value not in _APPROVAL_ACTION_STATES:
        raise AuditMetadataError(
            "metadata_value_invalid", "approval action state is not an allowlisted safe code"
        )
    if field_name == "decision" and value not in _APPROVAL_DECISIONS:
        raise AuditMetadataError(
            "metadata_value_invalid", "approval decision is not an allowlisted safe code"
        )
    if field_name == "closure_reason" and value not in _CLOSURE_REASONS:
        raise AuditMetadataError(
            "metadata_value_invalid", "action closure reason is not an allowlisted safe code"
        )
    if field_name == "supersession_reason" and value not in _SUPERSESSION_REASONS:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "approval supersession reason is not an allowlisted safe code",
        )


def seal_audit_metadata(
    event_type: str,
    metadata: Mapping[str, Any],
    *,
    occurred_at: datetime,
    classification: DataClassification = DataClassification.INTERNAL,
    retention_policy: RetentionPolicy | None = None,
) -> SealedAuditMetadata:
    """Type-check, redact, bound, and recursively freeze one event metadata object."""

    require_utc(occurred_at, "audit metadata event time")
    allowed = _EVENT_FIELDS.get(event_type)
    if allowed is None:
        raise AuditMetadataError("event_type_unsupported", "audit event type is not registered")
    if not isinstance(metadata, Mapping):
        raise AuditMetadataError("metadata_not_object", "audit metadata must be an object")
    unknown = set(metadata) - allowed
    if unknown:
        raise AuditMetadataError(
            "metadata_field_forbidden",
            f"audit metadata field is not allowlisted for {event_type}",
        )
    if classification in {DataClassification.PERSONAL, DataClassification.SENSITIVE}:
        safe: Mapping[str, Any] = {}
    else:
        for field_name, value in metadata.items():
            _validate_typed_value(field_name, value)
        redacted = redact(metadata)
        if not isinstance(redacted, Mapping):  # pragma: no cover - source is a mapping
            raise AssertionError("central redactor changed audit object shape")
        safe = redacted
    _measure(safe)
    if len(canonical_json_bytes(safe)) > MAX_AUDIT_METADATA_BYTES:
        raise AuditMetadataError("metadata_too_large", "audit metadata exceeds its byte limit")
    policy = retention_policy or RetentionPolicy()
    expires_at = policy.expires_at(
        RetentionCategory.AUDIT_METADATA,
        occurred_at,
        classification,
    )
    return _issue_sealed_audit_metadata(safe, classification, expires_at)


def hydrate_audit_metadata(
    event_type: str,
    stored_values: Mapping[str, Any],
    *,
    classification: DataClassification,
    occurred_at: datetime,
    expires_at: datetime,
) -> SealedAuditMetadata:
    """Revalidate persisted metadata without recomputing its historical retention policy."""

    require_utc(occurred_at, "persisted audit event time")
    require_utc(expires_at, "persisted audit metadata expiry")
    if expires_at <= occurred_at:
        raise AuditMetadataError(
            "metadata_expiry_invalid", "persisted audit metadata expiry is not future"
        )
    allowed = _EVENT_FIELDS.get(event_type)
    if allowed is None:
        raise AuditMetadataError("event_type_unsupported", "audit event type is not registered")
    if not isinstance(stored_values, Mapping):
        raise AuditMetadataError("metadata_not_object", "audit metadata must be an object")
    if set(stored_values) - allowed:
        raise AuditMetadataError(
            "metadata_field_forbidden", "persisted audit metadata contains a forbidden field"
        )
    if classification in {DataClassification.PERSONAL, DataClassification.SENSITIVE}:
        if stored_values:
            raise AuditMetadataError(
                "metadata_redaction_invalid",
                "classified persisted audit metadata must be wholly redacted",
            )
    elif classification is DataClassification.SECRET:
        raise AuditMetadataError(
            "metadata_classification_invalid", "secret audit metadata is never retainable"
        )
    else:
        for field_name, value in stored_values.items():
            _validate_typed_value(field_name, value)
        if redact(stored_values) != dict(stored_values):
            raise AuditMetadataError(
                "metadata_redaction_invalid", "persisted audit metadata is not safely redacted"
            )
    _measure(stored_values)
    if len(canonical_json_bytes(stored_values)) > MAX_AUDIT_METADATA_BYTES:
        raise AuditMetadataError("metadata_too_large", "audit metadata exceeds its byte limit")
    return _issue_sealed_audit_metadata(stored_values, classification, expires_at)
