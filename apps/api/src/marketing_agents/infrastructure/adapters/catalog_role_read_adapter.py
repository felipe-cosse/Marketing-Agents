"""OBJ-03: per-template controlled model adapters with no real-provider seam."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from marketing_agents.application.orchestration.executable_workflows import (
    ExecutableWorkflowRegistry,
)
from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
    RuntimePolicyViolation,
)
from marketing_agents.application.ports.read_adapter import (
    ReadAdapter,
    ReadAdapterContract,
    ReadAdapterPermanentError,
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.runtime_inputs import RuntimeInputContract
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.infrastructure.adapters.catalog_roles import CatalogRoleRenderer
from marketing_agents.infrastructure.adapters.llm.deterministic import (
    DeterministicLLMProvider,
    DeterministicRendererRegistry,
)
from marketing_agents.infrastructure.adapters.llm.read_adapter import StructuredLLMReadAdapter
from marketing_agents.infrastructure.catalog.models import AgentTemplateRecord, CompiledCatalog


@dataclass(frozen=True, slots=True)
class _TemplateAdapter:
    template: AgentTemplateRecord
    provider: DeterministicLLMProvider
    guard: RuntimePolicyGuard
    adapter: StructuredLLMReadAdapter

    def require_mock(self) -> None:
        if (
            type(self.provider) is not DeterministicLLMProvider
            or self.provider.provider_id != "mock"
            or self.provider.model_id != "deterministic"
            or self.provider.version != "v1"
            or type(self.adapter) is not StructuredLLMReadAdapter
            or self.adapter._provider is not self.provider
        ):
            raise ReadAdapterPermanentError(
                "adapter_provider_invalid", "catalog roles require the exact offline provider"
            )

    def require_contract(self, contract: ReadAdapterContract) -> None:
        self.require_mock()
        budget = self.template.budget_policy
        if (
            contract.max_input_bytes > budget.max_input_bytes
            or contract.max_input_field_bytes > budget.max_input_field_bytes
            or contract.max_output_bytes > budget.max_output_bytes
            or contract.max_model_output_tokens > budget.max_model_output_tokens
            or contract.effective_timeout_seconds > self.template.timeout_policy.step_seconds
        ):
            raise ReadAdapterPermanentError(
                "adapter_policy_drift", "catalog role contract exceeds its template policy"
            )


@dataclass(frozen=True, slots=True)
class CatalogRoleReadAdapter:
    """Route only exact model instances; all trigger variants share their binding.

    The controlled executor remains responsible for durable budgets, rate-window
    reservations, deadlines and policy seal validation. This wrapper never widens
    those limits and independently enforces each template's input/output bounds.
    """

    _by_instance: Mapping[str, _TemplateAdapter]

    def _for(self, instance_id: str) -> _TemplateAdapter:
        try:
            selected = self._by_instance[instance_id]
        except KeyError:
            raise ReadAdapterPermanentError(
                "adapter_contract_unavailable", "catalog model instance is unavailable"
            ) from None
        selected.require_mock()
        return selected

    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        if type(operation) is not OperationExecutionPolicy:
            raise ReadAdapterPermanentError("invalid_request", "exact model operation required")
        selected = self._for(operation.selected_instance_id)
        if (
            operation.max_attempts > selected.template.retry_policy.max_attempts
            or operation.rate_window_max_calls > selected.template.rate_limit_policy.max_calls
            or operation.rate_window_seconds < selected.template.rate_limit_policy.window_seconds
        ):
            raise ReadAdapterPermanentError(
                "adapter_policy_drift", "catalog role operation exceeds its template policy"
            )
        contract = selected.adapter.contract_for(operation)
        selected.require_contract(contract)
        return contract

    def input_contract_for(self, operation: OperationExecutionPolicy) -> RuntimeInputContract:
        self.contract_for(operation)
        return self._for(operation.selected_instance_id).adapter.input_contract_for(operation)

    def output_contract_for(self, operation: OperationExecutionPolicy) -> RuntimeOutputContract:
        self.contract_for(operation)
        return self._for(operation.selected_instance_id).adapter.output_contract_for(operation)

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        if type(request) is not ReadAdapterRequest:
            raise ReadAdapterPermanentError("invalid_request", "exact model request required")
        selected = self._for(request.selected_instance_id)
        selected.require_contract(request.contract)
        binding = selected.adapter._binding_for_request(request)
        try:
            selected.guard.validate_input(request.input_payload, binding.input_schema)
            result = await selected.adapter.execute(request)
            selected.require_mock()
            selected.guard.validate_output(result.output_payload, binding.output_schema)
        except RuntimePolicyViolation:
            raise ReadAdapterPermanentError(
                "adapter_policy_violation", "catalog role payload exceeds its template policy"
            ) from None
        return result


def build_catalog_role_read_adapter(
    catalog: CompiledCatalog,
    workflows: ExecutableWorkflowRegistry,
    renderer: CatalogRoleRenderer,
) -> ReadAdapter:
    """Compose only credential-free providers, one policy snapshot per model role."""
    if type(renderer) is not CatalogRoleRenderer or renderer.catalog is not catalog:
        raise ValueError("catalog role adapter requires its exact catalog-bound renderer")
    registrations = {item.key.template_id: item for item in renderer.registrations()}
    by_instance: dict[str, _TemplateAdapter] = {}
    for template in catalog.templates:
        if template.id not in renderer.model_template_ids:
            continue
        workflow = workflows.for_catalog_role(template.id, TriggerKind.MANUAL)
        bindings = tuple(
            renderer.model_binding(instance.id, workflow.id)
            for instance in catalog.instances
            if instance.template_id == template.id
        )
        budget = template.budget_policy
        guard = RuntimePolicyGuard(
            RuntimePolicySnapshot(
                allowed_capabilities=(
                    CapabilityPolicy(
                        capability_id="cap.model.generate-structured",
                        effect="read",
                        connector_family="model",
                    ),
                ),
                input_max_bytes=budget.max_input_bytes,
                max_input_field_bytes=budget.max_input_field_bytes,
                output_max_bytes=budget.max_output_bytes,
                max_json_depth=16,
                max_content_parts=1,
                max_content_characters=min(budget.max_input_bytes, 1_000_000),
                max_model_calls=budget.max_model_calls,
                max_tool_calls=budget.max_tool_calls,
                rate_window_max_calls=template.rate_limit_policy.max_calls,
                rate_window_seconds=template.rate_limit_policy.window_seconds,
                step_timeout_seconds=template.timeout_policy.step_seconds,
                run_timeout_seconds=template.timeout_policy.run_seconds,
            )
        )
        provider = DeterministicLLMProvider(
            DeterministicRendererRegistry((registrations[template.id],)), guard
        )
        selected = _TemplateAdapter(
            template, provider, guard, StructuredLLMReadAdapter(provider, bindings)
        )
        for binding in bindings:
            by_instance[binding.instance_id] = selected
    return CatalogRoleReadAdapter(MappingProxyType(by_instance))


__all__ = ["CatalogRoleReadAdapter", "build_catalog_role_read_adapter"]
