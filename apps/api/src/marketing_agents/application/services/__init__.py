"""Framework-independent application services."""

from .incoming_work_validation import (
    CampaignBriefPolicy,
    CampaignBriefRevision,
    ConfiguredIncomingTrigger,
    IncomingWorkValidationError,
    IncomingWorkValidator,
    ValidatedIncomingWork,
    WorkflowAdmissionDefinition,
    WorkflowAdmissionSnapshot,
)
from .run_lifecycle import (
    ReceiveRunRequest,
    ReceiveRunResult,
    RunLifecycleService,
    RunLifecycleServiceError,
)
from .work_admission import (
    AdmissionDisposition,
    WorkAdmissionResult,
    WorkAdmissionService,
    WorkIdempotencyError,
)

__all__ = [
    "AdmissionDisposition",
    "CampaignBriefPolicy",
    "CampaignBriefRevision",
    "ConfiguredIncomingTrigger",
    "IncomingWorkValidationError",
    "IncomingWorkValidator",
    "ReceiveRunRequest",
    "ReceiveRunResult",
    "RunLifecycleService",
    "RunLifecycleServiceError",
    "ValidatedIncomingWork",
    "WorkAdmissionResult",
    "WorkAdmissionService",
    "WorkIdempotencyError",
    "WorkflowAdmissionDefinition",
    "WorkflowAdmissionSnapshot",
]
