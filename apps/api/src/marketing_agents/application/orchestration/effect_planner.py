"""Pure effect-aware planning and immutable external-write proposals."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel

from marketing_agents.application.orchestration.router import RoutingResult
from marketing_agents.application.ports.clock import Clock
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
from marketing_agents.domain.entities._validation import (
    frozen_json_mapping,
    require_digest,
    require_id,
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
    def idempotency_support(self) -> str: ...

    @property
    def default_timeout_seconds(self) -> int: ...

    @property
    def request_redaction_fields(self) -> Sequence[str]: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def disabled_reason(self) -> str | None: ...


class PlanningOperationSource(Protocol):
    @property
    def metadata(self) -> PlanningOperationMetadataSource: ...

    @property
    def request_type(self) -> type[BaseModel]: ...


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
    request_redaction_fields: tuple[str, ...]
    idempotency_support: str
    connector_timeout_seconds: int | None
    approval_policy_id: str
    approval_required_roles: tuple[str, ...]
    approval_required_scopes: tuple[str, ...]
    approval_expires_after_seconds: int | None
    approval_allow_self_approval: bool | None

    def __post_init__(self) -> None:
        for values, name in (
            (self.request_redaction_fields, "request redaction fields"),
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
        if self.plan_hash != _effect_plan_hash(
            workflow_id=self.workflow_id,
            workflow_version=self.workflow_version,
            workflow_definition_hash=self.workflow_definition_hash,
            catalog_content_hash=self.catalog_content_hash,
            graph_hash=self.graph_hash,
            routing_hash=self.routing_hash,
            steps=self.steps,
        ):
            raise ValueError("effect plan hash does not bind its structural snapshot")
        for step in self.steps:
            if step.connector_family in _NON_CONNECTOR_FAMILIES:
                if (
                    step.binding_id is not None
                    or step.binding_configuration_revision is not None
                    or step.connector_timeout_seconds is not None
                ):
                    raise ValueError("non-connector plan steps cannot retain bindings")
            elif (
                step.binding_id is None
                or step.binding_configuration_revision != step.configuration_revision
                or step.connector_timeout_seconds is None
            ):
                raise ValueError("external plan steps require the routed effective binding")
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
    request_redaction_fields: tuple[str, ...]
    idempotency_support: str
    default_timeout_seconds: int
    enabled: bool
    disabled_reason: str | None
    request_type: type[BaseModel]


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
        approval_policies: Sequence[PlanningApprovalPolicySource],
        operations: Sequence[PlanningOperationSource],
        bindings: Sequence[PlanningBindingSource],
    ) -> None:
        if _CATALOG_HASH.fullmatch(catalog_content_hash) is None:
            raise EffectPlanningError(
                "invalid_catalog_hash", "planner requires a compiled-catalog hash"
            )
        self._catalog_content_hash = catalog_content_hash
        self._clock = clock
        self._ids = ids
        self._capabilities = self._snapshot_capabilities(capabilities)
        self._templates = self._snapshot_templates(templates)
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
                request_schema_id=operation.request_schema_id if operation else None,
                request_redaction_fields=(operation.request_redaction_fields if operation else ()),
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
            )
            self._validate_intent(spec, planned, template, operation)
            planned_steps.append(planned)
            resolved.append((spec, planned, template, operation, approval_policy))

        plan_hash = self._plan_hash(request, tuple(planned_steps))
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
                release=EffectPlanRelease.DIRECT,
                steps=tuple(planned_steps),
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
            release=EffectPlanRelease.APPROVAL_REQUIRED,
            steps=tuple(planned_steps),
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

    def _plan_hash(self, request: EffectPlanRequest, steps: tuple[EffectPlannedStep, ...]) -> str:
        return _effect_plan_hash(
            workflow_id=request.routing.workflow_id,
            workflow_version=request.routing.workflow_version,
            workflow_definition_hash=request.workflow_definition_hash,
            catalog_content_hash=request.routing.catalog_content_hash,
            graph_hash=request.graph.semantic_hash,
            routing_hash=request.routing.semantic_hash,
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
    ) -> Mapping[str, _TemplateSnapshot]:
        result: dict[str, _TemplateSnapshot] = {}
        for source in sources:
            try:
                require_id(source.id, "template ID")
                require_id(source.approval_policy_id, "approval policy ID")
                for capability_id in source.allowed_tool_capability_ids:
                    require_id(capability_id, "template capability ID")
            except ValueError as exc:
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
            result[source.id] = _TemplateSnapshot(
                id=source.id,
                allowed_capability_ids=frozenset(source.allowed_tool_capability_ids),
                operation_classification=source.operation_classification,
                approval_policy_id=source.approval_policy_id,
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
            result[metadata.capability_id] = _OperationSnapshot(
                capability_id=metadata.capability_id,
                connector_family=metadata.connector_family,
                effect=metadata.effect,
                request_schema_id=metadata.request_schema_id,
                request_redaction_fields=redaction_fields,
                idempotency_support=metadata.idempotency_support,
                default_timeout_seconds=metadata.default_timeout_seconds,
                enabled=metadata.enabled,
                disabled_reason=metadata.disabled_reason,
                request_type=source.request_type,
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
    steps: tuple[EffectPlannedStep, ...],
) -> str:
    return effect_plan_hash(
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        workflow_definition_hash=workflow_definition_hash,
        catalog_content_hash=catalog_content_hash,
        graph_hash=graph_hash,
        routing_hash=routing_hash,
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
                request_redaction_fields=step.request_redaction_fields,
                idempotency_support=step.idempotency_support,
                connector_timeout_seconds=step.connector_timeout_seconds,
                approval_policy_id=step.approval_policy_id,
                approval_required_roles=step.approval_required_roles,
                approval_required_scopes=step.approval_required_scopes,
                approval_expires_after_seconds=step.approval_expires_after_seconds,
                approval_allow_self_approval=step.approval_allow_self_approval,
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
