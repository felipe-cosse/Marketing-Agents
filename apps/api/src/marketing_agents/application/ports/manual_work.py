"""Inward-facing resolution contract for one manual-work admission binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.validation import require_id

if TYPE_CHECKING:
    from marketing_agents.application.services.incoming_work_validation import (
        ValidatedIncomingWork,
    )
    from marketing_agents.application.services.manual_work_intake import (
        ManualDryRunCommand,
    )


class ManualAdmissionResolutionError(RuntimeError):
    """Stable safe failure while resolving server-owned manual routing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ManualIncomingWorkValidator(Protocol):
    """Issue only a sealed incoming-work marker for one resolved binding."""

    def validate(self, envelope: AdmissionEnvelope) -> ValidatedIncomingWork: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualAdmissionBinding:
    """Complete server-owned routing snapshot used for one manual admission."""

    instance_id: str
    source: str
    trigger_id: str
    workflow_id: str
    configuration_revision: int
    brief_id: str | None
    brief_revision: int | None
    demo_scenario_id: str | None
    validator: ManualIncomingWorkValidator

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.instance_id, "manual binding instance ID"),
            (self.source, "manual binding source"),
            (self.trigger_id, "manual binding trigger ID"),
            (self.workflow_id, "manual binding workflow ID"),
        ):
            require_id(value, field_name)
        if self.source != "manual":
            raise ValueError("manual admission binding source must be manual")
        if type(self.configuration_revision) is not int or self.configuration_revision < 1:
            raise ValueError("manual binding configuration revision must be positive")
        if (self.brief_id is None) != (self.brief_revision is None):
            raise ValueError("manual binding brief ID and revision must be supplied together")
        if self.brief_id is not None:
            require_id(self.brief_id, "manual binding campaign brief ID")
        if self.brief_revision is not None and (
            type(self.brief_revision) is not int or self.brief_revision < 1
        ):
            raise ValueError("manual binding campaign brief revision must be positive")
        if self.demo_scenario_id is not None:
            require_id(self.demo_scenario_id, "manual binding demo scenario ID")
        if not callable(getattr(self.validator, "validate", None)):
            raise ValueError("manual admission binding requires an incoming-work validator")


class ManualAdmissionResolver(Protocol):
    """Resolve server-owned manual routing inside the caller's transaction."""

    async def resolve_in_uow(
        self,
        unit_of_work: UnitOfWork,
        command: ManualDryRunCommand,
    ) -> ManualAdmissionBinding: ...
