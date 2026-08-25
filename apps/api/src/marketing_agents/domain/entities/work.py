"""Admitted campaign and idempotent work entities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from marketing_agents.domain.data_classification import DataClassification
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

_UNSET_PROJECTION_TIME = datetime.min.replace(tzinfo=UTC)
_LEGACY_SCHEMA_HASH = "schema-sha256-v1:" + ("0" * 64)


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
    redacted_input_projection: Mapping[str, Any] = field(
        kw_only=True,
        default_factory=dict,
        repr=False,
    )
    input_schema_id: str = field(kw_only=True, default="schema.legacy.unknown")
    input_schema_hash: str = field(kw_only=True, default=_LEGACY_SCHEMA_HASH)
    input_classification: DataClassification = field(
        kw_only=True,
        default=DataClassification.INTERNAL,
    )
    input_projection_created_at: datetime = field(
        kw_only=True,
        default=_UNSET_PROJECTION_TIME,
    )
    input_projection_expires_at: datetime = field(
        kw_only=True,
        default=_UNSET_PROJECTION_TIME,
    )
    input_projection_integrity_digest: str = field(
        kw_only=True,
        default="0" * 64,
        repr=False,
    )

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
        object.__setattr__(
            self,
            "redacted_input_projection",
            frozen_json_mapping(
                self.redacted_input_projection,
                "redacted input projection",
            ),
        )
        require_id(self.input_schema_id, "input schema ID")
        if not self.input_schema_hash.startswith("schema-sha256-v1:"):
            raise ValueError("input schema hash version is invalid")
        require_digest(
            self.input_schema_hash.removeprefix("schema-sha256-v1:"),
            "input schema hash",
        )
        if not isinstance(self.input_classification, DataClassification):
            raise ValueError("input classification is invalid")
        if self.input_projection_created_at == _UNSET_PROJECTION_TIME:
            object.__setattr__(self, "input_projection_created_at", self.created_at)
        if self.input_projection_expires_at == _UNSET_PROJECTION_TIME:
            object.__setattr__(
                self,
                "input_projection_expires_at",
                self.created_at + timedelta(days=7),
            )
        require_utc(self.input_projection_created_at, "input projection creation time")
        require_utc(self.input_projection_expires_at, "input projection expiry time")
        if self.input_projection_created_at != self.created_at:
            raise ValueError("input projection creation time must match work creation time")
        if self.input_projection_expires_at <= self.input_projection_created_at:
            raise ValueError("input projection expiry must follow its creation time")
        require_digest(
            self.input_projection_integrity_digest,
            "input projection integrity digest",
        )

    @property
    def source_idempotency_key(self) -> tuple[str, str, str]:
        return (self.source, self.event_id, self.instance_id)
