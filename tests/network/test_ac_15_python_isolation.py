"""AC-15: actual pytest lifecycle denial, with non-networking delegate tripwires.

Fresh interpreters explicitly load the real conftest after stub installation.
This proves the instrumentation, not inheritance into arbitrary child tools;
the separate kernel-isolated suite runner owns that boundary.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.network.python_network_guard import (
    NetworkAccessBlocked,
    deny_external_network,
    expected_network_denial,
    network_attempt_counts,
)

ROOT = Path(__file__).resolve().parents[2]
SECRET_HOST = "ac15-private-credential.invalid"
MODULE_APIS = (
    "create_connection",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
)
SOCKET_APIS = ("connect", "connect_ex", "sendto", "sendmsg")


def stub_transports(monkeypatch):
    calls = []

    def delegate(*args, **kwargs):
        calls.append((args, kwargs))
        return "in-memory delegate"

    for name in MODULE_APIS:
        monkeypatch.setattr(socket, name, delegate)
    for name in SOCKET_APIS:
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, delegate)
    return calls


def forbidden_operation(name):
    peer = (SECRET_HOST, 443)
    tcp = SimpleNamespace(family=socket.AF_INET)
    if name == "connect":
        return lambda: socket.socket.connect(tcp, peer)
    if name == "connect_ex":
        return lambda: socket.socket.connect_ex(tcp, peer)
    if name == "sendto":
        return lambda: socket.socket.sendto(tcp, b"synthetic", 0, peer)
    if name == "sendmsg":
        return lambda: socket.socket.sendmsg(tcp, [b"synthetic"], [], 0, peer)
    if name == "create_connection":
        return lambda: socket.create_connection(peer)
    if name == "getaddrinfo":
        return lambda: socket.getaddrinfo(SECRET_HOST, 443)
    if name == "getnameinfo":
        return lambda: socket.getnameinfo(("192.0.2.1", 443), 0)
    return lambda: getattr(socket, name)(SECRET_HOST)


@pytest.mark.parametrize("name", (*MODULE_APIS, *SOCKET_APIS))
def test_ac_15_every_python_transport_denies_before_stub_delegate(monkeypatch, name):
    calls = stub_transports(monkeypatch)
    before = network_attempt_counts()
    with deny_external_network(), expected_network_denial():
        forbidden_operation(name)()
    assert calls == []
    assert network_attempt_counts() == (before[0] + 1, before[1])


def test_ac_15_local_names_become_numeric_before_any_os_delegate(monkeypatch):
    calls = stub_transports(monkeypatch)
    before = network_attempt_counts()
    with deny_external_network():
        assert socket.gethostbyname("LOCALHOST.") == "127.0.0.1"
        assert socket.gethostbyname_ex(b"localhost.localdomain") == (
            "localhost.localdomain",
            [],
            ["127.0.0.1"],
        )
        assert socket.gethostbyaddr("::1") == ("localhost", [], ["::1"])
        assert socket.getnameinfo(("127.0.0.1", 443), 0) == ("localhost", "443")
        assert socket.getnameinfo(("::1", 443, 0, 0), socket.NI_NUMERICHOST) == ("::1", "443")
        assert calls == []  # Forward/reverse lookups above have no OS resolver delegate.
        assert socket.getaddrinfo("localhost", 443) == "in-memory delegate"
        args, kwargs = calls.pop()
        assert args == (
            "127.0.0.1",
            443,
            0,
            0,
            0,
            socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
        )
        assert kwargs == {}
        socket.getaddrinfo("localhost", "443", socket.AF_INET6, socket.SOCK_STREAM)
        assert calls.pop()[0] == (
            "::1",
            "443",
            socket.AF_INET6,
            socket.SOCK_STREAM,
            0,
            socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
        )
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("localhost", "https")
        assert calls == []
        tcp = SimpleNamespace(family=socket.AF_INET)
        tcp6 = SimpleNamespace(family=socket.AF_INET6)
        unix = SimpleNamespace(family=socket.AF_UNIX)
        socket.socket.connect(tcp, ("localhost", 80))
        socket.socket.connect_ex(tcp6, ("localhost", 80, 0, 0))
        socket.socket.sendto(tcp, b"bytes", ("localhost", 80))
        socket.create_connection(("localhost", 80))
        socket.socket.connect(unix, "/tmp/ac15-in-memory.sock")
        socket.socket.sendto(unix, b"bytes", b"\0ac15-in-memory")
        assert [args for args, _ in calls] == [
            (tcp, ("127.0.0.1", 80)),
            (tcp6, ("::1", 80, 0, 0)),
            (tcp, b"bytes", ("127.0.0.1", 80)),
            (("127.0.0.1", 80),),
            (unix, "/tmp/ac15-in-memory.sock"),
            (unix, b"bytes", b"\0ac15-in-memory"),
        ]
    assert network_attempt_counts() == before


def test_ac_15_expected_denial_cannot_acknowledge_a_noop_or_counterfeit_exception(monkeypatch):
    calls = stub_transports(monkeypatch)
    before = network_attempt_counts()
    with deny_external_network():
        with (
            pytest.raises(AssertionError, match="installed-guard denial"),
            expected_network_denial(),
        ):
            pass
        with (
            pytest.raises(AssertionError, match="exactly one installed-guard denial"),
            expected_network_denial(),
        ):
            raise NetworkAccessBlocked("counterfeit")
        with pytest.raises(ValueError, match="unrelated"), expected_network_denial():
            raise ValueError("unrelated")
        with (
            pytest.raises(AssertionError, match="cannot nest"),
            expected_network_denial(),
            expected_network_denial(),
        ):
            pass
    assert calls == []
    assert network_attempt_counts() == before


def test_ac_15_nested_guards_count_one_denial_and_restore_exact_delegates(monkeypatch):
    calls = stub_transports(monkeypatch)
    original = {name: getattr(socket, name) for name in MODULE_APIS}
    before = network_attempt_counts()
    with deny_external_network():
        outer = socket.getaddrinfo
        with deny_external_network() as inner, expected_network_denial():
            socket.getaddrinfo(SECRET_HOST, 443)
        assert socket.getaddrinfo is outer
        inner.restore()  # Idempotent restoration must not remove the outer guard.
        assert socket.getaddrinfo is outer
    assert {name: getattr(socket, name) for name in MODULE_APIS} == original
    assert calls == []
    assert network_attempt_counts() == (before[0] + 1, before[1])


PREAMBLE = """
import socket
import pytest
from tests.network.python_network_guard import expected_network_denial

def swallowed():
    try:
        socket.gethostbyname('ac15-private-credential.invalid')
    except RuntimeError:
        pass
"""
CASES = {
    "test": "def test_caught():\n    swallowed()\n",
    "collection": "swallowed()\ndef test_collected():\n    pass\n",
    "session-fixture": (
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def session_fixture():\n    swallowed()\n"
        "def test_body():\n    pass\n"
    ),
    "fixture-teardown": (
        "@pytest.fixture(autouse=True)\n"
        "def fixture():\n    yield\n    swallowed()\n"
        "def test_body():\n    pass\n"
    ),
    "expected": (
        "def test_expected():\n    with expected_network_denial():\n"
        "        socket.gethostbyname('ac15-private-credential.invalid')\n"
    ),
    "caught-invalid-scope": (
        "def test_caught_invalid_scope():\n"
        "    try:\n        with expected_network_denial():\n            swallowed()\n"
        "    except AssertionError:\n        pass\n"
    ),
    "multiple-denials": (
        "def test_caught_invalid_scope():\n"
        "    try:\n        with expected_network_denial():\n            swallowed()\n"
        "            socket.gethostbyname('ac15-private-credential.invalid')\n"
        "    except AssertionError:\n        pass\n"
    ),
    "expired-async-scope": (
        "import asyncio\n"
        "async def late_child(ready):\n    await ready.wait()\n    swallowed()\n"
        "async def exercise():\n    ready = asyncio.Event()\n"
        "    with expected_network_denial():\n"
        "        child = asyncio.create_task(late_child(ready))\n"
        "        socket.gethostbyname('ac15-private-credential.invalid')\n"
        "    ready.set()\n    await child\n"
        "def test_late_child():\n    asyncio.run(exercise())\n"
    ),
}


@pytest.mark.parametrize("phase", tuple(CASES))
@pytest.mark.parametrize("socket_policy", ("absent", "disabled", "enabled"))
def test_ac_15_real_pytest_lifecycle_cannot_swallow_denial(
    tmp_path: Path, phase: str, socket_policy: str
):
    source = tmp_path / "test_fixture.py"
    marker = "pytestmark = pytest.mark.enable_socket\n" if socket_policy == "enabled" else ""
    source.write_text(PREAMBLE + marker + CASES[phase], encoding="utf-8")
    # This subprocess never relies on the parent's monkeypatches. Stub all native
    # delegates first, then explicitly import the real root conftest as a plugin.
    plugin_args = [] if socket_policy == "absent" else ["-p", "pytest_socket"]
    if socket_policy == "disabled":
        plugin_args += ["--disable-socket", "--allow-unix-socket"]
    program = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import socket
calls = []
def tripwire(*args, **kwargs):
    calls.append('delegate')
    raise AssertionError('AC15 native delegate reached')
for name in {MODULE_APIS!r}:
    setattr(socket, name, tripwire)
for name in {SOCKET_APIS!r}:
    if hasattr(socket.socket, name):
        setattr(socket.socket, name, tripwire)
import pytest
arguments = {plugin_args!r} + ['-p', 'tests.conftest', '-q', '-p', 'no:cacheprovider']
code = pytest.main(arguments + [{str(source)!r}])
print('AC15_DELEGATES=' + str(len(calls)))
raise SystemExit(code)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (0 if phase == "expected" else 1), output
    assert "AC15_DELEGATES=0" in output
    assert "1 passed" in output  # Denial was caught; the lifecycle still rejects it.
    assert "INTERNALERROR" not in output
    if phase != "expected":
        count = 2 if phase == "multiple-denials" else 1
        assert f"{count} unacknowledged network denial(s)" in output
    assert SECRET_HOST not in output


def run_xdist_case(
    tmp_path: Path, phase: str, proof: str = "valid", workers: int = 2, distribution: str = "each"
):
    source = tmp_path / "test_fixture.py"
    source.write_text(PREAMBLE + CASES.get(phase, "def test_body():\n    pass\n"))
    plugin = tmp_path / "ac15_tripwire.py"
    plugin.write_text(
        f"""
import json
import os
import socket
from pathlib import Path
import pytest
calls = []
final_proofs = []
def tripwire(*args, **kwargs):
    Path({str(tmp_path / "native-delegate-hit")!r}).touch()
    calls.append('delegate')
    raise AssertionError('AC15 native delegate reached')
for name in {MODULE_APIS!r}:
    setattr(socket, name, tripwire)
for name in {SOCKET_APIS!r}:
    if hasattr(socket.socket, name):
        setattr(socket.socket, name, tripwire)

@pytest.hookimpl(wrapper=True)
def pytest_sessionfinish(session, exitstatus):
    if {phase!r} == 'session-hook' and (
        hasattr(session.config, 'workerinput') or {workers!r} == 0
    ):
        try:
            socket.gethostbyname({SECRET_HOST!r})
        except RuntimeError:
            pass
    result = yield
    if {phase!r} == 'session-hook-post' and (
        hasattr(session.config, 'workerinput') or {workers!r} == 0
    ):
        try:
            socket.gethostbyname({SECRET_HOST!r})
        except RuntimeError:
            pass
    if hasattr(session.config, 'workeroutput'):
        output = session.config.workeroutput
        output['ac15_tripwire_hits'] = len(calls)
        key = 'ac15_unacknowledged_network_denials_v1'
        if {proof!r} == 'missing':
            output.pop(key, None)
        elif {proof!r} != 'valid' and not {proof!r}.startswith('final-'):
            output[key] = {{
                'boolean': False, 'negative': -1, 'oversize': 2**31,
                'string': '0', 'mapping': {{}}, 'null': None,
            }}[{proof!r}]
    elif {proof!r}.startswith('final-'):
        for descriptor in final_proofs:
            path = Path(descriptor['path'])
            if {proof!r} == 'final-missing':
                path.unlink()
                continue
            if {proof!r} == 'final-oversize':
                path.write_bytes(b'x' * 257)
                continue
            if {proof!r} == 'final-fifo':
                path.unlink()
                os.mkfifo(path)
                continue
            if {proof!r} == 'final-truncated':
                path.write_bytes(b'{{"version":1')
                continue
            if {proof!r} == 'final-duplicate-field':
                data = path.read_bytes()
                path.write_bytes(data[:-1] + b',"count":0}}')
                continue
            if {proof!r} == 'final-duplicate':
                path.write_bytes(Path(final_proofs[0]['path']).read_bytes())
                continue
            proof = json.loads(path.read_text())
            if {proof!r} == 'final-boolean':
                proof['count'] = False
            elif {proof!r} == 'final-incomplete':
                proof['complete'] = False
            elif {proof!r} == 'final-repeated':
                proof['finalizations'] = 2
            elif {proof!r} == 'final-version':
                proof['version'] = 2
            path.write_text(json.dumps(proof))
    if {phase!r} == 'session-interrupted' and hasattr(session.config, 'workerinput'):
        raise KeyboardInterrupt()
    return result

@pytest.hookimpl(specname='pytest_sessionfinish', wrapper=True, tryfirst=True)
def pytest_after_workerfinished(session, exitstatus):
    result = yield
    if {phase!r} == 'session-hook-outer-post' and (
        hasattr(session.config, 'workerinput') or {workers!r} == 0
    ):
        try:
            socket.gethostbyname({SECRET_HOST!r})
        except RuntimeError:
            pass
    if {phase!r} == 'session-abrupt' and hasattr(session.config, 'workerinput'):
        os._exit(0)
    return result

@pytest.hookimpl(optionalhook=True, trylast=True)
def pytest_configure_node(node):
    final_proofs.append(node.workerinput['ac15_final_network_proof_v1'])

@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    output = getattr(node, 'workeroutput', {{}})
    reporter = node.config.pluginmanager.get_plugin('terminalreporter')
    reporter.write_line('AC15_WORKER_TRIPWIRE=' + json.dumps({{
        'worker': node.gateway.id,
        'hits': output.get('ac15_tripwire_hits'),
    }}, sort_keys=True))

def pytest_unconfigure(config):
    if not hasattr(config, 'workerinput'):
        print('AC15_CONTROLLER_TRIPWIRE=' + str(len(calls)))
""",
        encoding="utf-8",
    )
    program = f"""
import sys
sys.path[:0] = [{str(tmp_path)!r}, {str(ROOT)!r}]
from pathlib import Path
import pytest
code = pytest.main([
    '-p', 'ac15_tripwire', '-p', 'tests.conftest', '-p', 'xdist.plugin',
    '-p', 'no:cacheprovider', '-n', {str(workers)!r},
    '--dist=' + ({distribution!r} if {workers!r} else 'no'),
    '--max-worker-restart=0', '-q', {str(source)!r},
])
from ac15_tripwire import final_proofs
print('AC15_PROOF_DIRECTORY_CLEAN=' + str(all(
    not Path(descriptor['path']).parent.exists() for descriptor in final_proofs
)))
raise SystemExit(code)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert not (tmp_path / "native-delegate-hit").exists(), output
    evidence = [
        json.loads(line.split("AC15_WORKER_TRIPWIRE=", 1)[1])
        for line in output.splitlines()
        if "AC15_WORKER_TRIPWIRE=" in line
    ]
    assert sorted(evidence, key=lambda entry: entry["worker"]) == [
        {"worker": f"gw{number}", "hits": 0} for number in range(workers)
    ], output
    assert "AC15_CONTROLLER_TRIPWIRE=0" in output
    assert "AC15_PROOF_DIRECTORY_CLEAN=True" in output
    assert "INTERNALERROR" not in output
    assert SECRET_HOST not in output
    return result, output


@pytest.mark.parametrize(
    "phase", (*CASES, "session-hook", "session-hook-post", "session-hook-outer-post")
)
@pytest.mark.parametrize("workers", (0, 2))
def test_ac_15_real_xdist_workers_cannot_swallow_denial(tmp_path, phase, workers):
    result, output = run_xdist_case(tmp_path, phase, workers=workers)
    assert result.returncode == (0 if phase == "expected" else 1), output
    copies = workers or 1
    assert f"{copies} passed" in output
    if phase != "expected":
        count = copies * (2 if phase == "multiple-denials" else 1)
        assert f"{count} unacknowledged network denial(s)" in output


@pytest.mark.parametrize(
    "proof", ("missing", "boolean", "negative", "oversize", "string", "mapping", "null")
)
def test_ac_15_real_xdist_missing_or_invalid_worker_proof_cannot_pass(tmp_path, proof):
    result, output = run_xdist_case(tmp_path, "clean", proof)
    assert result.returncode == 1, output
    assert "2 passed" in output
    assert "AC-15: missing or invalid worker network-denial proof" in output


def test_ac_15_idle_xdist_worker_collection_denial_still_reaches_controller(tmp_path):
    result, output = run_xdist_case(tmp_path, "collection", distribution="load")
    assert result.returncode == 1, output
    assert "1 passed" in output
    assert "2 unacknowledged network denial(s)" in output


@pytest.mark.parametrize(
    "proof",
    (
        "final-missing",
        "final-oversize",
        "final-duplicate",
        "final-boolean",
        "final-incomplete",
        "final-repeated",
        "final-version",
        "final-fifo",
        "final-truncated",
        "final-duplicate-field",
    ),
)
def test_ac_15_real_xdist_final_worker_proof_is_mandatory(tmp_path, proof):
    result, output = run_xdist_case(tmp_path, "clean", proof)
    assert result.returncode == 1, output
    assert "2 passed" in output
    assert "AC-15: missing or invalid worker network-denial proof" in output


def test_ac_15_real_xdist_interruption_after_workerfinished_fails(tmp_path):
    result, output = run_xdist_case(tmp_path, "session-interrupted")
    assert result.returncode == 1, output
    assert "2 passed" in output
    assert "AC-15: missing or invalid worker network-denial proof" in output


def test_ac_15_real_xdist_abrupt_exit_after_workerfinished_fails(tmp_path):
    result, output = run_xdist_case(tmp_path, "session-abrupt")
    assert result.returncode == 1, output
    assert "2 passed" in output
    assert "AC-15: missing or invalid worker network-denial proof" in output


def test_ac_15_real_loadfile_accepts_zero_denial_proof_from_active_and_idle_workers(tmp_path):
    result, output = run_xdist_case(tmp_path, "clean", distribution="loadfile")
    assert result.returncode == 0, output
    assert "1 passed" in output
