"""OBJ-04: installed-tool discovery is part of the credential-free startup boundary."""

from __future__ import annotations

import errno
import io
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import dev


def test_obj_04_prerequisite_process_does_not_inherit_credentials_or_network_opt_ins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-obj04-not-a-credential")
    monkeypatch.setenv("COREPACK_NPM_TOKEN", "synthetic-obj04-not-a-credential")
    monkeypatch.setenv("NODE_OPTIONS", "--require=untrusted")
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid")
    monkeypatch.setenv("COREPACK_ENABLE_NETWORK", "1")
    monkeypatch.setenv("COREPACK_HOME", str(tmp_path / "cache"))

    def inspect(command, **kwargs):
        assert command == ["/installed/pnpm", "--version"]
        environment = kwargs.get("env")
        assert environment is not None, (
            "OBJ-04 version checks must not inherit the host environment"
        )
        assert environment["COREPACK_ENABLE_NETWORK"] == "0"
        assert environment["COREPACK_ENABLE_DOWNLOAD_PROMPT"] == "0"
        assert environment["COREPACK_ENABLE_AUTO_PIN"] == "0"
        assert environment["PNPM_CONFIG_OFFLINE"] == "true"
        assert environment["PNPM_CONFIG_UPDATE_NOTIFIER"] == "false"
        assert environment["COREPACK_HOME"] == str(tmp_path / "cache")
        assert (
            not {"AWS_ACCESS_KEY_ID", "COREPACK_NPM_TOKEN", "NODE_OPTIONS", "HTTP_PROXY"}
            & environment.keys()
        )
        assert kwargs["cwd"] == dev.ROOT
        return SimpleNamespace(stdout="11.24.0\n")

    monkeypatch.setattr(dev.subprocess, "run", inspect)
    assert dev._version(["/installed/pnpm", "--version"]) == "11.24.0"


def test_obj_04_supervised_processes_keep_offline_flags_and_explicit_local_cache(
    tmp_path: Path,
) -> None:
    inherited = {
        "PATH": "/usr/bin",
        "HOME": str(tmp_path / "home"),
        "COREPACK_HOME": str(tmp_path / "corepack"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "COREPACK_ENABLE_NETWORK": "1",
        "PNPM_CONFIG_OFFLINE": "false",
        "OPENAI_API_KEY": "synthetic-obj04-not-a-credential",
    }
    original = dict(inherited)
    environment = dev.safe_environment(
        tmp_path / "installation",
        dev.Tools(Path(sys.executable), Path("/installed/node"), Path("/installed/pnpm")),
        api_port=18000,
        web_port=15173,
        inherited=inherited,
    )
    assert environment.get("COREPACK_ENABLE_NETWORK") == "0"
    assert environment.get("PNPM_CONFIG_OFFLINE") == "true"
    assert environment["COREPACK_HOME"] == inherited["COREPACK_HOME"]
    assert environment["XDG_CACHE_HOME"] == inherited["XDG_CACHE_HOME"]
    assert environment["PATH"] == "/installed" + os.pathsep + "/usr/bin"
    assert "OPENAI_API_KEY" not in environment
    assert inherited == original


def test_obj_04_missing_cached_tool_reports_bootstrap_without_child_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="synthetic-private-diagnostic")

    monkeypatch.setattr(dev.subprocess, "run", unavailable)
    with pytest.raises(dev.NativeStartupError, match="run make bootstrap first") as error:
        dev._version(["/installed/pnpm", "--version"])
    assert "synthetic-private-diagnostic" not in str(error.value)


def test_obj_04_tool_checks_use_repository_authority_and_selected_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / ".venv/bin/python"
    vite = tmp_path / "apps/web/node_modules/.bin/vite"
    python.parent.mkdir(parents=True)
    vite.parent.mkdir(parents=True)
    python.touch()
    vite.touch()
    selected_node = tmp_path / "selected/bin/node"
    pnpm = "/installed/pnpm"
    calls = []

    def version(command, **kwargs):
        calls.append((command, kwargs))
        return {
            str(python): "Python 3.12.12",
            str(selected_node): dev.NODE_VERSION,
            pnpm: dev.PNPM_VERSION,
        }[command[0]]

    monkeypatch.setattr(dev, "_version", version)
    monkeypatch.setattr(dev.shutil, "which", lambda name: pnpm)
    tools = dev.prerequisites(tmp_path, node=selected_node)
    assert tools.node == selected_node
    assert len(calls) == 3
    assert all(kwargs["cwd"] == tmp_path for _, kwargs in calls)
    assert calls[-1][1]["node"] == selected_node


def test_obj_04_version_process_uses_explicit_node_path_and_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def inspect(command, **kwargs):
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"]["PATH"].startswith("/selected/bin" + os.pathsep)
        return SimpleNamespace(stdout="11.24.0\n")

    monkeypatch.setattr(dev.subprocess, "run", inspect)
    assert (
        dev._version(
            ["/installed/pnpm", "--version"], cwd=tmp_path, node=Path("/selected/bin/node")
        )
        == "11.24.0"
    )


class _ShutdownProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.polls = 0
        self.waits: list[float] = []

    def poll(self) -> None:
        self.polls += 1

    def wait(self, *, timeout: float) -> int:
        self.waits.append(timeout)
        return 0


def _shutdown_supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    elapsed = [0.0]
    monkeypatch.setattr(dev.time, "monotonic", lambda: elapsed[0])

    def sleep(duration: float) -> None:
        elapsed[0] += duration

    monkeypatch.setattr(dev.time, "sleep", sleep)
    supervisor = dev.Supervisor(environment={}, logs=tmp_path, grace=0.1)
    processes = [_ShutdownProcess(41), _ShutdownProcess(42)]
    logs = [io.BytesIO(), io.BytesIO()]
    supervisor.children = [
        dev.Child(f"worker-{process.pid}", process, log)
        for process, log in zip(processes, logs, strict=True)
    ]
    return supervisor, processes, logs, elapsed


def test_obj_04_shutdown_retries_transient_probe_denial_without_resignalling_absent_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, processes, logs, elapsed = _shutdown_supervisor(tmp_path, monkeypatch)
    calls = []
    first_probe = True

    def killpg(group: int, signum: int) -> None:
        nonlocal first_probe
        assert group in {41, 42}, "shutdown touched an unowned process group"
        calls.append((group, signum))
        if group == 42 or (group == 41 and signum == 0 and not first_probe):
            raise ProcessLookupError(errno.ESRCH, "synthetic absent group")
        if signum == 0:
            first_probe = False
            raise PermissionError(errno.EPERM, "synthetic transient probe denial")
        assert signum == signal.SIGTERM

    monkeypatch.setattr(dev.os, "killpg", killpg)
    supervisor.close()
    assert calls == [(41, signal.SIGTERM), (42, signal.SIGTERM), (41, 0), (41, 0)]
    assert elapsed[0] == 0.05
    assert all(log.closed for log in logs)
    assert all(process.waits == [5] for process in processes)
    original_calls = list(calls)
    supervisor.close()
    assert calls == original_calls


def test_obj_04_shutdown_persistent_probe_denial_fails_after_bounded_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, processes, logs, elapsed = _shutdown_supervisor(tmp_path, monkeypatch)
    calls = []

    def killpg(group: int, signum: int) -> None:
        assert group in {41, 42}, "shutdown touched an unowned process group"
        calls.append((group, signum))
        if group == 42:
            raise ProcessLookupError(errno.ESRCH, "synthetic absent group")
        if signum == 0:
            raise PermissionError(errno.EPERM, "synthetic persistent probe denial")

    monkeypatch.setattr(dev.os, "killpg", killpg)
    with pytest.raises(dev.NativeStartupError, match="native_shutdown_unverified"):
        supervisor.close()
    assert [(group, signum) for group, signum in calls if signum != 0] == [
        (41, signal.SIGTERM),
        (42, signal.SIGTERM),
        (41, signal.SIGKILL),
    ]
    assert calls[-1] == (41, 0)
    assert elapsed[0] == pytest.approx(supervisor.grace + 5)
    assert all(log.closed for log in logs)
    assert all(process.waits == [5] for process in processes)
    assert supervisor._absent_groups == {42}


@pytest.mark.parametrize("post_force_denial", (False, True))
def test_obj_04_shutdown_force_kills_owned_group_after_unchanged_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, post_force_denial: bool
) -> None:
    supervisor, processes, logs, elapsed = _shutdown_supervisor(tmp_path, monkeypatch)
    calls = []
    force_killed = False

    def killpg(group: int, signum: int) -> None:
        nonlocal force_killed, post_force_denial
        assert group in {41, 42}, "shutdown touched an unowned process group"
        calls.append((group, signum))
        if force_killed and post_force_denial and signum == 0:
            post_force_denial = False
            raise PermissionError(errno.EPERM, "synthetic post-force probe denial")
        if group == 42 or force_killed:
            raise ProcessLookupError(errno.ESRCH, "synthetic absent group")
        if signum == signal.SIGKILL:
            force_killed = True

    monkeypatch.setattr(dev.os, "killpg", killpg)
    expected_extra_probe = [(41, 0)] if post_force_denial else []
    supervisor.close()
    assert calls == [
        (41, signal.SIGTERM),
        (42, signal.SIGTERM),
        (41, 0),
        (41, 0),
        (41, signal.SIGKILL),
        (41, 0),
        *expected_extra_probe,
    ]
    assert elapsed[0] == supervisor.grace
    assert all(log.closed for log in logs)
    assert all(process.waits == [5] for process in processes)
    original_calls = list(calls)
    supervisor.close()
    assert calls == original_calls
