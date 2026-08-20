"""Framework-independent immutable domain entities."""

from marketing_agents.domain.audit import AuditEvent

from .catalog import (
    AgentInstance,
    AgentTemplate,
    ApprovalPolicy,
    Department,
    FunctionTeam,
    ToolCapability,
    TriggerDefinition,
)
from .control import Schedule, ScheduleOccurrence
from .external_action import (
    MAX_DELIVERY_ATTEMPTS,
    ActionReservationSnapshot,
    ConnectorActionReceipt,
    DeliveryContractSnapshot,
    DispatchLease,
    ExternalAction,
    ExternalActionResultSnapshot,
)
from .runtime import (
    Artifact,
    Run,
    RunPlanRoutingAssignment,
    RunPlanSelectedInstance,
    RunPlanSnapshot,
    RunStep,
)
from .work import CampaignBrief, WorkItem

__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "ActionReservationSnapshot",
    "AgentInstance",
    "AgentTemplate",
    "ApprovalPolicy",
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
    "RunPlanRoutingAssignment",
    "RunPlanSelectedInstance",
    "RunPlanSnapshot",
    "RunStep",
    "Schedule",
    "ScheduleOccurrence",
    "ToolCapability",
    "TriggerDefinition",
    "WorkItem",
]
