"""OBJ-04 guard controls require no native frontend dependencies or real egress."""

from __future__ import annotations

import errno
import io
import json
import signal
import socket
import subprocess

import pytest

from tests.acceptance.obj_04_guard import GuardRecorder
from tests.acceptance.obj_04_native_offline import _stop
from tests.network import python_network_guard


def test_obj_04_disabled_child_guard_hits_tripwire_without_egress(tmp_path, monkeypatch):
    # An intentionally broken primary guard still must not delegate a negative
    # control to real DNS/socket functions. Restore all touched APIs afterward.
    touched = (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket.socket, "sendto"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    )
    for owner, name in touched:
        monkeypatch.setattr(owner, name, getattr(owner, name))
    monkeypatch.setattr(python_network_guard, "install_network_guard", lambda: None)
    recorder = GuardRecorder(tmp_path / "broken-guard.json", "negative-control")
    with pytest.raises(AssertionError, match="external call reached delegate tripwire"):
        recorder.install()
    assert recorder.data["delegate_violations"] == 1
    assert recorder.data["canaries_blocked"] == 0


@pytest.mark.parametrize("controller_state", ("crashed", "clean", "timeout"))
def test_obj_04_cleanup_reaps_only_owned_groups_on_every_controller_outcome(
    tmp_path, monkeypatch, controller_state
):
    from tests.acceptance import obj_04_native_offline as native

    calls = []
    live_groups = {41, 42}  # 42 is deliberately not owned by this launcher.

    def fake_killpg(group, signum):
        calls.append((group, signum))
        assert group in {40, 41}, "cleanup inspected or signalled an unowned group"
        if group not in live_groups:
            raise ProcessLookupError
        if signum == signal.SIGKILL:
            live_groups.remove(group)

    class Controller:
        returncode = 1 if controller_state == "crashed" else 0
        waits = 0

        def poll(self):
            return None if controller_state == "timeout" else self.returncode

        def send_signal(self, signum):
            assert signum == signal.SIGTERM

        def wait(self, *, timeout):
            self.waits += 1
            if controller_state == "timeout" and self.waits == 1:
                raise subprocess.TimeoutExpired("synthetic-controller", timeout)
            return self.returncode

        def kill(self):
            self.returncode = -signal.SIGKILL

    (tmp_path / "launches.jsonl").write_text(
        "\n".join(
            json.dumps({"pid": group, "owned_group": owned})
            for group, owned in ((40, True), (41, True), (42, False))
        )
    )
    monkeypatch.setattr(native.os, "killpg", fake_killpg)
    log = io.BytesIO()
    with pytest.raises(pytest.fail.Exception, match="left an owned process group alive"):
        _stop(Controller(), log, tmp_path)
    assert log.closed
    assert live_groups == {42}
    assert calls == [(40, 0), (41, 0), (41, signal.SIGKILL), (41, 0)]


@pytest.mark.parametrize("probe_state", ("transient", "persistent", "post-kill-transient"))
def test_obj_04_cleanup_retries_denied_probes_until_confirmed_absent_or_bounded_failure(
    tmp_path, monkeypatch, probe_state
):
    from tests.acceptance import obj_04_native_offline as native

    elapsed = 0.0
    calls = []
    probes = 0

    def fake_sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    def fake_killpg(group, signum):
        nonlocal probes
        assert group == 41, "cleanup touched an unrecorded group"
        calls.append(signum)
        if signum == signal.SIGKILL:
            assert probe_state == "post-kill-transient"
            return
        assert signum == 0
        probes += 1
        if probe_state == "post-kill-transient" and probes == 1:
            return
        if probe_state == "persistent" or probes == (
            2 if probe_state == "post-kill-transient" else 1
        ):
            raise PermissionError(errno.EPERM, "synthetic denied group probe")
        raise ProcessLookupError(errno.ESRCH, "synthetic absent group")

    class Controller:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, *, timeout):
            assert timeout == 40
            return self.returncode

    (tmp_path / "launches.jsonl").write_text(json.dumps({"pid": 41, "owned_group": True}))
    monkeypatch.setattr(native.os, "killpg", fake_killpg)
    monkeypatch.setattr(native.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(native.time, "sleep", fake_sleep)
    log = io.BytesIO()
    if probe_state == "transient":
        _stop(Controller(), log, tmp_path)
        assert calls == [0, 0]  # EPERM is not absence; a later ESRCH is required.
    else:
        message = (
            "exit could not be confirmed within cleanup bound"
            if probe_state == "persistent"
            else "left an owned process group alive"
        )
        with pytest.raises(pytest.fail.Exception, match=message):
            _stop(Controller(), log, tmp_path)
        if probe_state == "persistent":
            assert elapsed == 5
            assert len(calls) > 1 and set(calls) == {0}
        else:
            assert calls == [0, signal.SIGKILL, 0, 0]
    assert log.closed
