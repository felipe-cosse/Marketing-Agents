"""Approval-gated worker drain for the deterministic Email signup demo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

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
    RoutingSlot,
    WorkflowRoutingDefinition,
    WriteActionIntent,
)
from marketing_agents.application.policies.write_authorization import WriteAuthorizationGuard
from marketing_agents.application.ports.connector_families import (
    SubscribeContactCommand,
    UpsertContactCommand,
)
from marketing_agents.application.ports.repositories import InspectableRunPlan
from marketing_agents.application.services.controlled_read_executor import (
    ControlledReadCommand,
    ControlledReadExecutor,
    ReadExecutionClassification,
)
from marketing_agents.application.services.external_action_dispatcher import (
    DispatchDisposition,
    ExternalActionDispatcher,
)
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
from marketing_agents.application.services.run_step_lifecycle import RunStepLifecycleService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import (
    ExternalAction,
    Run,
    RunStep,
    WorkItem,
)
from marketing_agents.domain.enums import (
    ExternalActionState,
    RunState,
    StepState,
    TriggerKind,
    WorkMode,
)
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
from marketing_agents.domain.step_lifecycle import NoStepTransitionContext, StepLifecycleCommand
from marketing_agents.domain.validation import frozen_json_mapping, require_id
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.adapters.connectors.mock.durable import (
    DurableMockReceiptLedger,
)
from marketing_agents.infrastructure.adapters.connectors.mock.families import MockConnectorBundle
from marketing_agents.infrastructure.adapters.connectors.registry import build_connector_registry
from marketing_agents.infrastructure.catalog.models import AgentTemplateRecord, CompiledCatalog
from marketing_agents.security.redaction import SecretValue

from .composition import DeterministicDemoReadAdapter
from .email_signup_onboarding import (
    EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID,
    EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
    EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
    EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA_ID,
    EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID,
    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,
    EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
    build_email_signup_onboarding_model_input,
    expected_email_signup_onboarding_artifact,
)
from .registry import DEMO_SCENARIOS, DemoScenarioRegistry

_CUSTOMER_SLOT = "email-customer-onboarder"
_WRITE_CAPABILITIES = ("cap.newsletter.subscribe", "cap.crm.upsert-contact")


class EmailSignupRunServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id


@dataclass(frozen=True, slots=True, kw_only=True)
class EmailSignupRunCommand:
    input_payload: Mapping[str, Any] = field(repr=False)
    correlation_id: str
    idempotency_key: SecretValue | None = None

    def __post_init__(self) -> None:
        require_id(self.correlation_id, "Email demo correlation ID")
        object.__setattr__(
            self,
            "input_payload",
            frozen_json_mapping(self.input_payload, "Email demo input payload"),
        )


@dataclass(frozen=True, slots=True)
class EmailSignupRunSnapshot:
    work_item: WorkItem
    run: Run
    disposition: WorkRunReceiptDisposition
    state_path: tuple[str, ...]
    actions: tuple[ExternalAction, ...]
    approval_count: int
    model_calls: int
    connector_calls: int
    artifact: ArtifactEnvelope | None


@dataclass(frozen=True, slots=True)
class _RuntimeInstance:
    id: str
    template_id: str
    display_order: int
    enabled: bool
    variant: RoutingInstanceVariantSource | None
    configuration_revision: int


@dataclass(frozen=True, slots=True)
class _PlanningBinding:
    instance_id: str
    connector_family: str
    binding_id: str
    enabled: bool
    configuration_revision: int


@dataclass(frozen=True, slots=True)
class _PlanningTemplate:
    source: AgentTemplateRecord
    allowed_tool_capability_ids: tuple[str, ...]
    input_schema_id: str
    output_schema_id: str

    @property
    def id(self) -> str:
        return self.source.id

    @property
    def display_order(self) -> int:
        return self.source.display_order

    @property
    def supported_trigger_types(self) -> tuple[str, ...]:
        return self.source.supported_trigger_types

    @property
    def operation_classification(self) -> str:
        return self.source.operation_classification

    @property
    def approval_policy_id(self) -> str:
        return self.source.approval_policy_id

    @property
    def retry_policy(self):  # type: ignore[no-untyped-def]
        return self.source.retry_policy

    @property
    def timeout_policy(self):  # type: ignore[no-untyped-def]
        return self.source.timeout_policy

    @property
    def budget_policy(self):  # type: ignore[no-untyped-def]
        return self.source.budget_policy

    @property
    def rate_limit_policy(self):  # type: ignore[no-untyped-def]
        return self.source.rate_limit_policy


class EmailSignupRunService:
    """Prepare, pause, and resume the exact three-step Email demo."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        manual: ManualDryRunService,
        catalog: CompiledCatalog,
        read_adapter: DeterministicDemoReadAdapter,
        *,
        registry: DemoScenarioRegistry = DEMO_SCENARIOS,
    ) -> None:
        if type(dependencies) is not OrchestrationDependencies:
            raise ValueError("Email demo service requires exact orchestration dependencies")
        if type(manual) is not ManualDryRunService:
            raise ValueError("Email demo service requires the manual admission service")
        if type(catalog) is not CompiledCatalog:
            raise ValueError("Email demo service requires one compiled catalog")
        if type(read_adapter) is not DeterministicDemoReadAdapter:
            raise ValueError("Email demo service requires its deterministic adapter")
        if type(registry) is not DemoScenarioRegistry:
            raise ValueError("Email demo service requires an exact scenario registry")
        read_adapter.require_deterministic_mock()
        self._dependencies = dependencies
        self._manual = manual
        self._catalog = catalog
        self._adapter = read_adapter
        self._registry = registry
        self._templates = {item.id: item for item in catalog.templates}
        self._instances = {item.id: item for item in catalog.instances}
        self._capabilities = {item.id: item for item in catalog.tool_capabilities}
        self._policies = {item.id: item for item in catalog.approval_policies}
        self._connector_registry = build_connector_registry(catalog)

    async def prepare(
        self,
        command: EmailSignupRunCommand,
        principal: AuthenticatedPrincipal,
    ) -> EmailSignupRunSnapshot:
        if type(command) is not EmailSignupRunCommand:
            raise TypeError("Email demo preparation requires an exact command")
        scenario = self._registry.get(EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID)
        if scenario.definition_hash != EMAIL_SIGNUP_ONBOARDING_SCENARIO.definition_hash:
            raise EmailSignupRunServiceError("demo_contract_drift", "Email demo contract drifted")
        resolved = self._registry.resolve_input(scenario.id, command.input_payload)
        receipt = await self._manual.submit(
            ManualDryRunCommand(
                instance_id=scenario.instance_id,
                input_payload=resolved,
                correlation_id=command.correlation_id,
                mode=WorkMode.MOCK_EXECUTION,
                idempotency_key=command.idempotency_key,
                campaign_brief_id=None,
                demo_scenario_id=scenario.id,
            ),
            principal,
        )
        context = AuditContext.worker(
            "worker.deterministic-demo",
            correlation_id=command.correlation_id,
        )
        await self._prepare_boundary(receipt, audit_context=context)
        return await self._snapshot(receipt)

    async def resume(
        self,
        run_id: str,
        *,
        correlation_id: str,
    ) -> EmailSignupRunSnapshot:
        require_id(run_id, "Email demo Run ID")
        require_id(correlation_id, "Email demo correlation ID")
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(run_id)
            work = None if run is None else await unit_of_work.works.get(run.work_item_id)
        if (
            run is None
            or work is None
            or work.workflow_id != EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID
            or work.mode is not WorkMode.MOCK_EXECUTION
        ):
            raise EmailSignupRunServiceError(
                "demo_run_unavailable", "Email demo Run is unavailable", run_id=run_id
            )
        receipt = ManualDryRunResult(
            event_id=work.event_id,
            work_item=work,
            run=run,
            disposition=WorkRunReceiptDisposition.REPLAYED,
            mode=WorkMode.MOCK_EXECUTION,
        )
        await self._resume_execution(
            receipt,
            audit_context=AuditContext.worker(
                "worker.deterministic-demo",
                correlation_id=correlation_id,
            ),
        )
        return await self._snapshot(receipt)

    async def _prepare_boundary(
        self,
        receipt: ManualDryRunResult,
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
            plan, graph, routing = await self._build_plan(receipt.work_item, current)
            current = (
                await AuditedPlanPersistenceService(
                    self._dependencies,
                    require_current_configuration_lock=True,
                ).persist(
                    plan,
                    graph,
                    routing,
                    expected_run_version=current.version,
                    audit_context=audit_context,
                )
            ).run
        if current.state not in {
            RunState.AWAITING_APPROVAL,
            RunState.EXECUTING,
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.REJECTED,
            RunState.FAILED,
        }:
            raise EmailSignupRunServiceError(
                "demo_boundary_invalid",
                "Email demo did not reach its approval boundary",
                run_id=current.id,
            )

    async def _build_plan(
        self,
        work: WorkItem,
        run: Run,
    ) -> tuple[EffectPlan, DependencyGraph, RoutingResult]:
        scenario = EMAIL_SIGNUP_ONBOARDING_SCENARIO
        async with self._dependencies.unit_of_work() as unit_of_work:
            configurations = {
                instance_id: await unit_of_work.configurations.get(instance_id)
                for instance_id in (
                    EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
                    EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
                )
            }
        newsletter_config = configurations[EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID]
        customer_config = configurations[EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID]
        if newsletter_config is None or customer_config is None:
            raise EmailSignupRunServiceError(
                "demo_configuration_unavailable",
                "Email demo configuration is unavailable",
                run_id=run.id,
            )
        newsletter_binding = newsletter_config.connector_bindings.get("newsletter")
        crm_binding = customer_config.connector_bindings.get("crm")
        if (
            not newsletter_config.enabled
            or not customer_config.enabled
            or newsletter_binding is None
            or not newsletter_binding.enabled
            or newsletter_binding.binding_id != EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID
            or crm_binding is None
            or not crm_binding.enabled
            or crm_binding.binding_id != EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID
        ):
            raise EmailSignupRunServiceError(
                "demo_binding_unavailable",
                "Email demo requires both registered mock bindings",
                run_id=run.id,
            )
        newsletter_template = self._templates[EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID]
        customer_template = self._templates[EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID]
        templates = (
            _PlanningTemplate(
                newsletter_template,
                ("cap.newsletter.subscribe",),
                newsletter_template.input_schema_id,
                newsletter_template.output_schema_id,
            ),
            _PlanningTemplate(
                customer_template,
                ("cap.crm.upsert-contact", "cap.model.generate-structured"),
                EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
                scenario.output_schema_id,
            ),
        )
        runtime_instances = tuple(
            _RuntimeInstance(
                id=record.id,
                template_id=record.template_id,
                display_order=record.display_order,
                enabled=True,
                variant=record.variant,
                configuration_revision=configurations[record.id].configuration_revision,  # type: ignore[union-attr]
            )
            for record in (
                self._instances[EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID],
                self._instances[EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID],
            )
        )
        router = DeterministicInstanceRouter(
            catalog_content_hash=self._catalog.content_hash,
            templates=templates,
            instances=runtime_instances,
            capability_ids=tuple(step.capability_id for step in scenario.steps),
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
                eligible_target_template_ids=(EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,),
                required_slots=(
                    RoutingSlot(
                        key=_CUSTOMER_SLOT,
                        source_order=10,
                        template_priorities=(EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,),
                        required_capability_ids=(
                            "cap.crm.upsert-contact",
                            "cap.model.generate-structured",
                        ),
                    ),
                ),
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
            workflow_max_steps=customer_template.budget_policy.max_steps,
            global_max_steps=20,
        )
        policy_ids = tuple(dict.fromkeys(template.approval_policy_id for template in templates))
        planner = EffectAwarePlanner(
            catalog_content_hash=self._catalog.content_hash,
            clock=self._dependencies.clock,
            ids=self._dependencies.ids,
            capabilities=tuple(
                self._capabilities[capability_id]
                for capability_id in (
                    "cap.newsletter.subscribe",
                    "cap.crm.upsert-contact",
                    "cap.model.generate-structured",
                )
            ),
            templates=templates,
            template_output_schemas={
                EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID: (
                    self._catalog.output_schema_by_template[
                        EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID
                    ]
                ),
                EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID: EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA,
            },
            approval_policies=tuple(self._policies[policy_id] for policy_id in policy_ids),
            operations=tuple(
                self._connector_registry.resolve(capability_id)
                for capability_id in _WRITE_CAPABILITIES
            ),
            bindings=(
                _PlanningBinding(
                    newsletter_config.instance_id,
                    "newsletter",
                    newsletter_binding.binding_id,
                    True,
                    newsletter_config.configuration_revision,
                ),
                _PlanningBinding(
                    customer_config.instance_id,
                    "crm",
                    crm_binding.binding_id,
                    True,
                    customer_config.configuration_revision,
                ),
            ),
            run_policy=RunRuntimePolicy(
                max_steps=3,
                max_model_calls=1,
                max_tool_calls=2,
                run_timeout_seconds=min(
                    newsletter_template.timeout_policy.run_seconds,
                    customer_template.timeout_policy.run_seconds,
                ),
            ),
        )
        payload = work.admitted_payload
        commands = {
            "subscribe-newsletter": SubscribeContactCommand(
                contact_ref=payload["contact_id"],
                list_ref=payload["newsletter_list_ref"],
            ),
            "upsert-crm-contact": UpsertContactCommand(
                contact_ref=payload["contact_id"],
                fields={
                    "name": payload["name"],
                    "email": payload["email"],
                    "consent": dict(payload["consent"]),
                    "signup_at": payload["signup_at"],
                },
            ),
        }
        step_ids = {step.key: self._dependencies.new_id("step") for step in scenario.steps}
        plan = planner.plan(
            EffectPlanRequest(
                run_id=run.id,
                workflow_definition_hash=scenario.definition_hash,
                graph=graph,
                routing=routing,
                steps=tuple(
                    EffectStepSpec(
                        runtime_step_id=step_ids[step.key],
                        step_key=step.key,
                        kind=step.kind,
                        selected_instance_id=step.selected_instance_id,
                        routing_slot_key=(
                            None
                            if step.selected_instance_id
                            == EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID
                            else _CUSTOMER_SLOT
                        ),
                        capability_id=step.capability_id,
                        binding_id=(
                            newsletter_binding.binding_id
                            if step.key == "subscribe-newsletter"
                            else crm_binding.binding_id
                            if step.key == "upsert-crm-contact"
                            else None
                        ),
                        write_intent=(
                            WriteActionIntent(commands[step.key])
                            if step.effect == "write"
                            else None
                        ),
                    )
                    for step in scenario.steps
                ),
                requested_by="worker.deterministic-demo",
            )
        )
        return plan, graph, routing

    async def _resume_execution(
        self,
        receipt: ManualDryRunResult,
        *,
        audit_context: AuditContext,
    ) -> None:
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(receipt.run.id)
            inspectable = await unit_of_work.run_steps.get_inspectable_plan(receipt.run.id)
            plan = None if inspectable is None else inspectable.plan
            actions = (
                ()
                if plan is None
                else await unit_of_work.external_actions.list_run_plan(run.id, plan.plan_hash)  # type: ignore[union-attr]
            )
            steps = () if inspectable is None else inspectable.steps
            selection = await unit_of_work.approvals.get_current_authorization_set(receipt.run.id)
            approvals = (
                ()
                if selection is None
                else await unit_of_work.approvals.list_current_set(
                    receipt.run.id,
                    selection.authorization_set.plan_hash,
                    selection.authorization_set.proposal_revision,
                )
            )
        if run is None or plan is None:
            raise EmailSignupRunServiceError("demo_plan_missing", "Email demo plan is missing")
        self._validate_durable_contract(
            receipt.work_item,
            run,
            inspectable,
            actions,
            selection,
            approvals,
        )
        if run.state is RunState.AWAITING_APPROVAL:
            return
        if run.state is not RunState.EXECUTING:
            if run.state in {
                RunState.COMPLETED,
                RunState.CANCELLED,
                RunState.REJECTED,
                RunState.FAILED,
            }:
                return
            raise EmailSignupRunServiceError(
                "demo_run_not_executable", "Email demo Run is not executable", run_id=run.id
            )
        revisions = {
            action.connector_binding_id: action.delivery_contract.binding_configuration_revision
            for action in actions
        }
        ledger = DurableMockReceiptLedger(
            self._dependencies.unit_of_work_factory,
            self._dependencies.clock,
        )
        gateway = RegistryConnectorWriteGateway(
            self._connector_registry,
            MockConnectorBundle.create(self._connector_registry, ledger),
            binding_configuration_revisions=revisions,
        )
        dispatcher = ExternalActionDispatcher(
            self._dependencies,
            gateway,
            WriteAuthorizationGuard(),
            lease_duration=timedelta(minutes=1),
        )
        step_order = {step.id: step.ordinal for step in steps}
        for action in sorted(actions, key=lambda item: step_order[item.step_id]):
            if action.state is ExternalActionState.DISPATCHING:
                await dispatcher.recover_action(
                    action.id,
                    lease_owner="worker.deterministic-demo-recovery",
                )
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(receipt.run.id)
            inspectable = await unit_of_work.run_steps.get_inspectable_plan(receipt.run.id)
            recovered_plan = None if inspectable is None else inspectable.plan
            steps = () if inspectable is None else inspectable.steps
            actions = (
                ()
                if recovered_plan is None
                else await unit_of_work.external_actions.list_run_plan(
                    receipt.run.id,
                    recovered_plan.plan_hash,
                )
            )
            selection = await unit_of_work.approvals.get_current_authorization_set(receipt.run.id)
            approvals = (
                ()
                if selection is None
                else await unit_of_work.approvals.list_current_set(
                    receipt.run.id,
                    selection.authorization_set.plan_hash,
                    selection.authorization_set.proposal_revision,
                )
            )
        if run is None or recovered_plan is None:
            raise EmailSignupRunServiceError(
                "demo_plan_missing",
                "Email demo plan disappeared during recovery",
                run_id=receipt.run.id,
            )
        self._validate_durable_contract(
            receipt.work_item,
            run,
            inspectable,
            actions,
            selection,
            approvals,
        )
        plan = recovered_plan
        if run.state in {
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.REJECTED,
            RunState.FAILED,
        }:
            return
        if run.state is not RunState.EXECUTING:
            raise EmailSignupRunServiceError(
                "demo_run_not_executable",
                "Email demo Run left execution during recovery",
                run_id=run.id,
            )
        step_order = {step.id: step.ordinal for step in steps}
        if any(action.state is ExternalActionState.DISPATCHING for action in actions):
            return
        for action in sorted(actions, key=lambda item: step_order[item.step_id]):
            result = await dispatcher.dispatch_once(
                action.id,
                lease_owner="worker.deterministic-demo",
            )
            if result.disposition not in {
                DispatchDisposition.SUCCEEDED,
                DispatchDisposition.ALREADY_SUCCEEDED,
            }:
                raise EmailSignupRunServiceError(
                    "demo_write_failed", "Email demo mock write did not succeed", run_id=run.id
                )
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(run.id)
            steps = await unit_of_work.run_steps.list_for_run(run.id)  # type: ignore[union-attr]
            actions = await unit_of_work.external_actions.list_run_plan(run.id, plan.plan_hash)  # type: ignore[union-attr]
        if run is None:
            raise EmailSignupRunServiceError("demo_run_missing", "Email demo Run disappeared")
        by_key = {step.key: step for step in steps}
        model_step = by_key["create-welcome-draft"]
        if model_step.state is StepState.PENDING:
            model_step = (
                await RunStepLifecycleService(self._dependencies).advance(
                    model_step.id,
                    model_step.version,
                    StepLifecycleCommand.MARK_READY,
                    NoStepTransitionContext(),
                    audit_context=audit_context,
                )
            ).step
        if model_step.state is StepState.READY:
            refs = self._receipt_refs(actions, by_key)
            executed = await ControlledReadExecutor(self._dependencies, self._adapter).execute(
                ControlledReadCommand(
                    model_step.id,
                    build_email_signup_onboarding_model_input(
                        receipt.work_item.admitted_payload,
                        refs,
                    ),
                ),
                audit_context=audit_context,
            )
            if executed.classification is not ReadExecutionClassification.SUCCEEDED:
                raise EmailSignupRunServiceError(
                    "demo_model_failed", "Email demo welcome draft did not succeed", run_id=run.id
                )
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(run.id)
            steps = await unit_of_work.run_steps.list_for_run(run.id)  # type: ignore[union-attr]
        if run is None or any(step.state is not StepState.SUCCEEDED for step in steps):
            raise EmailSignupRunServiceError(
                "demo_execution_incomplete",
                "Email demo steps are incomplete",
                run_id=receipt.run.id,
            )
        await RunLifecycleService(self._dependencies).advance(
            run.id,
            run.version,
            RunLifecycleCommand.COMPLETE,
            CompletionContext(3, 3, 0, 0),
            audit_context=audit_context,
        )

    def _validate_durable_contract(
        self,
        work: WorkItem,
        run: Run,
        inspectable: InspectableRunPlan | None,
        actions: tuple[ExternalAction, ...],
        selection: Any,
        approvals: tuple[Any, ...],
    ) -> None:
        scenario = EMAIL_SIGNUP_ONBOARDING_SCENARIO
        plan = None if inspectable is None else inspectable.plan
        steps = () if inspectable is None else inspectable.steps
        catalog_hash = self._catalog.content_hash
        if not catalog_hash.startswith("catalog-sha256-v1:"):
            catalog_hash = "catalog-sha256-v1:" + catalog_hash
        expected_steps = tuple(
            (
                expected.key,
                expected.source_order,
                expected.dependency_keys,
                expected.terminal_result,
                expected.kind,
                expected.capability_id,
                expected.selected_instance_id,
            )
            for expected in scenario.steps
        )
        actual_steps = tuple(
            (
                step.key,
                step.source_order,
                step.dependency_keys,
                step.terminal_result,
                step.kind,
                step.capability_id,
                step.selected_instance_id,
            )
            for step in sorted(steps, key=lambda item: item.ordinal)
        )
        steps_by_key = {step.key: step for step in steps}
        selected_instances = () if inspectable is None else inspectable.selected_instances
        assignments = () if inspectable is None else inspectable.assignments
        newsletter_record = self._instances[EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID]
        customer_record = self._instances[EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID]
        expected_selected = (
            (
                EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID,
                EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,
                steps_by_key["subscribe-newsletter"].configuration_revision
                if "subscribe-newsletter" in steps_by_key
                else None,
                newsletter_record.display_order,
                None
                if newsletter_record.variant is None
                else newsletter_record.variant.source_ordinal,
                1,
                True,
            ),
            (
                EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
                EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
                steps_by_key["upsert-crm-contact"].configuration_revision
                if "upsert-crm-contact" in steps_by_key
                else None,
                customer_record.display_order,
                None if customer_record.variant is None else customer_record.variant.source_ordinal,
                2,
                False,
            ),
        )
        actual_selected = tuple(
            (
                item.instance_id,
                item.template_id,
                item.configuration_revision,
                item.display_order,
                item.source_ordinal,
                item.selection_order,
                item.target,
            )
            for item in selected_instances
        )
        expected_assignments = (
            (
                _CUSTOMER_SLOT,
                EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID,
                EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
                ("cap.crm.upsert-contact", "cap.model.generate-structured"),
                1,
            ),
        )
        actual_assignments = tuple(
            (
                item.slot_key,
                item.instance_id,
                item.template_id,
                item.required_capability_ids,
                item.assignment_order,
            )
            for item in assignments
        )
        if (
            work.id != run.work_item_id
            or work.workflow_id != scenario.workflow_id
            or work.mode is not WorkMode.MOCK_EXECUTION
            or work.instance_id != EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_INSTANCE_ID
            or work.input_schema_id != EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA_ID
            or work.input_schema_hash != canonical_schema_hash(EMAIL_SIGNUP_ONBOARDING_INPUT_SCHEMA)
            or work.brief_id is not None
            or run.configuration_revision != work.configuration_revision
            or run.catalog_hash != catalog_hash
            or plan is None
            or plan.run_id != run.id
            or plan.workflow_id != scenario.workflow_id
            or plan.workflow_version != scenario.version
            or plan.workflow_definition_hash != scenario.definition_hash
            or plan.catalog_content_hash != catalog_hash
            or plan.step_count != 3
            or not plan.approval_required
            or actual_steps != expected_steps
            or actual_selected != expected_selected
            or actual_assignments != expected_assignments
            or any(step.plan_hash != plan.plan_hash for step in steps)
            or any(step.graph_hash != plan.graph_hash for step in steps)
        ):
            raise EmailSignupRunServiceError(
                "demo_durable_contract_invalid",
                "Email demo durable plan differs from its frozen contract",
                run_id=run.id,
            )
        expected_step_details = {
            "subscribe-newsletter": (
                EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_TEMPLATE_ID,
                "write",
                "newsletter",
                None,
                EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID,
                "schema:connector:newsletter.subscribe:request:v1",
                "schema:connector:newsletter.subscribe:result:v1",
            ),
            "upsert-crm-contact": (
                EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
                "write",
                "crm",
                _CUSTOMER_SLOT,
                EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID,
                "schema:connector:crm.upsert-contact:request:v1",
                "schema:connector:crm.upsert-contact:result:v1",
            ),
            "create-welcome-draft": (
                EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID,
                "read",
                "model",
                _CUSTOMER_SLOT,
                None,
                EMAIL_SIGNUP_ONBOARDING_MODEL_INPUT_SCHEMA_ID,
                scenario.output_schema_id,
            ),
        }
        if any(
            (
                step.template_id,
                step.effect.value,
                step.connector_family,
                step.routing_slot_key,
                step.binding_id,
                step.request_schema_id,
                step.result_schema_id,
            )
            != expected_step_details[step.key]
            or step.result_schema_hash is None
            or step.runtime_policy.retry.max_attempts != 1
            or step.runtime_policy.budget.max_input_bytes <= 0
            or step.runtime_policy.budget.max_output_bytes <= 0
            for step in steps
        ):
            raise EmailSignupRunServiceError(
                "demo_durable_steps_invalid",
                "Email demo durable steps differ from its frozen execution contract",
                run_id=run.id,
            )
        expected_actions = {
            "subscribe-newsletter": (
                "newsletter.subscribe",
                "cap.newsletter.subscribe",
                EMAIL_SIGNUP_ONBOARDING_NEWSLETTER_BINDING_ID,
                {
                    "contact_ref": work.admitted_payload["contact_id"],
                    "list_ref": work.admitted_payload["newsletter_list_ref"],
                },
            ),
            "upsert-crm-contact": (
                "crm.upsert-contact",
                "cap.crm.upsert-contact",
                EMAIL_SIGNUP_ONBOARDING_CRM_BINDING_ID,
                {
                    "contact_ref": work.admitted_payload["contact_id"],
                    "fields": {
                        "name": work.admitted_payload["name"],
                        "email": work.admitted_payload["email"],
                        "consent": work.admitted_payload["consent"],
                        "signup_at": work.admitted_payload["signup_at"],
                    },
                },
            ),
        }
        actions_by_step = {
            next((step.key for step in steps if step.id == action.step_id), ""): action
            for action in actions
        }
        if set(actions_by_step) != set(expected_actions):
            raise EmailSignupRunServiceError(
                "demo_durable_actions_invalid",
                "Email demo durable actions differ from its frozen contract",
                run_id=run.id,
            )
        for step_key, expected in expected_actions.items():
            action = actions_by_step[step_key]
            step = steps_by_key[step_key]
            if (
                (
                    action.envelope.action_type,
                    action.envelope.capability_id,
                    action.connector_binding_id,
                )
                != expected[:3]
                or canonical_json_bytes(action.envelope.minimized_payload)
                != canonical_json_bytes(expected[3])
                or action.run_id != run.id
                or action.step_id != step.id
                or action.envelope.step_key != step.key
                or action.envelope.plan_hash != plan.plan_hash
                or action.envelope.instance_id != step.selected_instance_id
                or action.envelope.template_id != step.template_id
                or action.envelope.connector_family != step.connector_family
                or action.envelope.binding_id != step.binding_id
                or action.delivery_contract.capability_id != step.capability_id
                or action.delivery_contract.connector_family != step.connector_family
                or action.delivery_contract.binding_id != step.binding_id
                or action.delivery_contract.binding_configuration_revision
                != step.binding_configuration_revision
                or action.delivery_contract.request_schema_id != step.request_schema_id
            ):
                raise EmailSignupRunServiceError(
                    "demo_durable_actions_invalid",
                    "Email demo durable actions differ from its frozen contract",
                    run_id=run.id,
                )
        if selection is None:
            raise EmailSignupRunServiceError(
                "demo_approval_membership_invalid",
                "Email demo lacks its complete approval membership",
                run_id=run.id,
            )
        authorization_set = selection.authorization_set
        member_by_action = {member.action_id: member for member in authorization_set.members}
        request_by_action = {stored.request.action_id: stored.request for stored in approvals}
        if (
            authorization_set.run_id != run.id
            or authorization_set.plan_hash != plan.plan_hash
            or len(member_by_action) != 2
            or len(request_by_action) != 2
            or set(member_by_action) != {action.id for action in actions}
            or set(request_by_action) != set(member_by_action)
            or any(
                member_by_action[action.id].action_hash != action.action_hash
                or member_by_action[action.id].step_id != action.step_id
                or member_by_action[action.id].step_key
                != next(step.key for step in steps if step.id == action.step_id)
                or request_by_action[action.id].action_hash != action.action_hash
                or request_by_action[action.id].authorization_set_id != authorization_set.id
                for action in actions
            )
        ):
            raise EmailSignupRunServiceError(
                "demo_approval_membership_invalid",
                "Email demo approval membership differs from its frozen actions",
                run_id=run.id,
            )

    @staticmethod
    def _receipt_refs(
        actions: tuple[ExternalAction, ...],
        steps: dict[str, RunStep],
    ) -> tuple[dict[str, Any], ...]:
        by_step_id = {step.id: step.key for step in steps.values()}
        ordered = sorted(actions, key=lambda action: steps[by_step_id[action.step_id]].ordinal)
        refs: list[dict[str, Any]] = []
        for action in ordered:
            result = action.result
            if action.state is not ExternalActionState.SUCCEEDED or result is None:
                raise EmailSignupRunServiceError(
                    "demo_receipt_missing", "Email demo action lacks its durable receipt"
                )
            refs.append(
                {
                    "receipt_id": result.receipt_id,
                    "action_id": action.id,
                    "action_type": action.envelope.action_type,
                    "capability_id": action.envelope.capability_id,
                    "binding_id": action.connector_binding_id,
                    "status": result.status,
                    "external_side_effect": result.safe_metadata.get("external_side_effect"),
                }
            )
        return tuple(refs)

    async def _snapshot(
        self,
        receipt: ManualDryRunResult,
    ) -> EmailSignupRunSnapshot:
        history = await RunLifecycleService(self._dependencies).history(receipt.run.id)
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(receipt.run.id)
            inspectable = await unit_of_work.run_steps.get_inspectable_plan(receipt.run.id)
            plan = None if inspectable is None else inspectable.plan
            steps = () if inspectable is None else inspectable.steps
            control = await unit_of_work.execution_control.get(receipt.run.id)
            artifacts = await unit_of_work.artifacts.list_for_run(receipt.run.id)
            selection = await unit_of_work.approvals.get_current_authorization_set(receipt.run.id)
            actions = (
                ()
                if plan is None
                else await unit_of_work.external_actions.list_run_plan(
                    receipt.run.id, plan.plan_hash
                )
            )
            approvals = (
                ()
                if selection is None
                else await unit_of_work.approvals.list_current_set(
                    receipt.run.id,
                    selection.authorization_set.plan_hash,
                    selection.authorization_set.proposal_revision,
                )
            )
        if run is None or control is None or len(actions) != 2 or len(approvals) != 2:
            raise EmailSignupRunServiceError(
                "demo_evidence_invalid",
                "Email demo durable evidence is incomplete",
                run_id=receipt.run.id,
            )
        self._validate_durable_contract(
            receipt.work_item,
            run,
            inspectable,
            actions,
            selection,
            approvals,
        )
        artifact = artifacts[0] if len(artifacts) == 1 else None
        if (
            plan is None
            or len(steps) != 3
            or {action.envelope.capability_id for action in actions} != set(_WRITE_CAPABILITIES)
            or any(action.envelope.capability_id == "cap.email.send-message" for action in actions)
            or any(step.capability_id == "cap.email.send-message" for step in steps)
        ):
            raise EmailSignupRunServiceError(
                "demo_plan_invalid",
                "Email demo plan differs from its frozen capability set",
                run_id=run.id,
            )
        state_path = tuple(item.new_state.value for item in history)
        if run.state is RunState.AWAITING_APPROVAL and (
            state_path != EMAIL_SIGNUP_ONBOARDING_SCENARIO.expected_state_path[:4]
            or control.model_calls != 0
            or control.tool_calls != 0
            or artifacts
            or any(
                action.state
                not in {
                    ExternalActionState.AWAITING_APPROVAL,
                    ExternalActionState.APPROVED,
                }
                for action in actions
            )
        ):
            raise EmailSignupRunServiceError(
                "demo_boundary_evidence_invalid",
                "Email demo approval boundary evidence is invalid",
                run_id=run.id,
            )
        if run.state is RunState.COMPLETED:
            by_key = {step.key: step for step in steps}
            refs = self._receipt_refs(actions, by_key)
            model_input = build_email_signup_onboarding_model_input(
                receipt.work_item.admitted_payload,
                refs,
            )
            expected_artifact = expected_email_signup_onboarding_artifact(model_input)
            model_step = by_key["create-welcome-draft"]
            if (
                state_path != EMAIL_SIGNUP_ONBOARDING_SCENARIO.expected_state_path
                or control.model_calls != 1
                or control.tool_calls != 2
                or any(step.state is not StepState.SUCCEEDED for step in steps)
                or any(action.state is not ExternalActionState.SUCCEEDED for action in actions)
                or artifact is None
                or not artifact.verify_payload()
                or canonical_json_bytes(artifact.payload) != canonical_json_bytes(expected_artifact)
                or artifact.provenance.work_item_id != receipt.work_item.id
                or artifact.provenance.run_id != run.id
                or artifact.provenance.step_id != model_step.id
                or artifact.provenance.workflow_id != EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID
                or artifact.provenance.workflow_version
                != str(EMAIL_SIGNUP_ONBOARDING_SCENARIO.version)
                or artifact.provenance.template_id != EMAIL_SIGNUP_ONBOARDING_CUSTOMER_TEMPLATE_ID
                or artifact.provenance.instance_id != EMAIL_SIGNUP_ONBOARDING_CUSTOMER_INSTANCE_ID
                or artifact.provenance.admitted_input_digest != receipt.work_item.input_digest
                or artifact.provenance.catalog_hash != run.catalog_hash
                or artifact.provenance.instance_config_revision != model_step.configuration_revision
                or len(artifact.provenance.providers) != 1
                or (
                    artifact.provenance.providers[0].provider_kind,
                    artifact.provenance.providers[0].name,
                    artifact.provenance.providers[0].mode,
                    artifact.provenance.providers[0].version,
                )
                != ("llm", "mock", "mock", "v1")
                or artifact.provenance.output_schema_id
                != EMAIL_SIGNUP_ONBOARDING_SCENARIO.output_schema_id
                or artifact.provenance.output_schema_hash
                != canonical_schema_hash(EMAIL_SIGNUP_ONBOARDING_OUTPUT_SCHEMA)
            ):
                raise EmailSignupRunServiceError(
                    "demo_completion_evidence_invalid",
                    "Email demo completion evidence is invalid",
                    run_id=run.id,
                )
        return EmailSignupRunSnapshot(
            work_item=receipt.work_item,
            run=run,
            disposition=receipt.disposition,
            state_path=state_path,
            actions=actions,
            approval_count=len(approvals),
            model_calls=control.model_calls,
            connector_calls=control.tool_calls,
            artifact=artifact,
        )


__all__ = [
    "EmailSignupRunCommand",
    "EmailSignupRunService",
    "EmailSignupRunServiceError",
    "EmailSignupRunSnapshot",
]
