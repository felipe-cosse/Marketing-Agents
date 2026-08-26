"""Trusted deployment resolution contract for authenticated webhook work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.validation import require_id

if TYPE_CHECKING:
    from marketing_agents.application.services.incoming_work_validation import (
        ValidatedIncomingWork,
    )


class WebhookAdmissionResolutionError(RuntimeError):
    """Stable safe failure while resolving server-owned webhook routing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WebhookIncomingWorkValidator(Protocol):
    def validate(self, envelope: AdmissionEnvelope) -> ValidatedIncomingWork: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookAdmissionBinding:
    """One exact enabled target derived only from catalog and locked configuration."""

    source: str
    trigger_id: str
    instance_id: str
    workflow_id: str
    configuration_revision: int
    mode: WorkMode
    validator: WebhookIncomingWorkValidator

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.source, "webhook binding source"),
            (self.trigger_id, "webhook binding trigger ID"),
            (self.instance_id, "webhook binding instance ID"),
            (self.workflow_id, "webhook binding workflow ID"),
        ):
            require_id(value, field_name)
        if type(self.configuration_revision) is not int or self.configuration_revision < 1:
            raise ValueError("webhook binding configuration revision must be positive")
        if type(self.mode) is not WorkMode:
            raise ValueError("webhook binding mode must use the exact WorkMode enum")
        if not callable(getattr(self.validator, "validate", None)):
            raise ValueError("webhook binding requires an incoming-work validator")


class WebhookAdmissionResolver(Protocol):
    async def resolve_all_in_uow(
        self,
        unit_of_work: UnitOfWork,
        *,
        source: str,
        trigger_id: str,
    ) -> tuple[WebhookAdmissionBinding, ...]: ...


__all__ = [
    "WebhookAdmissionBinding",
    "WebhookAdmissionResolutionError",
    "WebhookAdmissionResolver",
]
