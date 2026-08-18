"""Pure identity envelope for one admitted work item."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from marketing_agents.domain.entities._validation import (
    frozen_json_mapping,
    require_id,
)
from marketing_agents.domain.enums import WorkMode


@dataclass(frozen=True, slots=True)
class AdmissionEnvelope:
    """Every semantic field whose change must collide under one source key."""

    source: str
    event_id: str
    instance_id: str
    trigger_id: str
    workflow_id: str
    mode: WorkMode
    brief_id: str | None
    brief_revision: int | None
    configuration_revision: int
    admitted_payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.source, "work source"),
            (self.event_id, "source event ID"),
            (self.instance_id, "agent instance ID"),
            (self.trigger_id, "trigger ID"),
            (self.workflow_id, "workflow ID"),
        ):
            require_id(value, field_name)
        if not isinstance(self.mode, WorkMode):
            raise ValueError("work mode must be a supported execution mode")
        if (self.brief_id is None) != (self.brief_revision is None):
            raise ValueError("campaign brief ID and revision must be supplied together")
        if self.brief_id is not None:
            require_id(self.brief_id, "campaign brief ID")
        if self.brief_revision is not None and self.brief_revision < 1:
            raise ValueError("campaign brief revision must be positive")
        if self.configuration_revision < 1:
            raise ValueError("configuration revision must be positive")
        object.__setattr__(
            self,
            "admitted_payload",
            frozen_json_mapping(self.admitted_payload, "admitted payload"),
        )

    @property
    def source_key(self) -> tuple[str, str, str]:
        return (self.source, self.event_id, self.instance_id)
