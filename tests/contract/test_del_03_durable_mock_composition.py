"""DEL-03 configured offline mocks retain exact durable receipts across restart."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.application.policies.write_authorization import (
    AuthorizedExternalWrite,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.connector_families import ExplicitIds, ReadPostsRequest
from marketing_agents.application.ports.connectors import ConnectorCallContext, ConnectorWriteResult
from marketing_agents.application.ports.external_writes import ConnectorDeliveryContract
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services import DispatchDisposition, ExternalActionDispatcher
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.infrastructure.adapters.connectors import registry
from marketing_agents.infrastructure.adapters.connectors.composition import (
    LocalMockConnectorSettings,
    build_durable_connector_bundle,
)
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.adapters.connectors.mock import (
    DurableMockReceiptLedger,
    build_connector_bundle,
)
from marketing_agents.infrastructure.adapters.connectors.registry import (
    DISABLED_V1_CAPABILITIES,
    EXTERNAL_CONNECTOR_FAMILIES,
    ConnectorBundleConfigurationError,
)
from marketing_agents.infrastructure.db import create_database_runtime

from tests.integration.db.test_run_05_external_action_idempotency import (
    CATALOG,
    CountingGateway,
    MutableClock,
    _counts,
    _dependencies,
    _released_action,
    _runtime,
    _uow_factory,
)


@dataclass(frozen=True)
class _ConnectorSettings:
    connector_mode: str = "mock"
    allow_external_network: bool = False
    real_connector_opt_in: bool = False


class _UnusedUnitOfWorkFactory:
    calls = 0

    def __call__(self) -> UnitOfWork:
        self.calls += 1
        raise AssertionError("mock composition and reads must not open a database transaction")


class _FalseyDurableLedger(DurableMockReceiptLedger):
    def __bool__(self) -> bool:
        return False


class _RecordingGateway:
    """Capture only a proof actually issued by the normal dispatcher."""

    def __init__(self, delegate: RegistryConnectorWriteGateway) -> None:
        self.delegate = delegate
        self.authorization: AuthorizedExternalWrite | None = None
        self.result: ConnectorWriteResult | None = None

    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        return self.delegate.contract_for(action)

    async def execute(self, authorization: AuthorizedExternalWrite) -> ConnectorWriteResult:
        self.authorization = authorization
        self.result = await self.delegate.execute(authorization)
        return self.result


def test_del_03_defaults_are_immutable_offline_and_runtime_bundle_is_durable() -> None:
    settings = LocalMockConnectorSettings()
    assert settings.connector_mode == "mock"
    assert settings.allow_external_network is False
    assert settings.real_connector_opt_in is False
    with pytest.raises(FrozenInstanceError):
        settings.allow_external_network = True  # type: ignore[misc]

    unit_of_work_factory = _UnusedUnitOfWorkFactory()
    bundle = build_durable_connector_bundle(
        CATALOG,
        unit_of_work_factory=unit_of_work_factory,
        clock=MutableClock(),
    )
    assert type(bundle.ledger) is DurableMockReceiptLedger
    assert bundle.ledger.durable is True
    assert bundle.ledger.side_effect_count == 0
    assert unit_of_work_factory.calls == 0
    assert len(bundle.registry.operations) == 20
    assert {item.metadata.connector_family for item in bundle.registry.operations} == set(
        EXTERNAL_CONNECTOR_FAMILIES
    )
    assert {
        item.metadata.capability_id
        for item in bundle.registry.operations
        if not item.metadata.enabled
    } == set(DISABLED_V1_CAPABILITIES)
    for operation in bundle.registry.operations:
        connector = getattr(bundle, operation.metadata.connector_family)
        assert callable(getattr(connector, operation.method_name))
    bundle.registry.validate_catalog(CATALOG)

    # The explicit runtime factory closes the gap without changing low-level test mocks.
    low_level = build_connector_bundle(settings, CATALOG)
    assert low_level.ledger.durable is False
    with pytest.raises(ConnectorBundleConfigurationError, match="durable"):
        RegistryConnectorWriteGateway(
            low_level.registry, low_level, binding_configuration_revisions={}
        )


def test_del_03_configured_builder_preserves_an_explicit_falsey_ledger() -> None:
    unit_of_work_factory = _UnusedUnitOfWorkFactory()
    ledger = _FalseyDurableLedger(unit_of_work_factory, MutableClock())
    bundle = build_connector_bundle(LocalMockConnectorSettings(), CATALOG, ledger=ledger)
    assert bundle.ledger is ledger
    RegistryConnectorWriteGateway(bundle.registry, bundle, binding_configuration_revisions={})
    assert unit_of_work_factory.calls == 0


@pytest.mark.parametrize(
    "settings",
    (
        _ConnectorSettings(connector_mode="real"),
        _ConnectorSettings(connector_mode="MOCK"),
        _ConnectorSettings(allow_external_network=True),
        _ConnectorSettings(real_connector_opt_in=True),
        _ConnectorSettings(allow_external_network=True, real_connector_opt_in=True),
    ),
)
def test_del_03_runtime_factory_rejects_unregistered_or_network_configuration(
    settings: _ConnectorSettings,
) -> None:
    unit_of_work_factory = _UnusedUnitOfWorkFactory()
    with pytest.raises(ConnectorBundleConfigurationError):
        build_durable_connector_bundle(
            CATALOG,
            settings=settings,
            unit_of_work_factory=unit_of_work_factory,
            clock=MutableClock(),
        )
    assert unit_of_work_factory.calls == 0


@pytest.mark.parametrize("corruption", ("duplicate", "missing", "metadata_drift"))
def test_del_03_runtime_factory_rechecks_complete_catalog_registry(
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    registrations = registry.OPERATION_REGISTRATIONS
    if corruption == "duplicate":
        registrations = (*registrations, registrations[0])
    elif corruption == "missing":
        registrations = registrations[1:]
    else:
        first = registrations[0]
        registrations = (
            replace(first, metadata=replace(first.metadata, default_timeout_seconds=29)),
            *registrations[1:],
        )
    monkeypatch.setattr(registry, "OPERATION_REGISTRATIONS", registrations)
    unit_of_work_factory = _UnusedUnitOfWorkFactory()
    with pytest.raises(ConnectorBundleConfigurationError):
        build_durable_connector_bundle(
            CATALOG,
            unit_of_work_factory=unit_of_work_factory,
            clock=MutableClock(),
        )
    assert unit_of_work_factory.calls == 0


@pytest.mark.asyncio
async def test_del_03_reads_remain_deterministic_across_runtime_bundle_reconstruction() -> None:
    clock = MutableClock()
    unit_of_work_factory = _UnusedUnitOfWorkFactory()
    request = ReadPostsRequest(
        context=ConnectorCallContext(
            binding_id="mock.social.default",
            run_id="run.del-03.first",
            step_id="step.del-03.first",
            correlation_id="correlation.del-03.first",
            deadline=clock.now() + timedelta(seconds=30),
            provenance_ids=("work-input:del-03.first",),
            requested_timeout_seconds=30,
        ),
        parameters=ExplicitIds(resource_ids=("post:del-03",)),
    )
    first_bundle = build_durable_connector_bundle(
        CATALOG, unit_of_work_factory=unit_of_work_factory, clock=clock
    )
    first = await first_bundle.social.read_posts(request)
    clock.tick(60)
    second_bundle = build_durable_connector_bundle(
        CATALOG, unit_of_work_factory=unit_of_work_factory, clock=clock
    )
    second = await second_bundle.social.read_posts(
        request.model_copy(
            update={
                "context": request.context.model_copy(
                    update={
                        "run_id": "run.del-03.second",
                        "step_id": "step.del-03.second",
                        "correlation_id": "correlation.del-03.second",
                        "deadline": clock.now() + timedelta(seconds=30),
                        "provenance_ids": ("work-input:del-03.second",),
                    }
                )
            }
        )
    )
    assert first_bundle is not second_bundle
    assert first_bundle.ledger is not second_bundle.ledger
    assert first.payload == second.payload
    assert first.observation_id == second.observation_id
    assert first.provenance_ids != second.provenance_ids
    assert first.trust_class == second.trust_class == "untrusted_tool_result"
    assert unit_of_work_factory.calls == 0


@pytest.mark.asyncio
async def test_del_03_configured_write_receipt_replays_after_engine_and_bundle_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "del-03-durable-replay.db"
    runtime = await _runtime(path)
    clock = MutableClock()
    try:
        action = await _released_action(runtime, clock, seed=303)
        bundle = build_durable_connector_bundle(
            CATALOG, unit_of_work_factory=_uow_factory(runtime), clock=clock
        )
        revisions = {
            action.connector_binding_id: action.delivery_contract.binding_configuration_revision
        }
        gateway = _RecordingGateway(
            RegistryConnectorWriteGateway(
                bundle.registry, bundle, binding_configuration_revisions=revisions
            )
        )
        completed = await ExternalActionDispatcher(
            _dependencies(runtime, clock), gateway, WriteAuthorizationGuard()
        ).dispatch_once(action.id, lease_owner="worker.del-03.first")
        assert completed.disposition is DispatchDisposition.SUCCEEDED
        assert bundle.ledger.side_effect_count == 1
        assert gateway.authorization is not None
        assert gateway.result is not None
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()

    # Reopen the existing database without create_all or any seed/replay mutation.
    restarted = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    clock.tick(60)
    try:
        restarted_bundle = build_durable_connector_bundle(
            CATALOG, unit_of_work_factory=_uow_factory(restarted), clock=clock
        )
        restarted_gateway = RegistryConnectorWriteGateway(
            restarted_bundle.registry,
            restarted_bundle,
            binding_configuration_revisions=revisions,
        )
        assert restarted_bundle.ledger is not bundle.ledger
        # Adapter-level replay exercises the new ledger, not only a terminal Run shortcut.
        repeated = await restarted_gateway.execute(gateway.authorization)
        assert repeated == gateway.result
        assert restarted_bundle.ledger.side_effect_count == 0

        counted = CountingGateway(restarted_gateway)
        replayed = await ExternalActionDispatcher(
            _dependencies(restarted, clock), counted, WriteAuthorizationGuard()
        ).dispatch_once(action.id, lease_owner="worker.del-03.restarted")
        assert replayed.disposition is DispatchDisposition.ALREADY_SUCCEEDED
        assert replayed.action == completed.action
        assert counted.calls == 0
        assert restarted_bundle.ledger.side_effect_count == 0
        assert await _counts(restarted) == (1, 1)
    finally:
        await restarted.dispose()
