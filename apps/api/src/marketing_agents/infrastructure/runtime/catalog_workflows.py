"""Executable catalog read roles over the normal persisted orchestration stack."""

from __future__ import annotations

from dataclasses import dataclass

from marketing_agents.application.orchestration import (
    DeterministicInstanceRouter,
    EffectAwarePlanner,
    EffectPlan,
    EffectPlanRequest,
    EffectStepSpec,
    OrchestrationDependencies,
    RoutingInstanceVariantSource,
    RoutingRequest,
    RoutingResult,
    WorkflowRoutingDefinition,
)
from marketing_agents.application.orchestration.executable_workflows import (
    CatalogRoleExecutionKind,
    ExecutableWorkflowDefinition,
    ExecutableWorkflowHandler,
    ExecutableWorkflowRegistry,
)
from marketing_agents.application.ports.read_adapter import ReadAdapter
from marketing_agents.application.services.controlled_read_executor import (
    ControlledReadCommand,
    ControlledReadExecutor,
    ReadExecutionClassification,
)
from marketing_agents.application.services.execution_activation import ExecutionActivationService
from marketing_agents.application.services.local_artifact_executor import LocalArtifactExecutor
from marketing_agents.application.services.plan_persistence import AuditedPlanPersistenceService
from marketing_agents.application.services.run_lifecycle import RunLifecycleService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.run_lifecycle import (
    CompletionContext,
    NoRunTransitionContext,
    RunLifecycleCommand,
)
from marketing_agents.domain.runtime_policy import RunRuntimePolicy
from marketing_agents.infrastructure.adapters.catalog_roles import CatalogRoleRenderer
from marketing_agents.infrastructure.catalog.models import CompiledCatalog


@dataclass(frozen=True, slots=True)
class _AdmittedInstance:
    id: str
    template_id: str
    display_order: int
    enabled: bool
    variant: RoutingInstanceVariantSource | None
    configuration_revision: int


class CatalogReadWorkflowService:
    """Resume an admitted read-role workflow without substituting demo inputs."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        catalog: CompiledCatalog,
        workflows: ExecutableWorkflowRegistry,
        adapter: ReadAdapter,
        renderer: CatalogRoleRenderer,
    ) -> None:
        self._dependencies = dependencies
        self._catalog = catalog
        self._workflows = workflows
        self._adapter = adapter
        self._local = LocalArtifactExecutor(dependencies, workflows, renderer)
        self._templates = {item.id: item for item in catalog.templates}
        self._instances = {item.id: item for item in catalog.instances}
        self._capabilities = {item.id: item for item in catalog.tool_capabilities}
        self._policies = {item.id: item for item in catalog.approval_policies}

    async def resume_persisted(self, run_id: str, *, worker_id: str, correlation_id: str) -> None:
        async with self._dependencies.unit_of_work() as uow:
            current = await uow.runs.get(run_id)
            work = None if current is None else await uow.works.get(current.work_item_id)
        if current is None or work is None:
            raise ValueError("catalog_run_missing")
        definition = self._workflows.get(work.workflow_id)
        instance = self._instances[work.instance_id]
        self._workflows.require_match(
            work.workflow_id,
            instance_id=work.instance_id,
            template_id=instance.template_id,
            trigger_kind=definition.eligible_trigger_kinds[0],
            mode=work.mode,
            catalog_content_hash=current.catalog_hash,
        )
        if definition.handler_kind is not ExecutableWorkflowHandler.CATALOG_ROLE:
            raise ValueError("catalog_read_handler_required")
        context = AuditContext.worker(worker_id, correlation_id=correlation_id)
        lifecycle = RunLifecycleService(self._dependencies)
        if current.state is RunState.RECEIVED:
            current = (
                await lifecycle.advance(
                    current.id,
                    current.version,
                    RunLifecycleCommand.MARK_VALIDATED,
                    NoRunTransitionContext(),
                    audit_context=context,
                )
            ).run
        if current.state is RunState.VALIDATED:
            plan, graph, routing = self._build_plan(work, current, definition)
            current = (
                await AuditedPlanPersistenceService(self._dependencies).persist(
                    plan,
                    graph,
                    routing,
                    expected_run_version=current.version,
                    audit_context=context,
                )
            ).run
        if current.state is RunState.PLANNED:
            current = (
                await ExecutionActivationService(self._dependencies).activate(
                    current.id,
                    audit_context=context,
                )
            ).run
        if current.state is RunState.EXECUTING:
            async with self._dependencies.unit_of_work() as uow:
                plan_snapshot = await uow.run_steps.get_plan(current.id)
                steps = await uow.run_steps.validate_plan_for_execution(current.id)
            if (
                plan_snapshot is None
                or plan_snapshot.workflow_definition_hash != definition.definition_hash
                or len(steps) != 1
                or steps[0].capability_id != definition.capability_id
            ):
                raise ValueError("catalog_plan_binding_invalid")
            step = steps[0]
            if step.state in {StepState.READY, StepState.EXECUTING}:
                if definition.execution_kind is CatalogRoleExecutionKind.LOCAL_TRANSFORM:
                    await self._local.execute(step.id, audit_context=context)
                else:
                    result = await ControlledReadExecutor(
                        self._dependencies, self._adapter
                    ).execute(
                        ControlledReadCommand(step.id, work.admitted_payload),
                        audit_context=context,
                    )
                    if result.retryable:
                        return
                    if result.classification is not ReadExecutionClassification.SUCCEEDED:
                        raise ValueError("catalog_read_execution_failed")
            async with self._dependencies.unit_of_work() as uow:
                completed_step = await uow.run_steps.get(step.id)
                artifacts = await uow.artifacts.list_for_run(current.id)
            if (
                completed_step is None
                or completed_step.state is not StepState.SUCCEEDED
                or len(artifacts) != 1
            ):
                raise ValueError("catalog_output_incomplete")
            artifact = artifacts[0]
            if (
                artifact.provenance.output_schema_id != definition.output_schema_id
                or artifact.provenance.output_schema_hash != definition.output_schema_hash
                or artifact.provenance.workflow_id != definition.id
                or not artifact.verify_payload()
            ):
                raise ValueError("catalog_output_binding_invalid")
            await lifecycle.advance(
                current.id,
                current.version,
                RunLifecycleCommand.COMPLETE,
                CompletionContext(1, 1, 0, 0),
                audit_context=context,
            )

    def _build_plan(
        self, work: WorkItem, run: Run, definition: ExecutableWorkflowDefinition
    ) -> tuple[EffectPlan, DependencyGraph, RoutingResult]:
        template = self._templates[definition.template_id]
        instance = self._instances[work.instance_id]
        capability = self._capabilities[definition.capability_id or ""]
        if capability.id not in template.allowed_tool_capability_ids:
            raise ValueError("catalog_capability_not_allowed")
        # A role report narrows its template to the sole selected READ capability;
        # other catalog capabilities do not enter this executable plan at all.
        planning_template = type(template).model_validate(
            {
                **template.model_dump(mode="python"),
                "allowed_tool_capability_ids": (capability.id,),
            }
        )
        runtime_instance = _AdmittedInstance(
            instance.id,
            instance.template_id,
            instance.display_order,
            True,
            instance.variant,
            work.configuration_revision,
        )
        router = DeterministicInstanceRouter(
            catalog_content_hash=self._catalog.content_hash,
            templates=(planning_template,),
            instances=(runtime_instance,),
            capability_ids=tuple(self._capabilities),
        )
        routing = router.route(
            RoutingRequest(
                target_instance_id=work.instance_id,
                trigger_id=work.trigger_id,
                trigger_source=work.source,
                trigger_kind=definition.eligible_trigger_kinds[0],
            ),
            WorkflowRoutingDefinition(
                workflow_id=definition.id,
                workflow_version=definition.version,
                catalog_content_hash=self._catalog.content_hash,
                eligible_trigger_kinds=definition.eligible_trigger_kinds,
                eligible_target_template_ids=(template.id,),
            ),
        )
        graph = DependencyGraph.build(
            (TopologyStep("produce-role-artifact", 1, (), terminal_result=True),),
            workflow_max_steps=template.budget_policy.max_steps,
            global_max_steps=template.budget_policy.max_steps,
        )
        planner = EffectAwarePlanner(
            catalog_content_hash=self._catalog.content_hash,
            clock=self._dependencies.clock,
            ids=self._dependencies.ids,
            capabilities=(capability,),
            templates=(planning_template,),
            template_output_schemas={template.id: definition.output_schema},
            approval_policies=(self._policies[template.approval_policy_id],),
            operations=(),
            bindings=(),
            run_policy=RunRuntimePolicy(
                max_steps=template.budget_policy.max_steps,
                max_model_calls=definition.expected_model_calls,
                max_tool_calls=0,
                run_timeout_seconds=template.timeout_policy.run_seconds,
            ),
        )
        plan = planner.plan(
            EffectPlanRequest(
                run_id=run.id,
                workflow_definition_hash=definition.definition_hash,
                graph=graph,
                routing=routing,
                steps=(
                    EffectStepSpec(
                        runtime_step_id=self._dependencies.new_id("step"),
                        step_key="produce-role-artifact",
                        kind="catalog-role",
                        selected_instance_id=instance.id,
                        routing_slot_key=None,
                        capability_id=capability.id,
                        binding_id=None,
                    ),
                ),
                requested_by="worker.catalog-role",
            )
        )
        return plan, graph, routing
