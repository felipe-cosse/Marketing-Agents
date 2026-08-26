"""Event-specific, SAFE-07-redacted audit metadata issuance."""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
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
from marketing_agents.domain.validation import (
    require_digest,
    require_iana_timezone,
    require_id,
    require_utc,
)

from .digest_key import DigestKey
from .redaction import is_sensitive_key, redact

MAX_AUDIT_METADATA_BYTES = 8_192
MAX_AUDIT_METADATA_DEPTH = 8
MAX_AUDIT_METADATA_KEYS = 64
MAX_AUDIT_METADATA_ARRAY = 256
MAX_INSTANCE_CONFIGURATION_AUDIT_METADATA_BYTES = 65_536
MAX_INSTANCE_CONFIGURATION_AUDIT_METADATA_KEYS = 512

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
_SCHEDULE_OCCURRENCE_FIELDS = frozenset(
    {
        "claim_fingerprint",
        "next_run_at_utc",
        "recurrence_version",
        "scheduled_for_utc",
        "work_admitted",
    }
)
_SCHEDULE_MISFIRE_FIELDS = _SCHEDULE_OCCURRENCE_FIELDS | frozenset(
    {
        "first_missed_at_utc",
        "last_missed_at_utc",
        "missed_count",
    }
)
_INSTANCE_CONFIGURATION_AUDIT_FIELDS = frozenset(
    {
        "new_configuration",
        "new_revision",
        "previous_configuration",
        "previous_revision",
    }
)
_MANUAL_WORK_AUDIT_FIELDS = frozenset(
    {
        "configuration_revision",
        "instance_id",
        "manual_attempt_id",
        "mode",
        "receipt_disposition",
        "trigger_id",
        "work_item_id",
        "workflow_id",
    }
)
_SCHEMA_REJECTION_AUDIT_FIELDS = frozenset(
    {
        "configuration_revision",
        "instance_id",
        "manual_attempt_id",
        "mode",
        "rejection_code",
        "trigger_id",
        "workflow_id",
    }
)
_WEBHOOK_SIGNATURE_AUDIT_FIELDS = frozenset({"source", "trigger_id", "webhook_attempt_id"})
_WEBHOOK_RECEIPT_AUDIT_FIELDS = _WEBHOOK_SIGNATURE_AUDIT_FIELDS | frozenset(
    {"receipt_disposition", "target_count", "webhook_receipt_id"}
)
_WEBHOOK_SCHEMA_REJECTION_AUDIT_FIELDS = _WEBHOOK_SIGNATURE_AUDIT_FIELDS | frozenset(
    {
        "configuration_revision",
        "instance_id",
        "rejection_code",
        "workflow_id",
    }
)
_EVENT_FIELDS: Mapping[str, frozenset[str]] = {
    "ingress.manual_received": _MANUAL_WORK_AUDIT_FIELDS,
    "ingress.schema_rejected": _SCHEMA_REJECTION_AUDIT_FIELDS,
    "webhook.signature_validated": _WEBHOOK_SIGNATURE_AUDIT_FIELDS,
    "webhook.signature_rejected": _WEBHOOK_SIGNATURE_AUDIT_FIELDS,
    "webhook.received": _WEBHOOK_RECEIPT_AUDIT_FIELDS,
    "webhook.duplicate_suppressed": _WEBHOOK_RECEIPT_AUDIT_FIELDS,
    "webhook.idempotency_collision": _WEBHOOK_RECEIPT_AUDIT_FIELDS,
    "webhook.schema_rejected": _WEBHOOK_SCHEMA_REJECTION_AUDIT_FIELDS,
    "work.created": _MANUAL_WORK_AUDIT_FIELDS,
    "work.duplicate_returned": _MANUAL_WORK_AUDIT_FIELDS,
    "work.idempotency_collision": _MANUAL_WORK_AUDIT_FIELDS,
    "instance.configuration_changed": _INSTANCE_CONFIGURATION_AUDIT_FIELDS,
    "schedule.occurrence_created": _SCHEDULE_OCCURRENCE_FIELDS,
    "schedule.misfire_skipped": _SCHEDULE_MISFIRE_FIELDS,
    "schedule.misfire_run_once": _SCHEDULE_MISFIRE_FIELDS,
    "schedule.next_occurrence_persisted": frozenset(
        {
            "claim_fingerprint",
            "disposition",
            "last_scheduled_at_utc",
            "next_run_at_utc",
            "occurrence_id",
            "previous_next_run_at_utc",
        }
    ),
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
_DIGEST_FIELDS = frozenset(
    {
        "claim_fingerprint",
        "graph_hash",
        "plan_hash",
        "routing_hash",
        "workflow_definition_hash",
    }
)
_POSITIVE_INTEGER_FIELDS = frozenset(
    {
        "action_version",
        "attempt_number",
        "configuration_revision",
        "generation",
        "missed_count",
        "ordinal",
        "new_revision",
        "previous_revision",
        "proposal_revision",
        "retry_after_seconds",
        "step_count",
        "target_count",
        "workflow_version",
    }
)
_BOOLEAN_FIELDS = frozenset({"terminal_result", "work_admitted"})
_UTC_TIMESTAMP_FIELDS = frozenset(
    {
        "first_missed_at_utc",
        "last_missed_at_utc",
        "last_scheduled_at_utc",
        "next_run_at_utc",
        "previous_next_run_at_utc",
        "scheduled_for_utc",
    }
)
_SCHEDULE_DISPOSITIONS = frozenset({"on_time", "skip", "run_once"})
_MANUAL_RECEIPT_DISPOSITIONS = frozenset({"created", "replayed", "collision"})
_MANUAL_WORK_MODES = frozenset({"dry_run", "mock_execution"})
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
_DEPLOYMENT_CONFIGURATION_FIELDS = frozenset(
    {
        "connector_bindings",
        "enabled",
        "schedule",
        "trigger_bindings",
        "variant_label",
    }
)
_TRIGGER_BINDING_FIELDS = frozenset(
    {
        "cron",
        "enabled",
        "event_source",
        "misfire_grace_seconds",
        "misfire_policy",
        "timezone",
        "type",
    }
)
_CONNECTOR_BINDING_FIELDS = frozenset({"binding_id", "connector_family", "enabled"})
_SCHEDULE_FIELDS = frozenset({"cron", "misfire_grace_seconds", "misfire_policy", "timezone"})
_SAFE_CONFIGURATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_CONNECTOR_FAMILY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CONNECTOR_BINDING_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_AUDIT_CONFIGURATION_TEXT_DOMAIN = b"marketing-agents:audit-configuration-text:hmac-sha256:v1\x00"
_AUDIT_CONFIGURATION_TEXT_PREFIX = "audit-value-hmac-sha256-v1:"
_AUDIT_CONFIGURATION_TEXT_PATTERN = re.compile(
    rf"^{re.escape(_AUDIT_CONFIGURATION_TEXT_PREFIX)}[0-9a-f]{{64}}$"
)


class AuditMetadataError(ValueError):
    """Stable fail-closed error before timeline sequence allocation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _measure(
    value: Any,
    *,
    depth: int = 1,
    maximum_keys: int = MAX_AUDIT_METADATA_KEYS,
    maximum_array_items: int = MAX_AUDIT_METADATA_ARRAY,
) -> tuple[int, int]:
    if depth > MAX_AUDIT_METADATA_DEPTH:
        raise AuditMetadataError("metadata_too_deep", "audit metadata nesting is too deep")
    if isinstance(value, Mapping):
        if len(value) > maximum_keys:
            raise AuditMetadataError("metadata_too_many_keys", "audit metadata has too many keys")
        keys = len(value)
        array_items = 0
        for item in value.values():
            child_keys, child_items = _measure(
                item,
                depth=depth + 1,
                maximum_keys=maximum_keys,
                maximum_array_items=maximum_array_items,
            )
            keys += child_keys
            array_items += child_items
        if keys > maximum_keys:
            raise AuditMetadataError("metadata_too_many_keys", "audit metadata has too many keys")
        return keys, array_items
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > maximum_array_items:
            raise AuditMetadataError(
                "metadata_array_too_large", "audit metadata array is too large"
            )
        keys = 0
        array_items = len(value)
        for item in value:
            child_keys, child_items = _measure(
                item,
                depth=depth + 1,
                maximum_keys=maximum_keys,
                maximum_array_items=maximum_array_items,
            )
            keys += child_keys
            array_items += child_items
        if array_items > maximum_array_items:
            raise AuditMetadataError(
                "metadata_array_too_large", "audit metadata array is too large"
            )
        return keys, array_items
    return 0, 0


def _metadata_limits(event_type: str) -> tuple[int, int]:
    if event_type == "instance.configuration_changed":
        return (
            MAX_INSTANCE_CONFIGURATION_AUDIT_METADATA_KEYS,
            MAX_INSTANCE_CONFIGURATION_AUDIT_METADATA_BYTES,
        )
    return MAX_AUDIT_METADATA_KEYS, MAX_AUDIT_METADATA_BYTES


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


def _validate_canonical_utc(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        require_utc(parsed, field_name)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a canonical UTC timestamp") from exc
    if parsed.astimezone(UTC).isoformat(timespec="microseconds") != value:
        raise ValueError(f"{field_name} must be a canonical UTC timestamp")


def _configuration_error(message: str) -> AuditMetadataError:
    return AuditMetadataError("metadata_value_invalid", message)


def _pseudonymize_configuration_text(
    value: Any,
    *,
    field_domain: str,
    key: DigestKey,
) -> Any:
    if type(value) is not str:
        return value
    digest = hmac.new(
        key.bytes_for_digest(),
        _AUDIT_CONFIGURATION_TEXT_DOMAIN
        + field_domain.encode("ascii")
        + b"\x00"
        + canonical_json_bytes(value),
        hashlib.sha256,
    ).hexdigest()
    return f"{_AUDIT_CONFIGURATION_TEXT_PREFIX}{digest}"


def _pseudonymize_configuration_snapshot(value: Any, key: DigestKey) -> Any:
    """Copy one validated deployment snapshot while pseudonymizing operator text."""

    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    if "variant_label" in result:
        result["variant_label"] = _pseudonymize_configuration_text(
            result["variant_label"],
            field_domain="variant_label",
            key=key,
        )
    trigger_bindings = result.get("trigger_bindings")
    if isinstance(trigger_bindings, Sequence) and not isinstance(
        trigger_bindings, (str, bytes, bytearray)
    ):
        sanitized_triggers: list[Any] = []
        for binding in trigger_bindings:
            if not isinstance(binding, Mapping):
                sanitized_triggers.append(binding)
                continue
            sanitized = dict(binding)
            for field_name in ("event_source", "cron", "timezone"):
                if field_name in sanitized:
                    sanitized[field_name] = _pseudonymize_configuration_text(
                        sanitized[field_name],
                        field_domain=field_name,
                        key=key,
                    )
            sanitized_triggers.append(sanitized)
        result["trigger_bindings"] = sanitized_triggers
    connector_bindings = result.get("connector_bindings")
    if isinstance(connector_bindings, Mapping):
        sanitized_connectors: dict[Any, Any] = {}
        for slot, binding in connector_bindings.items():
            if not isinstance(binding, Mapping):
                sanitized_connectors[slot] = binding
                continue
            sanitized = dict(binding)
            for field_name in ("binding_id",):
                if field_name in sanitized:
                    sanitized[field_name] = _pseudonymize_configuration_text(
                        sanitized[field_name],
                        field_domain=field_name,
                        key=key,
                    )
            sanitized_connectors[slot] = sanitized
        result["connector_bindings"] = sanitized_connectors
    schedule = result.get("schedule")
    if isinstance(schedule, Mapping):
        sanitized_schedule = dict(schedule)
        for field_name in ("cron", "timezone"):
            if field_name in sanitized_schedule:
                sanitized_schedule[field_name] = _pseudonymize_configuration_text(
                    sanitized_schedule[field_name],
                    field_domain=field_name,
                    key=key,
                )
        result["schedule"] = sanitized_schedule
    return result


def _pseudonymize_instance_configuration_metadata(
    event_type: str,
    metadata: Mapping[str, Any],
    key: DigestKey,
) -> Mapping[str, Any]:
    if event_type != "instance.configuration_changed":
        return metadata
    result = dict(metadata)
    for field_name in ("previous_configuration", "new_configuration"):
        if field_name in result:
            result[field_name] = _pseudonymize_configuration_snapshot(result[field_name], key)
    return result


def _require_exact_configuration_fields(
    value: Any,
    expected: frozenset[str],
    field_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise _configuration_error(f"{field_name} must be a canonical JSON object")
    actual = set(value)
    if actual != expected:
        raise _configuration_error(f"{field_name} must contain only its exact safe fields")
    if any(is_sensitive_key(key) for key in value):
        raise _configuration_error(f"{field_name} contains a sensitive field name")
    return value


def _require_bounded_configuration_text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise _configuration_error(f"{field_name} must be nonempty, trimmed, and bounded")
    return value


def _require_pseudonymous_configuration_text(
    value: Any,
    field_name: str,
) -> str:
    if type(value) is not str or _AUDIT_CONFIGURATION_TEXT_PATTERN.fullmatch(value) is None:
        raise _configuration_error(f"{field_name} must use the keyed audit pseudonym scheme")
    return value


def _require_optional_configuration_text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    stored_representation: bool,
) -> str | None:
    if value is None:
        return None
    if stored_representation:
        return _require_pseudonymous_configuration_text(value, field_name)
    return _require_bounded_configuration_text(value, field_name, maximum=maximum)


def _require_configuration_boolean(value: Any, field_name: str) -> None:
    if type(value) is not bool:
        raise _configuration_error(f"{field_name} must be boolean")


def _require_misfire_grace(value: Any, field_name: str, *, optional: bool) -> None:
    if value is None and optional:
        return
    if type(value) is not int or not 0 <= value <= 86_400:
        raise _configuration_error(f"{field_name} must be an integer from 0 through 86400")


def _validate_trigger_bindings(
    value: Any,
    field_name: str,
    *,
    stored_representation: bool,
) -> tuple[Mapping[str, Any], ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > 16
    ):
        raise _configuration_error(f"{field_name} must be a bounded JSON array")
    validated: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        item_name = f"{field_name}[{index}]"
        binding = _require_exact_configuration_fields(
            item,
            _TRIGGER_BINDING_FIELDS,
            item_name,
        )
        trigger_type = binding["type"]
        if trigger_type not in {"manual", "webhook", "schedule"}:
            raise _configuration_error(f"{item_name}.type is unsupported")
        _require_configuration_boolean(binding["enabled"], f"{item_name}.enabled")
        event_source = _require_optional_configuration_text(
            binding["event_source"],
            f"{item_name}.event_source",
            maximum=100,
            stored_representation=stored_representation,
        )
        if (
            not stored_representation
            and event_source is not None
            and _SAFE_CONFIGURATION_ID.fullmatch(event_source) is None
        ):
            raise _configuration_error(f"{item_name}.event_source must be a stable safe identifier")
        _require_optional_configuration_text(
            binding["cron"],
            f"{item_name}.cron",
            maximum=100,
            stored_representation=stored_representation,
        )
        timezone = _require_optional_configuration_text(
            binding["timezone"],
            f"{item_name}.timezone",
            maximum=100,
            stored_representation=stored_representation,
        )
        if not stored_representation and timezone is not None:
            try:
                require_iana_timezone(timezone, f"{item_name}.timezone")
            except (TypeError, ValueError):
                raise _configuration_error(
                    f"{item_name}.timezone must be a valid IANA key"
                ) from None
        if binding["misfire_policy"] not in {None, "skip", "run_once"}:
            raise _configuration_error(f"{item_name}.misfire_policy is unsupported")
        _require_misfire_grace(
            binding["misfire_grace_seconds"],
            f"{item_name}.misfire_grace_seconds",
            optional=True,
        )
        schedule_values = (
            binding["cron"],
            binding["timezone"],
            binding["misfire_policy"],
            binding["misfire_grace_seconds"],
        )
        if trigger_type == "manual":
            if event_source is not None or any(item is not None for item in schedule_values):
                raise _configuration_error(f"{item_name} manual trigger has forbidden parameters")
        elif trigger_type == "webhook":
            if event_source is None or any(item is not None for item in schedule_values):
                raise _configuration_error(f"{item_name} webhook trigger has invalid parameters")
        else:
            if event_source is not None:
                raise _configuration_error(
                    f"{item_name} schedule trigger has a forbidden event source"
                )
            supplied_schedule_values = sum(item is not None for item in schedule_values)
            if binding["enabled"] is True and supplied_schedule_values != len(schedule_values):
                raise _configuration_error(
                    f"{item_name} enabled schedule trigger requires complete parameters"
                )
            if binding["enabled"] is False and supplied_schedule_values != 0:
                raise _configuration_error(
                    f"{item_name} disabled schedule trigger retains parameters"
                )
        validated.append(binding)
    trigger_types = tuple(binding["type"] for binding in validated)
    if len(trigger_types) != len(set(trigger_types)):
        raise _configuration_error(f"{field_name} trigger types must be unique")
    return tuple(validated)


def _validate_connector_bindings(
    value: Any,
    field_name: str,
    *,
    stored_representation: bool,
) -> None:
    if not isinstance(value, Mapping) or len(value) > 16:
        raise _configuration_error(f"{field_name} must be a bounded JSON object")
    for slot, item in value.items():
        if type(slot) is not str or _SAFE_CONFIGURATION_ID.fullmatch(slot) is None:
            raise _configuration_error(f"{field_name} contains an unsafe binding slot")
        item_name = f"{field_name}.{slot}"
        binding = _require_exact_configuration_fields(
            item,
            _CONNECTOR_BINDING_FIELDS,
            item_name,
        )
        family = _require_bounded_configuration_text(
            binding["connector_family"],
            f"{item_name}.connector_family",
            maximum=100,
        )
        if _CONNECTOR_FAMILY.fullmatch(family) is None:
            raise _configuration_error(f"{item_name}.connector_family is invalid")
        if slot != family:
            raise _configuration_error(f"{item_name}.connector_family must match its binding key")
        if stored_representation:
            binding_id = _require_pseudonymous_configuration_text(
                binding["binding_id"],
                f"{item_name}.binding_id",
            )
        else:
            binding_id = _require_bounded_configuration_text(
                binding["binding_id"],
                f"{item_name}.binding_id",
                maximum=120,
            )
        if not stored_representation and _CONNECTOR_BINDING_ID.fullmatch(binding_id) is None:
            raise _configuration_error(f"{item_name}.binding_id must be a stable safe identifier")
        _require_configuration_boolean(binding["enabled"], f"{item_name}.enabled")


def _validate_schedule(
    value: Any,
    field_name: str,
    *,
    stored_representation: bool,
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    schedule = _require_exact_configuration_fields(value, _SCHEDULE_FIELDS, field_name)
    cron = schedule["cron"]
    timezone = schedule["timezone"]
    if stored_representation:
        _require_pseudonymous_configuration_text(cron, f"{field_name}.cron")
        _require_pseudonymous_configuration_text(timezone, f"{field_name}.timezone")
    else:
        _require_bounded_configuration_text(cron, f"{field_name}.cron", maximum=100)
        _require_bounded_configuration_text(timezone, f"{field_name}.timezone", maximum=100)
        try:
            require_iana_timezone(timezone, f"{field_name}.timezone")
        except (TypeError, ValueError):
            raise _configuration_error(f"{field_name}.timezone must be a valid IANA key") from None
    if schedule["misfire_policy"] not in {"skip", "run_once"}:
        raise _configuration_error(f"{field_name}.misfire_policy is unsupported")
    _require_misfire_grace(
        schedule["misfire_grace_seconds"],
        f"{field_name}.misfire_grace_seconds",
        optional=False,
    )
    return schedule


def _validate_deployment_configuration(
    value: Any,
    field_name: str,
    *,
    stored_representation: bool,
) -> None:
    configuration = _require_exact_configuration_fields(
        value,
        _DEPLOYMENT_CONFIGURATION_FIELDS,
        field_name,
    )
    _require_configuration_boolean(configuration["enabled"], f"{field_name}.enabled")
    variant_label = _require_optional_configuration_text(
        configuration["variant_label"],
        f"{field_name}.variant_label",
        maximum=100,
        stored_representation=stored_representation,
    )
    if (
        not stored_representation
        and variant_label is not None
        and unicodedata.normalize("NFC", variant_label) != variant_label
    ):
        raise _configuration_error(f"{field_name}.variant_label must use NFC normalization")
    triggers = _validate_trigger_bindings(
        configuration["trigger_bindings"],
        f"{field_name}.trigger_bindings",
        stored_representation=stored_representation,
    )
    _validate_connector_bindings(
        configuration["connector_bindings"],
        f"{field_name}.connector_bindings",
        stored_representation=stored_representation,
    )
    schedule = _validate_schedule(
        configuration["schedule"],
        f"{field_name}.schedule",
        stored_representation=stored_representation,
    )
    schedule_triggers = tuple(item for item in triggers if item["type"] == "schedule")
    enabled_schedule = len(schedule_triggers) == 1 and schedule_triggers[0]["enabled"] is True
    if (schedule is not None) is not enabled_schedule:
        raise _configuration_error(
            f"{field_name} schedule and enabled schedule trigger must appear together"
        )
    if enabled_schedule:
        trigger = schedule_triggers[0]
        assert schedule is not None
        if (
            trigger["cron"] != schedule["cron"]
            or trigger["timezone"] != schedule["timezone"]
            or trigger["misfire_policy"] != schedule["misfire_policy"]
            or trigger["misfire_grace_seconds"] != schedule["misfire_grace_seconds"]
        ):
            raise _configuration_error(
                f"{field_name} schedule trigger parameters must match the schedule"
            )
    try:
        canonical = canonical_json_bytes(configuration)
        redacted = canonical_json_bytes(redact(configuration))
    except (TypeError, ValueError) as exc:
        raise _configuration_error(f"{field_name} must be canonical JSON") from exc
    if stored_representation and canonical != redacted:
        raise _configuration_error(f"{field_name} contains material requiring redaction")


def _validate_typed_value(
    field_name: str,
    value: Any,
    *,
    stored_configuration: bool = False,
) -> None:
    validator: Callable[[Any, str], None]
    if field_name in {"new_configuration", "previous_configuration"}:
        _validate_deployment_configuration(
            value,
            f"audit metadata {field_name}",
            stored_representation=stored_configuration,
        )
        return
    if field_name in _DIGEST_FIELDS:
        validator = require_digest
    elif field_name == "catalog_content_hash":
        validator = _validate_catalog_hash
    elif field_name == "output_schema_hash":
        validator = _validate_schema_hash
    elif field_name in _POSITIVE_INTEGER_FIELDS:
        validator = _validate_positive_integer
    elif field_name in _UTC_TIMESTAMP_FIELDS:
        validator = _validate_canonical_utc
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
    if field_name == "missed_count" and value > 10_000:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "schedule missed count exceeds its safe bound",
        )
    if field_name == "target_count" and value > 64:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "webhook target count exceeds its safe bound",
        )
    if field_name == "disposition" and value not in _SCHEDULE_DISPOSITIONS:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "schedule disposition is not an allowlisted value",
        )
    if field_name == "receipt_disposition" and value not in _MANUAL_RECEIPT_DISPOSITIONS:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "work receipt disposition is not an allowlisted value",
        )
    if field_name == "mode" and value not in _MANUAL_WORK_MODES:
        raise AuditMetadataError(
            "metadata_value_invalid",
            "manual work mode is not an allowlisted value",
        )
    if field_name == "rejection_code" and value != "schema_rejected":
        raise AuditMetadataError(
            "metadata_value_invalid",
            "schema rejection code is not an allowlisted value",
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
    configuration_pseudonym_key: DigestKey | None = None,
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
        if event_type == "instance.configuration_changed":
            if type(configuration_pseudonym_key) is not DigestKey:
                raise AuditMetadataError(
                    "metadata_pseudonym_key_missing",
                    "instance configuration audit requires an installation pseudonym key",
                )
            pseudonymized = _pseudonymize_instance_configuration_metadata(
                event_type,
                metadata,
                configuration_pseudonym_key,
            )
        else:
            pseudonymized = metadata
        for field_name, value in pseudonymized.items():
            _validate_typed_value(field_name, value, stored_configuration=True)
        redacted = redact(pseudonymized)
        if not isinstance(redacted, Mapping):  # pragma: no cover - source is a mapping
            raise AssertionError("central redactor changed audit object shape")
        safe = redacted
    maximum_keys, maximum_bytes = _metadata_limits(event_type)
    _measure(safe, maximum_keys=maximum_keys)
    if len(canonical_json_bytes(safe)) > maximum_bytes:
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
            _validate_typed_value(field_name, value, stored_configuration=True)
        if redact(stored_values) != dict(stored_values):
            raise AuditMetadataError(
                "metadata_redaction_invalid", "persisted audit metadata is not safely redacted"
            )
    maximum_keys, maximum_bytes = _metadata_limits(event_type)
    _measure(stored_values, maximum_keys=maximum_keys)
    if len(canonical_json_bytes(stored_values)) > maximum_bytes:
        raise AuditMetadataError("metadata_too_large", "audit metadata exceeds its byte limit")
    return _issue_sealed_audit_metadata(stored_values, classification, expires_at)
