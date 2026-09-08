"""DEL-04: normalized triggers share the configuration transaction and identity boundary."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.domain.instance_configuration import (
    InstanceConfiguration,
    InstanceTriggerBinding,
)
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import DatabaseRuntime, create_database_runtime
from marketing_agents.infrastructure.db.migrations import upgrade_database
from marketing_agents.infrastructure.db.models.deployment import TriggerDefinitionRecord
from marketing_agents.infrastructure.db.models.instance_configuration import (
    AgentInstanceConfigurationRecord,
)
from marketing_agents.infrastructure.db.repositories.instance_configuration import (
    InstanceConfigurationPersistenceError,
    SQLAlchemyInstanceConfigurationRepository,
)
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

CATALOG_ROOT = Path(__file__).resolve().parents[3] / "catalog" / "v1"
INSTANCE_ID = "inst.community.events.attendee-scheduler.01"


@pytest.fixture(scope="module")
def catalog() -> CompiledCatalog:
    return compile_catalog(CATALOG_ROOT)


@pytest.fixture
async def runtime(tmp_path: Path, catalog: CompiledCatalog) -> AsyncIterator[DatabaseRuntime]:
    value = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'triggers.db'}")
    try:
        assert await upgrade_database(value) == "0005"
        seeded = await seed_catalog(catalog, value, CroniterRecurrenceCalculator())
        assert seeded.configuration_inserted == 43
        yield value
    finally:
        await value.dispose()


async def _configuration(runtime: DatabaseRuntime) -> InstanceConfiguration:
    async with runtime.session_factory() as session:
        current = await SQLAlchemyInstanceConfigurationRepository(session).get(INSTANCE_ID)
        assert current is not None
        return current


async def _snapshot(runtime: DatabaseRuntime) -> dict[str, tuple[dict[str, Any], ...]]:
    async with runtime.session_factory() as session:
        snapshot = {}
        for model in (AgentInstanceConfigurationRecord, TriggerDefinitionRecord):
            table = model.__table__
            rows = await session.execute(select(table).order_by(*table.primary_key.columns))
            snapshot[table.name] = tuple(dict(row._mapping) for row in rows)
        return snapshot


def _with_triggers(current: InstanceConfiguration) -> InstanceConfiguration:
    return replace(
        current,
        trigger_bindings=(
            InstanceTriggerBinding(kind=TriggerKind.MANUAL),
            InstanceTriggerBinding(kind=TriggerKind.WEBHOOK, event_source="local.events"),
        ),
        configuration_revision=current.configuration_revision + 1,
    )


async def _configure_triggers(runtime: DatabaseRuntime) -> InstanceConfiguration:
    current = await _configuration(runtime)
    replacement = _with_triggers(current)
    async with runtime.session_factory() as session, session.begin():
        assert await SQLAlchemyInstanceConfigurationRepository(session).compare_and_swap(
            current, replacement
        )
    return replacement


async def test_del_04_trigger_projection_tracks_replacement_removal_and_restart(
    runtime: DatabaseRuntime,
) -> None:
    before = await _snapshot(runtime)
    assert before["trigger_definitions"] == ()
    replacement = await _configure_triggers(runtime)
    assert await _configuration(runtime) == replacement
    after = await _snapshot(runtime)
    rows = {row["kind"]: row for row in after["trigger_definitions"]}
    assert set(rows) == {"manual", "webhook"}
    for kind, row in rows.items():
        assert row["instance_id"] == INSTANCE_ID
        assert row["version"] == 2
        assert row["enabled"] is True
        assert json.loads(row["configuration_json"]) == {
            "type": kind,
            "enabled": True,
            "event_source": "local.events" if kind == "webhook" else None,
            "cron": None,
            "timezone": None,
            "misfire_policy": None,
            "misfire_grace_seconds": None,
        }
    assert tuple(
        row for row in after["agent_instance_configs"] if row["instance_id"] != INSTANCE_ID
    ) == tuple(row for row in before["agent_instance_configs"] if row["instance_id"] != INSTANCE_ID)

    next_configuration = replace(
        replacement,
        trigger_bindings=(
            InstanceTriggerBinding(
                kind=TriggerKind.WEBHOOK, enabled=False, event_source="local.updated-events"
            ),
        ),
        configuration_revision=3,
    )
    async with runtime.session_factory() as session, session.begin():
        assert await SQLAlchemyInstanceConfigurationRepository(session).compare_and_swap(
            replacement, next_configuration
        )
    await runtime.engine.dispose()
    assert await _configuration(runtime) == next_configuration
    final_rows = (await _snapshot(runtime))["trigger_definitions"]
    assert len(final_rows) == 1
    assert final_rows[0]["id"] == rows["webhook"]["id"]
    assert final_rows[0]["kind"] == "webhook"
    assert final_rows[0]["enabled"] is False
    assert final_rows[0]["version"] == 3
    assert json.loads(final_rows[0]["configuration_json"])["event_source"] == (
        "local.updated-events"
    )


@pytest.mark.parametrize("failure", ("stale", "incorrect_snapshot", "invalid_revision"))
async def test_del_04_failed_cas_preserves_configuration_and_trigger_rows(
    runtime: DatabaseRuntime, failure: str
) -> None:
    original = await _configuration(runtime)
    current = await _configure_triggers(runtime)
    before = await _snapshot(runtime)
    previous = original if failure == "stale" else current
    if failure == "incorrect_snapshot":
        previous = replace(current, variant_label="not the persisted snapshot")
    replacement = replace(
        previous,
        trigger_bindings=(),
        configuration_revision=previous.configuration_revision
        + (2 if failure == "invalid_revision" else 1),
    )
    async with runtime.session_factory() as session, session.begin():
        repository = SQLAlchemyInstanceConfigurationRepository(session)
        if failure == "invalid_revision":
            with pytest.raises(InstanceConfigurationPersistenceError) as rejected:
                await repository.compare_and_swap(previous, replacement)
            assert rejected.value.code == "instance_configuration_revision_invalid"
        else:
            assert await repository.compare_and_swap(previous, replacement) is False
    assert await _snapshot(runtime) == before
    assert await _configuration(runtime) == current


async def test_del_04_transaction_failure_rolls_back_configuration_and_trigger_replacement(
    runtime: DatabaseRuntime,
) -> None:
    current = await _configure_triggers(runtime)
    before = await _snapshot(runtime)
    replacement = replace(current, trigger_bindings=(), configuration_revision=3)
    with pytest.raises(RuntimeError, match="fault after projection flush"):
        async with runtime.session_factory() as session, session.begin():
            repository = SQLAlchemyInstanceConfigurationRepository(session)
            assert await repository.compare_and_swap(current, replacement)
            assert await repository.get(INSTANCE_ID) == replacement
            assert (await session.scalars(select(TriggerDefinitionRecord))).all() == []
            raise RuntimeError("fault after projection flush")
    assert await _snapshot(runtime) == before
    assert await _configuration(runtime) == current


@pytest.mark.parametrize("tamper", ("missing", "extra", "id", "enabled", "version", "payload"))
async def test_del_04_all_configuration_reads_reject_trigger_projection_tampering(
    runtime: DatabaseRuntime, tamper: str
) -> None:
    await _configure_triggers(runtime)
    async with runtime.session_factory() as session, session.begin():
        target = TriggerDefinitionRecord.kind == "webhook"
        if tamper == "missing":
            await session.execute(delete(TriggerDefinitionRecord).where(target))
        elif tamper == "extra":
            session.add(
                TriggerDefinitionRecord(
                    id="trigger.unexpected.schedule",
                    instance_id=INSTANCE_ID,
                    kind="schedule",
                    enabled=False,
                    configuration_json="{}",
                    version=2,
                )
            )
        else:
            changes = {
                "id": {"id": "trigger.replaced.identity"},
                "enabled": {"enabled": False},
                "version": {"version": 3},
                "payload": {"configuration_json": "{}"},
            }
            await session.execute(
                update(TriggerDefinitionRecord).where(target).values(**changes[tamper])
            )
    before = await _snapshot(runtime)
    async with runtime.session_factory() as session:
        repository = SQLAlchemyInstanceConfigurationRepository(session)
        for read in (repository.get, repository.get_for_update):
            with pytest.raises(InstanceConfigurationPersistenceError) as rejected:
                await read(INSTANCE_ID)
            assert rejected.value.code == "instance_configuration_tampered"
        with pytest.raises(InstanceConfigurationPersistenceError) as rejected_list:
            await repository.list_all()
        assert rejected_list.value.code == "instance_configuration_tampered"
    assert await _snapshot(runtime) == before


async def test_del_04_configuration_fk_rejects_unknown_catalog_instance(
    runtime: DatabaseRuntime,
) -> None:
    before = await _snapshot(runtime)
    with pytest.raises(IntegrityError) as rejected:
        async with runtime.session_factory() as session, session.begin():
            session.add(
                AgentInstanceConfigurationRecord(
                    instance_id="inst.unknown.catalog.instance.01",
                    enabled=True,
                    variant_label=None,
                    trigger_bindings_json="[]",
                    connector_bindings_json="{}",
                    schedule_json="null",
                    version=1,
                    integrity_digest="a" * 64,
                )
            )
            await session.flush()
    assert rejected.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY"
    assert await _snapshot(runtime) == before
