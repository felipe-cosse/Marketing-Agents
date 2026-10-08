"""AC-15 browser transport profile; host installation is always mocked."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import ac_15_browser_transport as transport
from scripts import ac_15_offline as offline
from scripts.del_05_clean_state import VerificationFailure


@pytest.fixture(autouse=True)
def never_install_host_filter(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Host libc/seccomp access is forbidden in this test file")

    monkeypatch.setattr(transport.ctypes, "CDLL", forbidden)


def interpret(machine, syscall, family=0, kind=0, protocol=0, *, arch=None):
    """Tiny interpreter for precisely the fixed filter instruction vocabulary."""
    data = struct.pack(
        "<IIQ6Q",
        syscall,
        arch or transport.ARCHITECTURES[machine][0],
        0,
        family,
        kind,
        protocol,
        0,
        0,
        0,
    )
    program = transport.browser_filter(machine)
    pc = accumulator = 0
    for _ in range(len(program)):
        code, yes, no, value = program[pc]
        if code == 0x20:
            accumulator = struct.unpack_from("<I", data, value)[0]
        elif code == 0x15:
            pc += yes if accumulator == value else no
        elif code == 0x45:
            pc += yes if accumulator & value else no
        elif code == 0x54:
            accumulator &= value
        elif code == 0x06:
            return value
        else:
            raise AssertionError(f"Unsupported BPF instruction: {code}")
        pc += 1
        assert 0 <= pc < len(program)
    raise AssertionError("Filter did not terminate")


@pytest.mark.parametrize("machine", transport.ARCHITECTURES)
def test_ac_15_fixed_filter_exact_socket_family_type_and_flag_boundary(machine):
    syscall = transport.ARCHITECTURES[machine][1]
    for family in range(41):
        for kind in range(16):
            for flags in (0, 0x80000, 0x800, 0x80800):
                for protocol in (0, 6, 17, 255):
                    actual = interpret(machine, syscall, family, kind | flags, protocol)
                    expected = (
                        transport.DENY_DATAGRAM
                        if family in (2, 10) and kind == 2
                        else transport.ALLOW
                    )
                    assert actual == expected
    # Linux's family/type parameters are ints: high argument words cannot evade it.
    assert interpret(machine, syscall, (1 << 32) | 2, (1 << 32) | 2) == transport.DENY_DATAGRAM


@pytest.mark.parametrize("machine", transport.ARCHITECTURES)
def test_ac_15_filter_does_not_mask_connect_send_or_filter_install_syscalls(machine):
    socket_syscall = transport.ARCHITECTURES[machine][1]
    for syscall in range(512):
        if syscall != socket_syscall:
            assert interpret(machine, syscall, 2, 2) == transport.ALLOW


@pytest.mark.parametrize("machine", transport.ARCHITECTURES)
def test_ac_15_foreign_and_compat_abis_cannot_skip_the_socket_filter(machine):
    syscall = transport.ARCHITECTURES[machine][1]
    for arch in (0x40000003, 0x40000028, 0xDEADBEEF):
        assert interpret(machine, syscall, 2, 2, arch=arch) == transport.KILL_PROCESS
    assert interpret(machine, syscall | 0x40000000, 2, 2) == transport.KILL_PROCESS


@pytest.mark.parametrize("machine", ["armv7l", "i686", "AMD64", "unknown"])
def test_ac_15_unsupported_architecture_fails_before_filter_construction(machine):
    with pytest.raises(VerificationFailure, match="architecture_unsupported"):
        transport.browser_filter(machine)


@pytest.fixture
def installer(monkeypatch):
    calls, checks, captured = [], [], []
    responses = {21: [2, 2], 39: [1, 1], 22: [0]}

    def prctl(*arguments):
        calls.append(arguments)
        if arguments[0] == 22:
            program = ctypes.cast(arguments[2], ctypes.POINTER(transport._SockFprog)).contents
            captured.extend(
                (row.code, row.jt, row.jf, row.k) for row in program.filters[: program.length]
            )
        return responses[arguments[0]].pop(0)

    monkeypatch.setattr(transport.sys, "platform", "linux")
    monkeypatch.setattr(transport.sys, "byteorder", "little")
    monkeypatch.setattr(transport.os, "getuid", lambda: 10001)
    monkeypatch.setattr(transport.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(transport, "_validate_process_state", lambda: checks.append(True))
    monkeypatch.setattr(transport, "_load_prctl", lambda: prctl)
    return SimpleNamespace(calls=calls, checks=checks, captured=captured, responses=responses)


def test_ac_15_installer_preserves_existing_policy_and_uses_native_width_abi(installer):
    result = transport.install_browser_transport()
    assert len(installer.checks) == 2
    assert [call[0] for call in installer.calls] == [21, 39, 22, 21, 39]
    assert all(len(call) == 5 for call in installer.calls)
    assert installer.calls[2][1] == 2 and installer.calls[2][3:] == (0, 0)
    assert tuple(installer.captured) == transport.browser_filter("aarch64")
    assert result == {
        "schema_version": 1,
        "profile": "browser_no_ip_datagram_sockets",
        "architecture": "aarch64",
        "installed": True,
        "denial_errno": "EAFNOSUPPORT",
        "existing_seccomp_preserved": True,
        "single_threaded_install": True,
        "no_inherited_socket_descriptors": True,
        "filter_sha256": hashlib.sha256(
            b"".join(struct.pack("<HBBI", *row) for row in installer.captured)
        ).hexdigest(),
    }


@pytest.mark.parametrize(
    "operation,responses,message,installed",
    [
        (21, [0], "existing_seccomp_required", False),
        (21, [1], "existing_seccomp_required", False),
        (21, [-1], "existing_seccomp_required", False),
        (39, [0], "no_new_privileges_required", False),
        (39, [-1], "no_new_privileges_required", False),
        (22, [-1], "install_failed", True),
        (21, [2, 0], "postcondition_failed", True),
        (39, [1, 0], "postcondition_failed", True),
    ],
)
def test_ac_15_installer_fails_closed_on_kernel_denial_or_bad_postcondition(
    installer, operation, responses, message, installed
):
    installer.responses[operation] = responses
    with pytest.raises(VerificationFailure, match=message):
        transport.install_browser_transport()
    assert any(call[0] == 22 for call in installer.calls) is installed


@pytest.mark.parametrize("bad_state", ["platform", "byteorder", "width", "uid", "arch"])
def test_ac_15_installer_rejects_unsupported_process_without_touching_libc(
    installer, monkeypatch, bad_state
):
    if bad_state == "platform":
        monkeypatch.setattr(transport.sys, "platform", "darwin")
    elif bad_state == "byteorder":
        monkeypatch.setattr(transport.sys, "byteorder", "big")
    elif bad_state == "width":
        monkeypatch.setattr(transport.ctypes, "sizeof", lambda value: 4)
    elif bad_state == "uid":
        monkeypatch.setattr(transport.os, "getuid", lambda: 0)
    else:
        monkeypatch.setattr(transport.platform, "machine", lambda: "i686")
    with pytest.raises(VerificationFailure, match="browser_transport_"):
        transport.install_browser_transport()
    assert not installer.calls


def test_ac_15_installer_rechecks_process_state_before_irreversible_operation(
    installer, monkeypatch
):
    def validate():
        if installer.checks:
            raise VerificationFailure("browser_transport_socket_present")
        installer.checks.append(True)

    monkeypatch.setattr(transport, "_validate_process_state", validate)
    with pytest.raises(VerificationFailure, match="socket_present"):
        transport.install_browser_transport()
    assert [call[0] for call in installer.calls] == [21, 39]


def test_ac_15_libc_binding_declares_full_prctl_signature(monkeypatch):
    fake = SimpleNamespace()
    calls = []

    def library(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(prctl=fake)

    monkeypatch.setattr(transport.ctypes, "CDLL", library)
    assert transport._load_prctl() is fake
    assert calls == [((None,), {"use_errno": True})]
    assert fake.argtypes == [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    assert fake.restype is ctypes.c_int


@pytest.mark.parametrize("failure", [OSError("private-loader-details"), AttributeError("private")])
def test_ac_15_libc_loading_reports_only_categorical_failure(monkeypatch, failure):
    def library(*args, **kwargs):
        raise failure

    monkeypatch.setattr(transport.ctypes, "CDLL", library)
    with pytest.raises(VerificationFailure, match=r"^browser_transport_prctl_unavailable$"):
        transport._load_prctl()


@pytest.fixture
def proc(tmp_path):
    (tmp_path / "task" / str(os.getpid())).mkdir(parents=True)
    (tmp_path / "fd").mkdir()
    (tmp_path / "fd" / "0").symlink_to("/dev/null")
    (tmp_path / "fd" / "1").symlink_to("pipe:[123]")
    return tmp_path


def test_ac_15_process_state_accepts_only_one_thread_and_nonsocket_descriptors(proc):
    transport._validate_process_state(proc)


@pytest.mark.parametrize("bad_state", ["thread", "socket", "opaque_fd", "missing", "regular_fd"])
def test_ac_15_process_state_fails_closed_for_unproven_threads_or_descriptors(proc, bad_state):
    if bad_state == "thread":
        (proc / "task" / "extra").mkdir()
    elif bad_state == "socket":
        (proc / "fd" / "2").symlink_to("socket:[123]")
    elif bad_state == "opaque_fd":
        (proc / "fd" / "opaque").symlink_to("/dev/null")
    elif bad_state == "missing":
        proc = proc / "absent"
    else:
        (proc / "fd" / "2").touch()
    with pytest.raises(VerificationFailure, match="browser_transport_"):
        transport._validate_process_state(proc)


POSITIVE_OUTPUT = {
    "tcp_exchange": True,
    "unix_datagram_exchange": True,
    "netlink_kernel_exchange": True,
}
POSITIVE_TRACE = (
    b"1 connect(4, {sa_family=AF_INET, sin_port=htons(123), "
    b'sin_addr=inet_addr("127.0.0.1")}, 16) = 0\n'
    b'1 connect(5, {sa_family=AF_UNIX, sun_path="/tmp/private-server"}, 24) = 0\n'
    b"1 connect(6, {sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, 12) = 0\n"
    b'1 sendto(6, "", 32, 0, NULL, 0) = 32\n'
)


@pytest.fixture
def collector():
    calls = []
    outputs = [{"ip_datagram_denials": 8}, {"ip_datagram_denials": 8}, POSITIVE_OUTPUT]
    summaries = [
        offline.trace_summary(b""),
        offline.trace_summary(b""),
        offline.trace_summary(POSITIVE_TRACE),
    ]
    results = [
        subprocess.CompletedProcess([], 0, json.dumps(value).encode(), b"") for value in outputs
    ]

    def collect(command, *, timeout):
        index = len(calls)
        calls.append((command, timeout))
        return results[index], summaries[index]

    return SimpleNamespace(collect=collect, calls=calls, results=results, summaries=summaries)


def test_ac_15_proof_requires_child_grandchild_and_audited_positive_exchanges(collector):
    assert transport.prove_browser_transport(collector.collect) == {
        "child_ip_datagram_denials": 8,
        "grandchild_ip_datagram_denials": 8,
        "tcp_loopback_exchange": True,
        "unix_datagram_exchange": True,
        "netlink_kernel_exchange": True,
    }
    assert len(collector.calls) == 3
    for command, timeout in collector.calls:
        assert command[:4] == (sys.executable, "-I", "-S", "-c")
        assert timeout == 10
        assert "prctl" not in command[-1] and "ctypes" not in command[-1]
        compile(command[-1], "<canary>", "exec")
    assert collector.calls[0][0][-1] == transport.DATAGRAM_CANARY
    assert "subprocess.run" in collector.calls[1][0][-1]
    assert repr(transport.DATAGRAM_CANARY) in collector.calls[1][0][-1]
    assert collector.calls[2][0][-1] == transport.POSITIVE_CANARY


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize(
    "failure", ["exit", "json", "count", "oversize", "utf8", "rejected", "filter"]
)
def test_ac_15_any_unproven_control_aborts_without_later_controls(collector, index, failure):
    result = collector.results[index]
    if failure == "exit":
        result.returncode = 1
    elif failure == "json":
        result.stdout = b"not-json"
    elif failure == "count":
        result.stdout = b'{"ip_datagram_denials":7}'
    elif failure == "oversize":
        result.stdout = b" " * 1025
    elif failure == "utf8":
        result.stdout = b"\xff"
    else:
        collector.summaries[index]["rejected" if failure == "rejected" else "filter_changes"] = 1
    with pytest.raises(VerificationFailure, match="browser_transport_canary_"):
        transport.prove_browser_transport(collector.collect)
    assert len(collector.calls) == index + 1


@pytest.mark.parametrize("field", ["loopback", "unix", "kernel_ipc"])
@pytest.mark.parametrize("value", [None, 0, -1, True, "1"])
def test_ac_15_success_text_cannot_substitute_for_observed_allowed_transport(
    collector, field, value
):
    collector.summaries[2][field] = value
    with pytest.raises(VerificationFailure, match="positive_trace_missing"):
        transport.prove_browser_transport(collector.collect)


def test_ac_15_strict_trace_failure_is_not_converted_to_success():
    def collect(*args, **kwargs):
        raise VerificationFailure("network_trace_unclassified_record")

    with pytest.raises(VerificationFailure, match="network_trace_unclassified_record"):
        transport.prove_browser_transport(collect)


@pytest.mark.parametrize("denial", [errno.EAFNOSUPPORT, errno.EPERM, None])
def test_ac_15_canary_requires_exact_kernel_denial_for_both_families_and_all_flags(
    monkeypatch, capsys, denial
):
    calls, closed = [], []

    def socket(family, kind):
        calls.append((family, kind))
        if denial is not None:
            raise OSError(denial, "injected")
        return SimpleNamespace(close=lambda: closed.append(True))

    monkeypatch.setitem(
        sys.modules,
        "socket",
        SimpleNamespace(
            socket=socket,
            AF_INET=2,
            AF_INET6=10,
            SOCK_DGRAM=2,
            SOCK_CLOEXEC=0x80000,
            SOCK_NONBLOCK=0x800,
        ),
    )
    if denial == errno.EAFNOSUPPORT:
        exec(compile(transport.DATAGRAM_CANARY, "<canary>", "exec"), {})
        assert json.loads(capsys.readouterr().out) == {"ip_datagram_denials": 8}
        assert calls == [
            (family, 2 | flags) for family in (2, 10) for flags in (0, 0x80000, 0x800, 0x80800)
        ]
    else:
        with pytest.raises(AssertionError):
            exec(compile(transport.DATAGRAM_CANARY, "<canary>", "exec"), {})
        assert len(calls) == 1
        assert closed == ([True] if denial is None else [])
