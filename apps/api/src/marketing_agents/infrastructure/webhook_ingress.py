"""Compiled-catalog and locked-configuration routing for webhook admission."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.ports.webhook_admission import (
    WebhookAdmissionBinding,
    WebhookAdmissionResolutionError,
)
from marketing_agents.application.ports.webhooks import (
    require_webhook_source_id,
    require_webhook_trigger_id,
)
from marketing_agents.application.services.incoming_work_validation import (
    CampaignBriefPolicy,
    ConfiguredIncomingTrigger,
    IncomingWorkValidator,
    WorkflowAdmissionDefinition,
)
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


def _unavailable() -> WebhookAdmissionResolutionError:
    return WebhookAdmissionResolutionError(
        "webhook_binding_unavailable",
        "webhook admission binding is unavailable",
    )


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


@dataclass(frozen=True, slots=True)
class _EffectiveWebhookInstance:
    id: str
    template_id: str
    enabled: bool
    configuration_revision: int


class CompiledCatalogWebhookAdmissionResolver:
    """Resolve every explicitly bound target without consulting webhook payload text."""

    def __init__(self, catalog: CompiledCatalog, *, mock_connectors_active: bool) -> None:
        if type(catalog) is not CompiledCatalog:
            raise ValueError("webhook admission resolver requires one compiled catalog")
        if type(mock_connectors_active) is not bool:
            raise ValueError("mock connector mode must be an exact boolean")
        if not catalog.content_hash.startswith("catalog-sha256-v1:"):
            raise ValueError("compiled catalog hash version is invalid")
        require_digest(
            catalog.content_hash.removeprefix("catalog-sha256-v1:"),
            "compiled catalog hash",
        )
        self._catalog = catalog
        self._mock_connectors_active = mock_connectors_active
        self._instances = _unique_index(catalog.instances, label="instance")
        self._templates = _unique_index(catalog.templates, label="template")
        self._capabilities = _unique_index(catalog.tool_capabilities, label="capability")

    async def resolve_all_in_uow(
        self,
        unit_of_work: UnitOfWork,
        *,
        source: str,
        trigger_id: str,
    ) -> tuple[WebhookAdmissionBinding, ...]:
        try:
            require_webhook_source_id(source, "webhook source")
            require_webhook_trigger_id(trigger_id, "webhook trigger ID")
            bindings: list[WebhookAdmissionBinding] = []
            # The catalog is a fixed small set. Locking every effective row gives the
            # selected fan-out one transactionally coherent deployment snapshot.
            for instance_id in sorted(self._instances):
                configuration = await unit_of_work.configurations.get_for_update(instance_id)
                if configuration is None:
                    raise _unavailable()
                binding = self._binding(
                    self._instances[instance_id],
                    configuration,
                    source=source,
                    trigger_id=trigger_id,
                )
                if binding is not None:
                    bindings.append(binding)
            if not bindings:
                raise WebhookAdmissionResolutionError(
                    "webhook_binding_forbidden",
                    "webhook source is not bound to an enabled agent instance",
                )
            return tuple(bindings)
        except WebhookAdmissionResolutionError:
            raise
        except (InstanceConfigurationConstraintError, TypeError, ValueError):
            raise _unavailable() from None
        except Exception:
            raise _unavailable() from None

    def _binding(
        self,
        catalog_instance: AgentInstanceRecord,
        configuration: InstanceConfiguration,
        *,
        source: str,
        trigger_id: str,
    ) -> WebhookAdmissionBinding | None:
        if (
            type(catalog_instance) is not AgentInstanceRecord
            or type(configuration) is not InstanceConfiguration
            or configuration.instance_id != catalog_instance.id
        ):
            raise _unavailable()
        template = self._templates.get(catalog_instance.template_id)
        if type(template) is not AgentTemplateRecord:
            raise _unavailable()
        supported_kinds = frozenset(
            TriggerKind(value) for value in template.supported_trigger_types
        )
        configured_kinds = frozenset(binding.kind for binding in configuration.trigger_bindings)
        if not configured_kinds.issubset(supported_kinds):
            raise _unavailable()
        webhook_binding = next(
            (
                binding
                for binding in configuration.trigger_bindings
                if binding.kind is TriggerKind.WEBHOOK
            ),
            None,
        )
        if (
            not configuration.enabled
            or TriggerKind.WEBHOOK not in supported_kinds
            or webhook_binding is None
            or not webhook_binding.enabled
            or webhook_binding.event_source != source
        ):
            return None
        if not self._mock_connectors_active:
            raise WebhookAdmissionResolutionError(
                "webhook_mode_unavailable",
                "webhook work requires active mock connectors in v1",
            )
        validate_mock_connector_bindings(
            self._catalog,
            configuration.instance_id,
            configuration.connector_bindings,
        )
        workflow_id = self._workflow_id(template.id)
        mode = WorkMode.MOCK_EXECUTION
        return WebhookAdmissionBinding(
            source=source,
            trigger_id=trigger_id,
            instance_id=configuration.instance_id,
            workflow_id=workflow_id,
            configuration_revision=configuration.configuration_revision,
            mode=mode,
            validator=self._validator(
                template=template,
                configuration=configuration,
                source=source,
                trigger_id=trigger_id,
                workflow_id=workflow_id,
                mode=mode,
            ),
        )

    def _validator(
        self,
        *,
        template: AgentTemplateRecord,
        configuration: InstanceConfiguration,
        source: str,
        trigger_id: str,
        workflow_id: str,
        mode: WorkMode,
    ) -> IncomingWorkValidator:
        schema = self._catalog.input_schema_by_template.get(template.id)
        if schema is None:
            raise _unavailable()
        capabilities = self._selected_capabilities(template)
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
                max_model_calls=budget.max_model_calls,
                max_tool_calls=budget.max_tool_calls,
                rate_window_max_calls=rate_limit.max_calls,
                rate_window_seconds=rate_limit.window_seconds,
                step_timeout_seconds=timeout.step_seconds,
                run_timeout_seconds=timeout.run_seconds,
            )
        )
        effective_instance = _EffectiveWebhookInstance(
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
                    kind=TriggerKind.WEBHOOK,
                    source=source,
                    workflow_ids=(workflow_id,),
                ),
            ),
            workflows=(
                WorkflowAdmissionDefinition(
                    id=workflow_id,
                    eligible_template_ids=(template.id,),
                    eligible_trigger_kinds=(TriggerKind.WEBHOOK,),
                    allowed_modes=(mode,),
                    input_schema_ids_by_template={template.id: template.input_schema_id},
                    campaign_brief_policy=CampaignBriefPolicy.FORBIDDEN,
                ),
            ),
            campaign_brief_revisions=(),
            guard=guard,
        )

    def _selected_capabilities(
        self,
        template: AgentTemplateRecord,
    ) -> tuple[ToolCapabilityRecord, ...]:
        selected: list[ToolCapabilityRecord] = []
        for identifier in template.allowed_tool_capability_ids:
            capability = self._capabilities.get(identifier)
            if type(capability) is not ToolCapabilityRecord:
                raise _unavailable()
            selected.append(capability)
        if not selected or len(selected) != len(set(template.allowed_tool_capability_ids)):
            raise _unavailable()
        return tuple(selected)

    @staticmethod
    def _workflow_id(template_id: str) -> str:
        identifier = f"workflow.webhook.{template_id.removeprefix('tpl.')}.v1"
        require_id(identifier, "webhook workflow ID")
        return identifier


__all__ = ["CompiledCatalogWebhookAdmissionResolver"]
