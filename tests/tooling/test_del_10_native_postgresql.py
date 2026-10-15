"""Native primary-database selection, installed tools, ownership and fail-closed setup."""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import dev
from scripts import native_postgres as pg


def _tools() -> pg.PostgresTools:
    return pg.PostgresTools(
        *(Path("/installed") / name for name in ("initdb", "postgres", "pg_ctl", "psql")), major=14
    )


def test_native_database_selection_defaults_to_postgresql_and_accepts_explicit_sqlite(monkeypatch):
    selections = []
    monkeypatch.setattr(dev, "run", lambda args: selections.append(args.database))
    assert dev.main([]) == dev.main(["--database", "sqlite"]) == 0
    assert selections == ["postgresql", "sqlite"]


def test_native_postgres_tools_missing_does_not_fall_back_or_create_data(tmp_path, monkeypatch):
    monkeypatch.setattr(pg.shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(pg.NativePostgresError, match="native_postgres_tools_missing"):
        pg.ManagedPostgres(tmp_path, {})
    assert not list(tmp_path.iterdir())


def test_native_postgres_tool_discovery_drops_ambient_credentials(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "synthetic-not-a-secret")
    monkeypatch.setenv("PGSERVICE", "synthetic-invalid")
    environment = dev._tool_environment()
    seen = []
    monkeypatch.setattr(pg.shutil, "which", lambda name, **kwargs: f"/installed/{name}")

    def run(command, **kwargs):
        seen.append(command)
        assert kwargs["env"] is environment
        assert not {"PGPASSWORD", "PGSERVICE"} & kwargs["env"].keys()
        assert command[-1] == "--version"
        return SimpleNamespace(stdout=f"{Path(command[0]).name} (PostgreSQL) 14.17\n")

    monkeypatch.setattr(pg.subprocess, "run", run)
    assert pg.installed_tools(environment) == _tools()
    assert len(seen) == 4


@pytest.mark.parametrize("bad", ["postgres (PostgreSQL) 13.20", "private-invalid-version"])
def test_native_postgres_versions_fail_closed_without_raw_diagnostics(monkeypatch, bad):
    monkeypatch.setattr(pg.shutil, "which", lambda name, **kwargs: f"/installed/{name}")
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=bad))
    with pytest.raises(pg.NativePostgresError, match="native_postgres_version") as error:
        pg.installed_tools({})
    assert bad not in str(error.value)


@pytest.mark.parametrize(
    "filename", ["marketing_agents.db", "marketing_agents.db-wal", "marketing_agents.db-shm"]
)
def test_native_postgres_refuses_existing_sqlite_without_mutation(tmp_path, monkeypatch, filename):
    monkeypatch.setattr(pg, "installed_tools", lambda env: pytest.fail("checked tools before data"))
    existing = tmp_path / filename
    existing.write_bytes(b"retained-sqlite-data")
    with pytest.raises(pg.NativePostgresError, match="--database sqlite"):
        pg.ManagedPostgres(tmp_path, {})
    assert existing.read_bytes() == b"retained-sqlite-data"
    assert list(tmp_path.iterdir()) == [existing]


def test_native_postgres_refuses_orphan_key_before_cluster_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "installed_tools", lambda env: _tools())
    (tmp_path / "secrets").mkdir()
    key = tmp_path / "secrets/digest.key"
    key.write_bytes(b"retained-key")
    with pytest.raises(pg.NativePostgresError, match="pair_incomplete"):
        pg.ManagedPostgres(tmp_path, {})
    assert key.read_bytes() == b"retained-key"
    assert not (tmp_path / "postgres").exists()


def _owned_data(state: Path) -> None:
    data = state / "postgres"
    data.mkdir(mode=0o700)
    (data / "PG_VERSION").write_text("14\n")
    (data / "PG_VERSION").chmod(0o600)
    marker = state / pg.MARKER
    marker.write_text(
        json.dumps({"version": 1, "major": 14, "database": pg.APPLICATION, "role": pg.APPLICATION})
    )
    marker.chmod(0o600)


@pytest.mark.parametrize(
    "failure", ["marker", "major", "symlink", "permissions", "running", "oversized", "version_link"]
)
def test_native_postgres_refuses_unowned_incompatible_or_active_data(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(pg, "installed_tools", lambda env: _tools())
    _owned_data(tmp_path)
    marker = tmp_path / pg.MARKER
    if failure == "marker":
        marker.write_text("{}")
    elif failure == "major":
        (tmp_path / "postgres/PG_VERSION").write_text("15\n")
    elif failure == "symlink":
        marker.rename(tmp_path / "real-marker")
        marker.symlink_to(tmp_path / "real-marker")
    elif failure == "permissions":
        (tmp_path / "postgres").chmod(0o755)
    elif failure == "oversized":
        marker.write_bytes(b"x" * 1025)
    elif failure == "version_link":
        version = tmp_path / "postgres/PG_VERSION"
        version.rename(tmp_path / "real-version")
        version.symlink_to(tmp_path / "real-version")
    else:
        (tmp_path / "postgres/postmaster.pid").write_text("12345\n")
    with pytest.raises(pg.NativePostgresError):
        pg.ManagedPostgres(tmp_path, {})


def test_native_postgres_socket_private_and_cleanup_preserves_persistent_data(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(pg, "installed_tools", lambda env: _tools())
    _owned_data(tmp_path)
    cluster = pg.ManagedPostgres(tmp_path, {})
    socket = cluster.socket
    try:
        assert socket.stat().st_uid == os.getuid()
        assert socket.stat().st_mode & 0o777 == 0o700
        assert cluster.url.startswith("postgresql+asyncpg://marketing_agents@localhost:5432/")
        assert "host=%2Ftmp%2Fma-pg-" in cluster.url
        assert cluster.command("SELECT 1", application=True)[-1] == "SELECT 1"
    finally:
        cluster.close()
    assert not socket.exists()
    assert (tmp_path / "postgres/PG_VERSION").is_file()


def test_native_postgres_start_uses_owned_server_no_tcp_and_restricted_role(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "installed_tools", lambda env: _tools())
    calls = []

    class Supervisor:
        stopping = False

        def initialize(self, name, command, *, cwd):
            calls.append((name, command))
            if name == "postgres-init":
                (tmp_path / "postgres").mkdir(mode=0o700)

        def start(self, name, command, *, cwd):
            calls.append((name, command))
            return SimpleNamespace(name=name)

        def check_children(self, active):
            assert active[0].name == "postgres"

    cluster = pg.ManagedPostgres(tmp_path, {})
    results = iter(["1", "marketing_agents|f|f|f|f|f|t|marketing_agents", "marketing_agents"])
    monkeypatch.setattr(cluster, "query", lambda *a, **k: next(results))
    try:
        assert cluster.start(Supervisor()).name == "postgres"
        assert [name for name, _ in calls] == [
            "postgres-init",
            "postgres",
            "postgres-role",
            "postgres-database",
        ]
        assert "listen_addresses=" in calls[1][1]
        assert "unix_socket_permissions=0700" in calls[1][1]
        assert "--auth-host=reject" in calls[0][1]
        assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in calls[2][1][-1]
        assert (tmp_path / pg.MARKER).stat().st_mode & 0o777 == 0o600
    finally:
        cluster.close()


def test_native_explicit_sqlite_refuses_postgres_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "postgres").mkdir(mode=0o700)
    monkeypatch.setattr(
        dev, "prerequisites", lambda **k: SimpleNamespace(node=Path("/installed/node"))
    )
    monkeypatch.setattr(dev, "require_free_ports", lambda *a: None)
    with pytest.raises(dev.NativeStartupError, match="native_existing_postgres"):
        dev.run(
            Namespace(node=None, state_dir=state, api_port=8000, web_port=5173, database="sqlite")
        )
    assert not (state / "marketing_agents.db").exists()
