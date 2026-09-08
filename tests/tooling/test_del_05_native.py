"""DEL-05 native supervision, prerequisites, local authority and scoped cleanup."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import dev


def _tools() -> dev.Tools:
    return dev.Tools(Path(sys.executable), Path("/pinned/bin/node"), Path("/pinned/bin/pnpm"))


def test_del_05_native_environment_is_explicit_mock_local_and_credential_free(
    tmp_path: Path,
) -> None:
    environment = dev.safe_environment(
        tmp_path,
        _tools(),
        api_port=18000,
        web_port=15173,
        inherited={
            "HOME": "/test/home",
            "PATH": "/bin",
            "AWS_ACCESS_KEY_ID": "canary",
            "OPENAI_API_KEY": "canary",
            "ALLOW_EXTERNAL_NETWORK": "true",
            "DATABASE_URL": "unexpected",
            "HTTP_PROXY": "https://invalid.example",
        },
    )
    assert environment["LLM_PROVIDER"] == environment["CONNECTOR_MODE"] == "mock"
    assert environment["AUTH_MODE"] == "local"
    assert environment["ALLOW_EXTERNAL_NETWORK"] == "false"
    assert environment["API_HOST"] == "127.0.0.1"
    assert environment["API_PORT"] == environment["MARKETING_AGENTS_NATIVE_API_PORT"] == "18000"
    assert environment["DATABASE_URL"] == f"sqlite+aiosqlite:///{tmp_path / 'marketing_agents.db'}"
    assert "http://127.0.0.1:15173" in json.loads(environment["API_TRUSTED_ORIGINS"])
    assert not {"AWS_ACCESS_KEY_ID", "OPENAI_API_KEY", "HTTP_PROXY"} & environment.keys()
    assert environment["PATH"].startswith("/pinned/bin" + os.pathsep)


def test_del_05_native_refuses_unpinned_node_before_state_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / ".venv/bin/python"
    vite = tmp_path / "apps/web/node_modules/.bin/vite"
    python.parent.mkdir(parents=True)
    vite.parent.mkdir(parents=True)
    python.touch()
    vite.touch()
    monkeypatch.setattr(
        dev,
        "_version",
        lambda command: "Python 3.12.12" if command[0] == str(python) else "v24.3.0",
    )
    with pytest.raises(dev.NativeStartupError, match=r"activate Node 24\.20\.0"):
        dev.prerequisites(tmp_path, node=Path("/usr/bin/node"))
    assert not (tmp_path / "data").exists()


def test_del_05_native_port_conflict_is_explicit() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        other = 1024 if port != 1024 else 1025
        with pytest.raises(dev.NativeStartupError, match="native_port_in_use"):
            dev.require_free_ports(port, other)
    with pytest.raises(dev.NativeStartupError, match="native_ports_invalid"):
        dev.require_free_ports(8000, 8000)


def test_del_05_native_state_requires_scoped_path_and_exclusive_owner(tmp_path: Path) -> None:
    for broad in (Path("/"), Path.home(), dev.ROOT, Path("/tmp")):
        with pytest.raises(dev.NativeStartupError, match="native_state_path"):
            dev.state_directory(broad)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(dev.NativeStartupError, match="native_state_path"):
        dev.state_directory(link)
    with pytest.raises(dev.NativeStartupError, match="native_state_path"):
        dev.state_directory(link / "child")
    state = dev.state_directory(tmp_path / "native")
    with (
        dev.installation_lock(state),
        pytest.raises(dev.NativeStartupError, match="native_already_running"),
        dev.installation_lock(state),
    ):
        pytest.fail("a second supervisor acquired the live installation")
    with dev.installation_lock(state):
        assert state.is_dir()


def test_del_05_native_existing_permissions_and_owner_fail_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "existing"
    state.mkdir(mode=0o755)
    with (
        pytest.raises(dev.NativeStartupError, match="native_state_permissions"),
        dev.installation_lock(state),
    ):
        pytest.fail("permissive state was accepted")
    assert state.stat().st_mode & 0o777 == 0o755
    assert list(state.iterdir()) == []
    state.chmod(0o700)
    uid = os.getuid()
    monkeypatch.setattr(dev.os, "getuid", lambda: uid + 1)
    with (
        pytest.raises(dev.NativeStartupError, match="native_state_permissions"),
        dev.installation_lock(state),
    ):
        pytest.fail("foreign-owned state was accepted")
    assert list(state.iterdir()) == []


def test_del_05_native_cleanup_is_bounded_and_only_stops_owned_groups(tmp_path: Path) -> None:
    supervisor = dev.Supervisor(environment=dict(os.environ), logs=tmp_path, grace=0.1)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        child = supervisor.start(
            "worker",
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready',flush=True); time.sleep(30)",
            ],
            cwd=tmp_path,
        )
        deadline = time.monotonic() + 3
        while not (tmp_path / "worker.log").read_bytes():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        started = time.monotonic()
        supervisor.close()
        assert time.monotonic() - started < 2
        assert child.process.returncode == -signal.SIGKILL
        assert unrelated.poll() is None
    finally:
        supervisor.close()
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_del_05_native_child_exit_fails_the_supervised_stack(tmp_path: Path) -> None:
    supervisor = dev.Supervisor(environment=dict(os.environ), logs=tmp_path, grace=0.1)
    try:
        child = supervisor.start("api", [sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path)
        child.process.wait(timeout=5)
        with pytest.raises(dev.NativeStartupError, match="native_child_exited: api"):
            supervisor.check_children([child])
    finally:
        supervisor.close()


def test_del_05_native_readiness_rejects_unsafe_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    session = {
        "authMode": "local",
        "modelMode": "mock",
        "connectorMode": "mock",
        "networkPermission": False,
    }

    def get(port: int, path: str):
        del port
        if path == "/health/ready":
            return 200, b'{"status":"ready"}'
        if path == "/api/v1/session":
            return 200, json.dumps(session).encode()
        return 200, b"<html>local</html>"

    monkeypatch.setattr(dev, "_get", get)
    assert dev.ready(8000, 5173)
    session["networkPermission"] = True
    assert not dev.ready(8000, 5173)


def test_del_05_native_smoke_wires_all_services_after_shared_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    class RecordingSupervisor:
        def __init__(self, *, environment, logs):
            self.environment = environment
            self.logs = logs
            self.stopping = False

        def initialize(self, name, command, *, cwd):
            calls.append(("init", name, tuple(command), cwd))

        def start(self, name, command, *, cwd):
            calls.append(("start", name, tuple(command), cwd))
            return SimpleNamespace(name=name)

        def check_children(self, active):
            assert len(active) == 4

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(dev, "prerequisites", lambda **kwargs: _tools())
    monkeypatch.setattr(dev, "require_free_ports", lambda *args: None)
    monkeypatch.setattr(dev, "Supervisor", RecordingSupervisor)
    monkeypatch.setattr(dev, "ready", lambda *args: True)
    monkeypatch.setattr(
        dev.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    dev.run(
        Namespace(
            node=None,
            state_dir=tmp_path / "native",
            api_port=18000,
            web_port=15173,
            startup_timeout=1,
            smoke=True,
        )
    )
    assert [(item[0], item[1]) for item in calls[:-1]] == [
        ("init", "local-secret"),
        ("init", "migrate"),
        ("init", "seed"),
        ("start", "api"),
        ("start", "run-worker"),
        ("start", "scheduler-worker"),
        ("start", "web"),
    ]
    assert "--database-url" in calls[0][2] and "--key-path" in calls[0][2]
    assert calls[-2][2][-3:] == ("--port", "15173", "--strictPort")
    assert calls[-1] == ("close",)
