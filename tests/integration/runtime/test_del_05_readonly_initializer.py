"""DEL-05 read-only key initialization defers only SQLite pairing inspection."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from marketing_agents.infrastructure.db import local_installation
from marketing_agents.infrastructure.db.local_installation import (
    initialize_local_secret,
    migrate_local_database,
)
from marketing_agents.security.digest_key import DigestKeyError


async def _pair(tmp_path: Path) -> tuple[Path, Path, str]:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    database = data / "local.db"
    key = tmp_path / "secret" / "digest.key"
    url = f"sqlite+aiosqlite:///{database}"
    await migrate_local_database(url, key)
    # Close the final writer after a WAL checkpoint; no sidecars are available
    # for a subsequent mode=ro connection on an unwritable directory.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()
    return database, key, url


@pytest.mark.asyncio
async def test_del_05_readonly_wal_initializer_never_opens_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, key, url = await _pair(tmp_path)
    original = key.read_bytes()
    database_bytes = database.read_bytes()
    database.parent.chmod(0o500)

    def forbidden(*args, **kwargs):
        raise AssertionError("the read-only initializer must not query SQLite")

    monkeypatch.setattr(local_installation, "_installation_read_only_engine", forbidden)
    try:
        await initialize_local_secret(url, key, defer_database_check=True)
        assert key.read_bytes() == original
        assert database.read_bytes() == database_bytes
        assert sorted(path.name for path in database.parent.iterdir()) == ["local.db"]
    finally:
        database.parent.chmod(0o700)


@pytest.mark.asyncio
async def test_del_05_deferred_initializer_missing_key_cannot_replace_lost_pair(
    tmp_path: Path,
) -> None:
    database, key, url = await _pair(tmp_path)
    before = hashlib.sha256(database.read_bytes()).digest()
    key.unlink()
    with pytest.raises(DigestKeyError, match="missing"):
        await initialize_local_secret(url, key, defer_database_check=True)
    assert not key.exists()
    assert hashlib.sha256(database.read_bytes()).digest() == before


@pytest.mark.asyncio
async def test_del_05_deferred_initializer_enforces_owner_and_private_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, key, url = await _pair(tmp_path)
    original = key.read_bytes()
    key.chmod(0o644)
    with pytest.raises(DigestKeyError):
        await initialize_local_secret(url, key, defer_database_check=True)
    assert key.stat().st_mode & 0o777 == 0o644
    assert key.read_bytes() == original
    key.chmod(0o600)
    uid = os.getuid()
    monkeypatch.setattr(local_installation.os, "getuid", lambda: uid + 1)
    with pytest.raises(DigestKeyError):
        await initialize_local_secret(url, key, defer_database_check=True)
    assert key.read_bytes() == original


@pytest.mark.asyncio
async def test_del_05_deferred_cli_preserves_mismatched_key_then_migration_rejects(
    tmp_path: Path,
) -> None:
    database, key, url = await _pair(tmp_path)
    replacement = base64.urlsafe_b64encode(bytes([17]) * 32) + b"\n"
    key.write_bytes(replacement)
    before = database.read_bytes()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "marketing_agents.workers.local_secret_init",
            "--database-url",
            url,
            "--key-path",
            str(key),
            "--defer-database-check",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "code": "local_secret_verified",
        "database_check": "deferred",
    }
    assert key.read_bytes() == replacement
    with pytest.raises(DigestKeyError, match="fingerprint"):
        await migrate_local_database(url, key)
    assert key.read_bytes() == replacement
    assert database.read_bytes() == before
