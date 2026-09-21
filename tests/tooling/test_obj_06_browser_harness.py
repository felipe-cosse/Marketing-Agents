"""OBJ-06 browser harness cannot strand native services after failed evidence."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from scripts import obj_06_browser_harness as harness


@pytest.mark.parametrize("outcome", [0, 1, "timeout", "interrupt"])
def test_obj_06_browser_commands_always_reap_owned_groups(tmp_path, monkeypatch, outcome):
    calls = []

    class Supervisor:
        def __init__(self, *, environment, logs, grace):
            assert environment == {"PATH": "/installed/tools"}
            assert logs == tmp_path / "browser"
            assert grace == 10

        def start(self, name, command, *, cwd):
            assert name == "browser" and command == ["node", "installed-test"]
            assert cwd == tmp_path

            def wait(*, timeout):
                assert timeout == 25
                if outcome == "timeout":
                    raise subprocess.TimeoutExpired(command, timeout)
                if outcome == "interrupt":
                    raise KeyboardInterrupt()
                return outcome

            return SimpleNamespace(process=SimpleNamespace(wait=wait))

        def close(self):
            calls.append("reaped")

    monkeypatch.setattr(harness.dev, "Supervisor", Supervisor)
    options = dict(
        environment={"PATH": "/installed/tools"},
        cwd=tmp_path,
        evidence=tmp_path,
        name="browser",
        timeout=25,
    )
    error = {
        1: subprocess.CalledProcessError,
        "timeout": subprocess.TimeoutExpired,
        "interrupt": KeyboardInterrupt,
    }.get(outcome)
    if error is None:
        harness._run_bounded(["node", "installed-test"], **options)
    else:
        with pytest.raises(error):
            harness._run_bounded(["node", "installed-test"], **options)
    assert calls == ["reaped"]


@pytest.mark.parametrize("failure", ["none", "readiness", "journey", "interrupt"])
def test_obj_06_native_installation_stops_on_every_exit(tmp_path, monkeypatch, failure):
    calls = []
    process, log = object(), object()
    original_path = os.environ.get("PATH")

    def launch(state, reports, ports, empty_home, corepack):
        assert (state, reports, empty_home, corepack) == tuple(
            tmp_path / p for p in ("state", "reports", "home", "corepack")
        )
        assert ports == (8000, 4173)
        assert os.environ["PATH"] == "/installed/tools"
        return process, log

    def ready(actual_process, reports):
        assert actual_process is process and reports == tmp_path / "reports"
        assert os.environ.get("PATH") == original_path
        if failure == "readiness":
            raise RuntimeError("readiness failed")

    def stop(actual_process, actual_log, reports):
        assert actual_process is process and actual_log is log
        assert reports == tmp_path / "reports"
        calls.append("stopped")

    monkeypatch.setattr(harness, "_launch", launch)
    monkeypatch.setattr(harness, "_await_supervised_readiness", ready)
    monkeypatch.setattr(harness, "_stop", stop)
    monkeypatch.setattr(harness, "_verify_reports", lambda _reports: calls.append("verified"))

    def execute():
        with harness._native_installation(
            *(tmp_path / p for p in ("state", "reports", "home", "corepack")),
            {"PATH": "/installed/tools"},
        ):
            if failure == "journey":
                raise RuntimeError("journey failed")
            if failure == "interrupt":
                raise KeyboardInterrupt()

    if failure == "none":
        execute()
        assert calls == ["stopped", "verified"]
    else:
        with pytest.raises(KeyboardInterrupt if failure == "interrupt" else RuntimeError):
            execute()
        assert calls == ["stopped"]
    assert os.environ.get("PATH") == original_path
