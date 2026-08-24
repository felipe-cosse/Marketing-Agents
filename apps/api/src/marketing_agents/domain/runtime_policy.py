"""Immutable runtime-policy snapshots and deterministic planning budgets."""

from __future__ import annotations

import hashlib
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.validation import require_id

RUN_RUNTIME_POLICY_HASH_DOMAIN = b"marketing-agents:run-runtime-policy:v1\x00"
STEP_RUNTIME_POLICY_HASH_DOMAIN = b"marketing-agents:step-runtime-policy:v2\x00"
OPERATION_KEY_HASH_DOMAIN = b"marketing-agents:runtime-operation-key:v1\x00"
RATE_LIMIT_KEY_HASH_DOMAIN = b"marketing-agents:runtime-rate-limit-key:v1\x00"


def _bounded_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be from {minimum} through {maximum}")
    return value


class RetryBackoff(StrEnum):
    """The only catalog-owned retry schedules accepted by the runtime."""

    NONE = "none"
    BOUNDED_EXPONENTIAL = "bounded_exponential"


class AttemptKind(StrEnum):
    """The durable counter family consumed by one planned operation."""

    MODEL = "model"
    TOOL = "tool"
    NO_CALL = "no_call"


class RateLimitScope(StrEnum):
    """The catalog-supported durable rate-limit scope."""

    TEMPLATE = "template"


@dataclass(frozen=True, slots=True)
class RetryPolicySnapshot:
    """Finite retry authority copied from one selected template."""

    max_attempts: int
    backoff: RetryBackoff

    def __post_init__(self) -> None:
        _bounded_int(self.max_attempts, "retry maximum attempts", minimum=1, maximum=3)
        if type(self.backoff) is not RetryBackoff:
            raise ValueError("retry backoff must use the exact RetryBackoff enum")


@dataclass(frozen=True, slots=True)
class TimeoutPolicySnapshot:
    """Step and whole-run timeout bounds copied from one selected template."""

    step_seconds: int
    run_seconds: int

    def __post_init__(self) -> None:
        _bounded_int(self.step_seconds, "step timeout", minimum=1, maximum=120)
        _bounded_int(self.run_seconds, "run timeout", minimum=1, maximum=600)
        if self.step_seconds > self.run_seconds:
            raise ValueError("step timeout cannot exceed run timeout")


@dataclass(frozen=True, slots=True)
class BudgetPolicySnapshot:
    """Per-template graph and adapter-call ceilings."""

    max_steps: int
    max_model_calls: int
    max_tool_calls: int
    max_input_bytes: int = 65_536
    max_input_field_bytes: int = 16_384
    max_output_bytes: int = 262_144
    max_model_output_tokens: int = 4_096

    def __post_init__(self) -> None:
        _bounded_int(self.max_steps, "template step budget", minimum=1, maximum=20)
        _bounded_int(self.max_model_calls, "template model-call budget", minimum=0, maximum=10)
        _bounded_int(self.max_tool_calls, "template tool-call budget", minimum=0, maximum=20)
        _bounded_int(
            self.max_input_bytes,
            "template input byte budget",
            minimum=1,
            maximum=1_048_576,
        )
        _bounded_int(
            self.max_input_field_bytes,
            "template input field byte budget",
            minimum=1,
            maximum=262_144,
        )
        if self.max_input_field_bytes > self.max_input_bytes:
            raise ValueError("template input field budget cannot exceed total input bytes")
        _bounded_int(
            self.max_output_bytes,
            "template output byte budget",
            minimum=1,
            maximum=4_194_304,
        )
        _bounded_int(
            self.max_model_output_tokens,
            "template model output token budget",
            minimum=1,
            maximum=32_768,
        )


@dataclass(frozen=True, slots=True)
class RateLimitPolicySnapshot:
    """Finite template-scoped rate window copied into the plan."""

    scope: RateLimitScope
    key: str
    max_calls: int
    window_seconds: int

    def __post_init__(self) -> None:
        if type(self.scope) is not RateLimitScope:
            raise ValueError("rate-limit scope must use the exact RateLimitScope enum")
        require_id(self.key, "rate-limit key")
        _bounded_int(self.max_calls, "rate-window call budget", minimum=1, maximum=100)
        _bounded_int(self.window_seconds, "rate-window duration", minimum=1, maximum=3_600)


@dataclass(frozen=True, slots=True)
class StepRuntimePolicy:
    """The complete immutable policy selected for one planned step."""

    operation_key: str
    attempt_kind: AttemptKind
    retry: RetryPolicySnapshot
    timeout: TimeoutPolicySnapshot
    budget: BudgetPolicySnapshot
    rate_limit: RateLimitPolicySnapshot

    def __post_init__(self) -> None:
        require_id(self.operation_key, "runtime operation key")
        if type(self.attempt_kind) is not AttemptKind:
            raise ValueError("attempt kind must use the exact AttemptKind enum")
        for value, expected, name in (
            (self.retry, RetryPolicySnapshot, "retry policy"),
            (self.timeout, TimeoutPolicySnapshot, "timeout policy"),
            (self.budget, BudgetPolicySnapshot, "budget policy"),
            (self.rate_limit, RateLimitPolicySnapshot, "rate-limit policy"),
        ):
            if type(value) is not expected:
                raise ValueError(f"{name} must use the exact immutable snapshot type")

    @property
    def semantic_hash(self) -> str:
        """Hash every storage-facing operation-policy field."""

        return _policy_hash(STEP_RUNTIME_POLICY_HASH_DOMAIN, step_policy_projection(self))


@dataclass(frozen=True, slots=True)
class RunRuntimePolicy:
    """Trusted run-wide planning ceilings supplied by composition, never request data."""

    max_steps: int
    max_model_calls: int
    max_tool_calls: int
    run_timeout_seconds: int

    def __post_init__(self) -> None:
        _bounded_int(self.max_steps, "run step budget", minimum=1, maximum=20)
        _bounded_int(self.max_model_calls, "run model-call budget", minimum=0, maximum=100)
        _bounded_int(self.max_tool_calls, "run tool-call budget", minimum=0, maximum=1_000)
        _bounded_int(self.run_timeout_seconds, "run timeout", minimum=1, maximum=3_600)

    @property
    def semantic_hash(self) -> str:
        """Hash the trusted, effective run-wide ceiling projection."""

        return _policy_hash(RUN_RUNTIME_POLICY_HASH_DOMAIN, run_policy_projection(self))


class RuntimePlanningBudgetError(ValueError):
    """A stable fail-closed error for a deterministic logical-plan overrun."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StepRuntimeDemand:
    """Minimal immutable input for deterministic planning-budget accounting."""

    template_id: str
    connector_family: str
    policy: StepRuntimePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str) or not self.template_id:
            raise ValueError("runtime demand template ID must be nonempty")
        if not isinstance(self.connector_family, str) or not self.connector_family:
            raise ValueError("runtime demand connector family must be nonempty")
        if type(self.policy) is not StepRuntimePolicy:
            raise ValueError("runtime demand policy must use the exact snapshot type")
        if self.policy.attempt_kind is not attempt_kind_for_connector(self.connector_family):
            raise ValueError("runtime demand attempt kind differs from its connector family")


@dataclass(frozen=True, slots=True)
class RuntimePlanTotals:
    """Logical adapter operations derived from an immutable plan."""

    step_count: int
    model_calls: int
    tool_calls: int


def validate_runtime_plan_budget(
    run_policy: RunRuntimePolicy,
    demands: tuple[StepRuntimeDemand, ...],
) -> RuntimePlanTotals:
    """Enforce template-group and run-wide logical operation ceilings."""

    if type(run_policy) is not RunRuntimePolicy:
        raise ValueError("run policy must use the exact immutable contract")
    if (
        type(demands) is not tuple
        or not demands
        or any(type(item) is not StepRuntimeDemand for item in demands)
    ):
        raise ValueError("runtime demands must be a nonempty exact immutable tuple")

    grouped: dict[str, list[StepRuntimeDemand]] = defaultdict(list)
    model_calls = 0
    tool_calls = 0
    for demand in demands:
        grouped[demand.template_id].append(demand)
        if demand.policy.attempt_kind is AttemptKind.MODEL:
            model_calls += 1
        elif demand.policy.attempt_kind is AttemptKind.TOOL:
            tool_calls += 1

    for template_id, template_demands in sorted(grouped.items()):
        policies = {
            (
                item.policy.retry,
                item.policy.timeout,
                item.policy.budget,
                item.policy.rate_limit,
            )
            for item in template_demands
        }
        if len(policies) != 1:
            raise RuntimePlanningBudgetError(
                "template_policy_drift",
                f"selected template {template_id} has contradictory runtime policies",
            )
        _retry, _timeout, budget, _rate_limit = next(iter(policies))
        template_model_calls = sum(
            1 for item in template_demands if item.policy.attempt_kind is AttemptKind.MODEL
        )
        template_tool_calls = sum(
            1 for item in template_demands if item.policy.attempt_kind is AttemptKind.TOOL
        )
        if len(template_demands) > budget.max_steps:
            raise RuntimePlanningBudgetError(
                "template_step_budget_exceeded",
                f"selected template {template_id} exceeds its step budget",
            )
        if template_model_calls > budget.max_model_calls:
            raise RuntimePlanningBudgetError(
                "template_model_budget_exceeded",
                f"selected template {template_id} exceeds its model-call budget",
            )
        if template_tool_calls > budget.max_tool_calls:
            raise RuntimePlanningBudgetError(
                "template_tool_budget_exceeded",
                f"selected template {template_id} exceeds its tool-call budget",
            )

    totals = RuntimePlanTotals(
        step_count=len(demands),
        model_calls=model_calls,
        tool_calls=tool_calls,
    )
    if totals.step_count > run_policy.max_steps:
        raise RuntimePlanningBudgetError(
            "run_step_budget_exceeded", "planned graph exceeds the run step budget"
        )
    if totals.model_calls > run_policy.max_model_calls:
        raise RuntimePlanningBudgetError(
            "run_model_budget_exceeded",
            "planned model operations exceed the run model-call budget",
        )
    if totals.tool_calls > run_policy.max_tool_calls:
        raise RuntimePlanningBudgetError(
            "run_tool_budget_exceeded",
            "planned tool operations exceed the run tool-call budget",
        )
    return totals


def runtime_operation_key(*, workflow_id: str, workflow_version: int, step_key: str) -> str:
    """Return a stable workflow-operation identity without runtime IDs."""

    require_id(workflow_id, "runtime operation workflow ID")
    require_id(step_key, "runtime operation step key")
    _bounded_int(workflow_version, "runtime operation workflow version", minimum=1, maximum=2**31)
    projection = {
        "version": 1,
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "step_key": step_key,
    }
    digest = hashlib.sha256(
        OPERATION_KEY_HASH_DOMAIN + canonical_json_bytes(projection)
    ).hexdigest()
    return f"runtime-operation-sha256-v1:{digest}"


def runtime_rate_limit_key(
    *,
    template_id: str,
    max_calls: int,
    window_seconds: int,
) -> str:
    """Version one template policy so incompatible windows never share a counter key."""

    require_id(template_id, "rate-limit template ID")
    _bounded_int(max_calls, "rate-window call budget", minimum=1, maximum=100)
    _bounded_int(window_seconds, "rate-window duration", minimum=1, maximum=3_600)
    digest = hashlib.sha256(
        RATE_LIMIT_KEY_HASH_DOMAIN
        + canonical_json_bytes(
            {
                "version": 1,
                "template_id": template_id,
                "max_calls": max_calls,
                "window_seconds": window_seconds,
            }
        )
    ).hexdigest()
    return f"rate-template-sha256-v1:{digest}"


def attempt_kind_for_connector(connector_family: str) -> AttemptKind:
    """Classify model/tool/no-call accounting from trusted capability metadata."""

    require_id(connector_family, "runtime connector family")
    if connector_family == "model":
        return AttemptKind.MODEL
    if connector_family == "artifact":
        return AttemptKind.NO_CALL
    return AttemptKind.TOOL


def effective_call_timeout_seconds(
    policy: StepRuntimePolicy,
    connector_timeout_seconds: int | None,
) -> int:
    """Intersect the template policy with an operation/connector contract timeout."""

    if type(policy) is not StepRuntimePolicy:
        raise ValueError("effective timeout requires the exact step runtime policy")
    if connector_timeout_seconds is None:
        return policy.timeout.step_seconds
    _bounded_int(
        connector_timeout_seconds,
        "connector timeout",
        minimum=1,
        maximum=120,
    )
    return min(policy.timeout.step_seconds, connector_timeout_seconds)


def canonical_payload_size_bytes(payload: object) -> int:
    """Measure one JSON payload using the same canonical UTF-8 form as integrity hashes."""

    return len(canonical_json_bytes(payload))


def payload_fields_within_byte_limit(payload: object, max_field_bytes: int) -> bool:
    """Bound every JSON string value and object key by normalized UTF-8 bytes."""

    _bounded_int(
        max_field_bytes,
        "payload field byte limit",
        minimum=1,
        maximum=262_144,
    )

    def within(value: object) -> bool:
        if isinstance(value, str):
            return len(unicodedata.normalize("NFC", value).encode("utf-8")) <= max_field_bytes
        if isinstance(value, Mapping):
            return all(within(key) and within(item) for key, item in value.items())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return all(within(item) for item in value)
        return True

    return within(payload)


def run_policy_projection(policy: RunRuntimePolicy) -> dict[str, int]:
    """Return canonical, persistence-friendly run policy material."""

    if type(policy) is not RunRuntimePolicy:
        raise ValueError("run policy projection requires the exact immutable contract")
    return {
        "max_steps": policy.max_steps,
        "max_model_calls": policy.max_model_calls,
        "max_tool_calls": policy.max_tool_calls,
        "run_timeout_seconds": policy.run_timeout_seconds,
    }


def step_policy_projection(policy: StepRuntimePolicy) -> dict[str, object]:
    """Return canonical, persistence-friendly operation policy material."""

    if type(policy) is not StepRuntimePolicy:
        raise ValueError("step policy projection requires the exact immutable contract")
    return {
        "operation_key": policy.operation_key,
        "attempt_kind": policy.attempt_kind.value,
        "max_attempts": policy.retry.max_attempts,
        "backoff": policy.retry.backoff.value,
        "step_timeout_seconds": policy.timeout.step_seconds,
        "template_run_timeout_seconds": policy.timeout.run_seconds,
        "max_steps": policy.budget.max_steps,
        "max_model_calls": policy.budget.max_model_calls,
        "max_tool_calls": policy.budget.max_tool_calls,
        "max_input_bytes": policy.budget.max_input_bytes,
        "max_input_field_bytes": policy.budget.max_input_field_bytes,
        "max_output_bytes": policy.budget.max_output_bytes,
        "max_model_output_tokens": policy.budget.max_model_output_tokens,
        "rate_limit_scope": policy.rate_limit.scope.value,
        "rate_limit_key": policy.rate_limit.key,
        "rate_limit_max_calls": policy.rate_limit.max_calls,
        "rate_limit_window_seconds": policy.rate_limit.window_seconds,
    }


def _policy_hash(domain: bytes, projection: Mapping[str, object]) -> str:
    return hashlib.sha256(domain + canonical_json_bytes(projection)).hexdigest()


__all__ = [
    "OPERATION_KEY_HASH_DOMAIN",
    "RATE_LIMIT_KEY_HASH_DOMAIN",
    "RUN_RUNTIME_POLICY_HASH_DOMAIN",
    "STEP_RUNTIME_POLICY_HASH_DOMAIN",
    "AttemptKind",
    "BudgetPolicySnapshot",
    "RateLimitPolicySnapshot",
    "RateLimitScope",
    "RetryBackoff",
    "RetryPolicySnapshot",
    "RunRuntimePolicy",
    "RuntimePlanTotals",
    "RuntimePlanningBudgetError",
    "StepRuntimeDemand",
    "StepRuntimePolicy",
    "TimeoutPolicySnapshot",
    "attempt_kind_for_connector",
    "canonical_payload_size_bytes",
    "effective_call_timeout_seconds",
    "payload_fields_within_byte_limit",
    "run_policy_projection",
    "runtime_operation_key",
    "runtime_rate_limit_key",
    "step_policy_projection",
    "validate_runtime_plan_budget",
]
