"""One bounded, idempotent retention occurrence driven by an injected UTC clock."""

from marketing_agents.application.ports.clock import Clock
from marketing_agents.application.ports.retention import RetentionRepository
from marketing_agents.domain.retained_details import RetentionResult
from marketing_agents.domain.validation import require_id, require_utc


class RetentionMaintenanceService:
    def __init__(
        self,
        repository: RetentionRepository,
        clock: Clock,
        *,
        owner: str,
        batch_size: int = 50,
        lease_seconds: int = 30,
    ) -> None:
        require_id(owner, "retention lease owner")
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("retention batch size must be from 1 through 100")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("retention lease must be from 1 through 300 seconds")
        self.repository, self.clock, self.owner = repository, clock, owner
        self.batch_size, self.lease_seconds = batch_size, lease_seconds

    async def run_once(self, *, occurrence_key: str | None = None) -> RetentionResult | None:
        now = self.clock.now()
        require_utc(now, "retention clock")
        # Fixed one-minute windows prevent an idle worker creating unbounded ledgers.
        occurrence = occurrence_key or "retention." + now.strftime("%Y%m%dT%H%MZ")
        claim = await self.repository.claim(
            occurrence_key=occurrence,
            owner=self.owner,
            now=now,
            lease_seconds=self.lease_seconds,
            batch_size=self.batch_size,
        )
        if claim is None or isinstance(claim, RetentionResult):
            return claim
        return await self.repository.execute(claim, self.clock)
