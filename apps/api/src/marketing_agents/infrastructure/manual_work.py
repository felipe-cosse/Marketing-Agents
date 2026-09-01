"""Compiled-catalog resolution for authorized manual work admission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.ports.manual_work import (
    ManualAdmissionBinding,
    ManualAdmissionResolutionError,
    ManualIncomingWorkValidator,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services.incoming_work_validation import (
    CampaignBriefPolicy,
    ConfiguredIncomingTrigger,
    IncomingWorkValidationError,
    IncomingWorkValidator,
    ValidatedIncomingWork,
    WorkflowAdmissionDefinition,
)
from marketing_agents.application.services.manual_work_intake import ManualDryRunCommand
from marketing_agents.demos import (
    DEMO_SCENARIOS,
    DemoScenarioInputError,
    DemoScenarioRegistry,
    DemoScenarioRegistryError,
)
from marketing_agents.demos.contracts import DemoScenarioDefinition
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.domain.instance_configuration import InstanceConfiguration
from marketing_agents.domain.validation import require_digest, require_id
from marketing_agents.infrastructure.catalog.models import (
    AgentInstanceRecord,
    AgentTemplateRecord,
    CompiledCatalog,
    ToolCapabilityRecord,
)
from marketing_agents.infrastructure.instance_configuration_constraints import (
    InstanceConfigurationConstraintError,
    validate_mock_connector_bindings,
)

_MANUAL_SOURCE = "manual"


def _unavailable() -> ManualAdmissionResolutionError:
    return ManualAdmissionResolutionError(
        "manual_binding_unavailable",
        "manual admission binding is unavailable",
    )


def _resolution_error(code: str, message: str) -> ManualAdmissionResolutionError:
    return ManualAdmissionResolutionError(code, message)


def _unique_index[ValueT](
    values: tuple[ValueT, ...],
    *,
    label: str,
) -> MappingProxyType[str, ValueT]:
    indexed: dict[str, ValueT] = {}
    for value in values:
        identifier = getattr(value, "id", None)
        if type(identifier) is not str:
            raise ValueError(f"compiled {label} ID must be text")
        require_id(identifier, f"compiled {label} ID")
        if identifier in indexed:
            raise ValueError(f"compiled {label} IDs must be unique")
        indexed[identifier] = value
    return MappingProxyType(indexed)


def _canonical_difference_pointer(left: Any, right: Any, path: str = "") -> str:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        for key in sorted(set(left) | set(right)):
            pointer = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in left or key not in right:
                return pointer
            difference = _canonical_difference_pointer(left[key], right[key], pointer)
            if difference:
                return difference
        return ""
    if (
        isinstance(left, Sequence)
        and not isinstance(left, (str, bytes, bytearray))
        and isinstance(right, Sequence)
        and not isinstance(right, (str, bytes, bytearray))
    ):
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=False)):
            difference = _canonical_difference_pointer(
                left_item,
                right_item,
                path + f"/{index}",
            )
            if difference:
                return difference
        return path or "/" if len(left) != len(right) else ""
    return (path or "/") if left != right else ""


@dataclass(frozen=True, slots=True)
class _EffectiveManualInstance:
    """Narrow validator-facing projection of the locked mutable configuration."""

    id: str
    template_id: str
    enabled: bool
    configuration_revision: int


@dataclass(frozen=True, slots=True)
class _DemoIncomingWorkValidator:
    """Apply scenario semantic validation before issuing the generic admission seal."""

    delegate: IncomingWorkValidator
    registry: DemoScenarioRegistry
    scenario_id: str

    def validate(self, envelope: AdmissionEnvelope) -> ValidatedIncomingWork:
        try:
            normalized = self.registry.validate_input(
                self.scenario_id,
                envelope.admitted_payload,
            )
            if canonical_json_bytes(normalized) != canonical_json_bytes(envelope.admitted_payload):
                raise DemoScenarioInputError(
                    "demo_scenario_invalid",
                    "demo input must use its canonical representation",
                    pointer=(
                        _canonical_difference_pointer(envelope.admitted_payload, normalized) or "/"
                    ),
                )
        except DemoScenarioInputError as exc:
            relative = exc.pointer or "/"
            pointer = "/input" if relative == "/" else "/input" + relative
            raise IncomingWorkValidationError(
                "input_schema_invalid",
                "incoming payload does not conform to its schema",
                pointer=pointer,
            ) from None
        return self.delegate.validate(envelope)


class CompiledCatalogManualAdmissionResolver:
    """Resolve one manual route exclusively from trusted catalog and locked state."""

    def __init__(
        self,
        catalog: CompiledCatalog,
        *,
        mock_connectors_active: bool,
        demo_scenarios: DemoScenarioRegistry = DEMO_SCENARIOS,
    ) -> None:
        if type(catalog) is not CompiledCatalog:
            raise ValueError("manual admission resolver requires one compiled catalog")
        if type(mock_connectors_active) is not bool:
            raise ValueError("mock connector mode must be an exact boolean")
        if type(demo_scenarios) is not DemoScenarioRegistry:
            raise ValueError("manual admission resolver requires an exact demo registry")
        if not catalog.content_hash.startswith("catalog-sha256-v1:"):
            raise ValueError("compiled catalog hash version is invalid")
        require_digest(
            catalog.content_hash.removeprefix("catalog-sha256-v1:"),
            "compiled catalog hash",
        )
        self._catalog = catalog
        self._mock_connectors_active = mock_connectors_active
        self._demo_scenarios = demo_scenarios
        self._instances = _unique_index(catalog.instances, label="instance")
        self._templates = _unique_index(catalog.templates, label="template")
        self._capabilities = _unique_index(catalog.tool_capabilities, label="capability")

    async def resolve_in_uow(
        self,
        unit_of_work: UnitOfWork,
        command: ManualDryRunCommand,
    ) -> ManualAdmissionBinding:
        """Resolve the effective configuration inside the receipt transaction."""

        if type(command) is not ManualDryRunCommand:
            raise _unavailable()
        catalog_instance = self._instances.get(command.instance_id)
        if catalog_instance is None:
            raise _resolution_error(
                "instance_unknown",
                "agent instance is not registered",
            )
        try:
            configuration = await unit_of_work.configurations.get_for_update(command.instance_id)
            if configuration is None:
                raise _unavailable()
            return self._binding(catalog_instance, configuration, command)
        except ManualAdmissionResolutionError:
            raise
        except InstanceConfigurationConstraintError:
            raise _unavailable() from None
        except Exception:
            raise _unavailable() from None

    def _binding(
        self,
        catalog_instance: AgentInstanceRecord,
        configuration: InstanceConfiguration,
        command: ManualDryRunCommand,
    ) -> ManualAdmissionBinding:
        if type(catalog_instance) is not AgentInstanceRecord:
            raise _unavailable()
        if (
            type(configuration) is not InstanceConfiguration
            or configuration.instance_id != catalog_instance.id
            or command.instance_id != catalog_instance.id
        ):
            raise _unavailable()
        template = self._templates.get(catalog_instance.template_id)
        if type(template) is not AgentTemplateRecord:
            raise _unavailable()
        if not configuration.enabled:
            raise _resolution_error("instance_disabled", "agent instance is disabled")

        supported_kinds = frozenset(
            TriggerKind(value) for value in template.supported_trigger_types
        )
        configured_kinds = frozenset(binding.kind for binding in configuration.trigger_bindings)
        if not configured_kinds.issubset(supported_kinds):
            raise _unavailable()
        if TriggerKind.MANUAL not in supported_kinds:
            raise _resolution_error(
                "manual_trigger_unavailable",
                "agent instance has no enabled manual trigger",
            )
        manual_binding = next(
            (
                binding
                for binding in configuration.trigger_bindings
                if binding.kind is TriggerKind.MANUAL
            ),
            None,
        )
        if manual_binding is not None and not manual_binding.enabled:
            raise _resolution_error(
                "manual_trigger_unavailable",
                "agent instance has no enabled manual trigger",
            )

        validate_mock_connector_bindings(
            self._catalog,
            configuration.instance_id,
            configuration.connector_bindings,
        )
        allowed_modes: tuple[WorkMode, ...] = (WorkMode.DRY_RUN,)
        if self._mock_connectors_active:
            allowed_modes += (WorkMode.MOCK_EXECUTION,)
        if command.mode not in allowed_modes:
            raise _resolution_error(
                "work_mode_not_allowed",
                "execution mode is not allowed for manual work",
            )
        if command.campaign_brief_id is not None:
            raise _resolution_error(
                "campaign_brief_unknown",
                "campaign brief is not registered",
            )
        trigger_id = self._manual_trigger_id(catalog_instance.id)
        scenario = self._scenario(command.demo_scenario_id)
        if scenario is not None and (
            scenario.instance_id != catalog_instance.id or scenario.template_id != template.id
        ):
            raise _resolution_error(
                "demo_scenario_unknown",
                "demo scenario is not registered for this agent instance",
            )
        workflow_id = (
            scenario.workflow_id if scenario is not None else self._manual_workflow_id(template.id)
        )
        base_validator = self._validator(
            template=template,
            configuration=configuration,
            trigger_id=trigger_id,
            workflow_id=workflow_id,
            allowed_modes=allowed_modes,
            scenario=scenario,
        )
        validator: ManualIncomingWorkValidator = base_validator
        if scenario is not None:
            validator = _DemoIncomingWorkValidator(
                base_validator,
                self._demo_scenarios,
                scenario.id,
            )
        return ManualAdmissionBinding(
            instance_id=configuration.instance_id,
            source=_MANUAL_SOURCE,
            trigger_id=trigger_id,
            workflow_id=workflow_id,
            configuration_revision=configuration.configuration_revision,
            brief_id=None,
            brief_revision=None,
            demo_scenario_id=(scenario.id if scenario is not None else None),
            validator=validator,
        )

    def _validator(
        self,
        *,
        template: AgentTemplateRecord,
        configuration: InstanceConfiguration,
        trigger_id: str,
        workflow_id: str,
        allowed_modes: tuple[WorkMode, ...],
        scenario: DemoScenarioDefinition | None,
    ) -> IncomingWorkValidator:
        schema = (
            scenario.input_schema
            if scenario is not None
            else self._catalog.input_schema_by_template.get(template.id)
        )
        if schema is None:
            raise _unavailable()
        input_schema_id = (
            scenario.input_schema_id if scenario is not None else template.input_schema_id
        )
        capabilities = self._selected_capabilities(template, scenario=scenario)
        budget = template.budget_policy
        timeout = template.timeout_policy
        rate_limit = template.rate_limit_policy
        guard = RuntimePolicyGuard(
            RuntimePolicySnapshot(
                allowed_capabilities=tuple(
                    CapabilityPolicy(
                        capability_id=capability.id,
                        effect=capability.effect,
                        connector_family=capability.connector_family,
                    )
                    for capability in capabilities
                ),
                input_max_bytes=budget.max_input_bytes,
                max_input_field_bytes=budget.max_input_field_bytes,
                output_max_bytes=budget.max_output_bytes,
                max_json_depth=64,
                max_content_parts=256,
                max_content_characters=min(budget.max_input_bytes, 1_000_000),
                max_model_calls=(
                    scenario.expected_model_calls
                    if scenario is not None
                    else budget.max_model_calls
                ),
                max_tool_calls=(
                    scenario.expected_connector_calls
                    if scenario is not None
                    else budget.max_tool_calls
                ),
                rate_window_max_calls=rate_limit.max_calls,
                rate_window_seconds=rate_limit.window_seconds,
                step_timeout_seconds=timeout.step_seconds,
                run_timeout_seconds=timeout.run_seconds,
            )
        )
        effective_instance = _EffectiveManualInstance(
            id=configuration.instance_id,
            template_id=template.id,
            enabled=configuration.enabled,
            configuration_revision=configuration.configuration_revision,
        )
        return IncomingWorkValidator(
            catalog_hash=self._catalog.content_hash,
            templates=(template,),
            instances=(effective_instance,),
            input_schemas_by_template={template.id: schema},
            triggers=(
                ConfiguredIncomingTrigger(
                    id=trigger_id,
                    instance_id=configuration.instance_id,
                    kind=TriggerKind.MANUAL,
                    source=_MANUAL_SOURCE,
                    workflow_ids=(workflow_id,),
                ),
            ),
            workflows=(
                WorkflowAdmissionDefinition(
                    id=workflow_id,
                    eligible_template_ids=(template.id,),
                    eligible_trigger_kinds=(TriggerKind.MANUAL,),
                    allowed_modes=allowed_modes,
                    input_schema_ids_by_template={template.id: input_schema_id},
                    campaign_brief_policy=CampaignBriefPolicy.FORBIDDEN,
                ),
            ),
            campaign_brief_revisions=(),
            guard=guard,
        )

    def _scenario(self, scenario_id: str | None) -> DemoScenarioDefinition | None:
        if scenario_id is None:
            return None
        try:
            return self._demo_scenarios.get(scenario_id)
        except DemoScenarioRegistryError:
            raise _resolution_error(
                "demo_scenario_unknown",
                "demo scenario is not registered",
            ) from None

    def _selected_capabilities(
        self,
        template: AgentTemplateRecord,
        *,
        scenario: DemoScenarioDefinition | None,
    ) -> tuple[ToolCapabilityRecord, ...]:
        selected: list[ToolCapabilityRecord] = []
        if scenario is None:
            identifiers = template.allowed_tool_capability_ids
        else:
            identifiers = tuple(dict.fromkeys(step.capability_id for step in scenario.steps))
            selected_agents = {agent.instance_id: agent for agent in scenario.selected_agents}
            for step in scenario.steps:
                agent = selected_agents.get(step.selected_instance_id)
                instance = self._instances.get(step.selected_instance_id)
                owner_template = (
                    None
                    if agent is None or instance is None
                    else self._templates.get(agent.template_id)
                )
                if (
                    agent is None
                    or instance is None
                    or type(owner_template) is not AgentTemplateRecord
                    or instance.template_id != agent.template_id
                    or step.capability_id not in owner_template.allowed_tool_capability_ids
                ):
                    raise _unavailable()
        for identifier in identifiers:
            capability = self._capabilities.get(identifier)
            if type(capability) is not ToolCapabilityRecord:
                raise _unavailable()
            selected.append(capability)
        if not selected or len(selected) != len(set(identifiers)):
            raise _unavailable()
        return tuple(selected)

    @staticmethod
    def _manual_trigger_id(instance_id: str) -> str:
        identifier = f"trigger.manual.{instance_id.removeprefix('inst.')}.v1"
        require_id(identifier, "manual trigger ID")
        return identifier

    @staticmethod
    def _manual_workflow_id(template_id: str) -> str:
        identifier = f"workflow.manual.{template_id.removeprefix('tpl.')}.v1"
        require_id(identifier, "manual workflow ID")
        return identifier


__all__ = ["CompiledCatalogManualAdmissionResolver"]
