"""Catalog-derived constraints for local instance connector configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from marketing_agents.application.ports.instance_configuration import (
    InstanceConfigurationConstraints,
)
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.infrastructure.adapters.connectors.registry import (
    EXTERNAL_CONNECTOR_FAMILIES,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog


class ConnectorBindingLike(Protocol):
    @property
    def connector_family(self) -> str: ...

    @property
    def binding_id(self) -> str: ...


class InstanceConfigurationConstraintError(ValueError):
    """A mutable binding is not an exact registered catalog-backed mock binding."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _constraints_for(
    catalog: CompiledCatalog,
    instance_id: str,
) -> InstanceConfigurationConstraints | None:
    if type(catalog) is not CompiledCatalog:
        raise InstanceConfigurationConstraintError(
            "catalog_invalid",
            "instance configuration constraints require one compiled catalog",
        )
    instance = next((item for item in catalog.instances if item.id == instance_id), None)
    if instance is None:
        return None
    template = next((item for item in catalog.templates if item.id == instance.template_id), None)
    if template is None:
        raise InstanceConfigurationConstraintError(
            "template_unknown",
            "instance configuration template is absent from the compiled catalog",
        )
    capabilities = {item.id: item for item in catalog.tool_capabilities}
    if any(identifier not in capabilities for identifier in template.allowed_tool_capability_ids):
        raise InstanceConfigurationConstraintError(
            "capability_unknown",
            "instance configuration template references an unknown capability",
        )
    families = frozenset(
        capabilities[capability_id].connector_family
        for capability_id in template.allowed_tool_capability_ids
        if capabilities[capability_id].connector_family in EXTERNAL_CONNECTOR_FAMILIES
    )
    return InstanceConfigurationConstraints(
        instance_id=instance.id,
        template_id=template.id,
        supported_trigger_kinds=frozenset(
            TriggerKind(trigger_type) for trigger_type in template.supported_trigger_types
        ),
        allowed_connector_families=families,
    )


@dataclass(frozen=True, slots=True)
class CompiledCatalogInstanceConfigurationConstraintProvider:
    """Serve immutable deployment limits derived from one compiled catalog."""

    catalog: CompiledCatalog

    def __post_init__(self) -> None:
        if type(self.catalog) is not CompiledCatalog:
            raise ValueError("configuration constraint provider requires one compiled catalog")

    async def get(self, instance_id: str) -> InstanceConfigurationConstraints | None:
        return _constraints_for(self.catalog, instance_id)


class LocalMockRegisteredBindingProvider:
    """Expose the sole deterministic local binding ID registered for each connector family."""

    def registered_binding_ids(self, connector_family: str) -> frozenset[str]:
        if connector_family not in EXTERNAL_CONNECTOR_FAMILIES:
            return frozenset()
        return frozenset({f"mock.{connector_family}.default"})


def registered_mock_bindings(
    catalog: CompiledCatalog,
    instance_id: str,
) -> Mapping[str, str]:
    """Return exact mock binding IDs authorized by an instance's template capabilities."""

    constraints = _constraints_for(catalog, instance_id)
    if constraints is None:
        raise InstanceConfigurationConstraintError(
            "instance_unknown",
            "instance configuration must target one compiled catalog instance",
        )
    bindings = {
        family: f"mock.{family}.default"
        for family in sorted(constraints.allowed_connector_families)
    }
    return MappingProxyType(bindings)


def validate_mock_connector_bindings(
    catalog: CompiledCatalog,
    instance_id: str,
    connector_bindings: Mapping[str, ConnectorBindingLike],
) -> None:
    """Reject aliases, unsupported families, and non-registered local binding IDs."""

    if not isinstance(connector_bindings, Mapping):
        raise InstanceConfigurationConstraintError(
            "connector_bindings_invalid",
            "connector bindings must be one bounded mapping",
        )
    if len(connector_bindings) > 16:
        raise InstanceConfigurationConstraintError(
            "connector_bindings_too_many",
            "connector bindings cannot contain more than 16 families",
        )
    registered = registered_mock_bindings(catalog, instance_id)
    for family, binding in connector_bindings.items():
        if (
            type(family) is not str
            or family != family.strip()
            or family != binding.connector_family
        ):
            raise InstanceConfigurationConstraintError(
                "connector_family_mismatch",
                "connector binding keys must exactly match their connector family",
            )
        expected = registered.get(family)
        if expected is None:
            raise InstanceConfigurationConstraintError(
                "connector_family_unsupported",
                "connector family is not authorized by the instance template",
            )
        if binding.binding_id != expected:
            raise InstanceConfigurationConstraintError(
                "connector_binding_unregistered",
                "connector binding must select the registered local mock binding",
            )
