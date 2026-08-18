"""Database-backed deterministic receipt ledger for mock connector writes."""

from __future__ import annotations

from marketing_agents.application.ports.clock import Clock
from marketing_agents.application.ports.connectors import ConnectorPortError, ConnectorWriteResult
from marketing_agents.application.ports.unit_of_work import UnitOfWorkFactory
from marketing_agents.domain.entities import ConnectorActionReceipt
from marketing_agents.infrastructure.db.repositories import (
    ExternalActionPersistenceConflict,
)

from .base import build_mock_write_result


class DurableMockReceiptLedger:
    """Commit a mock effect receipt before returning it to the dispatcher."""

    __slots__ = ("_clock", "_side_effect_count", "_unit_of_work_factory")

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._side_effect_count = 0

    @property
    def durable(self) -> bool:
        return True

    @property
    def side_effect_count(self) -> int:
        """Count inserts by this process; durable replay does not increment it."""

        return self._side_effect_count

    async def record(
        self,
        *,
        external_action_id: str,
        binding_id: str,
        idempotency_key: str,
        action_hash: str,
        capability_id: str,
    ) -> ConnectorWriteResult:
        deterministic = build_mock_write_result(
            binding_id=binding_id,
            idempotency_key=idempotency_key,
            action_hash=action_hash,
            capability_id=capability_id,
        )
        candidate = ConnectorActionReceipt(
            external_action_id=external_action_id,
            connector_binding_id=binding_id,
            idempotency_key=idempotency_key,
            action_hash=action_hash,
            capability_id=capability_id,
            receipt_id=deterministic.receipt_id,
            status=deterministic.status,
            safe_metadata=deterministic.safe_metadata,
            created_at=self._clock.now(),
        )
        stored = None
        persistence_conflict = False
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                stored = await unit_of_work.connector_receipts.add_or_get(candidate)
                await unit_of_work.commit()
        except ExternalActionPersistenceConflict:
            persistence_conflict = True
        if persistence_conflict:
            raise ConnectorPortError(
                "idempotency_conflict",
                "durable connector receipt conflicts with the exact action",
            ) from None
        if stored is None:  # pragma: no cover - successful UoW always returns a result
            raise AssertionError("durable receipt persistence returned no result")
        if stored.inserted:
            self._side_effect_count += 1
        return ConnectorWriteResult(
            receipt_id=stored.receipt.receipt_id,
            status=stored.receipt.status,
            safe_metadata=dict(stored.receipt.safe_metadata),
        )
