"""Explicit non-executable, content-free retained detail contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.retention import RetentionCategory
from marketing_agents.domain.validation import frozen_json_mapping, require_id, require_utc


class DetailExpired(RuntimeError):
    """A historical identity exists, but its execution/detail content was erased."""

    code = "detail_expired"

    def __init__(self, record_kind: str, record_id: str) -> None:
        super().__init__("retained detail has expired")
        self.record_kind = record_kind
        self.record_id = record_id


@dataclass(frozen=True, slots=True)
class RetainedDetail:
    category: RetentionCategory
    record_kind: str
    record_id: str
    created_at: datetime
    expires_at: datetime
    purged_at: datetime
    run_id: str | None
    metadata: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        require_id(self.record_id, "retained record ID")
        for value in (self.created_at, self.expires_at, self.purged_at):
            require_utc(value, "retained detail time")
        if not self.created_at < self.expires_at <= self.purged_at:
            raise ValueError("retained detail timestamps are inconsistent")
        object.__setattr__(
            self, "metadata", frozen_json_mapping(self.metadata, "retained metadata")
        )


@dataclass(frozen=True, slots=True)
class RetainedWorkItem:
    """Identity/dedupe witness only; deliberately has no executable payload field."""

    id: str
    source: str
    event_id: str
    instance_id: str
    trigger_id: str
    workflow_id: str
    mode: WorkMode
    brief_id: str | None
    brief_revision: int | None
    configuration_revision: int
    created_at: datetime
    input_schema_id: str
    input_schema_hash: str
    input_classification: DataClassification
    input_projection_created_at: datetime
    input_projection_expires_at: datetime
    input_digest: str = field(repr=False)
    admission_digest: str = field(repr=False)
    digest_key_version: str = field(repr=False)
    input_projection_integrity_digest: str = field(repr=False)
    projection_digest: str = field(repr=False)
    retention: RetainedDetail = field(repr=False)

    @property
    def source_idempotency_key(self) -> tuple[str, str, str]:
        return (self.source, self.event_id, self.instance_id)


@dataclass(frozen=True, slots=True)
class RetentionClaim:
    job_id: str
    occurrence_key: str
    owner: str
    version: int
    claimed_at: datetime
    expires_at: datetime
    cutoff_at: datetime
    batch_size: int

    def __post_init__(self) -> None:
        for value in (self.job_id, self.occurrence_key, self.owner):
            require_id(value, "retention claim identity")
        for timestamp in (self.claimed_at, self.expires_at, self.cutoff_at):
            require_utc(timestamp, "retention claim time")
        if not self.cutoff_at <= self.claimed_at < self.expires_at:
            raise ValueError("retention claim times are invalid")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("retention claim version is invalid")
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 100:
            raise ValueError("retention claim batch is invalid")


@dataclass(frozen=True, slots=True)
class RetentionResult:
    occurrence_key: str
    counts: Mapping[str, int]
    completed_at: datetime
    replayed: bool = False

    def __post_init__(self) -> None:
        require_id(self.occurrence_key, "retention result occurrence")
        require_utc(self.completed_at, "retention result time")
        if (
            set(self.counts) != {category.value for category in RetentionCategory}
            or any(
                type(count) is not int or not 0 <= count <= 100 for count in self.counts.values()
            )
            or sum(self.counts.values()) > 100
            or type(self.replayed) is not bool
        ):
            raise ValueError("retention result counts are invalid")
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))
