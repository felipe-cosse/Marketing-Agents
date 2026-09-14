"""Exact operator-authored, approval-gated mock writes for four catalog roles.

The surrounding worker verifies the keyed admission envelope before invoking this
service. This service never interprets prose or model output as a command, and it
never adds model/artifact capabilities to a write-only role.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

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
    WriteActionIntent,
)
from marketing_agents.application.orchestration.executable_workflows import (
    CatalogRoleExecutionKind,
    ExecutableWorkflowDefinition,
    ExecutableWorkflowHandler,
    ExecutableWorkflowRegistry,
)
from marketing_agents.application.policies.json_schema import compile_json_schema
from marketing_agents.application.policies.write_authorization import (
    AuthorizedExternalWrite,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.connector_families import (
    EnrollAttendeeCommand,
    SendCommunityMessageCommand,
    SubscribeContactCommand,
    UnsubscribeContactCommand,
)
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.application.ports.external_writes import (
    ConnectorDeliveryContract,
    ConnectorDeliveryFailure,
)
from marketing_agents.application.services.approval_boundaries import ApprovalBoundaryService
from marketing_agents.application.services.external_action_dispatcher import (
    DispatchDisposition,
    ExternalActionDispatcher,
)
from marketing_agents.application.services.plan_persistence import AuditedPlanPersistenceService
from marketing_agents.application.services.run_lifecycle import RunLifecycleService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import ExternalAction, Run, WorkItem
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState, WorkMode
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.run_lifecycle import (
    CompletionContext,
    NoRunTransitionContext,
    RunLifecycleCommand,
)
from marketing_agents.domain.runtime_policy import RunRuntimePolicy
from marketing_agents.infrastructure.adapters.connectors.composition import (
    build_durable_connector_bundle,
)
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.instance_configuration_constraints import (
    registered_mock_bindings,
)

CatalogWriteCommand = (
    SubscribeContactCommand
    | UnsubscribeContactCommand
    | EnrollAttendeeCommand
    | SendCommunityMessageCommand
)

CATALOG_WRITE_CAPABILITIES: MappingProxyType[str, str] = MappingProxyType(
    {
        "tpl.email.newsletter.newsletter-subscriber": "cap.newsletter.subscribe",
        "tpl.email.newsletter.unsubscribe-assistant": "cap.newsletter.unsubscribe",
        "tpl.community.events.attendee-scheduler": "cap.events.enroll-attendee",
        "tpl.community.education.course-cohort-onboarder": "cap.messaging.send-message",
    }
)
_COMMAND_TYPES: MappingProxyType[str, type[CatalogWriteCommand]] = MappingProxyType(
    {
        "tpl.email.newsletter.newsletter-subscriber": SubscribeContactCommand,
        "tpl.email.newsletter.unsubscribe-assistant": UnsubscribeContactCommand,
        "tpl.community.events.attendee-scheduler": EnrollAttendeeCommand,
        "tpl.community.education.course-cohort-onboarder": SendCommunityMessageCommand,
    }
)
_STEP_KEY = "execute-approved-role-command"
_TERMINAL = frozenset({RunState.COMPLETED, RunState.CANCELLED, RunState.REJECTED, RunState.FAILED})


class CatalogWriteWorkflowError(ValueError):
    """Stable payload-safe failures; command content is never echoed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogWriteWorkflowError("catalog_write_command_invalid")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise CatalogWriteWorkflowError("catalog_write_command_invalid")


def parse_catalog_write_command(template_id: str, source_content: str) -> CatalogWriteCommand:
    """Parse a bounded, versioned operator command, never a proposed-actions list.

    The command type and capability are chosen only by this immutable role map.
    Destination references remain untrusted until the exact action is approved.
    """
    command_type = _COMMAND_TYPES.get(template_id)
    if command_type is None:
        raise CatalogWriteWorkflowError("catalog_write_role_unsupported")
    try:
        if (
            type(source_content) is not str
            or not 1 <= len(source_content.encode("utf-8")) <= 16_384
        ):
            raise ValueError("bounded command required")
        document = json.loads(
            source_content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if (
            type(document) is not dict
            or set(document) != {"version", "command"}
            or type(document["version"]) is not int
            or document["version"] != 1
            or type(document["command"]) is not dict
        ):
            raise ValueError("exact command envelope required")
        command = command_type.model_validate_json(
            canonical_json_bytes(document["command"]), strict=True
        )
        values = command.model_dump(mode="json")
        # The general messaging DTO leaves individual recipient bounds open;
        # the catalog workflow requires explicit, unique, normalized references.
        for key, value in values.items():
            if key.endswith("_ref"):
                _require_reference(value)
            elif key == "recipient_refs":
                if len(value) != len(set(value)):
                    raise ValueError("unique recipients required")
                for reference in value:
                    _require_reference(reference)
        return command
    except (ValueError, TypeError, RecursionError, UnicodeError, ValidationError):
        raise CatalogWriteWorkflowError("catalog_write_command_invalid") from None


def _require_reference(value: str) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 200
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("bounded normalized reference required")


@dataclass(frozen=True, slots=True)
class _AdmittedInstance:
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


class _ReceiptOnlyGateway:
    """Allow dispatcher reconciliation, but deny every new provider call permit."""

    def contract_for(self, _action: ExternalAction) -> ConnectorDeliveryContract:
        raise ConnectorDeliveryFailure(
            "delivery_contract_unavailable",
            "receipt-only recovery cannot authorize another provider call",
            request_may_have_left_process=False,
        )

    async def execute(self, _authorization: AuthorizedExternalWrite) -> ConnectorWriteResult:
        raise ConnectorDeliveryFailure(
            "delivery_contract_unavailable",
            "receipt-only recovery cannot invoke a provider",
            request_may_have_left_process=False,
        )


class CatalogWriteWorkflowService:
    """One WRITE, one approval, one durable mock receipt; no catalog artifact."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        catalog: CompiledCatalog,
        workflows: ExecutableWorkflowRegistry,
    ) -> None:
        self._dependencies = dependencies
        self._catalog = catalog
        self._workflows = workflows
        self._templates = {item.id: item for item in catalog.templates}
        self._instances = {item.id: item for item in catalog.instances}
        self._capabilities = {item.id: item for item in catalog.tool_capabilities}
        self._policies = {item.id: item for item in catalog.approval_policies}
        self._bundle = build_durable_connector_bundle(
            catalog,
            unit_of_work_factory=dependencies.unit_of_work_factory,
            clock=dependencies.clock,
        )

    async def resume_persisted(self, run_id: str, *, worker_id: str, correlation_id: str) -> None:
        async with self._dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            work = None if run is None else await uow.works.get(run.work_item_id)
        if run is None or work is None:
            raise CatalogWriteWorkflowError("catalog_write_run_missing")
        definition = self._require_admitted_contract(work, run)
        command = parse_catalog_write_command(
            definition.template_id, work.admitted_payload["source_content"]
        )
        expected, graph, routing = self._build_plan(work, run, definition, command)
        context = AuditContext.worker(worker_id, correlation_id=correlation_id)
        lifecycle = RunLifecycleService(self._dependencies)
        if run.state is RunState.RECEIVED:
            await self._require_current_configuration(work, expected)
            run = (
                await lifecycle.advance(
                    run.id,
                    run.version,
                    RunLifecycleCommand.MARK_VALIDATED,
                    NoRunTransitionContext(),
                    audit_context=context,
                )
            ).run
        if run.state is RunState.VALIDATED:
            await self._require_current_configuration(work, expected)
            run = (
                await AuditedPlanPersistenceService(
                    self._dependencies,
                    require_current_configuration_lock=True,
                ).persist(
                    expected,
                    graph,
                    routing,
                    expected_run_version=run.version,
                    audit_context=context,
                )
            ).run
        # Terminal validation does not require a still-current deployment config:
        # historical receipts and cancelled/rejected plans remain inspectable.
        run, action = await self._validated_snapshot(work, expected)
        if run.state in _TERMINAL:
            if run.state is RunState.COMPLETED:
                await self._require_receipt_and_success(run, action)
            return
        # An effect committed before a crash must still be reconciled if an
        # operator subsequently disables/reconfigures this instance. Recovery
        # gets a gateway that cannot authorize or execute another provider call;
        # this exception therefore grants receipt finalization, never a retry.
        if action.state in {
            ExternalActionState.DISPATCHING,
            ExternalActionState.SUCCEEDED,
        } and await self._has_bound_receipt(action):
            if action.state is ExternalActionState.DISPATCHING:
                await ExternalActionDispatcher(
                    self._dependencies,
                    _ReceiptOnlyGateway(),
                    WriteAuthorizationGuard(),
                    lease_duration=timedelta(minutes=1),
                ).recover_action(action.id, lease_owner=worker_id)
                run, action = await self._validated_snapshot(work, expected)
            if action.state is ExternalActionState.SUCCEEDED and run.state not in _TERMINAL:
                await self._require_receipt_and_success(run, action)
                await lifecycle.advance(
                    run.id,
                    run.version,
                    RunLifecycleCommand.COMPLETE,
                    CompletionContext(1, 1, 0, 0),
                    audit_context=context,
                )
            return
        await self._require_current_configuration(work, expected)
        if run.state is RunState.AWAITING_APPROVAL:
            await ApprovalBoundaryService(self._dependencies).evaluate(
                run.id,
                audit_context=context,
            )
            run, action = await self._validated_snapshot(work, expected)
        if run.state in _TERMINAL or run.state is RunState.AWAITING_APPROVAL:
            return
        if run.state is not RunState.EXECUTING:
            raise CatalogWriteWorkflowError("catalog_write_state_invalid")
        gateway = RegistryConnectorWriteGateway(
            self._bundle.registry,
            self._bundle,
            binding_configuration_revisions={
                action.connector_binding_id: work.configuration_revision,
            },
        )
        dispatcher = ExternalActionDispatcher(
            self._dependencies,
            gateway,
            WriteAuthorizationGuard(),
            lease_duration=timedelta(minutes=1),
        )
        if action.state is ExternalActionState.DISPATCHING:
            await dispatcher.recover_action(action.id, lease_owner=worker_id)
            run, action = await self._validated_snapshot(work, expected)
            if run.state in _TERMINAL or action.state is ExternalActionState.DISPATCHING:
                return
        await self._require_current_configuration(work, expected)
        result = await dispatcher.dispatch_once(action.id, lease_owner=worker_id)
        if result.disposition in {
            DispatchDisposition.RECOVERY_PENDING,
            DispatchDisposition.REQUEUED,
            DispatchDisposition.LOST_CLAIM,
        }:
            return
        if result.disposition not in {
            DispatchDisposition.SUCCEEDED,
            DispatchDisposition.ALREADY_SUCCEEDED,
        }:
            raise CatalogWriteWorkflowError("catalog_write_execution_failed")
        run, action = await self._validated_snapshot(work, expected)
        if run.state in {RunState.CANCELLED, RunState.FAILED, RunState.REJECTED}:
            return
        await self._require_receipt_and_success(run, action)
        if run.state is not RunState.COMPLETED:
            await lifecycle.advance(
                run.id,
                run.version,
                RunLifecycleCommand.COMPLETE,
                CompletionContext(1, 1, 0, 0),
                audit_context=context,
            )

    def _require_admitted_contract(self, work: WorkItem, run: Run) -> ExecutableWorkflowDefinition:
        definition = self._workflows.get(work.workflow_id)
        instance = self._instances.get(work.instance_id)
        if (
            instance is None
            or work.mode is not WorkMode.MOCK_EXECUTION
            or definition.handler_kind is not ExecutableWorkflowHandler.CATALOG_WRITE
            or definition.execution_kind is not CatalogRoleExecutionKind.WRITE
            or definition.capability_id != CATALOG_WRITE_CAPABILITIES.get(instance.template_id)
            or run.work_item_id != work.id
            or run.configuration_revision != work.configuration_revision
            or run.catalog_hash != self._catalog.content_hash
            or work.brief_id is not None
            or work.input_schema_id != definition.input_schema_id
            or work.input_schema_hash != definition.input_schema_hash
        ):
            raise CatalogWriteWorkflowError("catalog_write_admission_invalid")
        self._workflows.require_match(
            definition.id,
            instance_id=instance.id,
            template_id=instance.template_id,
            trigger_kind=definition.eligible_trigger_kinds[0],
            mode=work.mode,
            catalog_content_hash=run.catalog_hash,
        )
        compile_json_schema(
            definition.input_schema,
            expected_schema_id=definition.input_schema_id,
        ).validate(work.admitted_payload, pointer_root="/input", max_depth=16)
        return definition

    def _build_plan(
        self,
        work: WorkItem,
        run: Run,
        definition: ExecutableWorkflowDefinition,
        command: CatalogWriteCommand,
    ) -> tuple[EffectPlan, DependencyGraph, RoutingResult]:
        template = self._templates[definition.template_id]
        instance = self._instances[work.instance_id]
        capability = self._capabilities[CATALOG_WRITE_CAPABILITIES[template.id]]
        if (
            capability.id not in template.allowed_tool_capability_ids
            or template.operation_classification != "mutating"
            or template.budget_policy.max_model_calls != 0
            or capability.effect != "write"
        ):
            raise CatalogWriteWorkflowError("catalog_write_capability_invalid")
        planning_template = type(template).model_validate(
            {**template.model_dump(mode="python"), "allowed_tool_capability_ids": (capability.id,)}
        )
        binding_id = registered_mock_bindings(self._catalog, instance.id)[
            capability.connector_family
        ]
        router = DeterministicInstanceRouter(
            catalog_content_hash=self._catalog.content_hash,
            templates=(planning_template,),
            instances=(
                _AdmittedInstance(
                    instance.id,
                    instance.template_id,
                    instance.display_order,
                    True,
                    instance.variant,
                    work.configuration_revision,
                ),
            ),
            capability_ids=(capability.id,),
        )
        routing = router.route(
            RoutingRequest(
                target_instance_id=instance.id,
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
            (TopologyStep(_STEP_KEY, 1, (), terminal_result=True),),
            workflow_max_steps=1,
            global_max_steps=1,
        )
        planner = EffectAwarePlanner(
            catalog_content_hash=self._catalog.content_hash,
            clock=self._dependencies.clock,
            ids=self._dependencies.ids,
            capabilities=(capability,),
            templates=(planning_template,),
            template_output_schemas={template.id: definition.output_schema},
            approval_policies=(self._policies[template.approval_policy_id],),
            operations=(self._bundle.registry.resolve(capability.id),),
            bindings=(
                _PlanningBinding(
                    instance.id,
                    capability.connector_family,
                    binding_id,
                    True,
                    work.configuration_revision,
                ),
            ),
            run_policy=RunRuntimePolicy(
                max_steps=1,
                max_model_calls=0,
                max_tool_calls=1,
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
                        step_key=_STEP_KEY,
                        kind="catalog-write",
                        selected_instance_id=instance.id,
                        routing_slot_key=None,
                        capability_id=capability.id,
                        binding_id=binding_id,
                        write_intent=WriteActionIntent(command),
                    ),
                ),
                requested_by="worker.catalog-write",
            )
        )
        return plan, graph, routing

    async def _require_current_configuration(self, work: WorkItem, expected: EffectPlan) -> None:
        # This is a fresh-resume gate, not an atomic revocation of in-flight calls.
        # The dispatcher binds the approved immutable revision and separately
        # fences cancellation. A configuration edit racing after this read does
        # not retroactively revoke an already-started approved connector call.
        async with self._dependencies.unit_of_work() as uow:
            configuration = await uow.configurations.get(work.instance_id)
        step = expected.steps[0]
        binding = (
            None
            if configuration is None
            else configuration.connector_bindings.get(step.connector_family)
        )
        if (
            configuration is None
            or not configuration.enabled
            or configuration.configuration_revision != work.configuration_revision
            or binding is None
            or not binding.enabled
            or binding.binding_id != step.binding_id
        ):
            raise CatalogWriteWorkflowError("catalog_write_configuration_stale")

    async def _validated_snapshot(
        self, work: WorkItem, expected: EffectPlan
    ) -> tuple[Run, ExternalAction]:
        async with self._dependencies.unit_of_work() as uow:
            run = await uow.runs.get(expected.run_id)
            plan = await uow.run_steps.get_plan(expected.run_id)
            steps = await uow.run_steps.validate_plan_for_execution(expected.run_id)
            actions = await uow.external_actions.list_run_plan(expected.run_id, expected.plan_hash)
            selection = await uow.approvals.get_current_authorization_set(expected.run_id)
            approvals = (
                ()
                if selection is None
                else await uow.approvals.list_current_set(
                    expected.run_id,
                    selection.authorization_set.plan_hash,
                    selection.authorization_set.proposal_revision,
                )
            )
        if (
            run is None
            or run.work_item_id != work.id
            or plan is None
            or plan.plan_hash != expected.plan_hash
            or plan.graph_hash != expected.graph_hash
            or plan.routing_hash != expected.routing_hash
            or plan.step_count != 1
            or not plan.approval_required
            or len(steps) != 1
            or len(actions) != 1
            or selection is None
            or len(approvals) != 1
        ):
            raise CatalogWriteWorkflowError("catalog_write_plan_invalid")
        action = actions[0]
        step = steps[0]
        proposed = expected.proposed_actions[0].envelope
        if (
            action.envelope.semantic_action_hash != proposed.semantic_action_hash
            or canonical_json_bytes(action.envelope.minimized_payload)
            != canonical_json_bytes(proposed.minimized_payload)
            or action.run_id != run.id
            or action.step_id != step.id
            or action.envelope.step_key != _STEP_KEY
            or step.key != _STEP_KEY
            or action.envelope.plan_hash != plan.plan_hash
            or action.envelope.instance_id != work.instance_id
            or action.envelope.template_id != proposed.template_id
            or action.envelope.capability_id != proposed.capability_id
            or action.envelope.binding_id != proposed.binding_id
            or action.envelope.action_type != proposed.action_type
            or action.envelope.payload_schema_id != proposed.payload_schema_id
            or action.delivery_contract.binding_configuration_revision
            != work.configuration_revision
        ):
            raise CatalogWriteWorkflowError("catalog_write_action_invalid")
        authorization = selection.authorization_set
        request = approvals[0].request
        if (
            authorization.run_id != run.id
            or authorization.plan_hash != plan.plan_hash
            or len(authorization.members) != 1
            or authorization.members[0].action_id != action.id
            or authorization.members[0].action_hash != action.action_hash
            or authorization.members[0].step_id != step.id
            or authorization.members[0].step_key != step.key
            or request.action_id != action.id
            or request.action_hash != action.action_hash
            or request.authorization_set_id != authorization.id
        ):
            raise CatalogWriteWorkflowError("catalog_write_approval_invalid")
        return run, action

    async def _require_receipt_and_success(self, run: Run, action: ExternalAction) -> None:
        async with self._dependencies.unit_of_work() as uow:
            receipt = await uow.connector_receipts.get(
                action.connector_binding_id,
                action.idempotency_key,
            )
            steps = await uow.run_steps.validate_plan_for_execution(run.id)
            control = await uow.execution_control.get(run.id)
            artifacts = await uow.artifacts.list_for_run(run.id)
        if (
            action.state is not ExternalActionState.SUCCEEDED
            or action.result is None
            or receipt is None
            or receipt.external_action_id != action.id
            or receipt.action_hash != action.action_hash
            or receipt.capability_id != action.envelope.capability_id
            or receipt.receipt_id != action.result.receipt_id
            or receipt.status != action.result.status
            or receipt.safe_metadata.get("external_side_effect") is not False
            or len(steps) != 1
            or steps[0].state is not StepState.SUCCEEDED
            or control is None
            or control.model_calls != 0
            or control.tool_calls != 1
            or artifacts
        ):
            raise CatalogWriteWorkflowError("catalog_write_receipt_invalid")

    async def _has_bound_receipt(self, action: ExternalAction) -> bool:
        async with self._dependencies.unit_of_work() as uow:
            receipt = await uow.connector_receipts.get(
                action.connector_binding_id,
                action.idempotency_key,
            )
        if receipt is None:
            return False
        if (
            receipt.external_action_id != action.id
            or receipt.connector_binding_id != action.connector_binding_id
            or receipt.idempotency_key != action.idempotency_key
            or receipt.action_hash != action.action_hash
            or receipt.capability_id != action.envelope.capability_id
            or receipt.safe_metadata.get("external_side_effect") is not False
        ):
            raise CatalogWriteWorkflowError("catalog_write_receipt_invalid")
        return True
