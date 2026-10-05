"""AC-07 mock receipts retain trusted connector versions across durable replay."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from marketing_agents.application.policies.write_authorization import WriteAuthorizationGuard
from marketing_agents.application.ports.connectors import ConnectorPortError
from marketing_agents.application.services import DispatchDisposition, ExternalActionDispatcher
from marketing_agents.application.services.external_action_registration import (
    ExternalActionRegistrationService,
)
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.infrastructure.adapters.connectors.composition import (
    build_durable_connector_bundle,
)
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.adapters.connectors.mock import DurableMockReceiptLedger
from marketing_agents.infrastructure.adapters.connectors.mock.base import (
    MOCK_CONNECTOR_IMPLEMENTATION_VERSION,
    InMemoryMockReceiptLedger,
)
from marketing_agents.infrastructure.db import (
    ConnectorActionReceiptRecord,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.repositories import ExternalActionPersistenceConflict
from sqlalchemy import select, update

from tests.integration.db.test_run_05_external_action_idempotency import (
    CATALOG,
    MutableClock,
    _counts,
    _dependencies,
    _plan,
    _released_action,
    _runtime,
    _seed_parent,
    _uow_factory,
)


def _record_arguments(action: ExternalAction, *, version: str = "v1") -> dict[str, str]:
    return {
        "external_action_id": action.id,
        "binding_id": action.connector_binding_id,
        "idempotency_key": action.idempotency_key,
        "action_hash": action.action_hash,
        "capability_id": action.envelope.capability_id,
        "connector_family": action.envelope.connector_family,
        "provider_version": version,
    }


async def test_ac_07_trusted_binding_identity_survives_engine_restart_and_version_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector-provenance.db"
    runtime = await _runtime(path)
    clock = MutableClock()
    try:
        action = await _released_action(runtime, clock, seed=707)
        bundle = build_durable_connector_bundle(
            CATALOG, unit_of_work_factory=_uow_factory(runtime), clock=clock
        )
        binding = bundle.binding_registry.resolve(action.connector_binding_id)
        gateway = RegistryConnectorWriteGateway(
            bundle.registry,
            bundle,
            binding_configuration_revisions={
                action.connector_binding_id: action.delivery_contract.binding_configuration_revision
            },
        )
        completed = await ExternalActionDispatcher(
            _dependencies(runtime, clock), gateway, WriteAuthorizationGuard()
        ).dispatch_once(action.id, lease_owner="worker.ac-07.provenance")
        assert completed.disposition is DispatchDisposition.SUCCEEDED
        assert completed.action.result is not None
        assert binding.provider_version == MOCK_CONNECTOR_IMPLEMENTATION_VERSION
        expected_metadata = {
            "mode": binding.provider_mode,
            "external_side_effect": False,
            "capability_id": action.envelope.capability_id,
            "provider_kind": "connector",
            "provider_name": binding.provider_name,
            "provider_version": binding.provider_version,
        }
        assert dict(completed.action.result.safe_metadata) == expected_metadata
        async with _uow_factory(runtime)() as unit_of_work:
            original = await unit_of_work.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
        assert original is not None
        assert original.receipt_id == completed.action.result.receipt_id
        assert dict(original.safe_metadata) == expected_metadata
        assert bundle.ledger.side_effect_count == 1
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()

    restarted = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    clock.tick(60)
    try:
        ledger = DurableMockReceiptLedger(_uow_factory(restarted), clock)
        replay = await ledger.record(**_record_arguments(action, version="v2"))
        assert replay.receipt_id == original.receipt_id
        assert replay.status == original.status
        assert replay.safe_metadata == expected_metadata
        assert replay.safe_metadata["provider_version"] == "v1"
        assert ledger.side_effect_count == 0
        async with _uow_factory(restarted)() as unit_of_work:
            stored = await unit_of_work.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
        assert stored == original
        assert await _counts(restarted) == (1, 1)
    finally:
        await restarted.dispose()


async def test_ac_07_legacy_receipt_replay_does_not_backfill_current_version(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "legacy-provenance.db")
    clock = MutableClock()
    try:
        await _seed_parent(runtime)
        registered = await ExternalActionRegistrationService(
            _dependencies(runtime, clock)
        ).register_plan_actions(_plan(seed=708))
        action = registered.actions[0].action
        ledger = DurableMockReceiptLedger(_uow_factory(runtime), clock)
        result = await ledger.record(**_record_arguments(action))
        legacy_metadata = {
            "mode": "mock",
            "external_side_effect": False,
            "capability_id": action.envelope.capability_id,
        }
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(ConnectorActionReceiptRecord)
                .where(ConnectorActionReceiptRecord.external_action_id == action.id)
                .values(safe_metadata=legacy_metadata)
            )
        async with _uow_factory(runtime)() as unit_of_work:
            legacy = await unit_of_work.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
        restarted = DurableMockReceiptLedger(_uow_factory(runtime), clock)
        replay = await restarted.record(**_record_arguments(action, version="v2"))
        assert replay.receipt_id == result.receipt_id
        assert replay.safe_metadata == legacy_metadata
        assert "provider_version" not in replay.safe_metadata
        assert restarted.side_effect_count == 0
        async with _uow_factory(runtime)() as unit_of_work:
            assert (
                await unit_of_work.connector_receipts.get(
                    action.connector_binding_id, action.idempotency_key
                )
                == legacy
            )
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("receipt_id", "mock-receipt:tampered"),
        ("status", "tampered"),
        ("mode", "real"),
        ("external_side_effect", True),
        ("external_side_effect", 0),
        ("capability_id", "cap.newsletter.subscribe"),
        ("provider_kind", "llm"),
        ("provider_name", "newsletter"),
        ("provider_version", ""),
        ("provider_version", " v1"),
        ("provider_version", "v" * 101),
        ("provider_version", 1),
        ("unregistered_metadata", "unexpected"),
        ("missing_provider_name", None),
    ],
)
async def test_ac_07_durable_replay_rejects_tampered_deterministic_receipt(
    tmp_path: Path, field: str, value: object
) -> None:
    runtime = await _runtime(tmp_path / "tampered-provenance.db")
    clock = MutableClock()
    try:
        await _seed_parent(runtime)
        registered = await ExternalActionRegistrationService(
            _dependencies(runtime, clock)
        ).register_plan_actions(_plan(seed=709))
        action = registered.actions[0].action
        original = await DurableMockReceiptLedger(_uow_factory(runtime), clock).record(
            **_record_arguments(action)
        )
        if field in {"receipt_id", "status"}:
            changes = {field: value}
        else:
            metadata = dict(original.safe_metadata)
            if field == "missing_provider_name":
                del metadata["provider_name"]
            else:
                metadata[field] = value
            changes = {"safe_metadata": metadata}
        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(ConnectorActionReceiptRecord)
                .where(ConnectorActionReceiptRecord.external_action_id == action.id)
                .values(**changes)
            )
        restarted = DurableMockReceiptLedger(_uow_factory(runtime), clock)
        with pytest.raises(ConnectorPortError) as error:
            await restarted.record(**_record_arguments(action, version="v2"))
        assert error.value.code == "idempotency_conflict"
        assert restarted.side_effect_count == 0
        assert await _counts(runtime) == (1, 1)
    finally:
        await runtime.dispose()


async def test_ac_07_repository_does_not_accept_version_rewrites(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "receipt-version-collision.db")
    clock = MutableClock()
    try:
        await _seed_parent(runtime)
        registered = await ExternalActionRegistrationService(
            _dependencies(runtime, clock)
        ).register_plan_actions(_plan(seed=710))
        action = registered.actions[0].action
        await DurableMockReceiptLedger(_uow_factory(runtime), clock).record(
            **_record_arguments(action)
        )
        async with _uow_factory(runtime)() as unit_of_work:
            original = await unit_of_work.connector_receipts.get(
                action.connector_binding_id, action.idempotency_key
            )
        assert original is not None
        changed = replace(
            original, safe_metadata={**original.safe_metadata, "provider_version": "v2"}
        )
        async with _uow_factory(runtime)() as unit_of_work:
            with pytest.raises(ExternalActionPersistenceConflict) as error:
                await unit_of_work.connector_receipts.add_or_get(changed)
            assert error.value.code == "connector_receipt_collision"
        async with runtime.session_factory() as session:
            row = (
                await session.execute(
                    select(ConnectorActionReceiptRecord).where(
                        ConnectorActionReceiptRecord.external_action_id == action.id
                    )
                )
            ).scalar_one()
            assert row.safe_metadata["provider_version"] == "v1"
    finally:
        await runtime.dispose()


async def test_ac_07_in_memory_receipts_keep_historical_version_and_detached_metadata() -> None:
    arguments = {
        "external_action_id": "action.ac-07",
        "binding_id": "mock.newsletter.default",
        "idempotency_key": "idempotency.ac-07",
        "action_hash": "a" * 64,
        "capability_id": "cap.email.send-message",
        "connector_family": "newsletter",
        "provider_version": "v1",
    }
    ledger = InMemoryMockReceiptLedger()
    result = await ledger.record(**arguments)
    assert result.safe_metadata["provider_name"] == "newsletter"
    result.safe_metadata["provider_version"] = "caller-mutated"
    replay = await ledger.record(**{**arguments, "provider_version": "v2"})
    assert replay.safe_metadata["provider_version"] == "v1"
    assert ledger.side_effect_count == 1
    with pytest.raises(ConnectorPortError) as error:
        await ledger.record(**{**arguments, "connector_family": "crm"})
    assert error.value.code == "invalid_request"
    assert ledger.side_effect_count == 1
