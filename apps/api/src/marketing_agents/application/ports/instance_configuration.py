"""Application-facing ports for mutable agent-instance deployment configuration."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

from marketing_agents.domain.audit import AuditEvent, AuditEventDraft
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.domain.instance_configuration import InstanceConfiguration
from marketing_agents.domain.validation import require_id


class InstanceConfigurationRepositoryError(RuntimeError):
    """Stable fail-closed persistence or hydration conflict."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InstanceConfigurationConstraints:
    """Static template-owned limits used to validate a mutable deployment projection."""

    instance_id: str
    template_id: str
    supported_trigger_kinds: frozenset[TriggerKind]
    allowed_connector_families: frozenset[str]

    def __post_init__(self) -> None:
        require_id(self.instance_id, "constraint instance ID")
        require_id(self.template_id, "constraint template ID")
        if (
            type(self.supported_trigger_kinds) is not frozenset
            or not self.supported_trigger_kinds
            or any(type(item) is not TriggerKind for item in self.supported_trigger_kinds)
        ):
            raise ValueError("supported trigger kinds must be one nonempty exact immutable set")
        if type(self.allowed_connector_families) is not frozenset or any(
            type(item) is not str for item in self.allowed_connector_families
        ):
            raise ValueError("allowed connector families must be one exact immutable string set")
        for family in self.allowed_connector_families:
            require_id(family, "allowed connector family")


class InstanceConfigurationConstraintProvider(Protocol):
    async def get(self, instance_id: str) -> InstanceConfigurationConstraints | None:
        """Return catalog-derived limits, or ``None`` for an unknown instance."""
        ...


class RegisteredBindingProvider(Protocol):
    def registered_binding_ids(self, connector_family: str) -> frozenset[str]:
        """Return the exact installed binding identifiers for one connector family."""
        ...


class InstanceConfigurationRepository(Protocol):
    async def get(self, instance_id: str) -> InstanceConfiguration | None: ...

    async def list_all(self) -> tuple[InstanceConfiguration, ...]: ...

    async def insert_missing(self, configuration: InstanceConfiguration) -> bool:
        """Insert one seed only when no row exists; never overwrite operator state."""
        ...

    async def compare_and_swap(
        self,
        previous: InstanceConfiguration,
        replacement: InstanceConfiguration,
    ) -> bool:
        """Replace exactly ``previous`` with one exact +1 revision, or return false."""
        ...


class InstanceConfigurationAuditRepository(Protocol):
    async def append_global(self, event: AuditEventDraft) -> AuditEvent: ...


class InstanceConfigurationUnitOfWork(Protocol):
    @property
    def configurations(self) -> InstanceConfigurationRepository: ...

    @property
    def audits(self) -> InstanceConfigurationAuditRepository: ...

    async def __aenter__(self) -> InstanceConfigurationUnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class InstanceConfigurationUnitOfWorkFactory(Protocol):
    def __call__(self) -> InstanceConfigurationUnitOfWork: ...
