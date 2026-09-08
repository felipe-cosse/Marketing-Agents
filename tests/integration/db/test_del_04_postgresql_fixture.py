"""DEL-04: private PostgreSQL fixture lifecycle without a live server or IPC changes."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

from tests.support import postgresql_runtime as support


def _request() -> Any:
    finalizers: list[Any] = []
    return SimpleNamespace(
        config=SimpleNamespace(stash=pytest.Stash()),
        session=SimpleNamespace(addfinalizer=finalizers.append, finalizers=finalizers),
    )


@pytest.mark.asyncio
async def test_del_04_pg_fixture_reuses_one_session_cluster_but_isolates_each_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    cluster = support._Cluster(Path("/tmp/mock-del04-private-socket"))
    calls: list[str] = []

    @contextmanager
    def start(initdb: str, pg_ctl: str) -> Iterator[support._Cluster]:
        del initdb, pg_ctl
        calls.append("start")
        try:
            yield cluster
        finally:
            calls.append("stop")

    class Connection:
        async def fetchval(self, statement: str) -> str:
            return {
                "SHOW listen_addresses": "",
                "SHOW unix_socket_directories": str(cluster.sockets),
                "SHOW unix_socket_permissions": "0700",
            }[statement]

        async def execute(self, statement: str) -> None:
            calls.append(statement)

        async def close(self) -> None:
            calls.append("close connection")

    async def connect(asyncpg: Any, active: support._Cluster) -> Connection:
        del asyncpg
        assert active is cluster
        return Connection()

    monkeypatch.setenv("MARKETING_AGENTS_TEST_POSTGRES", "1")
    monkeypatch.setattr(support.importlib, "import_module", lambda _: object())
    monkeypatch.setattr(support.shutil, "which", lambda _: "/unused/mock-postgres-binary")
    monkeypatch.setattr(support, "_new_cluster", start)
    monkeypatch.setattr(support, "_administrative_connection", connect)
    databases = []
    for _ in range(2):
        fixture = support.pg_database_url.__wrapped__(request)
        url = make_url(await anext(fixture))
        assert url.drivername == "postgresql+asyncpg"
        assert url.query == {"host": str(cluster.sockets)}
        databases.append(url.database)
        await fixture.aclose()
    assert databases[0] != databases[1]
    assert calls.count("start") == 1
    assert calls.count("stop") == 0
    assert calls.count("close connection") == 4
    for database in databases:
        assert calls.count(f'CREATE DATABASE "{database}"') == 1
        assert calls.count(f'DROP DATABASE "{database}" WITH (FORCE)') == 1
    assert len(request.session.finalizers) == 1
    request.session.finalizers.pop()()
    assert calls.count("stop") == 1
    assert support._CLUSTER_KEY not in request.config.stash


def test_del_04_pg_fixture_caches_startup_failure_without_repeated_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    starts = 0

    @contextmanager
    def fail_start(initdb: str, pg_ctl: str) -> Iterator[support._Cluster]:
        del initdb, pg_ctl
        nonlocal starts
        starts += 1
        raise RuntimeError("injected shared-memory allocation failure")
        yield  # pragma: no cover

    monkeypatch.setattr(support, "_new_cluster", fail_start)
    with pytest.raises(RuntimeError, match="shared-memory allocation"):
        support._session_cluster(request, "initdb", "pg_ctl")
    with pytest.raises(pytest.fail.Exception, match="no repeated bootstrap"):
        support._session_cluster(request, "initdb", "pg_ctl")
    assert starts == 1
    assert request.session.finalizers == []


@pytest.mark.parametrize("confirmed_stopped", (False, True))
def test_del_04_pg_fixture_removes_owned_directory_only_after_confirmed_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed_stopped: bool
) -> None:
    directory = tmp_path / "mock-owned-cluster"
    temporary = SimpleNamespace(name=str(directory), cleanup=Mock())
    stop = Mock(return_value=confirmed_stopped)
    with monkeypatch.context() as patch:
        patch.setattr(support.tempfile, "TemporaryDirectory", lambda **_: temporary)
        patch.setattr(Path, "chmod", lambda *_: None)
        patch.setattr(Path, "mkdir", lambda *_, **__: None)
        patch.setattr(
            support,
            "_command",
            lambda arguments, **_: subprocess.CompletedProcess(arguments, 0, "", ""),
        )
        patch.setattr(support, "_stop_cluster", stop)
        manager = support._new_cluster("initdb", "pg_ctl")
        assert manager.__enter__().sockets == directory / "socket"
        if confirmed_stopped:
            manager.__exit__(None, None, None)
            temporary.cleanup.assert_called_once_with()
        else:
            with pytest.raises(pytest.fail.Exception, match="preserved its private directory"):
                manager.__exit__(None, None, None)
            temporary.cleanup.assert_not_called()
        stop.assert_called_once_with("pg_ctl", directory / "data")
    assert not directory.exists()


def test_del_04_pg_fixture_preserves_directory_when_initializer_completion_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "mock-interrupted-initializer"
    temporary = SimpleNamespace(name=str(directory), cleanup=Mock())
    stop = Mock()

    def timeout(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        raise subprocess.TimeoutExpired(arguments, timeout=45)

    with monkeypatch.context() as patch:
        patch.setattr(support.tempfile, "TemporaryDirectory", lambda **_: temporary)
        patch.setattr(Path, "chmod", lambda *_: None)
        patch.setattr(Path, "mkdir", lambda *_, **__: None)
        patch.setattr(support, "_command", timeout)
        patch.setattr(support, "_stop_cluster", stop)
        with (
            pytest.raises(pytest.fail.Exception, match="initializer completion or shutdown"),
            support._new_cluster("initdb", "pg_ctl"),
        ):
            pytest.fail("an interrupted initializer cannot yield a cluster")
        temporary.cleanup.assert_not_called()
        stop.assert_not_called()
    assert not directory.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("opted_in", (False, True))
async def test_del_04_pg_fixture_skips_by_default_but_missing_explicit_extra_fails(
    monkeypatch: pytest.MonkeyPatch, opted_in: bool
) -> None:
    def missing_extra(name: str) -> None:
        del name
        raise ImportError("injected unavailable asyncpg")

    if opted_in:
        monkeypatch.setenv("MARKETING_AGENTS_TEST_POSTGRES", "1")
    else:
        monkeypatch.delenv("MARKETING_AGENTS_TEST_POSTGRES", raising=False)
    monkeypatch.setattr(support.importlib, "import_module", missing_extra)
    start = Mock(side_effect=AssertionError("prerequisites must reject before cluster startup"))
    monkeypatch.setattr(support, "_session_cluster", start)
    error = pytest.fail.Exception if opted_in else pytest.skip.Exception
    message = "requires asyncpg" if opted_in else "MARKETING_AGENTS_TEST_POSTGRES=1"
    with pytest.raises(error, match=message):
        await anext(support.pg_database_url.__wrapped__(_request()))
    start.assert_not_called()
