"""DEL-04: native subprocess commands round-trip a private PostgreSQL installation."""

from __future__ import annotations

import base64
from pathlib import Path
from stat import S_IMODE

import pytest
from marketing_agents.infrastructure.db.migrations import expected_tables
from marketing_agents.security.digest_key import DigestKey, digest_key_fingerprint

from tests.integration.db.test_del_04_database_cli import _files, _invoke, _paths
from tests.integration.db.test_del_04_postgresql_installation import _snapshot
from tests.support.postgresql_runtime import pg_database_url as pg_database_url


@pytest.mark.asyncio
async def test_del_04_postgresql_native_cli_initializes_migrates_seeds_and_checks(
    pg_database_url: str, tmp_path: Path
) -> None:
    unused_sqlite_path, key_path = _paths(tmp_path)
    assert await _snapshot(pg_database_url) == {}
    _, initialized = _invoke(tmp_path, "initializer", database_url=pg_database_url)
    assert initialized == {"ok": True, "code": "local_secret_verified"}
    assert await _snapshot(pg_database_url) == {}
    key_bytes = key_path.read_bytes()
    key_modified = key_path.stat().st_mtime_ns
    assert S_IMODE(key_path.stat().st_mode) == 0o600
    assert S_IMODE(key_path.parent.stat().st_mode) == 0o700

    _, migrated = _invoke(tmp_path, "migrate", database_url=pg_database_url)
    assert migrated == {"ok": True, "revision": "0005"}
    empty_schema = await _snapshot(pg_database_url)
    assert set(empty_schema) == expected_tables("0005") | {"alembic_version"}
    assert len(empty_schema["local_runtime_identity"]) == 1
    fingerprint = digest_key_fingerprint(DigestKey(base64.urlsafe_b64decode(key_bytes.strip())))
    assert fingerprint in empty_schema["local_runtime_identity"][0]
    assert empty_schema["agent_instance_configs"] == ()

    _, seeded = _invoke(tmp_path, "seed", database_url=pg_database_url)
    assert seeded["ok"] is True and seeded["check"] is False
    assert seeded["configuration_inserted"] == 43
    before_rows = await _snapshot(pg_database_url)
    before_files = _files(tmp_path)
    assert len(before_rows["agent_instances"]) == 43
    assert len(before_rows["agent_instance_configs"]) == 43
    for operation in ("seed", "check", "migrate", "initializer"):
        _, result = _invoke(tmp_path, operation, database_url=pg_database_url)
        assert result["ok"] is True
        if operation in {"seed", "check"}:
            assert result["configuration_inserted"] == 0
            assert result["configuration_preserved"] == 43
            assert result["check"] is (operation == "check")
        assert await _snapshot(pg_database_url) == before_rows
        assert _files(tmp_path) == before_files
        assert key_path.read_bytes() == key_bytes
        assert key_path.stat().st_mtime_ns == key_modified
    assert not unused_sqlite_path.exists()
