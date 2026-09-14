"""Compose exact catalog-role workflows and injected existing demo contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from marketing_agents.application.orchestration.executable_workflows import (
    CatalogRoleExecutionKind,
    ExecutableWorkflowDefinition,
    ExecutableWorkflowHandler,
    ExecutableWorkflowRegistry,
    catalog_role_workflow_id,
)
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.infrastructure.catalog.models import CompiledCatalog

_MODEL_CAPABILITY = "cap.model.generate-structured"
_TRANSFORM_CAPABILITY = "cap.artifact.transform-deterministic"
_READ_DEMO_IDS = frozenset(
    {
        "demo.social-media.content-draft.v1",
        "demo.blog-seo.content-review.v1",
        "demo.community.reminder-draft.v1",
        "demo.partnerships.application-review.v1",
    }
)
_EMAIL_DEMO_ID = "demo.email.signup-onboarding.v1"


class DemoWorkflowAgentSource(Protocol):
    @property
    def instance_id(self) -> str: ...

    @property
    def template_id(self) -> str: ...


class DemoWorkflowStepSource(Protocol):
    @property
    def selected_instance_id(self) -> str: ...

    @property
    def capability_id(self) -> str: ...

    @property
    def effect(self) -> str: ...


class DemoWorkflowSource(Protocol):
    """Read-only structural seam; infrastructure does not import demo implementations."""

    @property
    def id(self) -> str: ...

    @property
    def workflow_id(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def template_id(self) -> str: ...

    @property
    def instance_id(self) -> str: ...

    @property
    def effect(self) -> str: ...

    @property
    def selected_agents(self) -> Sequence[DemoWorkflowAgentSource]: ...

    @property
    def steps(self) -> Sequence[DemoWorkflowStepSource]: ...

    @property
    def input_schema_id(self) -> str: ...

    @property
    def input_schema(self) -> Mapping[str, Any]: ...

    @property
    def output_schema_id(self) -> str: ...

    @property
    def output_schema(self) -> Mapping[str, Any]: ...

    @property
    def definition_hash(self) -> str: ...

    @property
    def expected_model_calls(self) -> int: ...

    @property
    def expected_connector_calls(self) -> int: ...

    @property
    def expected_external_actions(self) -> int: ...

    @property
    def expected_approvals(self) -> int: ...


def build_executable_workflow_registry(
    catalog: CompiledCatalog,
    *,
    demo_scenarios: Iterable[DemoWorkflowSource] = (),
) -> ExecutableWorkflowRegistry:
    """Register real catalog-schema simulations, never aliases to demo presets.

    Read handlers produce role-specific artifacts; write-only roles select an
    explicit action workflow. Model output never selects a write capability.
    """
    if type(catalog) is not CompiledCatalog:
        raise ValueError("executable workflow factory requires one compiled catalog")
    templates = {item.id: item for item in catalog.templates}
    instances = {item.id: item for item in catalog.instances}
    capabilities = {item.id: item for item in catalog.tool_capabilities}
    approval_policies = {item.id: item for item in catalog.approval_policies}
    if (
        len(templates) != len(catalog.templates)
        or len(instances) != len(catalog.instances)
        or len(capabilities) != len(catalog.tool_capabilities)
        or len(approval_policies) != len(catalog.approval_policies)
        or any(item.template_id not in templates for item in instances.values())
    ):
        raise ValueError("executable workflow catalog identities are inconsistent")
    definitions: list[ExecutableWorkflowDefinition] = []
    for template in sorted(templates.values(), key=lambda item: item.id):
        target_ids = tuple(
            sorted(item.id for item in instances.values() if item.template_id == template.id)
        )
        if not target_ids:
            raise ValueError("executable workflow template has no registered instances")
        if any(
            identifier not in capabilities for identifier in template.allowed_tool_capability_ids
        ):
            raise ValueError("executable workflow capability is unavailable")
        capability_id = None
        handler = ExecutableWorkflowHandler.CATALOG_ROLE
        execution_kind = CatalogRoleExecutionKind.LOCAL_TRANSFORM
        model_calls = 0
        if _MODEL_CAPABILITY in template.allowed_tool_capability_ids:
            capability = capabilities[_MODEL_CAPABILITY]
            if (
                capability.effect != "read"
                or capability.connector_family != "model"
                or template.budget_policy.max_model_calls < 1
            ):
                raise ValueError("catalog model workflow capability or budget is invalid")
            capability_id = _MODEL_CAPABILITY
            execution_kind = CatalogRoleExecutionKind.MODEL
            model_calls = 1
        elif _TRANSFORM_CAPABILITY in template.allowed_tool_capability_ids:
            capability = capabilities[_TRANSFORM_CAPABILITY]
            if capability.effect != "read" or capability.connector_family != "artifact":
                raise ValueError("catalog transform workflow capability is invalid")
            capability_id = _TRANSFORM_CAPABILITY
        else:
            writes = tuple(
                capabilities[identifier]
                for identifier in template.allowed_tool_capability_ids
                if capabilities[identifier].effect == "write"
            )
            approval_policy = approval_policies.get(template.approval_policy_id)
            if (
                len(writes) != 1
                or template.operation_classification != "mutating"
                or template.budget_policy.max_tool_calls < 1
                or approval_policy is None
                or approval_policy.kind != "human_external_write"
            ):
                raise ValueError("catalog role requires one explicitly supported execution kind")
            capability_id = writes[0].id
            execution_kind = CatalogRoleExecutionKind.WRITE
            handler = ExecutableWorkflowHandler.CATALOG_WRITE
        for raw_kind in template.supported_trigger_types:
            trigger_kind = TriggerKind(raw_kind)
            # Match existing ingress semantics; no schedule can acquire mock writes.
            modes = (
                (WorkMode.DRY_RUN,)
                if trigger_kind is TriggerKind.SCHEDULE
                else (WorkMode.MOCK_EXECUTION,)
                if trigger_kind is TriggerKind.WEBHOOK
                else (WorkMode.DRY_RUN, WorkMode.MOCK_EXECUTION)
            )
            definitions.append(
                ExecutableWorkflowDefinition(
                    id=catalog_role_workflow_id(template.id, trigger_kind),
                    version=1,
                    catalog_content_hash=catalog.content_hash,
                    handler_kind=handler,
                    template_id=template.id,
                    target_instance_ids=target_ids,
                    eligible_trigger_kinds=(trigger_kind,),
                    allowed_modes=modes,
                    input_schema_id=template.input_schema_id,
                    input_schema=catalog.input_schema_by_template[template.id],
                    output_schema_id=template.output_schema_id,
                    output_schema=catalog.output_schema_by_template[template.id],
                    execution_kind=execution_kind,
                    capability_id=capability_id,
                    expected_model_calls=model_calls,
                    expected_connector_calls=(
                        None if handler is ExecutableWorkflowHandler.CATALOG_WRITE else 0
                    ),
                    expected_external_actions=(
                        None if handler is ExecutableWorkflowHandler.CATALOG_WRITE else 0
                    ),
                    expected_approvals=(
                        None if handler is ExecutableWorkflowHandler.CATALOG_WRITE else 0
                    ),
                )
            )
    for scenario in demo_scenarios:
        if scenario.id in _READ_DEMO_IDS and scenario.effect == "read_only":
            handler = ExecutableWorkflowHandler.DEMO_READ
        elif scenario.id == _EMAIL_DEMO_ID and scenario.effect == "mutating":
            handler = ExecutableWorkflowHandler.DEMO_EMAIL
        else:
            raise ValueError("executable demo handler is unavailable")
        target = instances.get(scenario.instance_id)
        if (
            scenario.workflow_id != scenario.id
            or target is None
            or target.template_id != scenario.template_id
        ):
            raise ValueError("executable demo target does not match the catalog")
        selected = {agent.instance_id: agent for agent in scenario.selected_agents}
        for step in scenario.steps:
            agent = selected.get(step.selected_instance_id)
            instance = instances.get(step.selected_instance_id)
            step_template = None if agent is None else templates.get(agent.template_id)
            step_capability = capabilities.get(step.capability_id)
            if (
                agent is None
                or instance is None
                or step_template is None
                or instance.template_id != step_template.id
                or step.capability_id not in step_template.allowed_tool_capability_ids
                or step_capability is None
                or step_capability.effect != step.effect
            ):
                raise ValueError("executable demo step does not match its catalog capability")
        definitions.append(
            ExecutableWorkflowDefinition(
                id=scenario.workflow_id,
                version=scenario.version,
                catalog_content_hash=catalog.content_hash,
                handler_kind=handler,
                template_id=scenario.template_id,
                target_instance_ids=(scenario.instance_id,),
                eligible_trigger_kinds=(TriggerKind.MANUAL,),
                allowed_modes=(WorkMode.DRY_RUN, WorkMode.MOCK_EXECUTION),
                input_schema_id=scenario.input_schema_id,
                input_schema=scenario.input_schema,
                output_schema_id=scenario.output_schema_id,
                output_schema=scenario.output_schema,
                demo_scenario_id=scenario.id,
                source_definition_hash=scenario.definition_hash,
                expected_steps=len(scenario.steps),
                expected_model_calls=scenario.expected_model_calls,
                expected_connector_calls=scenario.expected_connector_calls,
                expected_external_actions=scenario.expected_external_actions,
                expected_approvals=scenario.expected_approvals,
            )
        )
    return ExecutableWorkflowRegistry(definitions)
