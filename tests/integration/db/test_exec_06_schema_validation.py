"""EXEC-06: bounded reflection optimizations retain exact schema-check semantics."""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import pytest
from marketing_agents.infrastructure.db import Base, create_database_runtime, schema
from marketing_agents.infrastructure.db.migrations import upgrade_database
from sqlalchemy import CheckConstraint, Table, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

from tests.integration.db.test_run_06_artifact_persistence import _prepare_run

# Freeze the previous implementation as the differential oracle. In particular,
# unmatched delimiters and quoted SQL literals must not acquire new semantics.
_LEGACY_SQL_TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^\]]*\]|\s+|.", re.S)


def _legacy_normalized_sql(value: object | None) -> str | None:
    if value is None:
        return None
    tokens: list[str] = []
    for match in _LEGACY_SQL_TOKEN.finditer(str(value)):
        token = match.group()
        if token.startswith("'"):
            tokens.append(token)
        elif token.startswith(('"', "`", "[")):
            tokens.append(token[1:-1].casefold())
        elif not token.isspace():
            tokens.append(token.casefold())
    return "".join(tokens)


def _actual_sql_fragments(connection: Connection) -> list[object | None]:
    inspector = inspect(connection)
    fragments: list[object | None] = []
    for table_name, table in Base.metadata.tables.items():
        fragments.extend(
            str(
                constraint.sqltext.compile(
                    dialect=connection.dialect,
                    compile_kwargs={"literal_binds": True, "include_table": False},
                )
            )
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        )
        fragments.extend(item["sqltext"] for item in inspector.get_check_constraints(table_name))
        fragments.extend(item.get("default") for item in inspector.get_columns(table_name))
        fragments.extend(
            getattr(column.server_default, "arg", column.server_default) for column in table.columns
        )
        fragments.extend(
            index.dialect_options[connection.dialect.name].get("where") for index in table.indexes
        )
        fragments.extend(
            item.get("dialect_options", {}).get(f"{connection.dialect.name}_where")
            for item in inspector.get_indexes(table_name)
        )
    fragments.extend(connection.execute(text("SELECT sql FROM sqlite_master")).scalars())
    return fragments


@pytest.mark.asyncio
async def test_grouped_normalization_matches_legacy_for_real_and_adversarial_sql(
    tmp_path: Path,
) -> None:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'normalization.db'}")
    try:
        await upgrade_database(runtime)
        async with runtime.engine.connect() as connection:
            fragments = await connection.run_sync(_actual_sql_fragments)
        assert len(fragments) > 500
        legacy_count = sum(len(_LEGACY_SQL_TOKEN.findall(str(value))) for value in fragments)
        grouped_count = sum(len(schema._SQL_TOKEN.findall(str(value))) for value in fragments)
        assert grouped_count < legacy_count
        atoms = (
            "AbC",
            "EQUALS",
            "=",
            "<>",
            ";",
            "\n",
            "\t",
            "\u2003",
            "\u00a0",
            "'Case Sensitive'",
            "'a''b'",
            '"Quoted Name"',
            '"a""b"',
            "`Quoted Name`",
            "[Name Space]",
            "'",
            '"',
            "`",
            "[",
            "]",
            "Σςσ",
            "İß",
            "-- comment",
            "/* Case */",
            "\\",
            "\x00",
        )
        generator = random.Random(6016)
        fragments.extend((None, "", 0, -12, *atoms))
        fragments.extend(
            "".join(generator.choices(atoms, k=generator.randrange(1, 20))) for _ in range(10_000)
        )
        for value in fragments:
            assert schema._normalized_sql(value) == _legacy_normalized_sql(value), repr(value)
        assert schema._normalized_sql("field = 'A B'") != schema._normalized_sql("field = 'ab'")
        assert schema._normalized_sql("field = 'a''B'") == "field='a''B'"
        assert schema._normalized_sql('"FIELD" = 1') == schema._normalized_sql("field = 1")
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_schema_validation_uses_one_new_inspector_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'inspectors.db'}")
    inspectors: list[Inspector] = []

    def fresh_inspector(connection: Connection) -> Inspector:
        inspector = inspect(connection)
        inspectors.append(inspector)
        return inspector

    try:
        await upgrade_database(runtime)
        monkeypatch.setattr(schema, "inspect", fresh_inspector)
        async with runtime.engine.connect() as connection:
            assert await connection.run_sync(schema.schema_matches_metadata)
            assert len(inspectors) == 1
            assert await connection.run_sync(schema.schema_matches_metadata)
            assert len(inspectors) == 2
            assert inspectors[0] is not inspectors[1]
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("other_connection", [False, True], ids=["same", "other"])
@pytest.mark.parametrize(
    ("change", "restore"),
    [
        (
            ("CREATE TABLE unexpected_table (id INTEGER)",),
            ("DROP TABLE unexpected_table",),
        ),
        (
            (
                "DROP INDEX ix_runs_state_created_at",
                "CREATE INDEX ix_runs_state_created_at ON runs (state, created_at) "
                "WHERE state = 'completed'",
            ),
            (
                "DROP INDEX ix_runs_state_created_at",
                "CREATE INDEX ix_runs_state_created_at ON runs (state, created_at)",
            ),
        ),
        (
            ("ALTER TABLE runs RENAME COLUMN state TO unexpected_state",),
            ("ALTER TABLE runs RENAME COLUMN unexpected_state TO state",),
        ),
    ],
    ids=["table-inventory", "index-predicate", "column"],
)
@pytest.mark.asyncio
async def test_later_validation_observes_schema_drift_and_restoration(
    tmp_path: Path,
    other_connection: bool,
    change: tuple[str, ...],
    restore: tuple[str, ...],
) -> None:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'drift.db'}")
    try:
        await upgrade_database(runtime)
        async with runtime.engine.connect() as reader:
            assert await reader.run_sync(schema.schema_matches_metadata)
            await reader.rollback()
            if other_connection:
                async with runtime.engine.begin() as writer:
                    assert writer is not reader
                    for statement in change:
                        await writer.execute(text(statement))
            else:
                for statement in change:
                    await reader.execute(text(statement))
                await reader.commit()
            assert not await reader.run_sync(schema.schema_matches_metadata)
            await reader.rollback()
            if other_connection:
                async with runtime.engine.begin() as writer:
                    for statement in restore:
                        await writer.execute(text(statement))
            else:
                for statement in restore:
                    await reader.execute(text(statement))
                await reader.commit()
            assert await reader.run_sync(schema.schema_matches_metadata)
    finally:
        await runtime.dispose()


def _audit_definition(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        "primary_key": inspector.get_pk_constraint("audit_events"),
        "unique": sorted(inspector.get_unique_constraints("audit_events"), key=repr),
        "foreign_keys": sorted(inspector.get_foreign_keys("audit_events"), key=repr),
        "indexes": sorted(inspector.get_indexes("audit_events"), key=repr),
        "checks": {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints("audit_events")
        },
    }


def _database_rows(connection: Connection) -> dict[str, tuple[tuple[Any, ...], ...]]:
    return {
        name: tuple(
            sorted(
                (tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{name}"')),
                key=repr,
            )
        )
        for name in inspect(connection).get_table_names()
        if name not in {"alembic_version", "retention_details"}
    }


@pytest.mark.asyncio
async def test_nonrecursive_audit_batch_preserves_populated_rows_keys_and_ddl(
    tmp_path: Path,
) -> None:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'populated.db'}")
    reflected_tables: set[str] = set()
    statements: list[str] = []

    def reflected(inspector: Inspector, table: Table, column_info: dict[str, Any]) -> None:
        del inspector, column_info
        reflected_tables.add(table.name)

    def executed(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(statement)

    try:
        await upgrade_database(runtime, revision="0010")
        await _prepare_run(runtime, "migration-retention", dependent_steps=True)
        async with runtime.engine.connect() as connection:
            before = await connection.run_sync(_audit_definition)
            rows = await connection.run_sync(_database_rows)
            unchanged_ddl = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                            "AND name NOT IN "
                            "('audit_events', 'maintenance_runs', 'alembic_version')"
                        )
                    )
                ).all()
            )
        assert len(rows["audit_events"]) > 1
        assert rows["run_steps"] and before["foreign_keys"]
        event.listen(Table, "column_reflect", reflected)
        event.listen(runtime.engine.sync_engine, "before_cursor_execute", executed)
        try:
            assert await upgrade_database(runtime) == "0011"
        finally:
            event.remove(runtime.engine.sync_engine, "before_cursor_execute", executed)
            event.remove(Table, "column_reflect", reflected)
        assert reflected_tables == {"audit_events"}
        async with runtime.engine.connect() as connection:
            after = await connection.run_sync(_audit_definition)
            assert await connection.run_sync(_database_rows) == rows
            assert await connection.run_sync(schema.schema_matches_metadata)
            assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []
            current_ddl = dict(
                (await connection.execute(text("SELECT name, sql FROM sqlite_master"))).all()
            )
        for name, value in unchanged_ddl.items():
            assert current_ddl[name] == value
        for field in ("primary_key", "unique", "foreign_keys", "indexes"):
            assert after[field] == before[field]
        changed_checks = {"ck_audit_events_aggregate_links", "ck_audit_events_timeline_scope"}
        assert set(after["checks"]) == set(before["checks"])
        for name, condition in before["checks"].items():
            if name in changed_checks:
                assert after["checks"][name] != condition
                assert "maintenance_run" in after["checks"][name]
            else:
                assert after["checks"][name] == condition
        ddl = [
            " ".join(statement.split())
            for statement in statements
            if statement.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP "))
        ]
        table_ddl = [
            statement for statement in ddl if re.match(r"(?:CREATE|ALTER|DROP) TABLE", statement)
        ]
        assert len(table_ddl) == 5
        assert any(
            statement.startswith("CREATE TABLE retention_details") for statement in table_ddl
        )
        assert any(
            statement.startswith("CREATE TABLE _alembic_tmp_audit_events")
            for statement in table_ddl
        )
        assert "DROP TABLE audit_events" in table_ddl
        assert "ALTER TABLE _alembic_tmp_audit_events RENAME TO audit_events" in table_ddl
        assert any(
            statement.startswith("ALTER TABLE maintenance_runs ADD COLUMN scope_json")
            for statement in table_ddl
        )
    finally:
        await runtime.dispose()
