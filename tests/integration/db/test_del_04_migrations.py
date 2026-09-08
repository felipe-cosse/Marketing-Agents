"""DEL-04: frozen schema-only upgrades and failure-safe SQLite migration ownership."""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from marketing_agents.infrastructure import db
from marketing_agents.infrastructure.db import Base, DatabaseRuntime, create_database_runtime
from marketing_agents.infrastructure.db.migrations import (
    HEAD_REVISION,
    DatabaseMigrationError,
    inspect_migration,
    migration_config,
    upgrade_database,
)
from marketing_agents.infrastructure.db.schema import schema_matches_metadata
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

REVISION_TABLES = {
    "0001": {
        "catalog_releases",
        "catalog_current_release",
        "departments",
        "function_teams",
        "tool_capabilities",
        "approval_policies",
        "agent_templates",
        "agent_template_capabilities",
        "agent_template_trigger_kinds",
        "agent_instances",
        "agent_instance_configs",
        "trigger_definitions",
        "local_runtime_identity",
    },
    "0002": {
        "campaign_briefs",
        "work_items",
        "runs",
        "run_plans",
        "run_plan_selected_instances",
        "run_plan_routing_assignments",
        "run_steps",
        "run_step_dependencies",
        "run_step_state_transitions",
        "run_state_transitions",
        "artifacts",
        "artifact_parent_edges",
    },
    "0003": {
        "external_actions",
        "external_action_dispatch_attempts",
        "connector_action_receipts",
        "approval_requests",
        "approval_decisions",
        "approval_uses",
        "authorization_sets",
        "authorization_set_heads",
        "authorization_set_members",
        "audit_feed_sequence",
    },
    "0004": {
        "webhook_receipts",
        "webhook_receipt_deliveries",
        "schedules",
        "schedule_occurrences",
    },
    "0005": {
        "run_execution_controls",
        "execution_operation_policies",
        "rate_limit_windows",
        "execution_attempts",
        "audit_events",
        "maintenance_runs",
    },
}
ALL_TABLES = set().union(*REVISION_TABLES.values())
SENTINEL_HASH = "catalog-sha256-v1:" + "b" * 64


def _runtime(path: Path) -> DatabaseRuntime:
    return create_database_runtime(f"sqlite+aiosqlite:///{path}")


def _schema_snapshot(connection: Connection) -> tuple[Any, ...]:
    tables = set(inspect(connection).get_table_names())
    schema = tuple(
        tuple(row)
        for row in connection.execute(
            text("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
        )
    )
    versions = (
        tuple(connection.execute(text("SELECT version_num FROM alembic_version ORDER BY 1")))
        if "alembic_version" in tables
        else ()
    )
    releases = (
        tuple(connection.execute(text("SELECT * FROM catalog_releases ORDER BY content_hash")))
        if "catalog_releases" in tables
        else ()
    )
    return schema, versions, releases


async def _snapshot(runtime: DatabaseRuntime) -> tuple[Any, ...]:
    async with runtime.engine.connect() as connection:
        return await connection.run_sync(_schema_snapshot)


async def _insert_sentinel(runtime: DatabaseRuntime) -> None:
    async with runtime.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO catalog_releases "
                "(content_hash, content_version, snapshot_json, recorded_at) "
                "VALUES (:hash, :version, :snapshot, :recorded_at)"
            ),
            {
                "hash": SENTINEL_HASH,
                "version": "del04-historical-sentinel",
                "snapshot": '{"historical":true}',
                "recorded_at": "2026-09-08 00:00:00.000000",
            },
        )


@pytest.mark.asyncio
async def test_del_04_fresh_upgrade_is_schema_only_and_matches_all_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_create_all(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        pytest.fail("deployed upgrades must execute frozen revisions, not metadata.create_all")

    monkeypatch.setattr(Base.metadata, "create_all", forbidden_create_all)
    runtime = _runtime(tmp_path / "fresh.db")
    try:
        assert await upgrade_database(runtime) == HEAD_REVISION == "0005"
        async with runtime.engine.connect() as connection:
            tables = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
            assert len(ALL_TABLES) == 45
            assert tables == ALL_TABLES | {"alembic_version"}
            assert set(Base.metadata.tables) == ALL_TABLES
            assert await connection.run_sync(schema_matches_metadata)
            status = await connection.run_sync(inspect_migration)
            assert status.revision == status.head == "0005"
            assert status.current and status.schema_matches
            for table_name in sorted(ALL_TABLES):
                count = await connection.scalar(
                    select(func.count()).select_from(Base.metadata.tables[table_name])
                )
                assert count == 0, f"migration inserted application data into {table_name}"
            assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("previous", "target"),
    [(None, "0001"), ("0001", "0002"), ("0002", "0003"), ("0003", "0004"), ("0004", "0005")],
)
@pytest.mark.asyncio
async def test_del_04_each_revision_upgrades_its_predecessor_and_preserves_data(
    tmp_path: Path, previous: str | None, target: str
) -> None:
    runtime = _runtime(tmp_path / "incremental.db")
    try:
        if previous is not None:
            assert await upgrade_database(runtime, previous) == previous
            await _insert_sentinel(runtime)
        before = await _snapshot(runtime)
        assert await upgrade_database(runtime, target) == target
        async with runtime.engine.connect() as connection:
            expected = set().union(
                *(tables for revision, tables in REVISION_TABLES.items() if revision <= target)
            )
            actual = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
            assert actual == expected | {"alembic_version"}
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == target
            assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []
        if previous is not None:
            assert (await _snapshot(runtime))[2] == before[2]
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_del_04_repeated_head_upgrade_performs_no_schema_or_data_write(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "repeat.db")
    statements: list[str] = []

    def capture(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(statement.split(maxsplit=1)[0].upper())

    try:
        await upgrade_database(runtime)
        await _insert_sentinel(runtime)
        before = await _snapshot(runtime)
        event.listen(runtime.engine.sync_engine, "before_cursor_execute", capture)
        try:
            assert await upgrade_database(runtime) == "0005"
        finally:
            event.remove(runtime.engine.sync_engine, "before_cursor_execute", capture)
        assert statements and set(statements) <= {"SELECT", "PRAGMA", "BEGIN"}
        assert await _snapshot(runtime) == before
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("previous", "fault_table"), [(None, "catalog_releases"), ("0001", "runs")]
)
@pytest.mark.asyncio
async def test_del_04_ddl_failure_rolls_back_schema_version_and_existing_data(
    tmp_path: Path, previous: str | None, fault_table: str
) -> None:
    runtime = _runtime(tmp_path / "rollback.db")
    injected = False

    def fail_after_ddl(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        nonlocal injected
        del connection, cursor, parameters, context, executemany
        if statement.lstrip().startswith(f"CREATE TABLE {fault_table} "):
            injected = True
            raise RuntimeError("del04-injected-after-create-table")

    try:
        if previous is not None:
            await upgrade_database(runtime, previous)
            await _insert_sentinel(runtime)
        before = await _snapshot(runtime)
        event.listen(runtime.engine.sync_engine, "after_cursor_execute", fail_after_ddl)
        try:
            with pytest.raises(RuntimeError, match="del04-injected-after-create-table"):
                await upgrade_database(runtime)
        finally:
            event.remove(runtime.engine.sync_engine, "after_cursor_execute", fail_after_ddl)
        assert injected
        assert await _snapshot(runtime) == before
        # The same durable database remains recoverable after the fault is removed.
        assert await upgrade_database(runtime) == "0005"
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("unversioned", "migration_unversioned_schema"),
        ("unknown_head", "migration_revision_unknown"),
        ("multiple_heads", "migration_multiple_heads"),
        ("downgrade", "migration_downgrade_unsupported"),
        ("missing_index", "migration_schema_drift"),
        ("extra_column", "migration_schema_drift"),
    ],
)
@pytest.mark.asyncio
async def test_del_04_unsafe_upgrade_states_are_rejected_without_mutation(
    tmp_path: Path, case: str, code: str
) -> None:
    runtime = _runtime(tmp_path / "refused.db")
    try:
        if case == "unversioned":
            async with runtime.engine.begin() as connection:
                await connection.execute(text("CREATE TABLE local_data (id INTEGER PRIMARY KEY)"))
                await connection.execute(text("INSERT INTO local_data (id) VALUES (123)"))
        else:
            await upgrade_database(runtime)
            await _insert_sentinel(runtime)
            mutations = {
                "unknown_head": "UPDATE alembic_version SET version_num = 'unknown'",
                "multiple_heads": "INSERT INTO alembic_version (version_num) VALUES ('0004')",
                "missing_index": "DROP INDEX ix_runs_state_created_at",
                "extra_column": "ALTER TABLE runs ADD COLUMN unauthorized VARCHAR(20)",
            }
            if case in mutations:
                async with runtime.engine.begin() as connection:
                    await connection.execute(text(mutations[case]))
        before = await _snapshot(runtime)
        with pytest.raises(DatabaseMigrationError) as caught:
            await upgrade_database(runtime, "0004" if case == "downgrade" else "head")
        assert caught.value.code == code
        assert await _snapshot(runtime) == before
        if case == "unversioned":
            async with runtime.engine.connect() as connection:
                assert (
                    await connection.execute(text("SELECT id FROM local_data"))
                ).scalar_one() == 123
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_del_04_direct_alembic_destructive_downgrade_fails_before_any_change(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "no-downgrade.db")
    try:
        await upgrade_database(runtime)
        await _insert_sentinel(runtime)
        before = await _snapshot(runtime)

        def downgrade(connection: Connection) -> None:
            config = migration_config()
            config.attributes["connection"] = connection
            command.downgrade(config, "0004")

        with pytest.raises(RuntimeError, match="Destructive downgrades are unsupported"):
            async with runtime.engine.begin() as connection:
                await connection.run_sync(downgrade)
        assert await _snapshot(runtime) == before
    finally:
        await runtime.dispose()


def test_del_04_revisions_are_frozen_literal_schema_operations() -> None:
    script = ScriptDirectory.from_config(migration_config())
    revisions = tuple(reversed(tuple(script.walk_revisions())))
    assert tuple(revision.revision for revision in revisions) == tuple(REVISION_TABLES)
    for revision in revisions:
        tree = ast.parse(Path(revision.path).read_text())
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            str(node.module).split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert imports <= {"__future__", "alembic", "sqlalchemy"}
        assert not any(
            isinstance(node, ast.Attribute)
            and node.attr in {"metadata", "create_all", "drop_all", "bulk_insert", "execute"}
            for node in ast.walk(tree)
        )
        tables = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
            and node.func.attr == "create_table"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        assert tables == REVISION_TABLES[revision.revision]


def test_del_04_postgresql_offline_ddl_compilation_is_not_runtime_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_connection(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        pytest.fail("offline DDL compilation must not open a database connection")

    monkeypatch.setattr(db, "create_database_runtime", forbid_connection)
    output = io.StringIO()
    config = migration_config()
    config.output_buffer = output
    config.set_main_option(
        "sqlalchemy.url", "postgresql+asyncpg://offline:unused@example.invalid/ddl_only"
    )
    command.upgrade(config, "head", sql=True)
    ddl = output.getvalue()
    created = set(re.findall(r"CREATE TABLE ([a-z_]+)\s*\(", ddl))
    assert created == ALL_TABLES | {"alembic_version"}
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "FOREIGN KEY" in ddl and "ON DELETE RESTRICT" in ddl
    assert set(re.findall(r"INSERT INTO ([a-z_]+)", ddl)) == {"alembic_version"}
    assert "CREATE INDEX ix_runs_state_created_at" in ddl


@pytest.mark.parametrize(
    ("owner", "claimed", "expires"),
    [
        ("worker", "2026-09-08 00:00:00", None),
        (None, "2026-09-08 00:00:00", "2026-09-08 00:01:00"),
        ("worker", None, "2026-09-08 00:01:00"),
        ("worker", "2026-09-08 00:01:00", "2026-09-08 00:00:00"),
    ],
)
async def test_del_04_maintenance_lease_requires_all_fields_and_forward_expiry(
    tmp_path: Path, owner: str | None, claimed: str | None, expires: str | None
) -> None:
    runtime = _runtime(tmp_path / "maintenance.db")
    try:
        await upgrade_database(runtime)
        with pytest.raises(IntegrityError, match="ck_maintenance_lease_complete"):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO maintenance_runs "
                        "(id, occurrence_key, job_kind, state, started_at, counts_json, version, "
                        "lease_owner, lease_claimed_at, lease_expires_at) VALUES "
                        "('maintenance.test', 'occurrence.test', 'retention', 'running', "
                        "'2026-09-08 00:00:00', '{}', 1, :owner, :claimed, :expires)"
                    ),
                    {"owner": owner, "claimed": claimed, "expires": expires},
                )
    finally:
        await runtime.dispose()
