"""Bounded worker/test drain for deterministic demo runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from marketing_agents.application.orchestration import (
    DeterministicInstanceRouter,
    EffectAwarePlanner,
    EffectPlan,
    EffectPlanRequest,
    EffectStepSpec,
    OrchestrationDependencies,
    PlanningBudgetPolicySource,
    PlanningRateLimitPolicySource,
    PlanningRetryPolicySource,
    PlanningTimeoutPolicySource,
    RoutingInstanceVariantSource,
    RoutingRequest,
    RoutingResult,
    WorkflowRoutingDefinition,
)
from marketing_agents.application.services.controlled_read_executor import (
    ControlledReadCommand,
    ControlledReadExecutor,
    ReadExecutionClassification,
)
from marketing_agents.application.services.execution_activation import ExecutionActivationService
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunResult,
    ManualDryRunService,
)
from marketing_agents.application.services.plan_persistence import AuditedPlanPersistenceService
from marketing_agents.application.services.run_lifecycle import RunLifecycleService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, StepState, TriggerKind, WorkMode
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.provenance import ArtifactEnvelope
from marketing_agents.domain.run_lifecycle import (
    CompletionContext,
    NoRunTransitionContext,
    RunLifecycleCommand,
)
from marketing_agents.domain.runtime_policy import RunRuntimePolicy
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.domain.validation import frozen_json_mapping, require_id
from marketing_agents.infrastructure.catalog.models import (
    AgentInstanceRecord,
    AgentTemplateRecord,
    CompiledCatalog,
)
from marketing_agents.security.redaction import SecretValue

from .blog_content_review import (
    BLOG_CONTENT_REVIEW_SCENARIO,
    BLOG_CONTENT_REVIEW_SCENARIO_ID,
    expected_blog_content_review_artifact,
)
from .community_reminder_draft import (
    COMMUNITY_REMINDER_DRAFT_SCENARIO,
    COMMUNITY_REMINDER_DRAFT_SCENARIO_ID,
    expected_community_reminder_draft_artifact,
)
from .composition import DeterministicDemoReadAdapter
from .contracts import DemoScenarioDefinition
from .registry import DEMO_SCENARIOS, DemoScenarioRegistry
from .social_content_draft import SOCIAL_CONTENT_DRAFT_SCENARIO, SOCIAL_CONTENT_DRAFT_SCENARIO_ID


class DemoRunServiceError(RuntimeError):
    """Stable failure from the bounded demo drain."""

    def __init__(self, code: str, message: str, *, run_id: str | None = None) -> None:
        require_id(code, "demo run error code")
        super().__init__(message)
        self.code = code
        self.run_id = run_id


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoRunCommand:
    scenario_id: str
    input_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    correlation_id: str
    idempotency_key: SecretValue | None = field(default=None, repr=False)
    mode: WorkMode = WorkMode.DRY_RUN

    def __post_init__(self) -> None:
        require_id(self.scenario_id, "demo command scenario ID")
        require_id(self.correlation_id, "demo command correlation ID")
        if self.mode is not WorkMode.DRY_RUN:
            raise ValueError("demo drain permits only dry-run work")
        object.__setattr__(
            self,
            "input_payload",
            frozen_json_mapping(self.input_payload, "demo command input payload"),
        )


@dataclass(frozen=True, slots=True)
class DemoRunResult:
    scenario: DemoScenarioDefinition
    work_item: WorkItem
    run: Run
    artifact: ArtifactEnvelope
    disposition: WorkRunReceiptDisposition
    state_path: tuple[str, ...]
    model_calls: int
    connector_calls: int
    external_actions: int
    approvals: int

    def __post_init__(self) -> None:
        if (
            type(self.scenario) is not DemoScenarioDefinition
            or type(self.work_item) is not WorkItem
            or type(self.run) is not Run
            or type(self.artifact) is not ArtifactEnvelope
        ):
            raise ValueError("demo result resources are invalid")
        if self.run.work_item_id != self.work_item.id or self.run.state is not RunState.COMPLETED:
            raise ValueError("demo result requires one completed admitted Run")
        if self.state_path != self.scenario.expected_state_path:
            raise ValueError("demo result lifecycle differs from its scenario contract")
        actual = (
            self.model_calls,
            self.connector_calls,
            self.external_actions,
            self.approvals,
        )
        expected = (
            self.scenario.expected_model_calls,
            self.scenario.expected_connector_calls,
            self.scenario.expected_external_actions,
            self.scenario.expected_approvals,
        )
        if actual != expected:
            raise ValueError("demo result call counts differ from its scenario contract")


@dataclass(frozen=True, slots=True)
class _RuntimeInstance:
    id: str
    template_id: str
    display_order: int
    enabled: bool
    variant: RoutingInstanceVariantSource | None
    configuration_revision: int


@dataclass(frozen=True, slots=True)
class _ScenarioRoutingTemplate:
    id: str
    display_order: int
    allowed_tool_capability_ids: tuple[str, ...]
    supported_trigger_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ScenarioPlanningTemplate:
    id: str
    allowed_tool_capability_ids: tuple[str, ...]
    operation_classification: str
    approval_policy_id: str
    input_schema_id: str
    output_schema_id: str
    retry_policy: PlanningRetryPolicySource
    timeout_policy: PlanningTimeoutPolicySource
    budget_policy: PlanningBudgetPolicySource
    rate_limit_policy: PlanningRateLimitPolicySource


class DemoRunService:
    """Submit then synchronously drain one demo for workers and acceptance tests only."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        manual_dry_run: ManualDryRunService,
        catalog: CompiledCatalog,
        read_adapter: DeterministicDemoReadAdapter,
        *,
        registry: DemoScenarioRegistry = DEMO_SCENARIOS,
    ) -> None:
        if type(dependencies) is not OrchestrationDependencies:
            raise ValueError("demo run service requires exact orchestration dependencies")
        if type(manual_dry_run) is not ManualDryRunService:
            raise ValueError("demo run service requires the durable manual admission service")
        if type(catalog) is not CompiledCatalog:
            raise ValueError("demo run service requires one compiled catalog")
        if type(registry) is not DemoScenarioRegistry:
            raise ValueError("demo run service requires an exact demo registry")
        if type(read_adapter) is not DeterministicDemoReadAdapter:
            raise ValueError("demo run service requires its credential-free deterministic adapter")
        read_adapter.require_deterministic_mock()
        self._dependencies = dependencies
        self._manual = manual_dry_run
        self._catalog = catalog
        self._adapter = read_adapter
        self._registry = registry
        self._templates = {item.id: item for item in catalog.templates}
        self._instances = {item.id: item for item in catalog.instances}
        self._capabilities = {item.id: item for item in catalog.tool_capabilities}
        self._policies = {item.id: item for item in catalog.approval_policies}

    async def run(
        self,
        command: DemoRunCommand,
        principal: AuthenticatedPrincipal,
    ) -> DemoRunResult:
        """Durably admit and completely drain the bounded scenario."""

        if type(command) is not DemoRunCommand:
            raise TypeError("demo run requires an exact command")
        self._adapter.require_deterministic_mock()
        scenario = self._registry.get(command.scenario_id)
        self._require_supported_contract(scenario)
        resolved = self._registry.resolve_input(scenario.id, command.input_payload)
        receipt = await self._manual.submit(
            ManualDryRunCommand(
                instance_id=scenario.instance_id,
                input_payload=resolved,
                correlation_id=command.correlation_id,
                mode=command.mode,
                idempotency_key=command.idempotency_key,
                campaign_brief_id=None,
                demo_scenario_id=scenario.id,
            ),
            principal,
        )
        audit_context = AuditContext.worker(
            "worker.deterministic-demo",
            correlation_id=command.correlation_id,
        )
        await self._drain(receipt, scenario, audit_context=audit_context)
        return await self._result(receipt, scenario)

    async def _drain(
        self,
        receipt: ManualDryRunResult,
        scenario: DemoScenarioDefinition,
        *,
        audit_context: AuditContext,
    ) -> None:
        lifecycle = RunLifecycleService(self._dependencies)
        current = receipt.run
        if current.state is RunState.RECEIVED:
            current = (
                await lifecycle.advance(
                    current.id,
                    current.version,
                    RunLifecycleCommand.MARK_VALIDATED,
                    NoRunTransitionContext(),
                    audit_context=audit_context,
                )
            ).run
        if current.state is RunState.VALIDATED:
            plan, graph, routing = self._build_plan(receipt.work_item, current, scenario)
            current = (
                await AuditedPlanPersistenceService(self._dependencies).persist(
                    plan,
                    graph,
                    routing,
                    expected_run_version=current.version,
                    audit_context=audit_context,
                )
            ).run
        if current.state is RunState.PLANNED:
            current = (
                await ExecutionActivationService(self._dependencies).activate(
                    current.id,
                    audit_context=audit_context,
                )
            ).run
        if current.state is RunState.EXECUTING:
            async with self._dependencies.unit_of_work() as unit_of_work:
                steps = await unit_of_work.run_steps.list_for_run(current.id)
            if len(steps) != 1:
                raise DemoRunServiceError(
                    "demo_plan_invalid", "demo Run must contain exactly one step", run_id=current.id
                )
            step = steps[0]
            if step.state is StepState.READY:
                executed = await ControlledReadExecutor(
                    self._dependencies,
                    self._adapter,
                ).execute(
                    ControlledReadCommand(step.id, receipt.work_item.admitted_payload),
                    audit_context=audit_context,
                )
                if executed.classification is not ReadExecutionClassification.SUCCEEDED:
                    raise DemoRunServiceError(
                        "demo_execution_failed",
                        "demo model generation did not complete",
                        run_id=current.id,
                    )
                step = executed.step
            if step.state is not StepState.SUCCEEDED:
                raise DemoRunServiceError(
                    "demo_step_invalid", "demo step is not complete", run_id=current.id
                )
            current = (
                await lifecycle.advance(
                    current.id,
                    current.version,
                    RunLifecycleCommand.COMPLETE,
                    CompletionContext(1, 1, 0, 0),
                    audit_context=audit_context,
                )
            ).run
        if current.state is not RunState.COMPLETED:
            raise DemoRunServiceError(
                "demo_run_not_completed", "demo Run is not complete", run_id=current.id
            )

    def _build_plan(
        self,
        work: WorkItem,
        run: Run,
        scenario: DemoScenarioDefinition,
    ) -> tuple[EffectPlan, DependencyGraph, RoutingResult]:
        scenario_step = scenario.steps[0]
        template = self._templates.get(scenario.template_id)
        instance = self._instances.get(scenario.instance_id)
        capability = self._capabilities.get(scenario_step.capability_id)
        if (
            type(template) is not AgentTemplateRecord
            or type(instance) is not AgentInstanceRecord
            or capability is None
            or capability.id not in template.allowed_tool_capability_ids
        ):
            raise DemoRunServiceError(
                "demo_catalog_binding_invalid",
                "demo catalog binding is unavailable",
                run_id=run.id,
            )
        runtime_instance = _RuntimeInstance(
            id=instance.id,
            template_id=instance.template_id,
            display_order=instance.display_order,
            enabled=True,
            variant=instance.variant,
            configuration_revision=work.configuration_revision,
        )
        routing_template = _ScenarioRoutingTemplate(
            id=template.id,
            display_order=template.display_order,
            allowed_tool_capability_ids=(capability.id,),
            supported_trigger_types=template.supported_trigger_types,
        )
        router = DeterministicInstanceRouter(
            catalog_content_hash=self._catalog.content_hash,
            templates=(routing_template,),
            instances=(runtime_instance,),
            capability_ids=(capability.id,),
        )
        routing = router.route(
            RoutingRequest(
                target_instance_id=work.instance_id,
                trigger_id=work.trigger_id,
                trigger_source=work.source,
                trigger_kind=TriggerKind.MANUAL,
            ),
            WorkflowRoutingDefinition(
                workflow_id=scenario.workflow_id,
                workflow_version=scenario.version,
                catalog_content_hash=self._catalog.content_hash,
                eligible_trigger_kinds=(TriggerKind.MANUAL,),
                eligible_target_template_ids=(scenario.template_id,),
            ),
        )
        graph = DependencyGraph.build(
            tuple(
                TopologyStep(
                    step.key,
                    step.source_order,
                    step.dependency_keys,
                    terminal_result=step.terminal_result,
                )
                for step in scenario.steps
            ),
            workflow_max_steps=template.budget_policy.max_steps,
            global_max_steps=template.budget_policy.max_steps,
        )
        runtime_step_ids = {step.key: self._dependencies.new_id("step") for step in scenario.steps}
        planning_template = _ScenarioPlanningTemplate(
            id=template.id,
            allowed_tool_capability_ids=tuple(
                dict.fromkeys(step.capability_id for step in scenario.steps)
            ),
            operation_classification="read_only",
            approval_policy_id=template.approval_policy_id,
            input_schema_id=scenario.input_schema_id,
            output_schema_id=scenario.output_schema_id,
            retry_policy=template.retry_policy,
            timeout_policy=template.timeout_policy,
            budget_policy=template.budget_policy,
            rate_limit_policy=template.rate_limit_policy,
        )
        policy = self._policies.get(template.approval_policy_id)
        if policy is None:
            raise DemoRunServiceError(
                "demo_catalog_binding_invalid", "demo approval policy is unavailable", run_id=run.id
            )
        planner = EffectAwarePlanner(
            catalog_content_hash=self._catalog.content_hash,
            clock=self._dependencies.clock,
            ids=self._dependencies.ids,
            capabilities=(capability,),
            templates=(planning_template,),
            template_output_schemas={template.id: scenario.output_schema},
            approval_policies=(policy,),
            operations=(),
            bindings=(),
            run_policy=RunRuntimePolicy(
                max_steps=template.budget_policy.max_steps,
                max_model_calls=scenario.expected_model_calls,
                max_tool_calls=scenario.expected_connector_calls,
                run_timeout_seconds=template.timeout_policy.run_seconds,
            ),
        )
        plan = planner.plan(
            EffectPlanRequest(
                run_id=run.id,
                workflow_definition_hash=scenario.definition_hash,
                graph=graph,
                routing=routing,
                steps=tuple(
                    EffectStepSpec(
                        runtime_step_id=runtime_step_ids[step.key],
                        step_key=step.key,
                        kind=step.kind,
                        selected_instance_id=step.selected_instance_id,
                        routing_slot_key=None,
                        capability_id=step.capability_id,
                        binding_id=None,
                    )
                    for step in scenario.steps
                ),
                requested_by="worker.deterministic-demo",
            )
        )
        return plan, graph, routing

    async def _result(
        self,
        receipt: ManualDryRunResult,
        scenario: DemoScenarioDefinition,
    ) -> DemoRunResult:
        lifecycle = RunLifecycleService(self._dependencies)
        history = await lifecycle.history(receipt.run.id)
        state_path = tuple(item.new_state.value for item in history)
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(receipt.run.id)
            plan = await unit_of_work.run_steps.get_plan(receipt.run.id)
            steps = await unit_of_work.run_steps.list_for_run(receipt.run.id)
            artifacts = await unit_of_work.artifacts.list_for_run(receipt.run.id)
            control = await unit_of_work.execution_control.get(receipt.run.id)
            authorization_set = await unit_of_work.approvals.get_current_authorization_set(
                receipt.run.id
            )
            actions = (
                ()
                if plan is None
                else await unit_of_work.external_actions.list_run_plan(
                    receipt.run.id,
                    plan.plan_hash,
                )
            )
            attempts = (
                ()
                if len(steps) != 1
                else await unit_of_work.execution_control.list_attempts(
                    steps[0].id,
                    steps[0].runtime_policy.operation_key,
                )
            )
        if (
            run is None
            or plan is None
            or control is None
            or len(steps) != 1
            or len(attempts) != 1
            or len(artifacts) != 1
            or actions
            or authorization_set is not None
            or control.model_calls != scenario.expected_model_calls
            or control.tool_calls != scenario.expected_connector_calls
        ):
            raise DemoRunServiceError(
                "demo_evidence_invalid",
                "demo durable evidence differs from its bounded contract",
                run_id=receipt.run.id,
            )
        artifact = artifacts[0]
        artifact_payload_valid = self._artifact_payload_is_valid(
            scenario,
            artifact.payload,
            receipt.work_item.admitted_payload,
        )
        if (
            not artifact.verify_payload()
            or artifact.payload.get("scenario_id") != scenario.id
            or artifact.payload.get("scenario_version") != scenario.version
            or not artifact_payload_valid
            or artifact.provenance.workflow_id != scenario.id
            or artifact.provenance.workflow_version != str(scenario.version)
            or artifact.provenance.output_schema_id != scenario.output_schema_id
            or artifact.provenance.output_schema_hash
            != canonical_schema_hash(scenario.output_schema)
        ):
            raise DemoRunServiceError(
                "demo_artifact_invalid",
                "demo artifact provenance differs from its scenario contract",
                run_id=receipt.run.id,
            )
        return DemoRunResult(
            scenario=scenario,
            work_item=receipt.work_item,
            run=run,
            artifact=artifact,
            disposition=receipt.disposition,
            state_path=state_path,
            model_calls=control.model_calls,
            connector_calls=control.tool_calls,
            external_actions=len(actions),
            approvals=0,
        )

    @staticmethod
    def _require_supported_contract(scenario: DemoScenarioDefinition) -> None:
        supported = {
            SOCIAL_CONTENT_DRAFT_SCENARIO_ID: SOCIAL_CONTENT_DRAFT_SCENARIO,
            BLOG_CONTENT_REVIEW_SCENARIO_ID: BLOG_CONTENT_REVIEW_SCENARIO,
            COMMUNITY_REMINDER_DRAFT_SCENARIO_ID: COMMUNITY_REMINDER_DRAFT_SCENARIO,
        }
        expected = supported.get(scenario.id)
        if (
            expected is None
            or scenario.definition_hash != expected.definition_hash
            or scenario.effect != "read_only"
            or len(scenario.selected_agents) != 1
            or len(scenario.steps) != 1
            or scenario.steps[0].source_order != 10
            or scenario.steps[0].dependency_keys
            or not scenario.steps[0].terminal_result
            or scenario.steps[0].kind != "model.generate-structured"
            or scenario.steps[0].selected_instance_id != scenario.instance_id
            or scenario.steps[0].capability_id != "cap.model.generate-structured"
            or scenario.steps[0].effect != "read"
            or scenario.expected_state_path
            != ("received", "validated", "planned", "executing", "completed")
            or (
                scenario.expected_model_calls,
                scenario.expected_connector_calls,
                scenario.expected_external_actions,
                scenario.expected_approvals,
            )
            != (1, 0, 0, 0)
        ):
            raise DemoRunServiceError(
                "demo_scenario_unsupported",
                "demo drain supports only its bounded deterministic read scenarios",
            )

    @staticmethod
    def _artifact_payload_is_valid(
        scenario: DemoScenarioDefinition,
        artifact_payload: Mapping[str, Any],
        input_payload: Mapping[str, Any],
    ) -> bool:
        if scenario.id == SOCIAL_CONTENT_DRAFT_SCENARIO_ID:
            draft_text = artifact_payload.get("draft_text")
            return type(draft_text) is str and artifact_payload.get("character_count") == len(
                draft_text
            )
        if scenario.id == BLOG_CONTENT_REVIEW_SCENARIO_ID:
            try:
                expected = expected_blog_content_review_artifact(input_payload)
                return canonical_json_bytes(artifact_payload) == canonical_json_bytes(expected)
            except (TypeError, ValueError):
                return False
        if scenario.id == COMMUNITY_REMINDER_DRAFT_SCENARIO_ID:
            try:
                expected = expected_community_reminder_draft_artifact(input_payload)
                return canonical_json_bytes(artifact_payload) == canonical_json_bytes(expected)
            except (TypeError, ValueError):
                return False
        return False


__all__ = ["DemoRunCommand", "DemoRunResult", "DemoRunService", "DemoRunServiceError"]
