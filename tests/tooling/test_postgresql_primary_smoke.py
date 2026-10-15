"""DEL-10: dialect-neutral evidence preserves SQLite and refuses unsafe reads."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from marketing_agents.infrastructure.db import (
    SQLAlchemyAuditRepository,
    SQLAlchemyReadOnlyUnitOfWorkFactory,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyUnitOfWork,
    SQLAlchemyUnitOfWorkError,
    SQLAlchemyWorkRepository,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.security.digest_key import load_or_create_digest_key
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from scripts import del_05_replay_fixture as replay
from scripts import del_05_runtime_smoke as smoke


async def test_primary_read_projection_factory_keeps_sqlite_snapshot_and_refuses_commit(
    tmp_path: Path,
):
    database = tmp_path / "local.db"
    key = tmp_path / "keys" / "digest.key"
    url = f"sqlite+aiosqlite:///{database}"
    await migrate_local_database(url, key)
    runtime = create_database_runtime(url)
    factories = SQLAlchemyRepositoryFactories(
        SQLAlchemyWorkRepository, SQLAlchemyRunRepository, SQLAlchemyAuditRepository
    )
    factory = SQLAlchemyReadOnlyUnitOfWorkFactory(runtime.session_factory, factories)
    try:
        before = await smoke.async_database_snapshot(url, key)
        async with factory() as unit:
            await unit._require_session().execute(
                text("UPDATE alembic_version SET version_num='0001'")
            )
            with pytest.raises(SQLAlchemyUnitOfWorkError, match="read-only"):
                await unit.commit()
        assert await smoke.async_database_snapshot(url, key) == before
        for read_only, write_intent in ((1, False), ("true", False), (True, True)):
            with pytest.raises(ValueError, match="read-only"):
                SQLAlchemyUnitOfWork(
                    runtime.session_factory,
                    factories,
                    read_only=read_only,
                    sqlite_write_intent=write_intent,
                )
    finally:
        await runtime.dispose()


async def test_primary_snapshot_sqlite_is_readonly_and_covers_all_rows_and_schema(tmp_path: Path):
    database = tmp_path / "local.db"
    key = tmp_path / "keys" / "digest.key"
    url = f"sqlite+aiosqlite:///{database}"
    await migrate_local_database(url, key)
    runtime = create_database_runtime(url)
    try:
        initial = await smoke.async_database_snapshot(url, key)
        assert smoke.database_snapshot(database, key) == initial
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE snapshot_sentinel (id INTEGER, value TEXT)")
            )
            await connection.execute(
                text("INSERT INTO snapshot_sentinel VALUES (2, 'two'), (1, 'one')")
            )
        first = await smoke.async_database_snapshot(url, key)
        assert first["logical_database_sha256"] != initial["logical_database_sha256"]
        async with runtime.engine.begin() as connection:
            await connection.execute(text("DELETE FROM snapshot_sentinel"))
            await connection.execute(
                text("INSERT INTO snapshot_sentinel VALUES (1, 'one'), (2, 'two')")
            )
        assert await smoke.async_database_snapshot(url, key) == first
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("CREATE INDEX sentinel_index ON snapshot_sentinel(value)")
            )
        indexed = await smoke.async_database_snapshot(url, key)
        assert indexed["logical_database_sha256"] != first["logical_database_sha256"]
        async with runtime.engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num='0001'"))
        changed = await smoke.async_database_snapshot(url, key)
        assert changed["logical_database_sha256"] != indexed["logical_database_sha256"]
        assert initial["counts"] == first["counts"] == indexed["counts"] == changed["counts"]
        detailed = await smoke.async_database_snapshot(url, key, include_table_digests=True)
        diagnostics = detailed.pop("table_digests")
        assert detailed == changed
        assert {"alembic_version", "snapshot_sentinel"} <= set(diagnostics)
        for value in diagnostics.values():
            assert set(value) == {"schema_sha256", "rows_sha256", "row_count"}
            assert len(value["schema_sha256"]) == len(value["rows_sha256"]) == 64
            assert type(value["row_count"]) is int
        engine = smoke.readonly_database_engine(url)
        try:
            async with engine.begin() as connection:
                with pytest.raises(DBAPIError):
                    await connection.execute(text("DELETE FROM snapshot_sentinel"))
        finally:
            await engine.dispose()
        assert await smoke.async_database_snapshot(url, key) == changed
    finally:
        await runtime.dispose()


async def test_primary_snapshot_never_creates_sqlite_or_replaces_key(tmp_path: Path):
    key = tmp_path / "keys" / "digest.key"
    load_or_create_digest_key(key, persistent_state_exists=False)
    original = key.read_bytes()
    missing = tmp_path / "missing.db"
    with pytest.raises(smoke.SmokeFailure, match="database_missing_or_unsafe"):
        await smoke.async_database_snapshot(missing, key)
    assert not missing.exists()
    database = tmp_path / "local.db"
    await migrate_local_database(f"sqlite+aiosqlite:///{database}", key)
    linked = tmp_path / "linked.db"
    linked.symlink_to(database)
    with pytest.raises(smoke.SmokeFailure, match="database_missing_or_unsafe"):
        await smoke.async_database_snapshot(linked, key)
    with pytest.raises(smoke.SmokeFailure, match="key_missing_or_unsafe"):
        await smoke.async_database_snapshot(database, tmp_path / "absent.key")
    assert not (tmp_path / "absent.key").exists()
    assert key.read_bytes() == original


async def test_primary_snapshot_constraint_order_is_not_schema_drift(tmp_path: Path):
    database = tmp_path / "local.db"
    key = tmp_path / "keys" / "digest.key"
    url = f"sqlite+aiosqlite:///{database}"
    await migrate_local_database(url, key)
    runtime = create_database_runtime(url)
    constraints = [
        "CONSTRAINT pk_snapshot_sentinel PRIMARY KEY (id)",
        "CONSTRAINT uq_snapshot_sentinel UNIQUE (value)",
        "CONSTRAINT ck_snapshot_id CHECK (id > 0)",
        "CONSTRAINT ck_snapshot_value CHECK (length(value) > 0)",
        "CONSTRAINT fk_snapshot_parent FOREIGN KEY (parent_id) "
        "REFERENCES snapshot_sentinel(id) ON DELETE CASCADE",
    ]
    try:
        hashes = []
        for definitions in (constraints, list(reversed(constraints))):
            async with runtime.engine.begin() as connection:
                await connection.execute(text("DROP TABLE IF EXISTS snapshot_sentinel"))
                await connection.execute(
                    text(
                        "CREATE TABLE snapshot_sentinel "
                        "(id INTEGER NOT NULL, value TEXT NOT NULL, parent_id INTEGER, "
                        + ", ".join(definitions)
                        + ")"
                    )
                )
                await connection.execute(
                    text("INSERT INTO snapshot_sentinel VALUES (1, 'one', NULL)")
                )
            hashes.append(await smoke.async_database_snapshot(url, key))
        assert hashes[0] == hashes[1]
        async with runtime.engine.begin() as connection:
            await connection.execute(text("DROP TABLE snapshot_sentinel"))
            await connection.execute(
                text(
                    "CREATE TABLE snapshot_sentinel "
                    "(id INTEGER NOT NULL, value TEXT NOT NULL, parent_id INTEGER, "
                    + ", ".join(constraints).replace("id > 0", "id >= 0")
                    + ")"
                )
            )
            await connection.execute(text("INSERT INTO snapshot_sentinel VALUES (1, 'one', NULL)"))
        changed = await smoke.async_database_snapshot(url, key)
        assert changed["logical_database_sha256"] != hashes[0]["logical_database_sha256"]
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE VIEW snapshot_hidden AS SELECT version_num FROM alembic_version",
        "CREATE TRIGGER snapshot_hidden AFTER UPDATE ON alembic_version BEGIN SELECT 1; END",
        "CREATE TABLE snapshot_hidden (id INTEGER PRIMARY KEY AUTOINCREMENT)",
    ],
)
async def test_primary_snapshot_refuses_uncovered_sqlite_views_triggers_and_sequences(
    tmp_path: Path, definition: str
):
    database = tmp_path / "local.db"
    key = tmp_path / "keys" / "digest.key"
    url = f"sqlite+aiosqlite:///{database}"
    await migrate_local_database(url, key)
    runtime = create_database_runtime(url)
    try:
        await smoke.async_database_snapshot(url, key)
        async with runtime.engine.begin() as connection:
            await connection.execute(text(definition))
        with pytest.raises(smoke.SmokeFailure, match="snapshot_unsupported_schema_objects"):
            await smoke.async_database_snapshot(url, key)
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents",
        "postgresql+asyncpg://marketing_agents@remote.example:5432/marketing_agents?host=/var/run/marketing-agents-postgres",
        "postgresql+asyncpg://other@localhost:5432/marketing_agents?host=/var/run/marketing-agents-postgres",
        "postgresql+asyncpg://marketing_agents:password@localhost:5432/marketing_agents?host=/var/run/marketing-agents-postgres",
        "postgresql+asyncpg://marketing_agents:@localhost:5432/marketing_agents?host=/var/run/marketing-agents-postgres",
        "postgresql+asyncpg://marketing_agents@localhost:5433/marketing_agents?host=/var/run/marketing-agents-postgres",
        "postgresql+asyncpg://marketing_agents@localhost:5432/other?host=/var/run/marketing-agents-postgres",
        "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents?host=/tmp/other",
        "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents?host=/var/run/marketing-agents-postgres&ssl=disable",
        "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents?host=/var/run/marketing-agents-postgres&host=remote.example",
        "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents?host=127.0.0.1",
        "postgresql+asyncpg://del04_test@localhost:5432/del04_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?host=/tmp/ma-pg-abcdefgh/socket",
    ],
)
def test_primary_snapshot_refuses_tcp_credentials_unknown_roles_and_query_overrides(
    monkeypatch: pytest.MonkeyPatch, url: str
):
    monkeypatch.delenv("MARKETING_AGENTS_TEST_POSTGRES", raising=False)
    with pytest.raises(smoke.SmokeFailure, match="snapshot_requires_owned_postgresql_socket"):
        smoke.readonly_database_engine(url)


@pytest.mark.parametrize("selector", ["--database", "--database-url"])
def test_primary_snapshot_cli_reports_no_connection_or_payload_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], selector: str
):
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic-password-and-payload-must-not-appear")

    monkeypatch.setattr(smoke, "database_snapshot", fail)
    monkeypatch.setattr(
        sys, "argv", ["smoke", "snapshot", selector, "synthetic", "--key", "synthetic.key"]
    )
    assert smoke.main() == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {"ok": False, "code": "smoke_contract_failure"}


@pytest.mark.parametrize(
    "database_url,accepted",
    [
        ("sqlite+aiosqlite:////var/lib/marketing-agents/data/marketing_agents.db", True),
        (
            "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents?host=/var/run/marketing-agents-postgres",
            True,
        ),
        ("postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents", False),
        (
            "postgresql+asyncpg://other@localhost:5432/marketing_agents?host=/var/run/marketing-agents-postgres",
            False,
        ),
        (
            "postgresql+asyncpg://marketing_agents@localhost:5432/other?host=/var/run/marketing-agents-postgres",
            False,
        ),
        (
            "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents?host=/tmp/other",
            False,
        ),
        ("sqlite+aiosqlite:////tmp/other.db", False),
    ],
)
def test_primary_replay_fixture_retains_exact_owned_database_scope(
    monkeypatch: pytest.MonkeyPatch, database_url: str, accepted: bool
):
    monkeypatch.setenv("DEL05_VERIFICATION_SCOPE", "marketing-agents-del05-0123456789abcdef")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", replay.PUBLIC_FIXTURE_HMAC)
    monkeypatch.setenv(
        "MARKETING_AGENTS_DIGEST_KEY_PATH", "/var/lib/marketing-agents/secrets/digest.key"
    )
    if accepted:
        assert replay.fixture_settings().database_url == database_url
    else:
        with pytest.raises(smoke.SmokeFailure, match="fixture_requires_fresh_compose_paths"):
            replay.fixture_settings()
