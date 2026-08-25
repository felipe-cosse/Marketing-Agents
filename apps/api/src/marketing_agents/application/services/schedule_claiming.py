"""Commit one exclusive schedule lease before any occurrence processing."""

from __future__ import annotations

from datetime import timedelta

from marketing_agents.application.orchestration.dependencies import (
    OrchestrationDependencies,
)
from marketing_agents.domain.entities import ScheduleClaim
from marketing_agents.domain.validation import require_id

MIN_SCHEDULE_LEASE = timedelta(seconds=1)
MAX_SCHEDULE_LEASE = timedelta(minutes=10)
DEFAULT_SCHEDULE_LEASE = timedelta(minutes=2)
DEFAULT_CLAIM_BATCH_SIZE = 16
MAX_CLAIM_BATCH_SIZE = 100


class ScheduleClaimService:
    """Find a bounded due set and commit at most one exact CAS lease."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        *,
        lease_duration: timedelta = DEFAULT_SCHEDULE_LEASE,
        batch_size: int = DEFAULT_CLAIM_BATCH_SIZE,
    ) -> None:
        if not MIN_SCHEDULE_LEASE <= lease_duration <= MAX_SCHEDULE_LEASE:
            raise ValueError("schedule lease must be from one second through ten minutes")
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_CLAIM_BATCH_SIZE:
            raise ValueError("schedule claim batch size must be from 1 through 100")
        self._dependencies = dependencies
        self._lease_duration = lease_duration
        self._batch_size = batch_size

    async def claim_due_once(self, *, lease_owner: str) -> ScheduleClaim | None:
        """Commit and return one lease, or return None after bounded CAS losses."""

        require_id(lease_owner, "schedule lease owner")
        claimed_at_utc = self._dependencies.utc_now()
        lease_expires_at_utc = claimed_at_utc + self._lease_duration

        async with self._dependencies.unit_of_work() as unit_of_work:
            candidates = await unit_of_work.schedules.list_claimable_due(
                now=claimed_at_utc,
                limit=self._batch_size,
            )

        for candidate in candidates:
            async with self._dependencies.unit_of_work() as unit_of_work:
                claim = await unit_of_work.schedules.try_claim(
                    schedule_id=candidate.id,
                    expected_version=candidate.version,
                    expected_due_at_utc=candidate.next_run_at_utc,
                    lease_owner=lease_owner,
                    claimed_at_utc=claimed_at_utc,
                    lease_expires_at_utc=lease_expires_at_utc,
                )
                if claim is None:
                    continue
                await unit_of_work.commit()
                return claim
        return None
