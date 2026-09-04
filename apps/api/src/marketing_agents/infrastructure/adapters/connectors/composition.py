"""Credential-free runtime composition with database-backed mock write receipts."""

from __future__ import annotations

from dataclasses import dataclass

from marketing_agents.application.ports.clock import Clock
from marketing_agents.application.ports.unit_of_work import UnitOfWorkFactory
from marketing_agents.infrastructure.adapters.connectors.mock.durable import (
    DurableMockReceiptLedger,
)
from marketing_agents.infrastructure.adapters.connectors.mock.families import (
    MockConnectorBundle,
    build_connector_bundle,
)
from marketing_agents.infrastructure.adapters.connectors.registry import ConnectorModeSettings
from marketing_agents.infrastructure.catalog.models import CompiledCatalog


@dataclass(frozen=True, slots=True)
class LocalMockConnectorSettings:
    """Explicit immutable profile for sealed mock-only demo composition."""

    connector_mode: str = "mock"
    allow_external_network: bool = False
    real_connector_opt_in: bool = False


_LOCAL_MOCK_SETTINGS = LocalMockConnectorSettings()


def build_durable_connector_bundle(
    catalog: CompiledCatalog,
    *,
    unit_of_work_factory: UnitOfWorkFactory,
    clock: Clock,
    settings: ConnectorModeSettings = _LOCAL_MOCK_SETTINGS,
) -> MockConnectorBundle:
    """Build catalog-checked mocks with durable idempotency, never a real fallback.

    Construction opens no transaction and creates no schema. The caller owns
    database lifecycle and passes the same receipt store to every reconstruction.
    All write authority still comes exclusively from the dispatcher gateway.
    """

    return build_connector_bundle(
        settings,
        catalog,
        ledger=DurableMockReceiptLedger(unit_of_work_factory, clock),
    )
