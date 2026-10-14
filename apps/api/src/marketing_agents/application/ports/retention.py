"""Leased retention maintenance and content-free historical inspection ports."""

from datetime import datetime
from typing import Protocol

from marketing_agents.application.ports.clock import Clock
from marketing_agents.domain.retained_details import RetainedDetail, RetentionClaim, RetentionResult


class RetentionRepository(Protocol):
    async def claim(
        self, *, occurrence_key: str, owner: str, now: datetime, lease_seconds: int, batch_size: int
    ) -> RetentionClaim | RetentionResult | None: ...

    async def execute(self, claim: RetentionClaim, clock: Clock) -> RetentionResult: ...

    async def inspect(self, record_kind: str, record_id: str) -> RetainedDetail | None: ...
