"""Scheduler-owned bounded maintenance using the installation's existing lease DB."""

from marketing_agents.application.services.retention_maintenance import RetentionMaintenanceService
from marketing_agents.infrastructure.db.repositories.retention import SQLAlchemyRetentionRepository
from marketing_agents.infrastructure.db.telemetry import TelemetryAuditRepository

from .composition import LocalRuntime


class RetentionWorker:
    def __init__(self, runtime: LocalRuntime, worker_id: str) -> None:
        self.runtime = runtime
        self.service = RetentionMaintenanceService(
            SQLAlchemyRetentionRepository(
                runtime.database.session_factory,
                runtime.digest_key,
                retention_policy=runtime.settings.retention_policy,
                audit_factory=lambda session: TelemetryAuditRepository(session, runtime.telemetry),
            ),
            runtime.dependencies.clock,
            owner=worker_id,
        )
        self._last_completed_occurrence: str | None = None

    async def drain_once(self) -> bool:
        occurrence = "retention." + self.runtime.dependencies.utc_now().strftime("%Y%m%dT%H%MZ")
        if occurrence == self._last_completed_occurrence:
            return False
        with self.runtime.telemetry.bind():
            result = await self.service.run_once(occurrence_key=occurrence)
        if result is None:
            return False
        self._last_completed_occurrence = occurrence
        return not result.replayed
