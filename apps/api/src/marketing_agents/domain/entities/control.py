"""Approval, schedule occurrence, and audit entities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    MisfirePolicy,
    OccurrenceState,
)

from ._validation import (
    frozen_mapping,
    require_digest,
    require_id,
    require_text,
    require_utc,
)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    action_id: str
    action_hash: str
    redacted_payload: Mapping[str, Any]
    policy_id: str
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    status: ApprovalStatus

    def __post_init__(self) -> None:
        for field_name in ("id", "action_id", "policy_id", "requested_by"):
            require_id(getattr(self, field_name), field_name)
        require_digest(self.action_hash, "approval action hash")
        require_utc(self.requested_at, "approval request time")
        require_utc(self.expires_at, "approval expiry time")
        if self.expires_at <= self.requested_at:
            raise ValueError("approval must expire after it is requested")
        object.__setattr__(self, "redacted_payload", frozen_mapping(self.redacted_payload))


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    id: str
    request_id: str
    actor_id: str
    decision: ApprovalDecisionKind
    expected_action_hash: str
    reason: str
    decided_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "request_id", "actor_id"):
            require_id(getattr(self, field_name), field_name)
        require_digest(self.expected_action_hash, "decision action hash")
        require_text(self.reason, "decision reason", maximum=1_000)
        require_utc(self.decided_at, "decision time")


@dataclass(frozen=True, slots=True)
class Schedule:
    id: str
    trigger_id: str
    instance_id: str
    cron: str
    timezone: str
    next_run_at_utc: datetime
    misfire_policy: MisfirePolicy
    enabled: bool
    version: int = 1

    def __post_init__(self) -> None:
        for field_name in ("id", "trigger_id", "instance_id"):
            require_id(getattr(self, field_name), field_name)
        require_text(self.cron, "schedule cron", maximum=100)
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("schedule timezone must be a valid IANA zone") from exc
        require_utc(self.next_run_at_utc, "next scheduled UTC time")
        if self.version < 1:
            raise ValueError("schedule version must be positive")


@dataclass(frozen=True, slots=True)
class ScheduleOccurrence:
    id: str
    schedule_id: str
    scheduled_at_utc: datetime
    state: OccurrenceState
    work_item_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        require_id(self.id, "occurrence ID")
        require_id(self.schedule_id, "schedule ID")
        require_utc(self.scheduled_at_utc, "scheduled occurrence time")
        if self.work_item_id is not None:
            require_id(self.work_item_id, "occurrence work item ID")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease owner and expiry must be set together")
        if self.lease_owner is not None:
            require_id(self.lease_owner, "lease owner")
            require_utc(self.lease_expires_at, "lease expiry")  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    sequence: int
    event_type: str
    aggregate_type: str
    aggregate_id: str
    actor_id: str
    correlation_id: str
    safe_metadata: Mapping[str, Any]
    occurred_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "aggregate_id", "actor_id", "correlation_id"):
            require_id(getattr(self, field_name), field_name)
        require_text(self.event_type, "audit event type", maximum=120)
        require_text(self.aggregate_type, "audit aggregate type", maximum=120)
        if self.sequence < 1:
            raise ValueError("audit sequence must be positive")
        require_utc(self.occurred_at, "audit event time")
        object.__setattr__(self, "safe_metadata", frozen_mapping(self.safe_metadata))
