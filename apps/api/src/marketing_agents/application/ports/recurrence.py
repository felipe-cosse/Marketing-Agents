"""Provider-independent recurrence calculation boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from marketing_agents.domain.recurrence_resolution import RecurrenceResult


class RecurrenceCalculationError(ValueError):
    """Stable fail-closed schedule expression or timezone error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecurrenceCalculator(Protocol):
    def next_occurrence_after(
        self,
        *,
        cron: str,
        timezone: str,
        after_utc: datetime,
    ) -> RecurrenceResult:
        """Return the selected instant and any recorded nonexistent-time resolution."""

    def next_after(
        self,
        *,
        cron: str,
        timezone: str,
        after_utc: datetime,
    ) -> datetime:
        """Return the first valid scheduled UTC instant strictly after the boundary."""
