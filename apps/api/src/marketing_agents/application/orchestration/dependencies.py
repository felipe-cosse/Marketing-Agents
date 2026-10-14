"""Pure dependency bundle used by orchestration application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from marketing_agents.application.ports.clock import Clock
from marketing_agents.application.ports.id_generator import IdGenerator
from marketing_agents.application.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.retention import RetentionPolicy
from marketing_agents.security.digest_key import DigestKey

if TYPE_CHECKING:
    from marketing_agents.application.services.audit_events import AuditEventFactory


class OrchestrationDependencyError(ValueError):
    """Raised when an injected primitive violates an inward-facing contract."""


@dataclass(frozen=True, slots=True)
class OrchestrationDependencies:
    clock: Clock
    ids: IdGenerator
    unit_of_work_factory: UnitOfWorkFactory
    retention_policy: RetentionPolicy = field(default_factory=RetentionPolicy)

    def audit_factory(
        self,
        context: AuditContext,
        *,
        configuration_pseudonym_key: DigestKey | None = None,
    ) -> AuditEventFactory:
        """Carry this runtime's explicit policy into every new audit fact."""
        from marketing_agents.application.services.audit_events import AuditEventFactory

        return AuditEventFactory(
            context,
            retention_policy=self.retention_policy,
            configuration_pseudonym_key=configuration_pseudonym_key,
        )

    def utc_now(self) -> datetime:
        value = self.clock.now()
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise OrchestrationDependencyError("clock must return timezone-aware UTC")
        return value

    def new_id(self, namespace: str) -> str:
        if not namespace or not namespace.replace("-", "").replace("_", "").isalnum():
            raise OrchestrationDependencyError("ID namespace must be a stable slug")
        value = self.ids.new(namespace)
        if not value.startswith(f"{namespace}."):
            raise OrchestrationDependencyError("generated ID must retain its namespace")
        return value

    def unit_of_work(self) -> UnitOfWork:
        return self.unit_of_work_factory()
