"""DEL-04: opt-in live PostgreSQL migration/seed parity and structural drift rejection."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import CatalogSeedError, seed_catalog
from marketing_agents.infrastructure.db import Base, create_database_runtime
from marketing_agents.infrastructure.db.migrations import (
    DatabaseMigrationError,
    inspect_migration,
    upgrade_database,
)
from marketing_agents.infrastructure.db.models.catalog import CatalogReleaseRecord
from marketing_agents.infrastructure.db.repositories.instance_configuration import (
    InstanceConfigurationSQLAlchemyUnitOfWork,
)
from marketing_agents.infrastructure.db.schema import schema_matches_metadata
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from sqlalchemy import event, inspect, text

from tests.integration.db.test_del_04_catalog_seed import EXPECTED_COUNTS, _snapshot
from tests.support import postgresql_runtime

ROOT = Path(__file__).resolve().parents[3]
pg_database_url = postgresql_runtime.pg_database_url


@pytest.mark.parametrize("previous", [None, "0001", "0002", "0003", "0004"])
async def test_del_04_postgresql_fresh_and_incremental_upgrades(
    pg_database_url: str, previous: str | None
) -> None:
    runtime = create_database_runtime(pg_database_url)
    try:
        if previous is not None:
            await upgrade_database(runtime, previous)
            async with runtime.session_factory() as session, session.begin():
                session.add(
                    CatalogReleaseRecord(
                        content_hash="catalog-sha256-v1:" + "b" * 64,
                        content_version="historic.sentinel",
                        snapshot_json='{"preserved":true}',
                        recorded_at=datetime(2026, 9, 8, tzinfo=UTC),
                    )
                )
        assert await upgrade_database(runtime) == "0005"
        assert await upgrade_database(runtime) == "0005"
        async with runtime.engine.connect() as connection:
            status = await connection.run_sync(inspect_migration)
            assert status.current and status.schema_matches
            tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            assert set(tables) == set(Base.metadata.tables) | {"alembic_version"}
        snapshot = await _snapshot(runtime)
        if previous is None:
            assert all(not rows for rows in snapshot.values())
        else:
            assert snapshot["catalog_releases"][0]["snapshot_json"] == '{"preserved":true}'
    finally:
        await runtime.dispose()


async def test_del_04_postgresql_seed_concurrency_preserves_overrides_and_checks_read_only(
    pg_database_url: str,
) -> None:
    runtime = create_database_runtime(pg_database_url)
    catalog = compile_catalog(ROOT / "catalog/v1")
    recurrence = CroniterRecurrenceCalculator()
    try:
        await upgrade_database(runtime)
        results = await asyncio.gather(
            *(seed_catalog(catalog, runtime, recurrence) for _ in range(2))
        )
        assert sum(result.configuration_inserted for result in results) == 43
        assert all(result.counts == EXPECTED_COUNTS for result in results)
        async with InstanceConfigurationSQLAlchemyUnitOfWork(runtime.session_factory) as uow:
            previous = await uow.configurations.get_for_update(catalog.instances[0].id)
            assert previous is not None
            replacement = replace(previous, enabled=not previous.enabled, configuration_revision=2)
            assert await uow.configurations.compare_and_swap(previous, replacement)
            await uow.commit()
        before = await _snapshot(runtime)
        result = await seed_catalog(catalog, runtime, recurrence)
        assert result.configuration_inserted == 0 and result.configuration_preserved == 43
        assert await _snapshot(runtime) == before
        assert (
            await seed_catalog(catalog, runtime, recurrence, check=True)
        ).counts == EXPECTED_COUNTS
        assert await _snapshot(runtime) == before
        async with runtime.engine.begin() as connection:
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            assert await connection.run_sync(schema_matches_metadata)
        async with runtime.engine.begin() as connection:
            await connection.execute(text("UPDATE departments SET display_name='Drift'"))
        drifted = await _snapshot(runtime)
        with pytest.raises(CatalogSeedError, match="projection"):
            await seed_catalog(catalog, runtime, recurrence, check=True)
        assert await _snapshot(runtime) == drifted
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("phase", ["migration", "seed"])
async def test_del_04_postgresql_partial_failure_rolls_back(
    pg_database_url: str, phase: str
) -> None:
    runtime = create_database_runtime(pg_database_url)
    fired = False

    def fail_after_write(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        nonlocal fired
        del connection, cursor, parameters, context, executemany
        normalized = " ".join(statement.upper().split())
        target = "CREATE TABLE RUNS" if phase == "migration" else "INSERT INTO AGENT_TEMPLATES"
        if not fired and target in normalized:
            fired = True
            raise RuntimeError("del04 injected PostgreSQL transaction failure")

    try:
        if phase == "seed":
            await upgrade_database(runtime)
        event.listen(runtime.engine.sync_engine, "after_cursor_execute", fail_after_write)
        try:
            if phase == "migration":
                with pytest.raises(RuntimeError, match="injected"):
                    await upgrade_database(runtime)
            else:
                with pytest.raises(CatalogSeedError):
                    await seed_catalog(
                        compile_catalog(ROOT / "catalog/v1"),
                        runtime,
                        CroniterRecurrenceCalculator(),
                    )
        finally:
            event.remove(runtime.engine.sync_engine, "after_cursor_execute", fail_after_write)
        assert fired
        if phase == "migration":
            async with runtime.engine.connect() as connection:
                assert await connection.run_sync(lambda sync: inspect(sync).get_table_names()) == []
            assert await upgrade_database(runtime) == "0005"
        else:
            assert all(not rows for rows in (await _snapshot(runtime)).values())
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "drift",
    [
        "check",
        "not_valid",
        "primary_key",
        "serial_default",
        "different_sequence",
        "partial_index",
        "foreign_schema",
    ],
)
async def test_del_04_postgresql_rejects_material_schema_drift(
    pg_database_url: str, drift: str
) -> None:
    runtime = create_database_runtime(pg_database_url)
    try:
        await upgrade_database(runtime)
        commands = {
            "check": [
                "ALTER TABLE maintenance_runs DROP CONSTRAINT ck_maintenance_lease_complete",
                "ALTER TABLE maintenance_runs ADD CONSTRAINT "
                "ck_maintenance_lease_complete CHECK (true)",
            ],
            "not_valid": [
                "ALTER TABLE maintenance_runs DROP CONSTRAINT ck_maintenance_version_positive",
                "ALTER TABLE maintenance_runs ADD CONSTRAINT "
                "ck_maintenance_version_positive CHECK (version >= 1) NOT VALID",
            ],
            "primary_key": [
                "ALTER TABLE maintenance_runs DROP CONSTRAINT pk_maintenance_runs",
            ],
            "serial_default": [
                "ALTER TABLE catalog_current_release ALTER COLUMN singleton_id DROP DEFAULT",
            ],
            "different_sequence": [
                "CREATE SEQUENCE wrong_sequence",
                "ALTER TABLE catalog_current_release ALTER COLUMN singleton_id "
                "SET DEFAULT nextval('wrong_sequence')",
            ],
            "partial_index": [
                "DROP INDEX ix_runs_state_created_at",
                "CREATE INDEX ix_runs_state_created_at ON runs (state, created_at) "
                "WHERE state='running'",
            ],
            "foreign_schema": [
                "CREATE SCHEMA other_scope",
                "CREATE TABLE other_scope.departments (id VARCHAR(240) PRIMARY KEY)",
                "ALTER TABLE function_teams DROP CONSTRAINT "
                "fk_function_teams_department_id_departments",
                "ALTER TABLE function_teams ADD CONSTRAINT "
                "fk_function_teams_department_id_departments FOREIGN KEY (department_id) "
                "REFERENCES other_scope.departments(id) ON DELETE RESTRICT",
            ],
        }[drift]
        async with runtime.engine.begin() as connection:
            for command in commands:
                await connection.exec_driver_sql(command)
        async with runtime.engine.connect() as connection:
            assert not await connection.run_sync(schema_matches_metadata)
        with pytest.raises(DatabaseMigrationError, match="migration_schema_drift"):
            await upgrade_database(runtime)
    finally:
        await runtime.dispose()
