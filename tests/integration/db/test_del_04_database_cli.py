"""DEL-04: explicit native database commands preserve durable local key identity."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.db.migrations import expected_tables
from marketing_agents.security.digest_key import DigestKey, digest_key_fingerprint
from sqlalchemy import event
from sqlalchemy.engine import URL, Engine

ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = ROOT / "catalog" / "v1"
DATABASE_MODULE = "marketing_agents.workers.database_cli"
SECRET_MODULE = "marketing_agents.workers.local_secret_init"


def _paths(directory: Path) -> tuple[Path, Path]:
    return directory / "installation #.db", directory / "private" / "digest.key"


def _database_url(database: Path) -> str:
    return f"sqlite+aiosqlite:///{database}"


def _invoke(
    directory: Path,
    operation: str,
    *,
    catalog_root: Path = CATALOG_ROOT,
    database_url: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    database, key = _paths(directory)
    module = SECRET_MODULE if operation == "initializer" else DATABASE_MODULE
    arguments = (
        [] if operation == "initializer" else ["seed" if operation == "check" else operation]
    )
    arguments.extend(
        ("--database-url", database_url or _database_url(database), "--key-path", str(key))
    )
    if operation in {"seed", "check"}:
        arguments.extend(("--root", str(catalog_root)))
    if operation == "check":
        arguments.append("--check")
    completed = subprocess.run(
        [sys.executable, "-m", module, *arguments],
        cwd=directory,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "apps" / "api" / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    assert completed.stderr == ""
    assert completed.returncode == (0 if result["ok"] else 1)
    return completed, result


def _rows(database: Path) -> dict[str, tuple[tuple[Any, ...], ...]]:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        )
        return {
            table: tuple(sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall()))
            for table in tables
        }
    finally:
        connection.close()


def _files(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _fresh_installation(directory: Path) -> None:
    _, migrated = _invoke(directory, "migrate")
    assert migrated == {"ok": True, "revision": "0005"}
    _, seeded = _invoke(directory, "seed")
    assert seeded["ok"] is True
    assert seeded["configuration_inserted"] == 43


def test_del_04_native_migrate_seed_reseed_check_preserve_key_and_rows(tmp_path: Path) -> None:
    database, key = _paths(tmp_path)
    _, initialized = _invoke(tmp_path, "initializer")
    assert initialized == {"ok": True, "code": "local_secret_verified"}
    assert not database.exists()
    key_bytes = key.read_bytes()
    key_modified = key.stat().st_mtime_ns
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700

    _, migrated = _invoke(tmp_path, "migrate")
    assert migrated == {"ok": True, "revision": "0005"}
    empty_schema = _rows(database)
    assert set(empty_schema) == expected_tables("0005") | {"alembic_version"}
    identity = empty_schema["local_runtime_identity"]
    assert len(identity) == 1
    assert identity[0][:3] == (
        1,
        1,
        digest_key_fingerprint(DigestKey(base64.urlsafe_b64decode(key_bytes.strip()))),
    )
    assert empty_schema["agent_instance_configs"] == ()

    _, seeded = _invoke(tmp_path, "seed")
    assert seeded["ok"] is True and seeded["check"] is False
    assert seeded["configuration_inserted"] == 43
    before_rows = _rows(database)
    before_files = _files(tmp_path)
    for operation in ("seed", "check", "migrate", "initializer"):
        _, result = _invoke(tmp_path, operation)
        assert result["ok"] is True
        if operation in {"seed", "check"}:
            assert result["configuration_inserted"] == 0
            assert result["configuration_preserved"] == 43
            assert result["check"] is (operation == "check")
        assert _rows(database) == before_rows
        assert _files(tmp_path) == before_files
        assert key.read_bytes() == key_bytes
        assert key.stat().st_mtime_ns == key_modified


def test_del_04_native_seed_check_preserves_delete_journal_mode_and_all_files(
    tmp_path: Path,
) -> None:
    _fresh_installation(tmp_path)
    database, key = _paths(tmp_path)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    finally:
        connection.close()
    before_rows = _rows(database)
    before_files = _files(tmp_path)
    before_paths = tuple(sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")))
    key_modified = key.stat().st_mtime_ns

    _, checked = _invoke(tmp_path, "check")
    assert checked["ok"] is True and checked["check"] is True
    assert checked["configuration_inserted"] == 0
    assert checked["configuration_preserved"] == 43
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        connection.close()
    assert _rows(database) == before_rows
    assert _files(tmp_path) == before_files
    assert tuple(sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))) == (
        before_paths
    )
    assert key.stat().st_mtime_ns == key_modified


@pytest.mark.parametrize("operation", ("migrate", "seed", "check", "initializer"))
@pytest.mark.parametrize(
    "problem", ("missing", "mismatched", "file_permissions", "directory_permissions")
)
def test_del_04_commands_reject_unsafe_or_unpaired_key_without_writes(
    tmp_path: Path, operation: str, problem: str
) -> None:
    _fresh_installation(tmp_path)
    database, key = _paths(tmp_path)
    if problem == "missing":
        key.unlink()
    elif problem == "mismatched":
        key.write_bytes(base64.urlsafe_b64encode(bytes([29]) * 32) + b"\n")
    elif problem == "file_permissions":
        key.chmod(0o640)
    else:
        key.parent.chmod(0o750)
    before_rows = _rows(database)
    before_files = _files(tmp_path)
    _, result = _invoke(tmp_path, operation)
    assert result == {"ok": False, "code": "local_secret_invalid"}
    assert _rows(database) == before_rows
    assert _files(tmp_path) == before_files


@pytest.mark.parametrize("operation", ("migrate", "seed", "initializer"))
@pytest.mark.parametrize("existing_state", ("unversioned", "missing_identity"))
def test_del_04_commands_do_not_adopt_unversioned_or_unpaired_existing_data(
    tmp_path: Path, operation: str, existing_state: str
) -> None:
    database, _ = _paths(tmp_path)
    if existing_state == "unversioned":
        _, initialized = _invoke(tmp_path, "initializer")
        assert initialized["ok"] is True
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE legacy_state (value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy_state VALUES ('existing user state')")
        connection.close()
    else:
        _fresh_installation(tmp_path)
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM local_runtime_identity")
        connection.close()
    before_rows = _rows(database)
    before_files = _files(tmp_path)
    _, result = _invoke(tmp_path, operation)
    expected = (
        "migration_unversioned_schema"
        if existing_state == "unversioned" and operation != "initializer"
        else "local_secret_invalid"
    )
    assert result == {"ok": False, "code": expected}
    assert _rows(database) == before_rows
    assert _files(tmp_path) == before_files


@pytest.mark.parametrize("existing", (False, True))
def test_del_04_invalid_catalog_seed_does_not_create_or_modify_installation(
    tmp_path: Path, existing: bool
) -> None:
    if existing:
        _fresh_installation(tmp_path)
    invalid_catalog = tmp_path / "invalid-catalog"
    invalid_catalog.mkdir()
    invalid_catalog.joinpath("manifest.yaml").write_text(
        "content_version: malformed-catalog-secret-canary\n", encoding="utf-8"
    )
    before = _files(tmp_path)
    completed, result = _invoke(tmp_path, "seed", catalog_root=invalid_catalog)
    assert result == {"ok": False, "code": "database_command_failed"}
    assert "malformed-catalog-secret-canary" not in completed.stdout + completed.stderr
    assert _files(tmp_path) == before


@pytest.mark.parametrize("operation", ("migrate", "seed", "initializer"))
def test_del_04_cli_failures_emit_only_safe_json_without_url_or_secret(
    tmp_path: Path, operation: str
) -> None:
    secret = "cli-credential-must-never-appear"
    url = URL.create(
        "postgresql+asyncpg",
        username="operator",
        password=secret,
        host="localhost",
        database="cli-private-database",
        query={"host": str(tmp_path / "absent-private-socket")},
    ).render_as_string(hide_password=False)
    before = _files(tmp_path)
    completed, result = _invoke(tmp_path, operation, database_url=url)
    assert result == {
        "ok": False,
        "code": "database_command_failed" if operation == "migrate" else "local_secret_invalid",
    }
    output = completed.stdout + completed.stderr
    for sensitive in (url, secret, str(tmp_path), "cli-private-database"):
        assert sensitive not in output
    assert _files(tmp_path) == before


@pytest.mark.asyncio
async def test_del_04_migration_identity_failure_rolls_back_schema_and_reuses_key(
    tmp_path: Path,
) -> None:
    database, key = _paths(tmp_path)

    def fail_after_identity_insert(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> None:
        del connection, cursor, parameters, context, many
        if statement.startswith("INSERT INTO local_runtime_identity"):
            raise RuntimeError("injected failure after identity insert")

    event.listen(Engine, "after_cursor_execute", fail_after_identity_insert)
    try:
        with pytest.raises(RuntimeError, match="injected failure after identity insert"):
            await migrate_local_database(_database_url(database), key)
    finally:
        event.remove(Engine, "after_cursor_execute", fail_after_identity_insert)
    assert key.is_file()
    key_bytes = key.read_bytes()
    assert _rows(database) == {}
    assert await migrate_local_database(_database_url(database), key) == "0005"
    assert len(_rows(database)["local_runtime_identity"]) == 1
    assert key.read_bytes() == key_bytes
