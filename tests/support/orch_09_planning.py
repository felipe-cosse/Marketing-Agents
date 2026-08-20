"""Small real-router/real-planner fixture for audited read-only plan tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from marketing_agents.application.orchestration import (
    DeterministicInstanceRouter,
    EffectAwarePlanner,
    EffectPlan,
    EffectPlanRequest,
    EffectStepSpec,
    RoutingRequest,
    RoutingResult,
    WorkflowRoutingDefinition,
)
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.domain.graph import DependencyGraph, TopologyStep


@dataclass(frozen=True, slots=True)
class _Capability:
    id: str = "cap.model.read"
    effect: str = "read"
    connector_family: str = "model"
    idempotency_support: str = "not_applicable"


@dataclass(frozen=True, slots=True)
class _Template:
    id: str = "tpl.test.read-only"
    display_order: int = 1
    allowed_tool_capability_ids: tuple[str, ...] = ("cap.model.read",)
    supported_trigger_types: tuple[str, ...] = ("manual",)
    operation_classification: str = "read_only"
    approval_policy_id: str = "approval.none"


@dataclass(frozen=True, slots=True)
class _Instance:
    id: str
    template_id: str = "tpl.test.read-only"
    display_order: int = 1
    enabled: bool = True
    variant: None = None
    configuration_revision: int = 1


@dataclass(frozen=True, slots=True)
class _Policy:
    id: str = "approval.none"
    kind: str = "none"
    required_roles: tuple[str, ...] = ()
    expiry_seconds: int = 60
    allow_self_approval: bool = False


class _UnexpectedClock:
    def now(self) -> datetime:
        raise AssertionError("read-only effect planning must not consult the clock")


class _UnexpectedIds:
    def new(self, namespace: str) -> str:
        raise AssertionError(f"read-only effect planning must not allocate {namespace}")


def build_read_only_plan(
    *,
    run_id: str,
    workflow_id: str,
    target_instance_id: str,
    configuration_revision: int,
    catalog_hash: str,
    dependent_steps: bool = False,
    parallel_steps: bool = False,
    workflow_definition_hash: str = "d" * 64,
) -> tuple[EffectPlan, DependencyGraph, RoutingResult]:
    """Build one target-only plan through the real ORCH-04 and RUN-02 contracts."""

    canonical_catalog = (
        catalog_hash
        if catalog_hash.startswith("catalog-sha256-v1:")
        else "catalog-sha256-v1:" + catalog_hash
    )
    template = _Template()
    instance = _Instance(
        id=target_instance_id,
        configuration_revision=configuration_revision,
    )
    router = DeterministicInstanceRouter(
        catalog_content_hash=canonical_catalog,
        templates=(template,),
        instances=(instance,),
        capability_ids=("cap.model.read",),
    )
    routing = router.route(
        RoutingRequest(
            target_instance_id=target_instance_id,
            trigger_id="trigger.manual.audit",
            trigger_source="operator.local",
            trigger_kind=TriggerKind.MANUAL,
        ),
        WorkflowRoutingDefinition(
            workflow_id=workflow_id,
            workflow_version=1,
            catalog_content_hash=canonical_catalog,
            eligible_trigger_kinds=(TriggerKind.MANUAL,),
            eligible_target_template_ids=(template.id,),
        ),
    )
    topology: tuple[TopologyStep, ...] = (TopologyStep("read", 10, terminal_result=True),)
    step_specs: tuple[EffectStepSpec, ...] = (
        EffectStepSpec(
            runtime_step_id=f"step.{run_id}.read",
            step_key="read",
            kind="model.read",
            selected_instance_id=target_instance_id,
            routing_slot_key=None,
            capability_id="cap.model.read",
            binding_id=None,
        ),
    )
    if dependent_steps and parallel_steps:
        raise ValueError("test plan cannot be both dependent and parallel")
    if dependent_steps or parallel_steps:
        topology = (
            TopologyStep("read", 10, terminal_result=parallel_steps),
            TopologyStep(
                "summarize",
                20,
                (("read",) if dependent_steps else ()),
                terminal_result=True,
            ),
        )
        step_specs = (
            step_specs[0],
            EffectStepSpec(
                runtime_step_id=f"step.{run_id}.summarize",
                step_key="summarize",
                kind="model.read",
                selected_instance_id=target_instance_id,
                routing_slot_key=None,
                capability_id="cap.model.read",
                binding_id=None,
            ),
        )
    graph = DependencyGraph.build(
        topology,
        workflow_max_steps=20,
        global_max_steps=20,
    )
    planner = EffectAwarePlanner(
        catalog_content_hash=canonical_catalog,
        clock=_UnexpectedClock(),
        ids=_UnexpectedIds(),
        capabilities=(_Capability(),),
        templates=(template,),
        approval_policies=(_Policy(),),
        operations=(),
        bindings=(),
    )
    plan = planner.plan(
        EffectPlanRequest(
            run_id=run_id,
            workflow_definition_hash=workflow_definition_hash,
            graph=graph,
            routing=routing,
            steps=step_specs,
            requested_by="principal.test.operator",
        )
    )
    return plan, graph, routing
