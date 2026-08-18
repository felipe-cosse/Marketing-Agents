"""Stable domain state and policy enumerations."""

from enum import StrEnum


class Effect(StrEnum):
    READ = "read"
    WRITE = "write"


class TriggerKind(StrEnum):
    MANUAL = "manual"
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"


class WorkMode(StrEnum):
    DRY_RUN = "dry_run"
    MOCK_EXECUTION = "mock_execution"


class RunState(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class StepState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ExternalActionState(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DISPATCH_RESERVED = "dispatch_reserved"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    SUPERSEDED = "superseded"


class ApprovalDecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class MisfirePolicy(StrEnum):
    SKIP = "skip"
    RUN_ONCE = "run_once"


class OccurrenceState(StrEnum):
    DUE = "due"
    CLAIMED = "claimed"
    ENQUEUED = "enqueued"
    SKIPPED = "skipped"
    COMPLETED = "completed"
