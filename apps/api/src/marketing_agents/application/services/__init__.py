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
from .plan_persistence import (
    AuditedPlanPersistenceService,
    PersistedRunPlan,
    PlanPersistenceError,
)
from .run_lifecycle import (
    ReceiveRunRequest,
    ReceiveRunResult,
    RunAdvanceAttempt,
    RunAdvanceDisposition,
    RunLifecycleService,
    RunLifecycleServiceError,
)
from .run_step_lifecycle import (
    RunStepLifecycleService,
    RunStepLifecycleServiceError,
)
from .work_admission import (
    AdmissionDisposition,
    WorkAdmissionResult,
    WorkAdmissionService,
    WorkIdempotencyError,
)

__all__ = [
    "AdmissionDisposition",
    "AuditedPlanPersistenceService",
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
    "PersistedRunPlan",
    "PlanPersistenceError",
    "ReceiveRunRequest",
    "ReceiveRunResult",
    "RegisteredExternalAction",
    "RegisteredExternalActionSet",
    "RunAdvanceAttempt",
    "RunAdvanceDisposition",
    "RunLifecycleService",
    "RunLifecycleServiceError",
    "RunStepLifecycleService",
    "RunStepLifecycleServiceError",
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
