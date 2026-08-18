"""Admitted campaign and idempotent work entities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from marketing_agents.domain.enums import WorkMode

from ._validation import (
    frozen_mapping,
    require_digest,
    require_id,
    require_text,
    require_unique,
    require_utc,
)


@dataclass(frozen=True, slots=True)
class CampaignBrief:
    id: str
    title: str
    objective: str
    constraints: Mapping[str, Any]
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_id(self.id, "campaign brief ID")
        require_text(self.title, "campaign title", maximum=200)
        require_text(self.objective, "campaign objective", maximum=2_000)
        require_unique(self.source_refs, "campaign source references")
        object.__setattr__(self, "constraints", frozen_mapping(self.constraints))


@dataclass(frozen=True, slots=True)
class WorkItem:
    id: str
    source: str
    event_id: str
    instance_id: str
    trigger_id: str
    workflow_id: str
    mode: WorkMode
    brief_id: str
    configuration_revision: int
    input_digest: str
    admission_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "source",
            "event_id",
            "instance_id",
            "trigger_id",
            "workflow_id",
            "brief_id",
        ):
            require_id(getattr(self, field_name), field_name)
        require_digest(self.input_digest, "input digest")
        require_digest(self.admission_digest, "admission digest")
        require_utc(self.created_at, "work creation time")
        if self.configuration_revision < 1:
            raise ValueError("configuration revision must be positive")

    @property
    def source_idempotency_key(self) -> tuple[str, str, str]:
        return (self.source, self.event_id, self.instance_id)
