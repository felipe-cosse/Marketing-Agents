"""DEL-04: opt-in temporary PostgreSQL databases with private Unix sockets only."""

from __future__ import annotations

import importlib
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import URL

_OPT_IN = "MARKETING_AGENTS_TEST_POSTGRES"
_USER = "del04_test"
_PORT = 5432


@dataclass(frozen=True)
class _Cluster:
    sockets: Path


_CLUSTER_KEY = pytest.StashKey[_Cluster | str]()


def _command(arguments: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    # Every destination is explicit; inherited libpq connection settings never
    # select a user-owned installation or enable a network listener.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    return subprocess.run(
        arguments,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _require_success(
    result: subprocess.CompletedProcess[str], operation: str, *, log_path: Path | None = None
) -> None:
    if result.returncode:
        diagnostics = result.stdout + result.stderr
        if log_path is not None and log_path.is_file():
            diagnostics += "\n" + log_path.read_text(encoding="utf-8", errors="replace")
        pytest.fail(
            f"Explicit {_OPT_IN}=1 PostgreSQL {operation} failed. "
            "The local PostgreSQL binaries and operating-system shared-memory permissions "
            "must be available; this opt-in check is not skipped.\n" + diagnostics[-8000:],
            pytrace=False,
        )


def _stop_cluster(pg_ctl: str, data: Path) -> bool:
    """Confirm no server owns this exact directory before permitting its cleanup."""
    for mode in ("fast", "immediate"):
        try:
            status = _command([pg_ctl, "-D", str(data), "status"], timeout=10)
            if status.returncode == 3:
                return True
            if status.returncode != 0:
                return False
            _command(
                [pg_ctl, "-D", str(data), "-m", mode, "-w", "-t", "15", "stop"],
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
    try:
        return _command([pg_ctl, "-D", str(data), "status"], timeout=10).returncode == 3
    except (OSError, subprocess.TimeoutExpired):
        return False


@contextmanager
def _new_cluster(initdb: str, pg_ctl: str) -> Iterator[_Cluster]:
    # /tmp keeps the Unix-domain socket path below PostgreSQL's platform limit.
    # delete=False is intentional: a server that cannot be confirmed stopped
    # retains its own directory and diagnostics rather than losing live data.
    temporary = tempfile.TemporaryDirectory(prefix="ma-pg-", dir="/tmp", delete=False)
    root = Path(temporary.name).resolve()
    root.chmod(0o700)
    data, sockets = root / "data", root / "socket"
    sockets.mkdir(mode=0o700)
    initialization_finished = False
    start_attempted = False
    try:
        initialized = _command(
            [
                initdb,
                "-D",
                str(data),
                "--username",
                _USER,
                "--auth-local=trust",
                "--auth-host=reject",
                "--encoding=UTF8",
                "--locale=C",
            ],
            timeout=45,
        )
        initialization_finished = True
        _require_success(initialized, "initialization")
        options = (
            "-c listen_addresses='' "
            f"-c unix_socket_directories={shlex.quote(str(sockets))} "
            f"-c unix_socket_permissions=0700 -p {_PORT}"
        )
        start_attempted = True
        _require_success(
            _command(
                [
                    pg_ctl,
                    "-D",
                    str(data),
                    "-l",
                    str(root / "postgres.log"),
                    "-o",
                    options,
                    "-w",
                    "-t",
                    "20",
                    "start",
                ]
            ),
            "startup",
            log_path=root / "postgres.log",
        )
        yield _Cluster(sockets)
    finally:
        if (initialization_finished and not start_attempted) or (
            start_attempted and _stop_cluster(pg_ctl, data)
        ):
            temporary.cleanup()
        else:
            pytest.fail(
                "Temporary PostgreSQL initializer completion or shutdown could not be confirmed; "
                f"preserved its private directory at {root}",
                pytrace=False,
            )


def _session_cluster(request: pytest.FixtureRequest, initdb: str, pg_ctl: str) -> _Cluster:
    """Cache one owned server without requiring hidden imported pytest fixtures."""
    cached = request.config.stash.get(_CLUSTER_KEY, None)
    if isinstance(cached, str):
        pytest.fail(
            "Temporary PostgreSQL startup failed earlier in this session; "
            "no repeated bootstrap will be attempted.\n" + cached,
            pytrace=False,
        )
    if cached is not None:
        return cached
    manager = _new_cluster(initdb, pg_ctl)
    try:
        cluster = manager.__enter__()
    except (Exception, pytest.fail.Exception) as exc:
        request.config.stash[_CLUSTER_KEY] = str(exc)
        raise

    def finalize() -> None:
        try:
            manager.__exit__(None, None, None)
        finally:
            del request.config.stash[_CLUSTER_KEY]

    try:
        request.session.addfinalizer(finalize)
    except BaseException:
        manager.__exit__(None, None, None)
        raise
    request.config.stash[_CLUSTER_KEY] = cluster
    return cluster


async def _administrative_connection(asyncpg: Any, cluster: _Cluster) -> Any:
    return await asyncpg.connect(
        host=str(cluster.sockets),
        port=_PORT,
        user=_USER,
        password="",
        passfile=os.devnull,
        database="postgres",
        ssl=False,
        timeout=10,
    )


@pytest.fixture
async def pg_database_url(request: pytest.FixtureRequest) -> AsyncIterator[str]:
    """Yield a fresh database in one session-owned private PostgreSQL cluster.

    Import this fixture directly into a test module. Set
    MARKETING_AGENTS_TEST_POSTGRES=1 and install the project's ``postgresql`` extra
    plus PostgreSQL's ``initdb``/``pg_ctl`` binaries to enable it. No pre-existing
    database URL is read or contacted. Cluster startup requires the host's normal
    PostgreSQL shared-memory permissions, even though TCP listeners are disabled.
    Every test gets a unique database, removed at teardown; the server is stopped
    and its directory cleaned only at the end of the owning pytest session.
    """
    if os.environ.get(_OPT_IN) != "1":
        pytest.skip(f"set {_OPT_IN}=1 to run isolated PostgreSQL integration checks")
    try:
        asyncpg = importlib.import_module("asyncpg")
    except ImportError:
        pytest.fail(
            f"Explicit {_OPT_IN}=1 requires asyncpg; install the project's postgresql extra",
            pytrace=False,
        )
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.fail(
            f"Explicit {_OPT_IN}=1 requires initdb and pg_ctl on PATH",
            pytrace=False,
        )
    cluster = _session_cluster(request, initdb, pg_ctl)
    connection = await _administrative_connection(asyncpg, cluster)
    database_name = "del04_" + uuid.uuid4().hex
    try:
        assert await connection.fetchval("SHOW listen_addresses") == ""
        assert await connection.fetchval("SHOW unix_socket_directories") == str(cluster.sockets)
        assert await connection.fetchval("SHOW unix_socket_permissions") == "0700"
        await connection.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await connection.close()
    try:
        yield URL.create(
            "postgresql+asyncpg",
            username=_USER,
            host="localhost",
            port=_PORT,
            database=database_name,
            query={"host": str(cluster.sockets)},
        ).render_as_string(hide_password=False)
    finally:
        connection = await _administrative_connection(asyncpg, cluster)
        try:
            # This generated database is owned only by this test. FORCE handles
            # a failed test that neglected to dispose its own connection pool.
            await connection.execute(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        finally:
            await connection.close()
