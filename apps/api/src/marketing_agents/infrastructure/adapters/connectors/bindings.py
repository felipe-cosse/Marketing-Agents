"""OBJ-05: explicit infrastructure bindings independent of provider implementations.

Bindings select implementations, not authority. A durable-receipts declaration
does not replace the dispatcher's exact authorization and persisted receipt checks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from inspect import iscoroutinefunction
from types import MappingProxyType
from typing import Literal, Protocol

from marketing_agents.domain.connector_families import EXTERNAL_CONNECTOR_FAMILIES

from .registry import ConnectorBundleConfigurationError, ConnectorOperationRegistry

ConnectorHandler = Callable[..., Awaitable[object]]


def _normalized(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


@dataclass(frozen=True, slots=True)
class ConnectorBindingRegistration:
    """One exact binding and its explicitly supported typed operations."""

    binding_id: str
    connector_family: str
    handlers: Mapping[str, ConnectorHandler] = field(repr=False)
    provider_mode: Literal["mock", "real", "local"]
    provider_name: str
    provider_version: str
    durable_receipts: bool = False
    operation_provider_versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            _normalized(value)
            for value in (self.binding_id, self.provider_name, self.provider_version)
        ):
            raise ConnectorBundleConfigurationError(
                "connector binding and provider identities must be normalized"
            )
        if self.connector_family not in EXTERNAL_CONNECTOR_FAMILIES:
            raise ConnectorBundleConfigurationError("unsupported connector binding family")
        if self.provider_mode not in {"mock", "real", "local"}:
            raise ConnectorBundleConfigurationError("unsupported connector provider mode")
        if type(self.durable_receipts) is not bool:
            raise ConnectorBundleConfigurationError("durable receipts must be an exact boolean")
        handlers = dict(self.handlers)
        if not handlers:
            raise ConnectorBundleConfigurationError("connector binding requires operation handlers")
        for capability_id, handler in handlers.items():
            if not _normalized(capability_id) or not iscoroutinefunction(handler):
                raise ConnectorBundleConfigurationError(
                    "connector binding requires normalized capabilities and async handlers"
                )
        versions = dict(self.operation_provider_versions)
        if not set(versions).issubset(handlers) or not all(
            _normalized(version) for version in versions.values()
        ):
            raise ConnectorBundleConfigurationError(
                "connector operation provider versions must match registered handlers"
            )
        object.__setattr__(self, "handlers", MappingProxyType(handlers))
        object.__setattr__(self, "operation_provider_versions", MappingProxyType(versions))


@dataclass(frozen=True, slots=True, init=False)
class ConnectorBindingRegistry:
    """Immutable exact binding-ID selection; never fallback by family or mode."""

    registry: ConnectorOperationRegistry
    _bindings: Mapping[str, ConnectorBindingRegistration]

    def __init__(
        self,
        operation_registry: ConnectorOperationRegistry,
        registrations: Iterable[ConnectorBindingRegistration],
    ) -> None:
        bindings: dict[str, ConnectorBindingRegistration] = {}
        for registration in registrations:
            if type(registration) is not ConnectorBindingRegistration:
                raise ConnectorBundleConfigurationError(
                    "connector bindings require exact registrations"
                )
            if registration.binding_id in bindings:
                raise ConnectorBundleConfigurationError(
                    f"duplicate connector binding {registration.binding_id!r}"
                )
            for capability_id in registration.handlers:
                operation = operation_registry.resolve(capability_id)
                if operation.metadata.connector_family != registration.connector_family:
                    raise ConnectorBundleConfigurationError(
                        f"connector binding family mismatch for {capability_id!r}"
                    )
            bindings[registration.binding_id] = registration
        object.__setattr__(self, "registry", operation_registry)
        object.__setattr__(self, "_bindings", MappingProxyType(bindings))

    @property
    def bindings(self) -> tuple[ConnectorBindingRegistration, ...]:
        return tuple(self._bindings[key] for key in self.binding_ids)

    @property
    def binding_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))

    @property
    def binding_registry(self) -> ConnectorBindingRegistry:
        return self

    def resolve(self, binding_id: str) -> ConnectorBindingRegistration:
        try:
            return self._bindings[binding_id]
        except KeyError as exc:
            raise ConnectorBundleConfigurationError(
                f"connector binding {binding_id!r} is not registered"
            ) from exc


class ConnectorBindingSource(Protocol):
    """Composition-only projection implemented by a binding registry or bundle."""

    @property
    def binding_registry(self) -> ConnectorBindingRegistry: ...
