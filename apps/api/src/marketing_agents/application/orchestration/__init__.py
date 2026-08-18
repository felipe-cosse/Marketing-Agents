"""Framework-independent orchestration composition primitives."""

from .dependencies import OrchestrationDependencies, OrchestrationDependencyError

__all__ = ["OrchestrationDependencies", "OrchestrationDependencyError"]
