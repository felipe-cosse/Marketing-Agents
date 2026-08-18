"""Injected clock port for deterministic orchestration."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return an aware UTC instant."""
        ...
