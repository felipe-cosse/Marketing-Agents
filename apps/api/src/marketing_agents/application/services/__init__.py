"""Framework-independent application services."""

from .work_admission import (
    AdmissionDisposition,
    WorkAdmissionResult,
    WorkAdmissionService,
    WorkIdempotencyError,
)

__all__ = [
    "AdmissionDisposition",
    "WorkAdmissionResult",
    "WorkAdmissionService",
    "WorkIdempotencyError",
]
