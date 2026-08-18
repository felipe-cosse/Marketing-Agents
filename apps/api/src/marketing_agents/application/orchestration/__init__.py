"""Framework-independent orchestration composition primitives."""

from .bindings import (
    ArtifactInputBinding,
    BindingContext,
    BindingError,
    BoundArtifactReference,
    BoundStepInput,
    StepInputContract,
    TypedInputBinder,
    WorkInputBinding,
)
from .dependencies import OrchestrationDependencies, OrchestrationDependencyError
from .router import (
    DeterministicInstanceRouter,
    RoutingAssignment,
    RoutingError,
    RoutingInstanceSource,
    RoutingInstanceVariantSource,
    RoutingRequest,
    RoutingResult,
    RoutingSlot,
    RoutingTemplateSource,
    SelectedInstanceSnapshot,
    WorkflowRoutingDefinition,
)

__all__ = [
    "ArtifactInputBinding",
    "BindingContext",
    "BindingError",
    "BoundArtifactReference",
    "BoundStepInput",
    "DeterministicInstanceRouter",
    "OrchestrationDependencies",
    "OrchestrationDependencyError",
    "RoutingAssignment",
    "RoutingError",
    "RoutingInstanceSource",
    "RoutingInstanceVariantSource",
    "RoutingRequest",
    "RoutingResult",
    "RoutingSlot",
    "RoutingTemplateSource",
    "SelectedInstanceSnapshot",
    "StepInputContract",
    "TypedInputBinder",
    "WorkInputBinding",
    "WorkflowRoutingDefinition",
]
