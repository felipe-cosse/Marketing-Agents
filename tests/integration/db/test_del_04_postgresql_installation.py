"""DEL-04: optional PostgreSQL native installation and read-only key readiness."""

from __future__ import annotations

import base64
from pathlib import Path
from stat import S_IMODE
from typing import Any

import pytest
from marketing_agents.application.ports.readiness import ReadinessCheckName, ReadinessCode
from marketing_agents.config import Settings
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db import local_installation as installation
from marketing_agents.infrastructure.db.local_installation import (
    initialize_local_secret,
    migrate_local_database,
    verify_local_installation,
)
from marketing_agents.infrastructure.db.migrations import (
    DatabaseMigrationError,
    expected_tables,
    upgrade_database,
)
from marketing_agents.infrastructure.readiness import LocalReadinessProbe
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.digest_key import DigestKeyError, digest_key_fingerprint
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.support.postgresql_runtime import pg_database_url as pg_database_url

CATALOG_ROOT = Path(__file__).resolve().parents[3] / "catalog" / "v1"


@pytest.fixture(scope="module")
def catalog() -> CompiledCatalog:
    return compile_catalog(CATALOG_ROOT)


def _settings(database_url: str, key_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        catalog_root=CATALOG_ROOT,
        marketing_agents_digest_key_path=key_path,
    )


async def _snapshot(database_url: str) -> dict[str, tuple[str, ...]]:
    runtime = create_database_runtime(database_url)
    try:
        async with runtime.engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            return {
                table: tuple(
                    sorted(
                        repr(tuple(row))
                        for row in await connection.execute(text(f'SELECT * FROM "{table}"'))
                    )
                )
                for table in sorted(tables)
            }
    finally:
        await runtime.dispose()


async def _seed(database_url: str, catalog: CompiledCatalog) -> None:
    runtime = create_database_runtime(database_url)
    try:
        await seed_catalog(catalog, runtime, CroniterRecurrenceCalculator())
    finally:
        await runtime.dispose()


def _files(directory: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(path.relative_to(directory)): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            S_IMODE(path.stat().st_mode),
        )
        for path in directory.rglob("*")
        if path.is_file()
    }


def _capture_reads(statements: list[str]) -> Any:
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
        if statements[-1] == "EXPLAIN":
            assert statement.startswith("EXPLAIN (VERBOSE, FORMAT JSON, COSTS FALSE) SELECT ")
            assert statement.endswith(" LIMIT 0")

    return capture


@pytest.mark.asyncio
async def test_del_04_postgresql_native_installation_is_paired_ready_and_read_only(
    pg_database_url: str, tmp_path: Path, catalog: CompiledCatalog
) -> None:
    key_path = tmp_path / "private" / "digest.key"
    key = await initialize_local_secret(pg_database_url, key_path)
    assert S_IMODE(key_path.stat().st_mode) == 0o600
    assert S_IMODE(key_path.parent.stat().st_mode) == 0o700
    assert await _snapshot(pg_database_url) == {}
    with pytest.raises(DatabaseMigrationError, match="local_installation_not_migrated"):
        await verify_local_installation(pg_database_url, key_path)
    assert await migrate_local_database(pg_database_url, key_path) == "0005"
    empty = await _snapshot(pg_database_url)
    assert set(empty) == expected_tables("0005") | {"alembic_version"}
    assert len(empty["local_runtime_identity"]) == 1
    assert digest_key_fingerprint(key) in empty["local_runtime_identity"][0]
    assert empty["agent_instance_configs"] == ()
    await _seed(pg_database_url, catalog)
    before = await _snapshot(pg_database_url)
    files = _files(tmp_path)
    assert await migrate_local_database(pg_database_url, key_path) == "0005"

    statements: list[str] = []
    capture = _capture_reads(statements)

    def reject_commit(connection: Any) -> None:
        del connection
        pytest.fail("native verification and readiness must not commit")

    event.listen(Engine, "before_cursor_execute", capture)
    event.listen(Engine, "commit", reject_commit)
    try:
        assert await initialize_local_secret(pg_database_url, key_path) == key
        await verify_local_installation(pg_database_url, key_path)
        report = await LocalReadinessProbe(_settings(pg_database_url, key_path)).check()
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
        event.remove(Engine, "commit", reject_commit)
    assert report.ready
    assert statements and set(statements) <= {"SELECT", "SHOW", "EXPLAIN"}
    assert await _snapshot(pg_database_url) == before
    assert _files(tmp_path) == files


@pytest.mark.parametrize(
    "problem",
    (
        "missing",
        "mismatched",
        "file_permissions",
        "directory_permissions",
        "identity_deleted",
        "identity_fingerprint",
    ),
)
@pytest.mark.asyncio
async def test_del_04_postgresql_rejects_unpaired_or_unsafe_identity_without_writes(
    pg_database_url: str, tmp_path: Path, catalog: CompiledCatalog, problem: str
) -> None:
    key_path = tmp_path / "private" / "digest.key"
    await migrate_local_database(pg_database_url, key_path)
    await _seed(pg_database_url, catalog)
    if problem == "missing":
        key_path.unlink()
    elif problem == "mismatched":
        key_path.write_bytes(base64.urlsafe_b64encode(bytes([19]) * 32) + b"\n")
    elif problem == "file_permissions":
        key_path.chmod(0o640)
    elif problem == "directory_permissions":
        key_path.parent.chmod(0o750)
    else:
        mutations = {
            "identity_deleted": "DELETE FROM local_runtime_identity",
            "identity_fingerprint": (
                "UPDATE local_runtime_identity SET key_fingerprint = '"
                + "digest-key-fingerprint-v1:"
                + "0" * 64
                + "'"
            ),
        }
        runtime = create_database_runtime(pg_database_url)
        try:
            async with runtime.engine.begin() as connection:
                await connection.execute(text(mutations[problem]))
        finally:
            await runtime.dispose()
    before = await _snapshot(pg_database_url)
    files = _files(tmp_path)
    statements: list[str] = []
    capture = _capture_reads(statements)
    event.listen(Engine, "before_cursor_execute", capture)
    try:
        for operation in (
            initialize_local_secret,
            migrate_local_database,
            verify_local_installation,
        ):
            with pytest.raises(DigestKeyError):
                await operation(pg_database_url, key_path)
        report = await LocalReadinessProbe(_settings(pg_database_url, key_path)).check()
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert not report.ready
    assert {check.name: check.code for check in report.checks}[ReadinessCheckName.DATABASE] == (
        ReadinessCode.DATABASE_UNAVAILABLE
    )
    assert set(statements) <= {"SELECT", "SHOW", "EXPLAIN"}
    assert await _snapshot(pg_database_url) == before
    assert _files(tmp_path) == files


@pytest.mark.asyncio
async def test_del_04_postgresql_identity_format_rejects_unsupported_version(
    pg_database_url: str, tmp_path: Path, catalog: CompiledCatalog
) -> None:
    key_path = tmp_path / "private" / "digest.key"
    await migrate_local_database(pg_database_url, key_path)
    await _seed(pg_database_url, catalog)
    before = await _snapshot(pg_database_url)
    runtime = create_database_runtime(pg_database_url)
    try:
        with pytest.raises(IntegrityError):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text("UPDATE local_runtime_identity SET format_version = 2")
                )
    finally:
        await runtime.dispose()
    assert await _snapshot(pg_database_url) == before
    assert (await LocalReadinessProbe(_settings(pg_database_url, key_path)).check()).ready


@pytest.mark.asyncio
async def test_del_04_postgresql_does_not_adopt_unrelated_unversioned_database(
    pg_database_url: str, tmp_path: Path
) -> None:
    key_path = tmp_path / "private" / "digest.key"
    runtime = create_database_runtime(pg_database_url)
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE unrelated_state (value TEXT NOT NULL)"))
            await connection.execute(text("INSERT INTO unrelated_state VALUES ('preserve me')"))
    finally:
        await runtime.dispose()
    before = await _snapshot(pg_database_url)
    for operation in (initialize_local_secret, migrate_local_database):
        with pytest.raises(DatabaseMigrationError, match="migration_unversioned_schema"):
            await operation(pg_database_url, key_path)
    assert await _snapshot(pg_database_url) == before
    assert not key_path.parent.exists()


@pytest.mark.parametrize(
    "definition",
    (
        "CREATE VIEW unrelated_state AS SELECT 'preserve me'::text AS value",
        "CREATE MATERIALIZED VIEW unrelated_state AS SELECT 'preserve me'::text AS value",
        "CREATE SEQUENCE unrelated_state START 17",
    ),
)
@pytest.mark.asyncio
async def test_del_04_postgresql_does_not_adopt_unversioned_non_table_objects(
    pg_database_url: str, tmp_path: Path, definition: str
) -> None:
    key_path = tmp_path / "private" / "digest.key"
    runtime = create_database_runtime(pg_database_url)
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text(definition))
        async with runtime.engine.connect() as connection:
            before = (await connection.execute(text("SELECT * FROM unrelated_state"))).all()
        for operation in (initialize_local_secret, migrate_local_database):
            with pytest.raises(DatabaseMigrationError, match="migration_unversioned_schema"):
                await operation(pg_database_url, key_path)
        async with runtime.engine.connect() as connection:
            assert (await connection.execute(text("SELECT * FROM unrelated_state"))).all() == before
        assert await _snapshot(pg_database_url) == {}
        assert not key_path.parent.exists()
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_del_04_postgresql_versioned_schema_cannot_create_replacement_key(
    pg_database_url: str, tmp_path: Path
) -> None:
    key_path = tmp_path / "private" / "digest.key"
    runtime = create_database_runtime(pg_database_url)
    try:
        await upgrade_database(runtime)
    finally:
        await runtime.dispose()
    before = await _snapshot(pg_database_url)
    with pytest.raises(DigestKeyError):
        await initialize_local_secret(pg_database_url, key_path)
    with pytest.raises(DigestKeyError):
        await migrate_local_database(pg_database_url, key_path)
    assert await _snapshot(pg_database_url) == before
    assert not key_path.parent.exists()


@pytest.mark.asyncio
async def test_del_04_postgresql_versioned_database_rejects_extra_tables(
    pg_database_url: str, tmp_path: Path
) -> None:
    key_path = tmp_path / "private" / "digest.key"
    await migrate_local_database(pg_database_url, key_path)
    runtime = create_database_runtime(pg_database_url)
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE unrelated_state (value TEXT NOT NULL)"))
    finally:
        await runtime.dispose()
    before = await _snapshot(pg_database_url)
    files = _files(tmp_path)
    for operation in (initialize_local_secret, migrate_local_database, verify_local_installation):
        with pytest.raises(DatabaseMigrationError, match="migration_schema_drift"):
            await operation(pg_database_url, key_path)
    assert await _snapshot(pg_database_url) == before
    assert _files(tmp_path) == files


@pytest.mark.asyncio
async def test_del_04_postgresql_initialization_preflight_is_enforced_read_only(
    pg_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "private" / "digest.key"

    def accidental_write(connection: Connection) -> str | None:
        assert connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on"
        connection.exec_driver_sql("CREATE TABLE forbidden_preflight_write (value INTEGER)")
        return None

    monkeypatch.setattr(installation, "_stored_identity", accidental_write)
    with pytest.raises(DBAPIError):
        await initialize_local_secret(pg_database_url, key_path)
    assert await _snapshot(pg_database_url) == {}
    assert not key_path.parent.exists()
