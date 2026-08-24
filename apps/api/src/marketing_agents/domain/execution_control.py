"""Immutable durable runtime-control snapshots and attempt contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.runtime_policy import AttemptKind, RateLimitScope, RetryBackoff
from marketing_agents.domain.validation import (
    require_digest,
    require_id,
    require_json_pointers,
    require_utc,
)


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"
    CANCELLED = "cancelled"


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be from {minimum} through {maximum}")


@dataclass(frozen=True, slots=True)
class OperationExecutionPolicy:
    """One immutable generic READ-operation execution policy."""

    run_id: str
    step_id: str
    operation_key: str
    kind: AttemptKind
    capability_id: str
    selected_instance_id: str
    configuration_revision: int
    connector_family: str
    binding_id: str | None
    binding_configuration_revision: int | None
    request_schema_id: str | None
    result_schema_id: str | None
    request_redaction_fields: tuple[str, ...]
    result_redaction_fields: tuple[str, ...]
    data_classification: DataClassification
    connector_timeout_seconds: int | None
    policy_hash: str
    max_attempts: int
    retry_backoff: RetryBackoff
    step_timeout_seconds: int
    max_input_bytes: int
    max_input_field_bytes: int
    max_output_bytes: int
    max_model_output_tokens: int
    rate_limit_scope: RateLimitScope
    rate_limit_key: str
    rate_window_max_calls: int
    rate_window_seconds: int

    def __post_init__(self) -> None:
        for identifier, name in (
            (self.run_id, "operation Run ID"),
            (self.step_id, "operation step ID"),
            (self.operation_key, "operation key"),
            (self.capability_id, "operation capability ID"),
            (self.selected_instance_id, "operation selected instance ID"),
            (self.connector_family, "operation connector family"),
            (self.rate_limit_key, "operation rate-limit key"),
        ):
            require_id(identifier, name)
        require_digest(self.policy_hash, "operation policy hash")
        if type(self.kind) is not AttemptKind:
            raise ValueError("operation kind must use the exact AttemptKind enum")
        if self.kind not in {AttemptKind.MODEL, AttemptKind.TOOL}:
            raise ValueError("durable adapter attempts must be model or tool operations")
        if type(self.retry_backoff) is not RetryBackoff:
            raise ValueError("operation retry must use the exact RetryBackoff enum")
        if type(self.rate_limit_scope) is not RateLimitScope:
            raise ValueError("rate-limit scope must use the exact RateLimitScope enum")
        if type(self.data_classification) is not DataClassification:
            raise ValueError("operation data classification must use the exact enum")
        if type(self.configuration_revision) is not int or self.configuration_revision < 1:
            raise ValueError("operation configuration revision must be positive")
        require_json_pointers(
            self.request_redaction_fields,
            "operation request redaction fields",
        )
        require_json_pointers(
            self.result_redaction_fields,
            "operation result redaction fields",
        )
        if self.connector_family == "model":
            if (
                self.binding_id is not None
                or self.binding_configuration_revision is not None
                or self.request_redaction_fields
                or self.result_redaction_fields
                or self.connector_timeout_seconds is not None
                or self.data_classification is not DataClassification.INTERNAL
            ):
                raise ValueError("model operations cannot retain connector contract metadata")
            for optional_identifier, optional_name in (
                (self.request_schema_id, "model operation request schema ID"),
                (self.result_schema_id, "model operation result schema ID"),
            ):
                if optional_identifier is None:
                    raise ValueError(f"{optional_name} is required")
                require_id(optional_identifier, optional_name)
        else:
            for optional_identifier, optional_name in (
                (self.binding_id, "operation binding ID"),
                (self.request_schema_id, "operation request schema ID"),
                (self.result_schema_id, "operation result schema ID"),
            ):
                if optional_identifier is None:
                    raise ValueError(f"{optional_name} is required for connector operations")
                require_id(optional_identifier, optional_name)
            if (
                self.binding_configuration_revision != self.configuration_revision
                or self.connector_timeout_seconds is None
            ):
                raise ValueError("connector operation binding and timeout must be complete")
            _bounded_int(
                self.connector_timeout_seconds,
                "connector timeout seconds",
                1,
                120,
            )
        _bounded_int(self.max_attempts, "maximum attempts", 1, 3)
        _bounded_int(self.step_timeout_seconds, "step timeout seconds", 1, 120)
        _bounded_int(self.max_input_bytes, "maximum input bytes", 1, 1_048_576)
        _bounded_int(self.max_input_field_bytes, "maximum input field bytes", 1, 262_144)
        if self.max_input_field_bytes > self.max_input_bytes:
            raise ValueError("maximum input field bytes cannot exceed total input bytes")
        _bounded_int(self.max_output_bytes, "maximum output bytes", 1, 4_194_304)
        _bounded_int(self.max_model_output_tokens, "maximum model output tokens", 1, 32_768)
        if (
            self.connector_timeout_seconds is not None
            and self.step_timeout_seconds > self.connector_timeout_seconds
        ):
            raise ValueError("effective step timeout cannot exceed connector timeout")
        _bounded_int(self.rate_window_max_calls, "rate-window capacity", 1, 100)
        _bounded_int(self.rate_window_seconds, "rate-window seconds", 1, 3_600)


@dataclass(frozen=True, slots=True)
class RunExecutionPolicy:
    """Trusted planner-owned policy values installed before execution starts."""

    run_id: str
    policy_hash: str
    run_timeout_seconds: int
    max_model_calls: int
    max_tool_calls: int
    operations: tuple[OperationExecutionPolicy, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        require_id(self.run_id, "execution-policy Run ID")
        require_digest(self.policy_hash, "execution policy hash")
        _bounded_int(self.run_timeout_seconds, "Run timeout seconds", 1, 3_600)
        _bounded_int(self.max_model_calls, "maximum model calls", 0, 100)
        _bounded_int(self.max_tool_calls, "maximum tool calls", 0, 1_000)
        require_utc(self.created_at, "execution policy creation time")
        if type(self.operations) is not tuple:
            raise ValueError("operation policies must be an immutable tuple")
        identities: set[tuple[str, str]] = set()
        for operation in self.operations:
            if type(operation) is not OperationExecutionPolicy:
                raise ValueError("operation policies must use exact immutable contracts")
            identity = (operation.step_id, operation.operation_key)
            if identity in identities:
                raise ValueError("operation policy identities must be unique")
            identities.add(identity)
            if operation.run_id != self.run_id or operation.policy_hash != self.policy_hash:
                raise ValueError("operation policy must bind the same Run policy hash")


@dataclass(frozen=True, slots=True)
class RunExecutionControl:
    """Authoritative persisted budget, deadline, and cancellation fence."""

    run_id: str
    policy_hash: str
    run_timeout_seconds: int
    max_model_calls: int
    max_tool_calls: int
    model_calls: int
    tool_calls: int
    started_at: datetime | None
    deadline_at: datetime | None
    cancel_requested_at: datetime | None
    cancel_actor_digest: str | None
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        require_id(self.run_id, "execution-control Run ID")
        require_digest(self.policy_hash, "execution-control policy hash")
        _bounded_int(self.run_timeout_seconds, "Run timeout seconds", 1, 3_600)
        _bounded_int(self.max_model_calls, "maximum model calls", 0, 100)
        _bounded_int(self.max_tool_calls, "maximum tool calls", 0, 1_000)
        _bounded_int(self.model_calls, "committed model calls", 0, self.max_model_calls)
        _bounded_int(self.tool_calls, "committed tool calls", 0, self.max_tool_calls)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("execution-control version must be positive")
        require_utc(self.created_at, "execution-control creation time")
        require_utc(self.updated_at, "execution-control update time")
        if self.updated_at < self.created_at:
            raise ValueError("execution-control update cannot precede creation")
        if (self.started_at is None) != (self.deadline_at is None):
            raise ValueError("execution start and deadline must be installed together")
        if self.started_at is not None and self.deadline_at is not None:
            require_utc(self.started_at, "execution start time")
            require_utc(self.deadline_at, "execution deadline")
            if self.started_at < self.created_at:
                raise ValueError("execution cannot start before policy creation")
            if self.deadline_at != self.started_at + timedelta(seconds=self.run_timeout_seconds):
                raise ValueError("execution deadline must derive from the stored Run timeout")
        if (self.cancel_requested_at is None) != (self.cancel_actor_digest is None):
            raise ValueError("cancellation time and actor digest must be installed together")
        if self.cancel_requested_at is not None and self.cancel_actor_digest is not None:
            require_utc(self.cancel_requested_at, "cancellation request time")
            require_digest(self.cancel_actor_digest, "cancellation actor digest")
            if self.cancel_requested_at < self.created_at:
                raise ValueError("cancellation cannot precede policy creation")


@dataclass(frozen=True, slots=True)
class AttemptReservationCommand:
    """Server-owned reservation identity; all policy is loaded from storage."""

    attempt_id: str
    run_id: str
    step_id: str
    operation_key: str
    expected_control_version: int
    expected_step_version: int
    reserved_at: datetime

    def __post_init__(self) -> None:
        for identifier, name in (
            (self.attempt_id, "attempt ID"),
            (self.run_id, "attempt Run ID"),
            (self.step_id, "attempt step ID"),
            (self.operation_key, "attempt operation key"),
        ):
            require_id(identifier, name)
        for version, name in (
            (self.expected_control_version, "expected execution-control version"),
            (self.expected_step_version, "expected step version"),
        ):
            if type(version) is not int or version < 1:
                raise ValueError(f"{name} must be positive")
        require_utc(self.reserved_at, "attempt reservation time")


@dataclass(frozen=True, slots=True)
class AttemptCompletionCommand:
    attempt_id: str
    outcome: AttemptOutcome
    expected_control_version: int
    completed_at: datetime

    def __post_init__(self) -> None:
        require_id(self.attempt_id, "attempt ID")
        if type(self.outcome) is not AttemptOutcome:
            raise ValueError("attempt outcome must use the exact AttemptOutcome enum")
        if type(self.expected_control_version) is not int or self.expected_control_version < 1:
            raise ValueError("expected execution-control version must be positive")
        require_utc(self.completed_at, "attempt completion time")


@dataclass(frozen=True, slots=True)
class ExpiredAttemptRecoveryCommand:
    """Identity and deadline fence for repository-derived crash recovery."""

    attempt_id: str
    expected_attempt_version: int
    expected_call_deadline_at: datetime
    recovered_at: datetime

    def __post_init__(self) -> None:
        require_id(self.attempt_id, "expired attempt ID")
        if type(self.expected_attempt_version) is not int or self.expected_attempt_version != 1:
            raise ValueError("expired attempt recovery requires the open version")
        require_utc(self.expected_call_deadline_at, "expected attempt call deadline")
        require_utc(self.recovered_at, "attempt recovery time")
        if self.recovered_at < self.expected_call_deadline_at:
            raise ValueError("attempt recovery cannot precede its expected call deadline")


@dataclass(frozen=True, slots=True)
class DeliveryCallReservationCommand:
    """Identity-only permit request for one RUN-05 provider delivery attempt."""

    run_id: str
    step_id: str
    action_id: str
    delivery_attempt_number: int
    expected_control_version: int
    expected_step_version: int
    expected_action_version: int
    reserved_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.run_id, "delivery Run ID"),
            (self.step_id, "delivery step ID"),
            (self.action_id, "delivery action ID"),
        ):
            require_id(value, name)
        for int_value, int_name in (
            (self.delivery_attempt_number, "delivery attempt number"),
            (self.expected_control_version, "delivery control version"),
            (self.expected_step_version, "delivery step version"),
            (self.expected_action_version, "delivery action version"),
        ):
            if type(int_value) is not int or int_value < 1:
                raise ValueError(f"{int_name} must be positive")
        require_utc(self.reserved_at, "delivery permit reservation time")


@dataclass(frozen=True, slots=True)
class DeliveryCallPermit:
    """Ephemeral result whose durable witnesses are the call marker, control, and window."""

    run_id: str
    step_id: str
    action_id: str
    delivery_attempt_number: int
    policy_hash: str
    source_control_version: int
    source_step_version: int
    source_action_version: int
    reserved_at: datetime
    call_deadline_at: datetime
    rate_limit_scope: RateLimitScope
    rate_limit_key: str
    rate_window_started_at: datetime
    logical_budget_consumed: bool

    def __post_init__(self) -> None:
        for value, name in (
            (self.run_id, "delivery permit Run ID"),
            (self.step_id, "delivery permit step ID"),
            (self.action_id, "delivery permit action ID"),
            (self.rate_limit_key, "delivery permit rate key"),
        ):
            require_id(value, name)
        require_digest(self.policy_hash, "delivery permit policy hash")
        for int_value, int_name in (
            (self.delivery_attempt_number, "delivery permit attempt number"),
            (self.source_control_version, "delivery permit control version"),
            (self.source_step_version, "delivery permit step version"),
            (self.source_action_version, "delivery permit action version"),
        ):
            if type(int_value) is not int or int_value < 1:
                raise ValueError(f"{int_name} must be positive")
        require_utc(self.reserved_at, "delivery permit reservation time")
        require_utc(self.call_deadline_at, "delivery permit call deadline")
        require_utc(self.rate_window_started_at, "delivery permit rate-window start")
        if self.call_deadline_at <= self.reserved_at:
            raise ValueError("delivery permit requires a positive call window")
        if type(self.rate_limit_scope) is not RateLimitScope:
            raise ValueError("delivery permit rate scope must use the exact enum")
        if type(self.logical_budget_consumed) is not bool:
            raise ValueError("delivery permit budget disposition must be boolean")

    @property
    def effective_timeout(self) -> timedelta:
        return self.call_deadline_at - self.reserved_at


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    id: str
    run_id: str
    step_id: str
    operation_key: str
    policy_hash: str
    kind: AttemptKind
    attempt_number: int
    source_control_version: int
    source_step_version: int
    eligible_at: datetime
    reserved_at: datetime
    call_deadline_at: datetime
    outcome: AttemptOutcome | None
    completed_at: datetime | None
    retry_not_before: datetime | None
    terminal_reason_code: str | None
    version: int

    def __post_init__(self) -> None:
        for identifier, name in (
            (self.id, "attempt ID"),
            (self.run_id, "attempt Run ID"),
            (self.step_id, "attempt step ID"),
            (self.operation_key, "attempt operation key"),
        ):
            require_id(identifier, name)
        require_digest(self.policy_hash, "attempt policy hash")
        if type(self.kind) is not AttemptKind:
            raise ValueError("attempt kind must use the exact AttemptKind enum")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt number must be positive")
        for source_version, name in (
            (self.source_control_version, "attempt source control version"),
            (self.source_step_version, "attempt source step version"),
        ):
            if type(source_version) is not int or source_version < 1:
                raise ValueError(f"{name} must be positive")
        for instant, name in (
            (self.eligible_at, "attempt eligibility time"),
            (self.reserved_at, "attempt reservation time"),
            (self.call_deadline_at, "attempt call deadline"),
        ):
            require_utc(instant, name)
        if not self.eligible_at <= self.reserved_at < self.call_deadline_at:
            raise ValueError("attempt times must retain eligibility and a positive call window")
        if self.outcome is None:
            if (
                self.completed_at is not None
                or self.retry_not_before is not None
                or self.terminal_reason_code is not None
                or self.version != 1
            ):
                raise ValueError("open attempts cannot retain completion state")
            return
        if type(self.outcome) is not AttemptOutcome:
            raise ValueError("attempt outcome must use the exact AttemptOutcome enum")
        if self.completed_at is None or self.version != 2:
            raise ValueError("completed attempts require an exact completion version")
        require_utc(self.completed_at, "attempt completion time")
        if self.completed_at < self.reserved_at:
            raise ValueError("attempt completion cannot precede reservation")
        if self.retry_not_before is not None:
            require_utc(self.retry_not_before, "attempt retry time")
            if (
                self.outcome is not AttemptOutcome.TRANSIENT_FAILURE
                or self.retry_not_before < self.completed_at
                or self.terminal_reason_code is not None
            ):
                raise ValueError("only eligible transient failures retain a retry time")
        elif self.outcome is AttemptOutcome.TRANSIENT_FAILURE:
            if self.terminal_reason_code not in {
                "attempts_exhausted",
                "retry_deadline_exceeded",
            }:
                raise ValueError("terminal transient attempt reason is invalid")
        elif self.outcome is AttemptOutcome.SUCCEEDED:
            if self.terminal_reason_code is not None:
                raise ValueError("successful attempts cannot retain a terminal reason")
        elif self.outcome is AttemptOutcome.PERMANENT_FAILURE:
            if self.terminal_reason_code != "permanent_failure":
                raise ValueError("permanent attempt failure requires its exact reason")
        elif self.terminal_reason_code not in {"cancelled", "run_cancelled"}:
            raise ValueError("cancelled attempt requires an exact cancellation reason")

    @property
    def effective_timeout(self) -> timedelta:
        return self.call_deadline_at - self.reserved_at


@dataclass(frozen=True, slots=True)
class RateLimitWindow:
    scope: RateLimitScope
    key: str
    started_at: datetime
    ends_at: datetime
    capacity: int
    used: int
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if type(self.scope) is not RateLimitScope:
            raise ValueError("rate-limit scope must use the exact RateLimitScope enum")
        require_id(self.key, "rate-limit key")
        require_utc(self.started_at, "rate-window start")
        require_utc(self.ends_at, "rate-window end")
        require_utc(self.updated_at, "rate-window update time")
        if self.ends_at <= self.started_at or not self.started_at <= self.updated_at < self.ends_at:
            raise ValueError("rate-window times are inconsistent")
        _bounded_int(self.capacity, "rate-window capacity", 1, 100)
        _bounded_int(self.used, "rate-window usage", 0, self.capacity)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("rate-window version must be positive")


def bounded_retry_delay_seconds(backoff: RetryBackoff, next_attempt_number: int) -> int:
    """Return the fixed v1 retry schedule (1s, 2s, ... capped at 60s)."""

    if type(backoff) is not RetryBackoff:
        raise ValueError("retry backoff must use the exact RetryBackoff enum")
    if type(next_attempt_number) is not int or next_attempt_number < 2:
        raise ValueError("retry delay requires an attempt number of at least two")
    if backoff is RetryBackoff.NONE:
        return 0
    return min(1 << (next_attempt_number - 2), 60)


def fixed_window_start(value: datetime, window_seconds: int) -> datetime:
    """Floor one UTC instant into a deterministic epoch-aligned fixed window."""

    require_utc(value, "rate-window instant")
    _bounded_int(window_seconds, "rate-window seconds", 1, 3_600)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = value - epoch
    elapsed_microseconds = (
        (elapsed.days * 86_400) + elapsed.seconds
    ) * 1_000_000 + elapsed.microseconds
    window_microseconds = window_seconds * 1_000_000
    return epoch + timedelta(
        microseconds=(elapsed_microseconds // window_microseconds) * window_microseconds
    )
