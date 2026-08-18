"""Framework-independent application services."""

from .external_action_dispatcher import (
    DispatchDisposition,
    ExternalActionDispatcher,
    ExternalActionDispatchError,
    ExternalActionDispatchResult,
)
from .external_action_registration import (
    ExternalActionRegistrationDisposition,
    ExternalActionRegistrationError,
    ExternalActionRegistrationService,
    RegisteredExternalAction,
    RegisteredExternalActionSet,
)
from .idempotent_work_receipt import (
    IdempotentWorkRunReceiptService,
    WorkRunReceiptDisposition,
    WorkRunReceiptError,
    WorkRunReceiptResult,
)
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
    "DispatchDisposition",
    "ExternalActionDispatchError",
    "ExternalActionDispatchResult",
    "ExternalActionDispatcher",
    "ExternalActionRegistrationDisposition",
    "ExternalActionRegistrationError",
    "ExternalActionRegistrationService",
    "IdempotentWorkRunReceiptService",
    "IncomingWorkValidationError",
    "IncomingWorkValidator",
    "ReceiveRunRequest",
    "ReceiveRunResult",
    "RegisteredExternalAction",
    "RegisteredExternalActionSet",
    "RunLifecycleService",
    "RunLifecycleServiceError",
    "ValidatedIncomingWork",
    "WorkAdmissionResult",
    "WorkAdmissionService",
    "WorkIdempotencyError",
    "WorkRunReceiptDisposition",
    "WorkRunReceiptError",
    "WorkRunReceiptResult",
    "WorkflowAdmissionDefinition",
    "WorkflowAdmissionSnapshot",
]
