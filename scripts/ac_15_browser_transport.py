"""Browser-only AC-15 profile: IP datagram sockets do not exist in this runtime.

The trusted runner verifies source and the route-free namespace BEFORE calling
this helper. It layers a fixed filter onto Docker's existing policy, before
starting strace or test children. Destination-bearing connect/send operations
are never filtered here: the ordinary strict syscall auditor still owns them.
This is not UDP, DNS, QUIC, WebRTC, or unrestricted Chromium-network evidence.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import struct
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from scripts.del_05_clean_state import VerificationFailure, require

# Linux UAPI audit architecture and native socket syscall, little-endian only.
ARCHITECTURES = {"aarch64": (0xC00000B7, 198), "x86_64": (0xC000003E, 41)}
ALLOW = 0x7FFF0000
KILL_PROCESS = 0x80000000
DENY_DATAGRAM = 0x00050000 | 97  # SECCOMP_RET_ERRNO | Linux EAFNOSUPPORT


class TraceCollector(Protocol):
    def __call__(
        self, command: tuple[str, ...], *, timeout: float
    ) -> tuple[subprocess.CompletedProcess[bytes], dict]: ...


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(_SockFilter))]


def browser_filter(machine: str) -> tuple[tuple[int, int, int, int], ...]:
    """Fixed cBPF; foreign audit ABI and x32-tagged syscalls fail closed."""
    require(machine in ARCHITECTURES, "browser_transport_architecture_unsupported")
    arch, socket_number = ARCHITECTURES[machine]
    return (
        (0x20, 0, 0, 4),  # LD seccomp_data.arch
        (0x15, 1, 0, arch),
        (0x06, 0, 0, KILL_PROCESS),
        (0x20, 0, 0, 0),  # LD seccomp_data.nr
        (0x45, 0, 1, 0x40000000),  # x32 has the same audit arch as x86_64.
        (0x06, 0, 0, KILL_PROCESS),
        (0x15, 0, 7, socket_number),
        (0x20, 0, 0, 16),  # Low word of int family, seccomp_data.args[0].
        (0x15, 1, 0, 2),  # AF_INET
        (0x15, 0, 4, 10),  # AF_INET6
        (0x20, 0, 0, 24),  # Low word of int type, seccomp_data.args[1].
        (0x54, 0, 0, 0xF),  # SOCK_TYPE_MASK: exclude CLOEXEC/NONBLOCK flags.
        (0x15, 0, 1, 2),  # SOCK_DGRAM, independent of protocol.
        (0x06, 0, 0, DENY_DATAGRAM),
        (0x06, 0, 0, ALLOW),
    )


def _validate_process_state(proc: Path = Path("/proc/self")) -> None:
    try:
        tasks = [entry.name for entry in (proc / "task").iterdir()]
        require(tasks == [str(os.getpid())], "browser_transport_requires_single_thread")
        # Keep scandir's directory FD open while examining procfs. A listdir
        # snapshot could contain its own already-closed enumeration descriptor.
        with os.scandir(proc / "fd") as entries:
            for entry in entries:
                require(entry.name.isdecimal(), "browser_transport_descriptor_unparsed")
                target = os.readlink(entry.path)
                require(not target.startswith("socket:"), "browser_transport_socket_present")
    except OSError as error:
        raise VerificationFailure("browser_transport_process_state_unavailable") from error


def _load_prctl():
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError) as error:
        raise VerificationFailure("browser_transport_prctl_unavailable") from error
    # prctl is variadic in libc; supply all five native-width arguments explicitly.
    prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    prctl.restype = ctypes.c_int
    return prctl


def install_browser_transport() -> dict:
    """Install in the trusted single-threaded Linux container runner."""
    require(
        sys.platform == "linux"
        and sys.byteorder == "little"
        and ctypes.sizeof(ctypes.c_void_p) == ctypes.sizeof(ctypes.c_ulong) == 8,
        "browser_transport_platform_unsupported",
    )
    require(os.getuid() == 10001, "browser_transport_requires_container_user")
    machine = platform.machine()
    instructions = browser_filter(machine)
    _validate_process_state()
    prctl = _load_prctl()
    require(prctl(21, 0, 0, 0, 0) == 2, "browser_transport_existing_seccomp_required")
    require(prctl(39, 0, 0, 0, 0) == 1, "browser_transport_no_new_privileges_required")
    filters = (_SockFilter * len(instructions))(*(_SockFilter(*row) for row in instructions))
    program = _SockFprog(len(filters), filters)
    # Recheck after loading libc and preparing the program, before the irreversible
    # operation. No untrusted code or new thread is invoked between here and prctl.
    _validate_process_state()
    require(
        prctl(22, 2, ctypes.addressof(program), 0, 0) == 0,
        "browser_transport_install_failed",
    )
    require(
        prctl(21, 0, 0, 0, 0) == 2 and prctl(39, 0, 0, 0, 0) == 1,
        "browser_transport_postcondition_failed",
    )
    return {
        "schema_version": 1,
        "profile": "browser_no_ip_datagram_sockets",
        "architecture": machine,
        "installed": True,
        "denial_errno": "EAFNOSUPPORT",
        "existing_seccomp_preserved": True,
        "single_threaded_install": True,
        "no_inherited_socket_descriptors": True,
        "filter_sha256": hashlib.sha256(
            b"".join(struct.pack("<HBBI", *row) for row in instructions)
        ).hexdigest(),
    }


DATAGRAM_CANARY = """
import errno, json, socket
denied = 0
for family in (socket.AF_INET, socket.AF_INET6):
    for flags in (0, socket.SOCK_CLOEXEC, socket.SOCK_NONBLOCK,
                  socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK):
        try:
            client = socket.socket(family, socket.SOCK_DGRAM | flags)
        except OSError as error:
            assert error.errno == errno.EAFNOSUPPORT, "wrong datagram denial"
            denied += 1
        else:
            client.close()
            raise AssertionError("IP datagram socket was created")
assert denied == 8
print(json.dumps({"ip_datagram_denials": denied}))
"""

POSITIVE_CANARY = """
import json, socket, struct, tempfile
def receive_exact(client, size):
    result = b""
    while len(result) < size:
        part = client.recv(size - len(result))
        assert part, "TCP peer closed early"
        result += part
    return result
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.settimeout(2)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(server.getsockname())
        peer, _ = server.accept()
        with peer:
            peer.settimeout(2)
            client.sendall(b"tcp-request")
            assert receive_exact(peer, len(b"tcp-request")) == b"tcp-request"
            peer.sendall(b"tcp-response")
            assert receive_exact(client, len(b"tcp-response")) == b"tcp-response"
with tempfile.TemporaryDirectory() as directory:
    with (socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server,
          socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client):
        server.settimeout(2)
        client.settimeout(2)
        server.bind(directory + "/server")
        client.bind(directory + "/client")
        client.connect(directory + "/server")
        client.send(b"unix-request")
        message, address = server.recvfrom(64)
        assert message == b"unix-request"
        server.sendto(b"unix-response", address)
        assert client.recv(64) == b"unix-response"
with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as client:
    client.settimeout(2)
    client.connect((0, 0))
    payload = struct.pack("=BxHiII", socket.AF_UNSPEC, 0, 0, 0, 0)
    request = struct.pack("=IHHII", 16 + len(payload), 18, 0x301, 73, 0) + payload
    assert client.send(request) == len(request)
    reply = client.recv(65536)
    assert len(reply) >= 32
    size, kind, flags, sequence, pid = struct.unpack("=IHHII", reply[:16])
    assert 32 <= size <= len(reply) and kind == 16 and sequence == 73
print(json.dumps({"tcp_exchange": True, "unix_datagram_exchange": True,
                  "netlink_kernel_exchange": True}))
"""


def _prove(collect_trace: TraceCollector, source: str, expected: dict) -> dict:
    result, summary = collect_trace((sys.executable, "-I", "-S", "-c", source), timeout=10)
    require(result.returncode == 0, "browser_transport_canary_failed")
    require(len(result.stdout) <= 1024, "browser_transport_canary_output_invalid")
    try:
        observed = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise VerificationFailure("browser_transport_canary_output_invalid") from error
    require(observed == expected, "browser_transport_canary_output_invalid")
    require(
        summary.get("rejected") == summary.get("filter_changes") == 0,
        "browser_transport_canary_trace_rejected",
    )
    return summary


def transport_canary_commands() -> tuple[tuple[str, ...], ...]:
    """The host and target bind the same exact controlled-child inventory."""
    grandchild = (
        "import json,subprocess,sys; "
        "result=subprocess.run([sys.executable,'-I','-S','-c',"
        + repr(DATAGRAM_CANARY)
        + "],capture_output=True,timeout=5,check=False); "
        "assert result.returncode == 0; "
        "assert json.loads(result.stdout) == {'ip_datagram_denials':8}; "
        "print(json.dumps({'ip_datagram_denials':8}))"
    )
    return tuple(
        (sys.executable, "-I", "-S", "-c", code)
        for code in (DATAGRAM_CANARY, grandchild, POSITIVE_CANARY)
    )


def prove_browser_transport(collect_trace: TraceCollector) -> dict:
    """Real kernel-denial and allowed-transport controls, always under strict tracing."""
    commands = transport_canary_commands()
    expected = {"ip_datagram_denials": 8}
    _prove(collect_trace, commands[0][-1], expected)
    _prove(collect_trace, commands[1][-1], expected)
    summary = _prove(
        collect_trace,
        POSITIVE_CANARY,
        {"tcp_exchange": True, "unix_datagram_exchange": True, "netlink_kernel_exchange": True},
    )
    require(
        all(
            type(summary.get(name)) is int and summary[name] > 0
            for name in ("loopback", "unix", "kernel_ipc")
        ),
        "browser_transport_positive_trace_missing",
    )
    return {
        "child_ip_datagram_denials": 8,
        "grandchild_ip_datagram_denials": 8,
        "tcp_loopback_exchange": True,
        "unix_datagram_exchange": True,
        "netlink_kernel_exchange": True,
    }
