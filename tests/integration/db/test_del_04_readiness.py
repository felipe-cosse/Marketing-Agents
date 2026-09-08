"""DEL-04: deployed-head, exact seed parity, and strictly read-only readiness."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.application.ports.readiness import (
    ReadinessCheckName,
    ReadinessCheckStatus,
    ReadinessCode,
    ReadinessReport,
)
from marketing_agents.config import Settings
from marketing_agents.infrastructure import readiness
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.catalog.seed import CatalogSeedError, seed_catalog
from marketing_agents.infrastructure.db import (
    DatabaseRuntime,
    InstanceConfigurationSQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.db.migrations import upgrade_database
from marketing_agents.infrastructure.db.schema import _normalized_sql, schema_matches_metadata
from marketing_agents.infrastructure.readiness import LocalReadinessProbe
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

CATALOG_ROOT = Path(__file__).resolve().parents[3] / "catalog" / "v1"


@pytest.fixture(scope="module")
def catalog() -> CompiledCatalog:
    return compile_catalog(CATALOG_ROOT)


def _settings(path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{path}",
        catalog_root=CATALOG_ROOT,
        marketing_agents_digest_key_path=path.parent / "secrets" / "digest.key",
    )


async def _seeded_runtime(path: Path, catalog: CompiledCatalog) -> DatabaseRuntime:
    runtime = create_database_runtime(_settings(path).database_url)
    try:
        settings = _settings(path)
        assert (
            await migrate_local_database(
                settings.database_url, settings.marketing_agents_digest_key_path
            )
            == "0005"
        )
        await seed_catalog(catalog, runtime, CroniterRecurrenceCalculator())
    except BaseException:
        await runtime.dispose()
        raise
    return runtime


def _codes(report: ReadinessReport) -> dict[ReadinessCheckName, ReadinessCode]:
    return {check.name: check.code for check in report.checks}


def _snapshot(path: Path) -> tuple[str, tuple[str, ...]]:
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        tuple(sorted(item.name for item in path.parent.iterdir())),
    )


@pytest.mark.asyncio
async def test_del_04_migrated_seeded_readiness_is_ready_and_read_only(
    tmp_path: Path, catalog: CompiledCatalog
) -> None:
    path = tmp_path / "ready #.db"
    runtime = await _seeded_runtime(path, catalog)
    await runtime.dispose()
    before = _snapshot(path)
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

    def reject_commit(connection: Any) -> None:
        del connection
        pytest.fail("readiness must not commit a database transaction")

    event.listen(Engine, "before_cursor_execute", capture)
    event.listen(Engine, "commit", reject_commit)
    try:
        first = await LocalReadinessProbe(_settings(path)).check()
        second = await LocalReadinessProbe(_settings(path)).check()
        async with AsyncClient(
            transport=ASGITransport(app=create_app(_settings(path))),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/health/ready")
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
        event.remove(Engine, "commit", reject_commit)

    assert first.ready and first == second
    assert {check.status for check in first.checks} == {ReadinessCheckStatus.READY}
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.headers["cache-control"] == "no-store"
    assert statements and set(statements) <= {"SELECT", "PRAGMA", "BEGIN"}
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP TABLE alembic_version",
        "UPDATE alembic_version SET version_num = '0004'",
        "INSERT INTO alembic_version (version_num) VALUES ('unrecognized-head')",
    ],
)
@pytest.mark.asyncio
async def test_del_04_revision_drift_is_not_repaired(
    tmp_path: Path, catalog: CompiledCatalog, mutation: str
) -> None:
    path = tmp_path / "revision-drift.db"
    runtime = await _seeded_runtime(path, catalog)
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text(mutation))
    finally:
        await runtime.dispose()
    before = _snapshot(path)

    report = await LocalReadinessProbe(_settings(path)).check()
    codes = _codes(report)
    assert not report.ready
    assert codes[ReadinessCheckName.DATABASE] is ReadinessCode.READY
    assert codes[ReadinessCheckName.WORKER_SCHEMA] is ReadinessCode.READY
    assert codes[ReadinessCheckName.MIGRATION] is (ReadinessCode.MIGRATION_VERIFICATION_UNAVAILABLE)
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE departments SET display_name = 'Changed locally'",
        "UPDATE agent_instance_configs SET integrity_digest = "
        "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
        "DELETE FROM agent_instance_configs",
    ],
)
@pytest.mark.asyncio
async def test_del_04_seed_drift_is_not_repaired_or_hidden(
    tmp_path: Path, catalog: CompiledCatalog, mutation: str
) -> None:
    path = tmp_path / "seed-drift.db"
    runtime = await _seeded_runtime(path, catalog)
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text(mutation))
    finally:
        await runtime.dispose()
    before = _snapshot(path)

    report = await LocalReadinessProbe(_settings(path)).check()
    codes = _codes(report)
    assert not report.ready
    assert codes[ReadinessCheckName.MIGRATION] is ReadinessCode.READY
    assert codes[ReadinessCheckName.WORKER_SCHEMA] is ReadinessCode.READY
    assert codes[ReadinessCheckName.CATALOG] is (
        ReadinessCode.CATALOG_SEED_VERIFICATION_UNAVAILABLE
    )
    assert _snapshot(path) == before


@pytest.mark.asyncio
async def test_del_04_valid_local_configuration_remains_ready(
    tmp_path: Path, catalog: CompiledCatalog
) -> None:
    path = tmp_path / "local-override.db"
    runtime = await _seeded_runtime(path, catalog)
    instance_id = catalog.instances[0].id
    try:
        factory = InstanceConfigurationSQLAlchemyUnitOfWorkFactory(runtime.session_factory)
        async with factory() as unit_of_work:
            previous = await unit_of_work.configurations.get(instance_id)
            assert previous is not None
            replacement = replace(
                previous,
                enabled=not previous.enabled,
                variant_label="Locally configured",
                configuration_revision=previous.configuration_revision + 1,
            )
            assert await unit_of_work.configurations.compare_and_swap(previous, replacement)
            await unit_of_work.commit()
    finally:
        await runtime.dispose()
    before = _snapshot(path)

    assert (await LocalReadinessProbe(_settings(path)).check()).ready
    assert _snapshot(path) == before
    restarted = create_database_runtime(_settings(path).database_url)
    try:
        factory = InstanceConfigurationSQLAlchemyUnitOfWorkFactory(restarted.session_factory)
        async with factory() as unit_of_work:
            assert await unit_of_work.configurations.get(instance_id) == replacement
    finally:
        await restarted.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "CREATE TABLE unexpected_table (id INTEGER PRIMARY KEY)",
        "DROP INDEX ix_runs_state_created_at",
    ],
)
@pytest.mark.asyncio
async def test_del_04_schema_drift_fails_worker_and_migration_readiness(
    tmp_path: Path, catalog: CompiledCatalog, mutation: str
) -> None:
    path = tmp_path / "schema-drift.db"
    runtime = await _seeded_runtime(path, catalog)
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text(mutation))
            assert not await connection.run_sync(schema_matches_metadata)
    finally:
        await runtime.dispose()
    before = _snapshot(path)

    codes = _codes(await LocalReadinessProbe(_settings(path)).check())
    assert codes[ReadinessCheckName.WORKER_SCHEMA] is ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE
    assert codes[ReadinessCheckName.MIGRATION] is (ReadinessCode.MIGRATION_VERIFICATION_UNAVAILABLE)
    assert _snapshot(path) == before


@pytest.mark.asyncio
async def test_del_04_seed_check_failure_details_are_not_exposed(
    tmp_path: Path, catalog: CompiledCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "safe-failure.db"
    runtime = await _seeded_runtime(path, catalog)
    await runtime.dispose()
    canary = "database-password-private-catalog-canary"

    async def fail(*args: Any, **kwargs: Any) -> None:
        assert kwargs["check"] is True
        raise CatalogSeedError("seed_persistence_failed", canary)

    monkeypatch.setattr(readiness, "seed_catalog", fail)
    report = await LocalReadinessProbe(_settings(path)).check()
    assert _codes(report)[ReadinessCheckName.CATALOG] is (
        ReadinessCode.CATALOG_SEED_VERIFICATION_UNAVAILABLE
    )
    assert canary not in repr(report)


@pytest.mark.asyncio
async def test_del_04_readiness_does_not_recreate_file_removed_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "removed.db"
    path.touch()
    original = readiness._sqlite_preflight

    def remove_after_preflight(url: Any) -> ReadinessCode | None:
        result = original(url)
        assert result is None
        path.unlink()
        return result

    monkeypatch.setattr(readiness, "_sqlite_preflight", remove_after_preflight)
    report = await LocalReadinessProbe(_settings(path)).check()
    assert _codes(report)[ReadinessCheckName.DATABASE] is ReadinessCode.DATABASE_UNAVAILABLE
    assert not path.exists()
    assert not tuple(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_del_04_readiness_connection_rejects_accidental_seed_writes(
    tmp_path: Path, catalog: CompiledCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "query-only.db"
    runtime = await _seeded_runtime(path, catalog)
    await runtime.dispose()
    before = _snapshot(path)
    attempted = False

    async def attempt_write(
        compiled: Any, read_runtime: DatabaseRuntime, recurrence: Any, *, check: bool
    ) -> None:
        nonlocal attempted
        del compiled, recurrence
        assert check
        attempted = True
        async with read_runtime.session_factory() as session:
            await session.execute(text("UPDATE departments SET display_name = 'Unwanted edit'"))
            await session.commit()

    monkeypatch.setattr(readiness, "seed_catalog", attempt_write)
    report = await LocalReadinessProbe(_settings(path)).check()
    assert attempted
    assert _codes(report)[ReadinessCheckName.CATALOG] is (
        ReadinessCode.CATALOG_SEED_VERIFICATION_UNAVAILABLE
    )
    assert _snapshot(path) == before


@pytest.mark.asyncio
async def test_del_04_partial_index_cannot_impersonate_the_full_mapped_index(
    tmp_path: Path, catalog: CompiledCatalog
) -> None:
    path = tmp_path / "partial-index.db"
    runtime = await _seeded_runtime(path, catalog)
    try:
        async with runtime.engine.begin() as connection:
            assert await connection.run_sync(schema_matches_metadata)
            await connection.execute(text("DROP INDEX ix_runs_state_created_at"))
            await connection.execute(
                text(
                    "CREATE INDEX ix_runs_state_created_at ON runs (state, created_at) "
                    "WHERE state = 'completed'"
                )
            )
            assert not await connection.run_sync(schema_matches_metadata)
    finally:
        await runtime.dispose()


def test_del_04_check_normalization_preserves_sql_literal_semantics() -> None:
    assert _normalized_sql(" value IN ('A B') ") == "valuein('A B')"
    assert _normalized_sql("value IN ('A B')") != _normalized_sql("value IN ('ab')")


@pytest.mark.asyncio
async def test_del_04_schema_only_library_database_is_not_deployment_ready(
    tmp_path: Path, catalog: CompiledCatalog
) -> None:
    path = tmp_path / "unpaired-library.db"
    settings = _settings(path)
    runtime = create_database_runtime(settings.database_url)
    try:
        await upgrade_database(runtime)
        await seed_catalog(catalog, runtime, CroniterRecurrenceCalculator())
    finally:
        await runtime.dispose()
    before = _snapshot(path)

    report = await LocalReadinessProbe(settings).check()
    codes = _codes(report)
    assert not report.ready
    assert codes[ReadinessCheckName.DATABASE] is ReadinessCode.DATABASE_UNAVAILABLE
    assert codes[ReadinessCheckName.MIGRATION] is ReadinessCode.READY
    assert codes[ReadinessCheckName.WORKER_SCHEMA] is ReadinessCode.READY
    assert not settings.marketing_agents_digest_key_path.exists()
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_key",
        "replaced_key",
        "public_key",
        "public_directory",
        "deleted_identity",
        "changed_fingerprint",
        "unknown_identity_version",
    ],
)
@pytest.mark.asyncio
async def test_del_04_native_identity_or_key_failure_is_not_ready_and_never_repaired(
    tmp_path: Path, catalog: CompiledCatalog, corruption: str
) -> None:
    path = tmp_path / "native.db"
    settings = _settings(path)
    key_path = settings.marketing_agents_digest_key_path
    runtime = await _seeded_runtime(path, catalog)
    try:
        if corruption == "missing_key":
            key_path.unlink()
        elif corruption == "replaced_key":
            key_path.write_bytes(base64.urlsafe_b64encode(b"x" * 32) + b"\n")
        elif corruption == "public_key":
            key_path.chmod(0o644)
        elif corruption == "public_directory":
            key_path.parent.chmod(0o755)
        else:
            async with runtime.engine.begin() as connection:
                if corruption == "deleted_identity":
                    await connection.execute(text("DELETE FROM local_runtime_identity"))
                elif corruption == "changed_fingerprint":
                    await connection.execute(
                        text("UPDATE local_runtime_identity SET key_fingerprint = :fingerprint"),
                        {"fingerprint": "digest-key-fingerprint-v1:" + "c" * 64},
                    )
                else:
                    await connection.execute(text("PRAGMA ignore_check_constraints=ON"))
                    await connection.execute(
                        text("UPDATE local_runtime_identity SET format_version = 2")
                    )
                    await connection.execute(text("PRAGMA ignore_check_constraints=OFF"))
    finally:
        await runtime.dispose()
    before = _snapshot(path)
    key_before = key_path.read_bytes() if key_path.exists() else None
    mode_before = key_path.stat().st_mode if key_path.exists() else None
    parent_mode_before = key_path.parent.stat().st_mode

    report = await LocalReadinessProbe(settings).check()
    assert not report.ready
    assert _codes(report)[ReadinessCheckName.DATABASE] is ReadinessCode.DATABASE_UNAVAILABLE
    assert _snapshot(path) == before
    assert (key_path.read_bytes() if key_path.exists() else None) == key_before
    assert (key_path.stat().st_mode if key_path.exists() else None) == mode_before
    assert key_path.parent.stat().st_mode == parent_mode_before
