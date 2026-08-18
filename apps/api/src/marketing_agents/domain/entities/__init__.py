"""Framework-independent immutable domain entities."""

from .catalog import (
    AgentInstance,
    AgentTemplate,
    ApprovalPolicy,
    Department,
    FunctionTeam,
    ToolCapability,
    TriggerDefinition,
)
from .control import ApprovalDecision, ApprovalRequest, AuditEvent, Schedule, ScheduleOccurrence
from .external_action import (
    MAX_DELIVERY_ATTEMPTS,
    ActionReservationSnapshot,
    ConnectorActionReceipt,
    DeliveryContractSnapshot,
    DispatchLease,
    ExternalAction,
    ExternalActionResultSnapshot,
)
from .runtime import Artifact, Run, RunStep
from .work import CampaignBrief, WorkItem

__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "ActionReservationSnapshot",
    "AgentInstance",
    "AgentTemplate",
    "ApprovalDecision",
    "ApprovalPolicy",
    "ApprovalRequest",
    "Artifact",
    "AuditEvent",
    "CampaignBrief",
    "ConnectorActionReceipt",
    "DeliveryContractSnapshot",
    "Department",
    "DispatchLease",
    "ExternalAction",
    "ExternalActionResultSnapshot",
    "FunctionTeam",
    "Run",
    "RunStep",
    "Schedule",
    "ScheduleOccurrence",
    "ToolCapability",
    "TriggerDefinition",
    "WorkItem",
]
