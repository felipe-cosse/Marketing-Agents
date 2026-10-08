"""OBJ-04 / AC-15 guard controls need no native frontend dependencies or real egress."""

from __future__ import annotations

import errno
import io
import json
import signal
import socket
import subprocess
import sys
from pathlib import Path

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


_OBJ04_CANARY_PROBE = r"""
import errno
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from tests.acceptance.obj_04_guard import GuardRecorder
from tests.network import python_network_guard as policy

report = Path(sys.argv[2])
mode = sys.argv[3]
boundaries = (
    "getaddrinfo", "create_connection", "connect", "connect_ex", "sendto"
)
assert mode == "healthy" or mode in boundaries
real_socket = socket.socket
allocations = {"stream": 0, "datagram": 0}
native_delegate_calls = 0
observed = []
sendto_sockets = []


class NoIPDatagrams(real_socket):
    def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM,
                 proto=0, fileno=None):
        if family in (socket.AF_INET, socket.AF_INET6):
            if type & 0xF == socket.SOCK_DGRAM:
                allocations["datagram"] += 1
                raise OSError(errno.EAFNOSUPPORT, "datagram_allocation_forbidden")
            if type & 0xF == socket.SOCK_STREAM:
                allocations["stream"] += 1
        super().__init__(family, type, proto, fileno=fileno)


def native_backstop(*args, **kwargs):
    global native_delegate_calls
    native_delegate_calls += 1
    raise AssertionError("native_delegate_backstop")


# Allocate a real stream socket, but no test failure may reach a real network
# delegate. These backstops sit BELOW the recorder's own delegate tripwires.
socket.socket = NoIPDatagrams
for name in ("connect", "connect_ex", "sendto", "sendmsg"):
    if hasattr(NoIPDatagrams, name):
        setattr(NoIPDatagrams, name, native_backstop)
for name in (
    "getaddrinfo", "create_connection", "gethostbyname", "gethostbyname_ex",
    "gethostbyaddr", "getnameinfo",
):
    setattr(socket, name, native_backstop)

real_install = policy.install_network_guard


def controlled_install():
    installed = real_install()
    if mode != "healthy":
        # Restore just this method's recorder tripwire, not its OS delegate.
        matches = [item for item in installed.originals if item[1] == mode]
        assert len(matches) == 1
        owner, name, tripwire = matches[0]
        setattr(owner, name, tripwire)
    for name in boundaries:
        owner = socket if name in ("getaddrinfo", "create_connection") else socket.socket
        guarded = getattr(owner, name)

        def witness(*args, _name=name, _guarded=guarded, **kwargs):
            observed.append(_name)
            if _name == "sendto":
                instance = args[0]
                assert isinstance(instance, real_socket)
                assert instance.fileno() >= 0
                sendto_sockets.append({
                    "family": int(instance.family), "type": int(instance.type)
                })
            return _guarded(*args, **kwargs)

        setattr(owner, name, witness)
    return installed


policy.install_network_guard = controlled_install
recorder = GuardRecorder(report, "isolated-regression")
before = policy.network_attempt_counts()
after_install = None
outcome = "not_started"
try:
    recorder.install()
    after_install = policy.network_attempt_counts()
    if mode != "healthy":
        outcome = "broken_boundary_not_detected"
    else:
        # Catching a later application denial must not reset either ledger or
        # reclassify it as one of the five startup canaries.
        try:
            socket.getaddrinfo("obj04.invalid", 443)
        except policy.NetworkAccessBlocked:
            outcome = "healthy"
        else:
            outcome = "post_install_denial_missing"
except OSError as error:
    outcome = (
        "datagram_allocation_forbidden"
        if error.errno == errno.EAFNOSUPPORT and allocations["datagram"]
        else "unexpected_os_error"
    )
except AssertionError as error:
    outcome = (
        "tripwire"
        if str(error) == "OBJ-04 external call reached delegate tripwire"
        else "native_delegate_backstop"
        if str(error) == "native_delegate_backstop"
        else "unexpected_assertion"
    )
except Exception:
    outcome = "unexpected_exception"

fields = ("canaries_blocked", "external_attempts", "delegate_violations")
persisted = json.loads(report.read_text()) if report.exists() else {}
print(json.dumps({
    "outcome": outcome,
    "allocations": allocations,
    "native_delegate_calls": native_delegate_calls,
    "boundaries": observed,
    "sendto_sockets": sendto_sockets,
    "ledger_before": before,
    "ledger_after_install": after_install,
    "ledger_after": policy.network_attempt_counts(),
    "canary_phase": recorder.canary_phase,
    "counts": {name: recorder.data[name] for name in fields},
    "persisted_counts": {name: persisted.get(name) for name in fields},
}, sort_keys=True))
"""


def _obj04_isolated_canary_probe(tmp_path, mode):
    root = Path(python_network_guard.__file__).resolve().parents[2]
    before_counts = python_network_guard.network_attempt_counts()
    before_apis = (
        socket.socket,
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.socket.sendto,
        socket.getaddrinfo,
        socket.create_connection,
    )
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            _OBJ04_CANARY_PROBE,
            str(root),
            str(tmp_path / "child-report.json"),
            mode,
        ),
        cwd=tmp_path,
        env={},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert python_network_guard.network_attempt_counts() == before_counts
    assert (
        socket.socket,
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.socket.sendto,
        socket.getaddrinfo,
        socket.create_connection,
    ) == before_apis
    assert result.returncode == 0
    assert result.stderr == ""
    proof = json.loads(result.stdout)
    assert proof["native_delegate_calls"] == 0, proof
    return proof


def test_obj_04_canaries_need_no_ip_datagram_and_preserve_post_install_ledger(tmp_path):
    proof = _obj04_isolated_canary_probe(tmp_path, "healthy")
    assert proof["outcome"] == "healthy", proof
    assert proof["allocations"] == {"stream": 1, "datagram": 0}
    assert proof["native_delegate_calls"] == 0
    assert proof["boundaries"] == [
        "getaddrinfo",
        "create_connection",
        "connect",
        "connect_ex",
        "sendto",
        "getaddrinfo",
    ]
    assert proof["sendto_sockets"] == [
        {"family": int(socket.AF_INET), "type": int(socket.SOCK_STREAM)}
    ]
    assert proof["ledger_before"] == [0, 0]
    # Recorder startup canaries are caught intentionally inside install(); they
    # are not pytest expected-denial scopes and must remain in the child ledger.
    assert proof["ledger_after_install"] == [5, 5]
    assert proof["ledger_after"] == [6, 6]
    assert proof["canary_phase"] is False
    assert (
        proof["counts"]
        == proof["persisted_counts"]
        == {
            "canaries_blocked": 5,
            "external_attempts": 1,
            "delegate_violations": 0,
        }
    )


@pytest.mark.parametrize(
    "boundary,preceding",
    (
        ("getaddrinfo", 0),
        ("create_connection", 1),
        ("connect", 2),
        ("connect_ex", 3),
        ("sendto", 4),
    ),
)
def test_obj_04_each_broken_canary_hits_tripwire_without_datagram_or_egress(
    tmp_path, boundary, preceding
):
    proof = _obj04_isolated_canary_probe(tmp_path, boundary)
    assert proof["outcome"] == "tripwire", proof
    assert proof["allocations"] == {"stream": 1, "datagram": 0}
    assert proof["native_delegate_calls"] == 0
    assert (
        proof["boundaries"]
        == [
            "getaddrinfo",
            "create_connection",
            "connect",
            "connect_ex",
            "sendto",
        ][: preceding + 1]
    )
    assert proof["sendto_sockets"] == (
        [{"family": int(socket.AF_INET), "type": int(socket.SOCK_STREAM)}]
        if boundary == "sendto"
        else []
    )
    assert proof["ledger_before"] == [0, 0]
    assert proof["ledger_after_install"] is None
    assert proof["ledger_after"] == [preceding, preceding]
    assert proof["canary_phase"] is True
    assert (
        proof["counts"]
        == proof["persisted_counts"]
        == {
            "canaries_blocked": preceding,
            "external_attempts": 0,
            "delegate_violations": 1,
        }
    )
