"""Pure effect-aware planning and immutable external-write proposals."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, TypeAdapter

from marketing_agents.application.orchestration.router import RoutingResult
from marketing_agents.application.ports.clock import Clock
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.application.ports.id_generator import IdGenerator
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    semantic_action_hash,
)
from marketing_agents.domain.approval import (
    ActionApprovalRequest,
    ApprovalPolicySnapshot,
    ProposedExternalAction,
    approval_redaction_schema,
    assert_request_binds_action,
    request_approval,
    safe_approval_destination,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities._validation import (
    frozen_json_mapping,
    require_digest,
    require_id,
    require_json_pointers,
)
from marketing_agents.domain.enums import Effect
from marketing_agents.domain.graph import DependencyGraph
from marketing_agents.domain.plan_hash import (
    EFFECT_PLAN_HASH_DOMAIN as _DOMAIN_EFFECT_PLAN_HASH,
)
from marketing_agents.domain.plan_hash import (
    EffectPlanStepHashMaterial,
    effect_plan_hash,
)
from marketing_agents.domain.run_lifecycle import PlanDispositionContext
from marketing_agents.domain.runtime_policy import (
    BudgetPolicySnapshot,
    RateLimitPolicySnapshot,
    RateLimitScope,
    RetryBackoff,
    RetryPolicySnapshot,
    RunRuntimePolicy,
    RuntimePlanningBudgetError,
    StepRuntimeDemand,
    StepRuntimePolicy,
    TimeoutPolicySnapshot,
    attempt_kind_for_connector,
    runtime_operation_key,
    runtime_rate_limit_key,
    validate_runtime_plan_budget,
)
from marketing_agents.domain.schema_hash import canonical_schema_hash, require_schema_hash

EFFECT_PLAN_HASH_DOMAIN = _DOMAIN_EFFECT_PLAN_HASH
_DESTINATION_HASH_DOMAIN = b"marketing-agents:external-action-destination:v1\x00"
EXTERNAL_WRITE_SCOPE = "scope.external-write"
_NON_CONNECTOR_FAMILIES = frozenset({"model", "artifact"})
_CATALOG_HASH = re.compile(r"^catalog-sha256-v1:[0-9a-f]{64}$")


class EffectPlanningError(ValueError):
    """A stable fail-closed planning error raised before any execution exists."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlanningCapabilitySource(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def effect(self) -> str: ...

    @property
    def connector_family(self) -> str: ...

    @property
    def idempotency_support(self) -> str: ...


class PlanningTemplateSource(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def allowed_tool_capability_ids(self) -> Sequence[str]: ...

    @property
    def operation_classification(self) -> str: ...

    @property
    def approval_policy_id(self) -> str: ...

    @property
    def input_schema_id(self) -> str: ...

    @property
    def output_schema_id(self) -> str: ...

    @property
    def retry_policy(self) -> PlanningRetryPolicySource: ...

    @property
    def timeout_policy(self) -> PlanningTimeoutPolicySource: ...

    @property
    def budget_policy(self) -> PlanningBudgetPolicySource: ...

    @property
    def rate_limit_policy(self) -> PlanningRateLimitPolicySource: ...


class PlanningRetryPolicySource(Protocol):
    @property
    def max_attempts(self) -> int: ...

    @property
    def backoff(self) -> str: ...


class PlanningTimeoutPolicySource(Protocol):
    @property
    def step_seconds(self) -> int: ...

    @property
    def run_seconds(self) -> int: ...


class PlanningBudgetPolicySource(Protocol):
    @property
    def max_steps(self) -> int: ...

    @property
    def max_model_calls(self) -> int: ...

    @property
    def max_tool_calls(self) -> int: ...

    @property
    def max_input_bytes(self) -> int: ...

    @property
    def max_input_field_bytes(self) -> int: ...

    @property
    def max_output_bytes(self) -> int: ...

    @property
    def max_model_output_tokens(self) -> int: ...


class PlanningRateLimitPolicySource(Protocol):
    @property
    def max_calls(self) -> int: ...

    @property
    def window_seconds(self) -> int: ...


class PlanningApprovalPolicySource(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def required_roles(self) -> Sequence[str]: ...

    @property
    def expiry_seconds(self) -> int: ...

    @property
    def allow_self_approval(self) -> bool: ...


class PlanningBindingSource(Protocol):
    @property
    def instance_id(self) -> str: ...

    @property
    def connector_family(self) -> str: ...

    @property
    def binding_id(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def configuration_revision(self) -> int: ...


class PlanningOperationMetadataSource(Protocol):
    @property
    def capability_id(self) -> str: ...

    @property
    def connector_family(self) -> str: ...

    @property
    def effect(self) -> Effect: ...

    @property
    def request_schema_id(self) -> str: ...

    @property
    def result_schema_id(self) -> str: ...

    @property
    def data_classification(self) -> DataClassification: ...

    @property
    def idempotency_support(self) -> str: ...

    @property
    def default_timeout_seconds(self) -> int: ...

    @property
    def request_redaction_fields(self) -> Sequence[str]: ...

    @property
    def result_redaction_fields(self) -> Sequence[str]: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def disabled_reason(self) -> str | None: ...


class PlanningOperationSource(Protocol):
    @property
    def metadata(self) -> PlanningOperationMetadataSource: ...

    @property
    def request_type(self) -> type[BaseModel]: ...

    @property
    def result_type(self) -> type[BaseModel] | type[ConnectorWriteResult]: ...


@dataclass(frozen=True, slots=True)
class WriteActionIntent:
    """Typed, untrusted-at-entry write details validated against the registry."""

    command: BaseModel
    payload_snapshot: Mapping[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.command, BaseModel):
            raise ValueError("write intent command must be a typed Pydantic model")
        object.__setattr__(
            self,
            "payload_snapshot",
            frozen_json_mapping(
                self.command.model_dump(mode="json"),
                "write intent command payload",
            ),
        )


@dataclass(frozen=True, slots=True)
class EffectStepSpec:
    """One graph step's trusted routing identity and requested capability."""

    runtime_step_id: str
    step_key: str
    kind: str
    selected_instance_id: str
    routing_slot_key: str | None
    capability_id: str
    binding_id: str | None
    write_intent: WriteActionIntent | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.runtime_step_id, "runtime step ID"),
            (self.step_key, "step key"),
            (self.kind, "step kind"),
            (self.selected_instance_id, "selected instance ID"),
            (self.capability_id, "capability ID"),
        ):
            require_id(value, name)
        if len(self.kind) > 120:
            raise ValueError("planned step kind must be at most 120 characters")
        if self.routing_slot_key is not None:
            require_id(self.routing_slot_key, "routing slot key")
        if self.binding_id is not None:
            require_id(self.binding_id, "binding ID")


@dataclass(frozen=True, slots=True)
class EffectPlanRequest:
    """Validated graph and route plus per-step capability inputs."""

    run_id: str
    workflow_definition_hash: str
    graph: DependencyGraph
    routing: RoutingResult
    steps: tuple[EffectStepSpec, ...]
    requested_by: str

    def __post_init__(self) -> None:
        require_id(self.run_id, "run ID")
        require_digest(self.workflow_definition_hash, "workflow definition hash")
        require_id(self.requested_by, "request actor")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ValueError("effect plan steps must be a nonempty immutable tuple")


class EffectPlanRelease(StrEnum):
    DIRECT = "direct"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class EffectPlannedStep:
    runtime_step_id: str
    step_key: str
    kind: str
    selected_instance_id: str
    routing_slot_key: str | None
    template_id: str
    configuration_revision: int
    capability_id: str
    effect: Effect
    connector_family: str
    binding_id: str | None
    binding_configuration_revision: int | None
    request_schema_id: str | None
    result_schema_id: str | None
    result_schema_hash: str | None
    request_redaction_fields: tuple[str, ...]
    result_redaction_fields: tuple[str, ...]
    data_classification: DataClassification
    idempotency_support: str
    connector_timeout_seconds: int | None
    approval_policy_id: str
    approval_required_roles: tuple[str, ...]
    approval_required_scopes: tuple[str, ...]
    approval_expires_after_seconds: int | None
    approval_allow_self_approval: bool | None
    runtime_policy: StepRuntimePolicy

    def __post_init__(self) -> None:
        for values, name in (
            (self.request_redaction_fields, "request redaction fields"),
            (self.result_redaction_fields, "result redaction fields"),
            (self.approval_required_roles, "approval roles"),
            (self.approval_required_scopes, "approval scopes"),
        ):
            if type(values) is not tuple:
                raise ValueError(f"{name} must be an immutable tuple")
        for value, name in (
            (self.runtime_step_id, "runtime step ID"),
            (self.step_key, "step key"),
            (self.kind, "step kind"),
            (self.selected_instance_id, "selected instance ID"),
            (self.template_id, "template ID"),
            (self.capability_id, "capability ID"),
            (self.connector_family, "connector family"),
            (self.approval_policy_id, "approval policy ID"),
        ):
            require_id(value, name)
        if len(self.kind) > 120 or len(self.connector_family) > 120:
            raise ValueError(
                "planned step kind and connector family must be at most 120 characters"
            )
        for value in (*self.approval_required_roles, *self.approval_required_scopes):
            require_id(value, "approval authority")
        if not isinstance(self.effect, Effect):
            raise ValueError("planned step effect must use Effect")
        if type(self.data_classification) is not DataClassification:
            raise ValueError(
                "planned step data classification must use the exact DataClassification enum"
            )
        for schema_id, name in (
            (self.request_schema_id, "request schema ID"),
            (self.result_schema_id, "result schema ID"),
        ):
            if schema_id is not None:
                require_id(schema_id, f"planned step {name}")
        if self.result_schema_hash is not None:
            require_schema_hash(self.result_schema_hash, "planned step result schema hash")
        if (self.result_schema_id is None) != (self.result_schema_hash is None):
            raise ValueError("planned step result schema ID and hash must be present together")
        if type(self.runtime_policy) is not StepRuntimePolicy:
            raise ValueError("planned step runtime policy must use the exact immutable snapshot")
        if self.runtime_policy.attempt_kind is not attempt_kind_for_connector(
            self.connector_family
        ):
            raise ValueError("planned step attempt kind differs from its connector family")
        if (
            self.runtime_policy.rate_limit.scope is not RateLimitScope.TEMPLATE
            or self.runtime_policy.rate_limit.key
            != runtime_rate_limit_key(
                template_id=self.template_id,
                max_calls=self.runtime_policy.rate_limit.max_calls,
                window_seconds=self.runtime_policy.rate_limit.window_seconds,
            )
        ):
            raise ValueError("planned step rate-limit identity differs from its template")
        for revision, revision_name in (
            (self.configuration_revision, "configuration revision"),
            (self.binding_configuration_revision, "binding configuration revision"),
        ):
            if revision is not None and (
                not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
            ):
                raise ValueError(f"{revision_name} must be positive")
        if self.connector_timeout_seconds is not None and (
            not isinstance(self.connector_timeout_seconds, int)
            or isinstance(self.connector_timeout_seconds, bool)
            or not 1 <= self.connector_timeout_seconds <= 120
        ):
            raise ValueError("connector timeout must be from 1 through 120 seconds")


@dataclass(frozen=True, slots=True)
class EffectPlan:
    """Immutable planning result; write presence alone controls release."""

    run_id: str
    plan_hash: str
    workflow_id: str
    workflow_version: int
    workflow_definition_hash: str
    catalog_content_hash: str
    graph_hash: str
    routing_hash: str
    run_policy: RunRuntimePolicy
    release: EffectPlanRelease
    steps: tuple[EffectPlannedStep, ...]
    proposed_actions: tuple[ProposedExternalAction, ...]
    approval_requests: tuple[ActionApprovalRequest, ...]

    def __post_init__(self) -> None:
        for values, name in (
            (self.steps, "effect plan steps"),
            (self.proposed_actions, "effect plan proposals"),
            (self.approval_requests, "effect plan approval requests"),
        ):
            if type(values) is not tuple:
                raise ValueError(f"{name} must be an immutable tuple")
        for values, expected_type, name in (
            (self.steps, EffectPlannedStep, "effect plan step"),
            (self.proposed_actions, ProposedExternalAction, "effect plan proposal"),
            (self.approval_requests, ActionApprovalRequest, "effect plan approval request"),
        ):
            if any(type(value) is not expected_type for value in values):
                raise ValueError(f"{name} elements must use the exact immutable contract type")
        require_id(self.run_id, "run ID")
        require_id(self.workflow_id, "workflow ID")
        require_digest(self.plan_hash, "effect plan hash")
        require_digest(self.workflow_definition_hash, "workflow definition hash")
        require_digest(self.graph_hash, "effect plan graph hash")
        require_digest(self.routing_hash, "effect plan routing hash")
        if (
            not isinstance(self.workflow_version, int)
            or isinstance(self.workflow_version, bool)
            or self.workflow_version < 1
        ):
            raise ValueError("workflow version must be positive")
        if _CATALOG_HASH.fullmatch(self.catalog_content_hash) is None:
            raise ValueError("effect plan catalog hash is invalid")
        if type(self.run_policy) is not RunRuntimePolicy:
            raise ValueError("effect plan run policy must use the exact immutable contract")
        if self.plan_hash != _effect_plan_hash(
            workflow_id=self.workflow_id,
            workflow_version=self.workflow_version,
            workflow_definition_hash=self.workflow_definition_hash,
            catalog_content_hash=self.catalog_content_hash,
            graph_hash=self.graph_hash,
            routing_hash=self.routing_hash,
            run_policy=self.run_policy,
            steps=self.steps,
        ):
            raise ValueError("effect plan hash does not bind its structural snapshot")
        for step in self.steps:
            if step.runtime_policy.operation_key != runtime_operation_key(
                workflow_id=self.workflow_id,
                workflow_version=self.workflow_version,
                step_key=step.step_key,
            ):
                raise ValueError("planned step runtime operation identity is not structural")
            if self.run_policy.run_timeout_seconds > step.runtime_policy.timeout.run_seconds:
                raise ValueError("effective run timeout exceeds a selected template timeout")
        validate_runtime_plan_budget(
            self.run_policy,
            tuple(
                StepRuntimeDemand(
                    template_id=step.template_id,
                    connector_family=step.connector_family,
                    policy=step.runtime_policy,
                )
                for step in self.steps
            ),
        )
        for step in self.steps:
            if step.connector_family in _NON_CONNECTOR_FAMILIES:
                if (
                    step.binding_id is not None
                    or step.binding_configuration_revision is not None
                    or step.connector_timeout_seconds is not None
                    or step.request_redaction_fields
                    or step.result_redaction_fields
                ):
                    raise ValueError(
                        "non-connector plan steps cannot retain connector contract metadata"
                    )
                if step.connector_family == "model" and (
                    step.request_schema_id is None or step.result_schema_id is None
                ):
                    raise ValueError("model plan steps require their template schema pair")
                if step.connector_family == "artifact" and (
                    step.request_schema_id is not None or step.result_schema_id is not None
                ):
                    raise ValueError("artifact no-call steps cannot retain call schema IDs")
            elif (
                step.binding_id is None
                or step.binding_configuration_revision != step.configuration_revision
                or step.connector_timeout_seconds is None
                or step.request_schema_id is None
                or step.result_schema_id is None
            ):
                raise ValueError(
                    "external plan steps require the complete routed connector contract"
                )
        writes = tuple(step for step in self.steps if step.effect is Effect.WRITE)
        expected_release = (
            EffectPlanRelease.APPROVAL_REQUIRED if writes else EffectPlanRelease.DIRECT
        )
        if self.release is not expected_release:
            raise ValueError("effect plan release must be derived from immutable step effects")
        if not writes:
            if self.proposed_actions or self.approval_requests:
                raise ValueError("direct effect plans cannot retain write approvals")
            return
        if len(self.proposed_actions) != len(writes) or len(self.approval_requests) != len(writes):
            raise ValueError("each write step requires exactly one proposal and approval request")
        for step, proposal, approval in zip(
            writes, self.proposed_actions, self.approval_requests, strict=True
        ):
            envelope = proposal.envelope
            if (
                envelope.run_id != self.run_id
                or envelope.plan_hash != self.plan_hash
                or envelope.step_id != step.runtime_step_id
                or envelope.step_key != step.step_key
                or envelope.template_id != step.template_id
                or envelope.instance_id != step.selected_instance_id
                or envelope.action_type != _action_type(step.capability_id)
                or envelope.capability_id != step.capability_id
                or envelope.connector_family != step.connector_family
                or envelope.binding_id != step.binding_id
                or envelope.payload_schema_id != step.request_schema_id
            ):
                raise ValueError("write proposal is outside its immutable plan step scope")
            expected_projection = ProposedExternalAction.create(
                envelope,
                redacted_destination=safe_approval_destination(envelope.binding_id),
                payload_schema=approval_redaction_schema(step.request_redaction_fields),
            ).redacted_projection
            if (
                proposal.redacted_projection != expected_projection
                or approval.redacted_projection != expected_projection
                or approval.redacted_destination != expected_projection["destination"]
            ):
                raise ValueError("approval projection is outside trusted redaction metadata")
            assert_request_binds_action(approval, envelope)
            policy = approval.policy
            if (
                policy.policy_id != step.approval_policy_id
                or tuple(sorted(policy.required_roles)) != step.approval_required_roles
                or tuple(sorted(policy.required_scopes)) != step.approval_required_scopes
                or policy.expires_after_seconds != step.approval_expires_after_seconds
                or policy.allow_self_approval != step.approval_allow_self_approval
            ):
                raise ValueError("approval policy is outside its immutable plan step scope")

    @property
    def lifecycle_context(self) -> PlanDispositionContext:
        """Derive lifecycle disposition; callers cannot forge write presence."""

        return PlanDispositionContext(
            contains_write_actions=any(step.effect is Effect.WRITE for step in self.steps)
        )


@dataclass(frozen=True, slots=True)
class _CapabilitySnapshot:
    id: str
    effect: Effect
    connector_family: str
    idempotency_support: str


@dataclass(frozen=True, slots=True)
class _TemplateSnapshot:
    id: str
    allowed_capability_ids: frozenset[str]
    operation_classification: str
    approval_policy_id: str
    input_schema_id: str
    output_schema_id: str
    output_schema_hash: str
    retry_policy: RetryPolicySnapshot
    timeout_policy: TimeoutPolicySnapshot
    budget_policy: BudgetPolicySnapshot
    rate_limit_policy: RateLimitPolicySnapshot


@dataclass(frozen=True, slots=True)
class _ApprovalPolicySnapshot:
    id: str
    kind: str
    required_roles: frozenset[str]
    expiry_seconds: int
    allow_self_approval: bool


@dataclass(frozen=True, slots=True)
class _OperationSnapshot:
    capability_id: str
    connector_family: str
    effect: Effect
    request_schema_id: str
    result_schema_id: str
    result_schema_hash: str
    request_redaction_fields: tuple[str, ...]
    result_redaction_fields: tuple[str, ...]
    data_classification: DataClassification
    idempotency_support: str
    default_timeout_seconds: int
    enabled: bool
    disabled_reason: str | None
    request_type: type[BaseModel]
    result_type: type[BaseModel] | type[ConnectorWriteResult]


@dataclass(frozen=True, slots=True)
class _BindingSnapshot:
    instance_id: str
    connector_family: str
    binding_id: str
    enabled: bool
    configuration_revision: int


class EffectAwarePlanner:
    """Snapshot metadata, validate every effect, and propose writes without I/O."""

    def __init__(
        self,
        *,
        catalog_content_hash: str,
        clock: Clock,
        ids: IdGenerator,
        capabilities: Sequence[PlanningCapabilitySource],
        templates: Sequence[PlanningTemplateSource],
        template_output_schemas: Mapping[str, Mapping[str, Any]],
        approval_policies: Sequence[PlanningApprovalPolicySource],
        operations: Sequence[PlanningOperationSource],
        bindings: Sequence[PlanningBindingSource],
        run_policy: RunRuntimePolicy,
    ) -> None:
        if _CATALOG_HASH.fullmatch(catalog_content_hash) is None:
            raise EffectPlanningError(
                "invalid_catalog_hash", "planner requires a compiled-catalog hash"
            )
        self._catalog_content_hash = catalog_content_hash
        self._clock = clock
        self._ids = ids
        if type(run_policy) is not RunRuntimePolicy:
            raise EffectPlanningError(
                "invalid_run_runtime_policy",
                "planner requires an exact trusted immutable run policy",
            )
        self._run_policy = run_policy
        self._capabilities = self._snapshot_capabilities(capabilities)
        self._templates = self._snapshot_templates(templates, template_output_schemas)
        self._policies = self._snapshot_policies(approval_policies)
        self._operations = self._snapshot_operations(operations)
        self._bindings = self._snapshot_bindings(bindings)
        self._validate_snapshot_parity()

    def plan(self, request: EffectPlanRequest) -> EffectPlan:
        ordered_specs = self._validate_graph_coverage(request)
        route_instances = {item.instance_id: item for item in request.routing.selected_instances}
        assignments = {item.slot_key: item for item in request.routing.assignments}
        if request.routing.catalog_content_hash != self._catalog_content_hash:
            raise EffectPlanningError(
                "catalog_drift", "routing and effect metadata must use the same catalog release"
            )

        planned_steps: list[EffectPlannedStep] = []
        resolved: list[
            tuple[
                EffectStepSpec,
                EffectPlannedStep,
                _TemplateSnapshot,
                _OperationSnapshot | None,
                ApprovalPolicySnapshot | None,
            ]
        ] = []
        for spec in ordered_specs:
            try:
                selected = route_instances[spec.selected_instance_id]
                template = self._templates[selected.template_id]
                capability = self._capabilities[spec.capability_id]
            except KeyError as exc:
                raise EffectPlanningError(
                    "unknown_plan_reference",
                    "step references an unknown routed instance, template, or capability",
                ) from exc
            if template.id != selected.template_id:
                raise EffectPlanningError("template_drift", "routed template identity drifted")
            if spec.capability_id not in template.allowed_capability_ids:
                raise EffectPlanningError(
                    "capability_not_allowed", "template does not allow the requested capability"
                )
            self._validate_route_binding(request.routing, spec, selected.template_id, assignments)
            operation = self._validate_operation(capability, spec)
            binding = self._validate_binding(
                spec,
                capability,
                configuration_revision=selected.configuration_revision,
            )
            approval_policy = (
                self._write_policy(template) if capability.effect is Effect.WRITE else None
            )
            planned = EffectPlannedStep(
                runtime_step_id=spec.runtime_step_id,
                step_key=spec.step_key,
                kind=spec.kind,
                selected_instance_id=selected.instance_id,
                routing_slot_key=spec.routing_slot_key,
                template_id=selected.template_id,
                configuration_revision=selected.configuration_revision,
                capability_id=capability.id,
                effect=capability.effect,
                connector_family=capability.connector_family,
                binding_id=spec.binding_id,
                binding_configuration_revision=(
                    binding.configuration_revision if binding else None
                ),
                request_schema_id=(
                    operation.request_schema_id
                    if operation
                    else template.input_schema_id
                    if capability.connector_family == "model"
                    else None
                ),
                result_schema_id=(
                    operation.result_schema_id
                    if operation
                    else template.output_schema_id
                    if capability.connector_family == "model"
                    else None
                ),
                result_schema_hash=(
                    operation.result_schema_hash
                    if operation
                    else template.output_schema_hash
                    if capability.connector_family == "model"
                    else None
                ),
                request_redaction_fields=(operation.request_redaction_fields if operation else ()),
                result_redaction_fields=(operation.result_redaction_fields if operation else ()),
                data_classification=(
                    operation.data_classification if operation else DataClassification.INTERNAL
                ),
                idempotency_support=capability.idempotency_support,
                connector_timeout_seconds=(
                    operation.default_timeout_seconds if operation else None
                ),
                approval_policy_id=template.approval_policy_id,
                approval_required_roles=(
                    tuple(sorted(approval_policy.required_roles)) if approval_policy else ()
                ),
                approval_required_scopes=(
                    tuple(sorted(approval_policy.required_scopes)) if approval_policy else ()
                ),
                approval_expires_after_seconds=(
                    approval_policy.expires_after_seconds if approval_policy else None
                ),
                approval_allow_self_approval=(
                    approval_policy.allow_self_approval if approval_policy else None
                ),
                runtime_policy=StepRuntimePolicy(
                    operation_key=runtime_operation_key(
                        workflow_id=request.routing.workflow_id,
                        workflow_version=request.routing.workflow_version,
                        step_key=spec.step_key,
                    ),
                    attempt_kind=attempt_kind_for_connector(capability.connector_family),
                    retry=template.retry_policy,
                    timeout=template.timeout_policy,
                    budget=template.budget_policy,
                    rate_limit=template.rate_limit_policy,
                ),
            )
            self._validate_intent(spec, planned, template, operation)
            planned_steps.append(planned)
            resolved.append((spec, planned, template, operation, approval_policy))

        step_tuple = tuple(planned_steps)
        try:
            selected_run_timeouts = tuple(
                self._templates[item.template_id].timeout_policy.run_seconds
                for item in request.routing.selected_instances
            )
        except KeyError as exc:  # pragma: no cover - selected step lookup normally catches this
            raise EffectPlanningError(
                "unknown_plan_reference",
                "routing selection references an unknown template runtime policy",
            ) from exc
        run_policy = replace(
            self._run_policy,
            run_timeout_seconds=min(
                self._run_policy.run_timeout_seconds,
                *selected_run_timeouts,
            ),
        )
        try:
            validate_runtime_plan_budget(
                run_policy,
                tuple(
                    StepRuntimeDemand(
                        template_id=step.template_id,
                        connector_family=step.connector_family,
                        policy=step.runtime_policy,
                    )
                    for step in step_tuple
                ),
            )
        except RuntimePlanningBudgetError as exc:
            raise EffectPlanningError(exc.code, str(exc)) from exc
        plan_hash = self._plan_hash(request, step_tuple, run_policy)
        write_rows = [row for row in resolved if row[1].effect is Effect.WRITE]
        if not write_rows:
            return EffectPlan(
                run_id=request.run_id,
                plan_hash=plan_hash,
                workflow_id=request.routing.workflow_id,
                workflow_version=request.routing.workflow_version,
                workflow_definition_hash=request.workflow_definition_hash,
                catalog_content_hash=request.routing.catalog_content_hash,
                graph_hash=request.graph.semantic_hash,
                routing_hash=request.routing.semantic_hash,
                run_policy=run_policy,
                release=EffectPlanRelease.DIRECT,
                steps=step_tuple,
                proposed_actions=(),
                approval_requests=(),
            )

        authorization_set_id = self._ids.new("authorization-set")
        requested_at = self._clock.now()
        proposed_actions: list[ProposedExternalAction] = []
        approval_requests: list[ActionApprovalRequest] = []
        for spec, planned, _, operation, policy in write_rows:
            if (
                spec.write_intent is None or operation is None or policy is None
            ):  # pragma: no cover - invariant
                raise AssertionError("validated write row lost its operation, intent, or policy")
            intent = spec.write_intent
            payload = json.loads(canonical_json_bytes(intent.payload_snapshot))
            destination = _canonical_destination(payload)
            semantic = SemanticExternalAction(
                template_id=planned.template_id,
                instance_id=planned.selected_instance_id,
                action_type=_action_type(planned.capability_id),
                capability_id=planned.capability_id,
                connector_family=planned.connector_family,
                binding_id=planned.binding_id or "",
                destination=destination,
                payload_schema_id=operation.request_schema_id,
                minimized_payload=payload,
            )
            envelope = CanonicalExternalAction(
                action_id=self._ids.new("external-action"),
                authorization_set_id=authorization_set_id,
                run_id=request.run_id,
                plan_hash=plan_hash,
                proposal_revision=1,
                step_id=planned.runtime_step_id,
                step_key=planned.step_key,
                template_id=planned.template_id,
                instance_id=planned.selected_instance_id,
                action_type=semantic.action_type,
                capability_id=planned.capability_id,
                connector_family=planned.connector_family,
                binding_id=semantic.binding_id,
                destination=destination,
                payload_schema_id=operation.request_schema_id,
                minimized_payload=payload,
                semantic_action_hash=semantic_action_hash(semantic),
            )
            proposal = ProposedExternalAction.create(
                envelope,
                redacted_destination=safe_approval_destination(envelope.binding_id),
                payload_schema=approval_redaction_schema(operation.request_redaction_fields),
            )
            proposed_actions.append(proposal)
            approval_requests.append(
                request_approval(
                    request_id=self._ids.new("approval-request"),
                    proposed_action=proposal,
                    policy=policy,
                    requested_by=request.requested_by,
                    requested_at=requested_at,
                )
            )
        return EffectPlan(
            run_id=request.run_id,
            plan_hash=plan_hash,
            workflow_id=request.routing.workflow_id,
            workflow_version=request.routing.workflow_version,
            workflow_definition_hash=request.workflow_definition_hash,
            catalog_content_hash=request.routing.catalog_content_hash,
            graph_hash=request.graph.semantic_hash,
            routing_hash=request.routing.semantic_hash,
            run_policy=run_policy,
            release=EffectPlanRelease.APPROVAL_REQUIRED,
            steps=step_tuple,
            proposed_actions=tuple(proposed_actions),
            approval_requests=tuple(approval_requests),
        )

    def _validate_graph_coverage(self, request: EffectPlanRequest) -> tuple[EffectStepSpec, ...]:
        by_key: dict[str, EffectStepSpec] = {}
        runtime_ids: set[str] = set()
        for spec in request.steps:
            if spec.step_key in by_key:
                raise EffectPlanningError("duplicate_step", "effect plan step keys must be unique")
            if spec.runtime_step_id in runtime_ids:
                raise EffectPlanningError(
                    "duplicate_runtime_step", "runtime step IDs must be unique"
                )
            by_key[spec.step_key] = spec
            runtime_ids.add(spec.runtime_step_id)
        if set(by_key) != set(request.graph.topological_order):
            raise EffectPlanningError(
                "graph_step_mismatch", "effect steps must exactly cover the dependency graph"
            )
        require_digest(request.graph.semantic_hash, "dependency graph hash")
        require_digest(request.routing.semantic_hash, "routing hash")
        return tuple(by_key[key] for key in request.graph.topological_order)

    def _validate_route_binding(
        self,
        routing: RoutingResult,
        spec: EffectStepSpec,
        template_id: str,
        assignments: Mapping[str, Any],
    ) -> None:
        if spec.routing_slot_key is None:
            if spec.selected_instance_id != routing.target_instance_id:
                raise EffectPlanningError(
                    "unbound_route", "non-target selected instances require an exact routing slot"
                )
            return
        assignment = assignments.get(spec.routing_slot_key)
        if (
            assignment is None
            or assignment.instance_id != spec.selected_instance_id
            or assignment.template_id != template_id
        ):
            raise EffectPlanningError(
                "routing_assignment_mismatch", "step does not match its routing assignment"
            )
        if (
            assignment.required_capability_ids
            and spec.capability_id not in assignment.required_capability_ids
        ):
            raise EffectPlanningError(
                "routing_capability_mismatch", "step capability is outside its routing slot"
            )

    def _validate_operation(
        self, capability: _CapabilitySnapshot, spec: EffectStepSpec
    ) -> _OperationSnapshot | None:
        if capability.connector_family in _NON_CONNECTOR_FAMILIES:
            if capability.effect is not Effect.READ:
                raise EffectPlanningError(
                    "non_connector_write", "model and artifact capabilities must be read-only"
                )
            if spec.binding_id is not None:
                raise EffectPlanningError(
                    "unexpected_binding", "non-connector capabilities cannot use connector bindings"
                )
            return None
        if spec.binding_id is None:
            raise EffectPlanningError(
                "binding_required", "external connector capabilities require a binding"
            )
        try:
            operation = self._operations[capability.id]
        except KeyError as exc:
            raise EffectPlanningError(
                "operation_missing", "external capability has no registered operation"
            ) from exc
        if not operation.enabled:
            raise EffectPlanningError(
                "operation_disabled",
                f"external operation is disabled: {operation.disabled_reason}",
            )
        if (
            operation.connector_family != capability.connector_family
            or operation.effect is not capability.effect
            or operation.idempotency_support != capability.idempotency_support
        ):
            raise EffectPlanningError(
                "operation_metadata_drift", "catalog and connector operation metadata differ"
            )
        return operation

    def _validate_binding(
        self,
        spec: EffectStepSpec,
        capability: _CapabilitySnapshot,
        *,
        configuration_revision: int,
    ) -> _BindingSnapshot | None:
        if capability.connector_family in _NON_CONNECTOR_FAMILIES:
            return None
        try:
            binding = self._bindings[(spec.selected_instance_id, capability.connector_family)]
        except KeyError as exc:
            raise EffectPlanningError(
                "binding_missing",
                "selected instance has no effective binding for the connector family",
            ) from exc
        if not binding.enabled:
            raise EffectPlanningError("binding_disabled", "effective connector binding is disabled")
        if binding.configuration_revision != configuration_revision:
            raise EffectPlanningError(
                "binding_revision_drift",
                "effective binding revision differs from the routed instance snapshot",
            )
        if binding.binding_id != spec.binding_id:
            raise EffectPlanningError(
                "binding_mismatch", "step binding is not the selected instance's effective binding"
            )
        return binding

    def _validate_intent(
        self,
        spec: EffectStepSpec,
        planned: EffectPlannedStep,
        template: _TemplateSnapshot,
        operation: _OperationSnapshot | None,
    ) -> None:
        if planned.effect is Effect.READ:
            if spec.write_intent is not None:
                raise EffectPlanningError(
                    "read_has_write_intent", "read-only steps cannot carry write action details"
                )
            return
        if template.operation_classification != "mutating":
            raise EffectPlanningError(
                "template_effect_mismatch", "read-only template cannot plan a write"
            )
        if spec.write_intent is None or operation is None:
            raise EffectPlanningError(
                "write_intent_required", "write steps require a typed external action intent"
            )
        if type(spec.write_intent.command) is not operation.request_type:
            raise EffectPlanningError(
                "command_type_mismatch", "write command type does not match the registry"
            )
        if operation.idempotency_support != "required":
            raise EffectPlanningError(
                "write_idempotency_unavailable", "external writes require provider idempotency"
            )
        self._write_policy(template)

    def _write_policy(self, template: _TemplateSnapshot) -> ApprovalPolicySnapshot:
        try:
            policy = self._policies[template.approval_policy_id]
        except KeyError as exc:
            raise EffectPlanningError(
                "approval_policy_missing", "write template has no approval policy"
            ) from exc
        if policy.kind != "human_external_write" or not policy.required_roles:
            raise EffectPlanningError(
                "approval_policy_invalid", "external writes require a human approval policy"
            )
        return ApprovalPolicySnapshot(
            policy_id=policy.id,
            required_roles=policy.required_roles,
            required_scopes=frozenset({EXTERNAL_WRITE_SCOPE}),
            expires_after_seconds=policy.expiry_seconds,
            allow_self_approval=policy.allow_self_approval,
        )

    def _plan_hash(
        self,
        request: EffectPlanRequest,
        steps: tuple[EffectPlannedStep, ...],
        run_policy: RunRuntimePolicy,
    ) -> str:
        return _effect_plan_hash(
            workflow_id=request.routing.workflow_id,
            workflow_version=request.routing.workflow_version,
            workflow_definition_hash=request.workflow_definition_hash,
            catalog_content_hash=request.routing.catalog_content_hash,
            graph_hash=request.graph.semantic_hash,
            routing_hash=request.routing.semantic_hash,
            run_policy=run_policy,
            steps=steps,
        )

    def _validate_snapshot_parity(self) -> None:
        external_capabilities = {
            capability_id
            for capability_id, capability in self._capabilities.items()
            if capability.connector_family not in _NON_CONNECTOR_FAMILIES
        }
        if external_capabilities != set(self._operations):
            raise EffectPlanningError(
                "operation_set_drift",
                "external capability and operation sets must match exactly",
            )
        for capability_id, operation in self._operations.items():
            capability = self._capabilities[capability_id]
            if (
                operation.connector_family != capability.connector_family
                or operation.effect is not capability.effect
                or operation.idempotency_support != capability.idempotency_support
            ):
                raise EffectPlanningError(
                    "operation_metadata_drift",
                    "catalog and connector operation metadata differ",
                )
            if capability.effect is Effect.READ:
                if not isinstance(operation.result_type, type) or not issubclass(
                    operation.result_type, BaseModel
                ):
                    raise EffectPlanningError(
                        "invalid_operation",
                        "READ operation result type must be a Pydantic model",
                    )
            elif operation.result_type is not ConnectorWriteResult:
                raise EffectPlanningError(
                    "invalid_operation",
                    "WRITE operation result type must use ConnectorWriteResult",
                )
        for template in self._templates.values():
            if not template.allowed_capability_ids <= set(self._capabilities):
                raise EffectPlanningError(
                    "template_capability_drift",
                    "template references an unknown capability",
                )
            if template.approval_policy_id not in self._policies:
                raise EffectPlanningError(
                    "template_policy_drift", "template references an unknown approval policy"
                )

    @staticmethod
    def _snapshot_capabilities(
        sources: Sequence[PlanningCapabilitySource],
    ) -> Mapping[str, _CapabilitySnapshot]:
        result: dict[str, _CapabilitySnapshot] = {}
        for source in sources:
            try:
                require_id(source.id, "capability ID")
                require_id(source.connector_family, "capability connector family")
            except ValueError as exc:
                raise EffectPlanningError("invalid_capability", str(exc)) from exc
            if source.id in result:
                raise EffectPlanningError(
                    "duplicate_capability", "capability metadata must be unique"
                )
            try:
                effect = Effect(source.effect)
            except ValueError as exc:
                raise EffectPlanningError(
                    "invalid_effect", "capability effect must be read or write"
                ) from exc
            if source.idempotency_support not in {
                "not_applicable",
                "required",
                "supported",
                "unavailable",
            }:
                raise EffectPlanningError(
                    "invalid_capability", "capability idempotency support is invalid"
                )
            result[source.id] = _CapabilitySnapshot(
                id=source.id,
                effect=effect,
                connector_family=source.connector_family,
                idempotency_support=source.idempotency_support,
            )
        return result

    @staticmethod
    def _snapshot_templates(
        sources: Sequence[PlanningTemplateSource],
        output_schemas: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, _TemplateSnapshot]:
        if set(output_schemas) != {source.id for source in sources}:
            raise EffectPlanningError(
                "template_schema_set_drift",
                "template output schema bodies must exactly match the template snapshot",
            )
        result: dict[str, _TemplateSnapshot] = {}
        for source in sources:
            try:
                require_id(source.id, "template ID")
                require_id(source.approval_policy_id, "approval policy ID")
                require_id(source.input_schema_id, "template input schema ID")
                require_id(source.output_schema_id, "template output schema ID")
                for capability_id in source.allowed_tool_capability_ids:
                    require_id(capability_id, "template capability ID")
            except (AttributeError, TypeError, ValueError) as exc:
                raise EffectPlanningError("invalid_template", str(exc)) from exc
            if source.id in result:
                raise EffectPlanningError("duplicate_template", "templates must be unique")
            if len(source.allowed_tool_capability_ids) != len(
                set(source.allowed_tool_capability_ids)
            ):
                raise EffectPlanningError(
                    "invalid_template", "template capabilities must be unique"
                )
            if source.operation_classification not in {"read_only", "mutating"}:
                raise EffectPlanningError(
                    "invalid_template", "template operation classification is invalid"
                )
            try:
                retry_policy = RetryPolicySnapshot(
                    max_attempts=source.retry_policy.max_attempts,
                    backoff=RetryBackoff(source.retry_policy.backoff),
                )
                timeout_policy = TimeoutPolicySnapshot(
                    step_seconds=source.timeout_policy.step_seconds,
                    run_seconds=source.timeout_policy.run_seconds,
                )
                budget_policy = BudgetPolicySnapshot(
                    max_steps=source.budget_policy.max_steps,
                    max_model_calls=source.budget_policy.max_model_calls,
                    max_tool_calls=source.budget_policy.max_tool_calls,
                    max_input_bytes=source.budget_policy.max_input_bytes,
                    max_input_field_bytes=source.budget_policy.max_input_field_bytes,
                    max_output_bytes=source.budget_policy.max_output_bytes,
                    max_model_output_tokens=source.budget_policy.max_model_output_tokens,
                )
                rate_limit_policy = RateLimitPolicySnapshot(
                    scope=RateLimitScope.TEMPLATE,
                    key=runtime_rate_limit_key(
                        template_id=source.id,
                        max_calls=source.rate_limit_policy.max_calls,
                        window_seconds=source.rate_limit_policy.window_seconds,
                    ),
                    max_calls=source.rate_limit_policy.max_calls,
                    window_seconds=source.rate_limit_policy.window_seconds,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise EffectPlanningError(
                    "invalid_template_runtime_policy",
                    "template runtime policy is missing, malformed, or outside global bounds",
                ) from exc
            result[source.id] = _TemplateSnapshot(
                id=source.id,
                allowed_capability_ids=frozenset(source.allowed_tool_capability_ids),
                operation_classification=source.operation_classification,
                approval_policy_id=source.approval_policy_id,
                input_schema_id=source.input_schema_id,
                output_schema_id=source.output_schema_id,
                output_schema_hash=canonical_schema_hash(output_schemas[source.id]),
                retry_policy=retry_policy,
                timeout_policy=timeout_policy,
                budget_policy=budget_policy,
                rate_limit_policy=rate_limit_policy,
            )
        return result

    @staticmethod
    def _snapshot_policies(
        sources: Sequence[PlanningApprovalPolicySource],
    ) -> Mapping[str, _ApprovalPolicySnapshot]:
        result: dict[str, _ApprovalPolicySnapshot] = {}
        for source in sources:
            try:
                require_id(source.id, "approval policy ID")
                for role in source.required_roles:
                    require_id(role, "approval role")
            except ValueError as exc:
                raise EffectPlanningError("invalid_policy", str(exc)) from exc
            if source.id in result:
                raise EffectPlanningError("duplicate_policy", "approval policies must be unique")
            if source.kind not in {"none", "human_external_write"}:
                raise EffectPlanningError("invalid_policy", "approval policy kind is invalid")
            if len(source.required_roles) != len(set(source.required_roles)):
                raise EffectPlanningError("invalid_policy", "approval roles must be unique")
            if (
                not isinstance(source.expiry_seconds, int)
                or isinstance(source.expiry_seconds, bool)
                or not 60 <= source.expiry_seconds <= 86_400
                or not isinstance(source.allow_self_approval, bool)
            ):
                raise EffectPlanningError("invalid_policy", "approval policy values are invalid")
            result[source.id] = _ApprovalPolicySnapshot(
                id=source.id,
                kind=source.kind,
                required_roles=frozenset(source.required_roles),
                expiry_seconds=source.expiry_seconds,
                allow_self_approval=source.allow_self_approval,
            )
        return result

    @staticmethod
    def _snapshot_operations(
        sources: Sequence[PlanningOperationSource],
    ) -> Mapping[str, _OperationSnapshot]:
        result: dict[str, _OperationSnapshot] = {}
        for source in sources:
            metadata = source.metadata
            try:
                require_id(metadata.capability_id, "operation capability ID")
                require_id(metadata.connector_family, "operation connector family")
                require_id(metadata.request_schema_id, "operation request schema ID")
                require_id(metadata.result_schema_id, "operation result schema ID")
            except ValueError as exc:
                raise EffectPlanningError("invalid_operation", str(exc)) from exc
            if metadata.capability_id in result:
                raise EffectPlanningError("duplicate_operation", "operations must be unique")
            if not isinstance(metadata.effect, Effect):
                raise EffectPlanningError("invalid_operation", "operation effect is invalid")
            if metadata.idempotency_support not in {
                "not_applicable",
                "required",
                "supported",
                "unavailable",
            }:
                raise EffectPlanningError(
                    "invalid_operation", "operation idempotency support is invalid"
                )
            if type(metadata.data_classification) is not DataClassification:
                raise EffectPlanningError(
                    "invalid_operation",
                    "operation data classification must use the exact enum",
                )
            if (
                not isinstance(metadata.default_timeout_seconds, int)
                or isinstance(metadata.default_timeout_seconds, bool)
                or not 1 <= metadata.default_timeout_seconds <= 120
            ):
                raise EffectPlanningError(
                    "invalid_operation", "operation timeout must be from 1 through 120 seconds"
                )
            redaction_fields = tuple(metadata.request_redaction_fields)
            if len(redaction_fields) != len(set(redaction_fields)) or any(
                not field.startswith("/") or field == "/" or "//" in field or "*" in field
                for field in redaction_fields
            ):
                raise EffectPlanningError(
                    "invalid_operation", "request redaction fields must be unique JSON pointers"
                )
            result_redaction_fields = tuple(metadata.result_redaction_fields)
            try:
                require_json_pointers(
                    result_redaction_fields,
                    "operation result redaction fields",
                )
            except ValueError as exc:
                raise EffectPlanningError(
                    "invalid_operation", "result redaction fields must be unique JSON pointers"
                ) from exc
            if not isinstance(metadata.enabled, bool) or metadata.enabled == (
                metadata.disabled_reason is not None
            ):
                raise EffectPlanningError(
                    "invalid_operation", "operation enabled state and reason are inconsistent"
                )
            if not isinstance(source.request_type, type) or not issubclass(
                source.request_type, BaseModel
            ):
                raise EffectPlanningError(
                    "invalid_operation", "operation request type must be a Pydantic model"
                )
            if isinstance(source.result_type, type) and issubclass(source.result_type, BaseModel):
                result_schema = source.result_type.model_json_schema()
            elif source.result_type is ConnectorWriteResult:
                result_schema = TypeAdapter(ConnectorWriteResult).json_schema()
            else:
                raise EffectPlanningError(
                    "invalid_operation",
                    "operation result type cannot expose a JSON Schema",
                )
            result[metadata.capability_id] = _OperationSnapshot(
                capability_id=metadata.capability_id,
                connector_family=metadata.connector_family,
                effect=metadata.effect,
                request_schema_id=metadata.request_schema_id,
                result_schema_id=metadata.result_schema_id,
                result_schema_hash=canonical_schema_hash(result_schema),
                request_redaction_fields=redaction_fields,
                result_redaction_fields=result_redaction_fields,
                data_classification=metadata.data_classification,
                idempotency_support=metadata.idempotency_support,
                default_timeout_seconds=metadata.default_timeout_seconds,
                enabled=metadata.enabled,
                disabled_reason=metadata.disabled_reason,
                request_type=source.request_type,
                result_type=source.result_type,
            )
        return result

    @staticmethod
    def _snapshot_bindings(
        sources: Sequence[PlanningBindingSource],
    ) -> Mapping[tuple[str, str], _BindingSnapshot]:
        result: dict[tuple[str, str], _BindingSnapshot] = {}
        for source in sources:
            key = (source.instance_id, source.connector_family)
            if key in result:
                raise EffectPlanningError(
                    "duplicate_binding", "one effective binding is allowed per instance and family"
                )
            try:
                require_id(source.instance_id, "binding instance ID")
                require_id(source.connector_family, "binding connector family")
                require_id(source.binding_id, "binding ID")
            except ValueError as exc:
                raise EffectPlanningError("invalid_binding", str(exc)) from exc
            if (
                not isinstance(source.configuration_revision, int)
                or isinstance(source.configuration_revision, bool)
                or source.configuration_revision < 1
            ):
                raise EffectPlanningError(
                    "invalid_binding", "binding configuration revision must be positive"
                )
            if not isinstance(source.enabled, bool):
                raise EffectPlanningError("invalid_binding", "binding enabled flag must be boolean")
            result[key] = _BindingSnapshot(
                instance_id=source.instance_id,
                connector_family=source.connector_family,
                binding_id=source.binding_id,
                enabled=source.enabled,
                configuration_revision=source.configuration_revision,
            )
        return result


def _effect_plan_hash(
    *,
    workflow_id: str,
    workflow_version: int,
    workflow_definition_hash: str,
    catalog_content_hash: str,
    graph_hash: str,
    routing_hash: str,
    run_policy: RunRuntimePolicy,
    steps: tuple[EffectPlannedStep, ...],
) -> str:
    return effect_plan_hash(
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        workflow_definition_hash=workflow_definition_hash,
        catalog_content_hash=catalog_content_hash,
        graph_hash=graph_hash,
        routing_hash=routing_hash,
        run_policy=run_policy,
        steps=tuple(
            EffectPlanStepHashMaterial(
                step_key=step.step_key,
                kind=step.kind,
                selected_instance_id=step.selected_instance_id,
                routing_slot_key=step.routing_slot_key,
                template_id=step.template_id,
                configuration_revision=step.configuration_revision,
                capability_id=step.capability_id,
                effect=step.effect,
                connector_family=step.connector_family,
                binding_id=step.binding_id,
                binding_configuration_revision=step.binding_configuration_revision,
                request_schema_id=step.request_schema_id,
                result_schema_id=step.result_schema_id,
                result_schema_hash=step.result_schema_hash,
                request_redaction_fields=step.request_redaction_fields,
                result_redaction_fields=step.result_redaction_fields,
                data_classification=step.data_classification,
                idempotency_support=step.idempotency_support,
                connector_timeout_seconds=step.connector_timeout_seconds,
                approval_policy_id=step.approval_policy_id,
                approval_required_roles=step.approval_required_roles,
                approval_required_scopes=step.approval_required_scopes,
                approval_expires_after_seconds=step.approval_expires_after_seconds,
                approval_allow_self_approval=step.approval_allow_self_approval,
                runtime_policy=step.runtime_policy,
            )
            for step in steps
        ),
    )


def _action_type(capability_id: str) -> str:
    value = capability_id.removeprefix("cap.")
    require_id(value, "action type")
    return value


def _canonical_destination(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_DESTINATION_HASH_DOMAIN + canonical_json_bytes(payload)).hexdigest()
    return f"destination-sha256-v1:{digest}"
