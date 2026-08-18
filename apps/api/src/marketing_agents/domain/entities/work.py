"""Admitted campaign and idempotent work entities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from marketing_agents.domain.enums import WorkMode

from ._validation import (
    frozen_json_mapping,
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
    brief_id: str | None
    configuration_revision: int
    input_digest: str = field(repr=False)
    admission_digest: str = field(repr=False)
    created_at: datetime
    brief_revision: int | None = field(kw_only=True)
    digest_key_version: str = field(kw_only=True)
    admitted_payload: Mapping[str, Any] = field(kw_only=True, repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "source",
            "event_id",
            "instance_id",
            "trigger_id",
            "workflow_id",
        ):
            require_id(getattr(self, field_name), field_name)
        if not isinstance(self.mode, WorkMode):
            raise ValueError("work mode must be a supported execution mode")
        if (self.brief_id is None) != (self.brief_revision is None):
            raise ValueError("campaign brief ID and revision must be supplied together")
        if self.brief_id is not None:
            require_id(self.brief_id, "campaign brief ID")
        if self.brief_revision is not None and self.brief_revision < 1:
            raise ValueError("campaign brief revision must be positive")
        require_digest(self.input_digest, "input digest")
        require_digest(self.admission_digest, "admission digest")
        if re.fullmatch(r"admission-hmac-sha256-v1:[0-9a-f]{64}", self.digest_key_version) is None:
            raise ValueError("digest key version is invalid")
        require_utc(self.created_at, "work creation time")
        if self.configuration_revision < 1:
            raise ValueError("configuration revision must be positive")
        object.__setattr__(
            self,
            "admitted_payload",
            frozen_json_mapping(self.admitted_payload, "admitted payload"),
        )

    @property
    def source_idempotency_key(self) -> tuple[str, str, str]:
        return (self.source, self.event_id, self.instance_id)
