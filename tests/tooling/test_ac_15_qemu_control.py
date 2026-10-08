"""Execute emitted ARM words in a fake machine, never native code or networking."""

from __future__ import annotations

import contextlib
import json
import struct
from pathlib import Path

import pytest

from scripts import ac_15_offline as offline
from scripts import ac_15_qemu_control as control
from scripts.del_05_clean_state import VerificationFailure


@pytest.fixture(autouse=True)
def forbid_native_execution(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("native processes and sockets are forbidden in the fixed control tests")

    monkeypatch.setattr(control.boundary.subprocess, "Popen", forbidden)
    monkeypatch.setattr(control.boundary.ctypes, "CDLL", forbidden)
    monkeypatch.setattr(control.socket, "socket", forbidden)


class ArmMachine:
    """Small exact-word ARM interpreter; every syscall is a deterministic fake."""

    def __init__(self, image, *, returns=None, release=b"G"):
        self.memory = {0x10000 + index: byte for index, byte in enumerate(image)}
        self.registers = [0] * 16
        self.registers[13] = 0x200000
        self.registers[15] = struct.unpack_from("<I", image, 24)[0]
        self.zero = self.negative = self.overflow = False
        self.calls = []
        self.returns = returns or {}
        self.release = release
        self.output = b""
        self.socket_count = 0
        self.connect_count = 0
        self.close_count = 0

    def read(self, address, size):
        return bytes(self.memory[address + index] for index in range(size))

    def syscall(self):
        number = self.registers[7]
        args = tuple(self.registers[:3])
        self.calls.append((number, args))
        if number == 4:
            assert args[0] == 1 and args[2] == 1
            self.output += self.read(args[1], args[2])
            key, result = "write", 1
        elif number == 3:
            assert args[0] == 0 and args[2] == 1
            if self.release:
                self.memory[args[1]] = self.release[0]
            key, result = "read", len(self.release)
        elif number == 281:
            self.socket_count += 1
            assert args == ((2 if self.socket_count == 1 else 1), 1, 0)
            key, result = f"socket{self.socket_count}", 10 + self.socket_count
        elif number == 283:
            self.connect_count += 1
            assert args[0] == 10 + self.connect_count
            key = f"connect{self.connect_count}"
            result = -101 if self.connect_count == 1 else 0
        elif number == 6:
            self.close_count += 1
            assert args[0] == 10 + self.close_count
            key, result = f"close{self.close_count}", 0
        elif number == 1:
            return args[0]
        else:
            raise AssertionError(f"unexpected syscall: {number}")
        self.registers[0] = self.returns.get(key, result) & 0xFFFFFFFF
        return None

    def run(self):
        for _ in range(512):
            pc = self.registers[15]
            word = struct.unpack("<I", self.read(pc, 4))[0]
            self.registers[15] += 4
            condition = word >> 28
            enabled = {
                0: self.zero,
                1: not self.zero,
                0xB: self.negative != self.overflow,
                0xE: True,
            }[condition]
            if not enabled:
                continue
            if word & 0x0F000000 == 0x0A000000:
                displacement = word & 0xFFFFFF
                if displacement & 0x800000:
                    displacement -= 1 << 24
                self.registers[15] = pc + 8 + displacement * 4
            elif word == 0xEF000000:
                status = self.syscall()
                if status is not None:
                    return status
            elif word & 0x0FFF0000 == 0x059F0000:
                register = (word >> 12) & 15
                self.registers[register] = struct.unpack(
                    "<I", self.read(pc + 8 + (word & 4095), 4)
                )[0]
            elif word & 0x0FF00000 in (0x05C00000, 0x05D00000):
                base, register = (word >> 16) & 15, (word >> 12) & 15
                address = self.registers[base] + (word & 4095)
                if word & 0x00100000:
                    self.registers[register] = self.memory[address]
                else:
                    self.memory[address] = self.registers[register] & 255
            elif word & 0x0FE00000 == 0x03A00000:
                assert word & 0xF00 == 0  # emitted MOV immediates need no rotation
                self.registers[(word >> 12) & 15] = word & 255
            elif word & 0x0FE00FF0 == 0x01A00000:
                self.registers[(word >> 12) & 15] = self.registers[word & 15]
            elif word == 0xE24DD004:
                self.registers[13] -= 4
            elif word & 0x0FF00000 in (0x03500000, 0x03700000):
                left, right = self.registers[(word >> 16) & 15], word & 255
                assert word & 0xF00 == 0
                if word & 0x0FF00000 == 0x03700000:
                    result = (left + right) & 0xFFFFFFFF
                    self.overflow = bool((~(left ^ right) & (left ^ result)) & 0x80000000)
                else:
                    result = (left - right) & 0xFFFFFFFF
                    self.overflow = bool(((left ^ right) & (left ^ result)) & 0x80000000)
                self.zero, self.negative = result == 0, bool(result & 0x80000000)
            else:
                raise AssertionError(f"unsupported emitted instruction {word:08x}")
        raise AssertionError("guest failed to terminate within the synthetic instruction bound")


def test_emitted_guest_is_static_arm_eabi5_rx_with_exact_segment_bound():
    image = control._guest_image(b"/owned/local.sock")
    assert image[:7] == b"\x7fELF\x01\x01\x01"
    assert struct.unpack_from("<HHI", image, 16) == (2, 40, 1)
    assert struct.unpack_from("<IIII", image, 24) == (0x10054, 52, 0, 0x05000000)
    assert struct.unpack_from("<HHHHHH", image, 40) == (52, 32, 1, 0, 0, 0)
    assert struct.unpack_from("<IIIIIIII", image, 52) == (
        1,
        0,
        0x10000,
        0x10000,
        len(image),
        len(image),
        5,
        0x10000,
    )
    assert len(image) < 4096
    prefix = control.boundary._emulation_handshake()
    assert image[84 : 84 + len(prefix)] == prefix


@pytest.mark.parametrize("path", [b"/owned/local.sock", b"/" + b"x" * 106])
def test_exact_guest_words_issue_only_fixed_stream_connects_and_close_both(path):
    machine = ArmMachine(control._guest_image(path))
    assert machine.run() == 42
    assert machine.output == b"R"
    assert [number for number, _ in machine.calls] == [4, 3, 281, 283, 6, 281, 283, 6, 1]
    connects = [args for number, args in machine.calls if number == 283]
    assert connects[0][2] == 16
    assert machine.read(connects[0][1], connects[0][2]) == (
        b"\x02\x00\x00\x09\xc0\x00\x02\x01" + bytes(8)
    )
    assert connects[1][2] == len(path) + 3
    assert machine.read(connects[1][1], connects[1][2]) == b"\x01\x00" + path + b"\0"


@pytest.mark.parametrize("path", [b"", b"relative", b"/bad\0suffix", b"/" + b"x" * 107, "/text"])
def test_bad_unix_paths_rejected_before_guest_creation(path):
    with pytest.raises(VerificationFailure, match="qemu_control_unix_path_invalid"):
        control._guest_image(path)


@pytest.mark.parametrize(
    ("operation", "value", "connects"),
    [
        ("write", 0, 0),
        ("write", -1, 0),
        ("read", 0, 0),
        ("read", -4, 0),
        ("socket1", -1, 0),
        ("socket1", -24, 0),
        ("connect1", 0, 1),
        ("connect1", -1, 1),
        ("connect1", -13, 1),
        ("connect1", -111, 1),
        ("connect1", 101, 1),
        ("close1", -4, 1),
        ("socket2", -1, 1),
        ("connect2", -1, 2),
        ("connect2", -111, 2),
        ("connect2", 1, 2),
        ("close2", -4, 2),
    ],
)
def test_guest_fails_closed_for_every_handshake_socket_connect_and_close_error(
    operation, value, connects
):
    machine = ArmMachine(control._guest_image(b"/owned/local.sock"), returns={operation: value})
    assert machine.run() == 43
    assert machine.connect_count == connects


@pytest.mark.parametrize("release", [b"X", b"\0", b""])
def test_wrong_or_missing_release_prevents_every_network_call(release):
    machine = ArmMachine(control._guest_image(b"/owned/local.sock"), release=release)
    assert machine.run() == 43
    assert machine.socket_count == machine.connect_count == 0


def profile_environment(monkeypatch, **changes):
    monkeypatch.setattr(control.sys, "platform", "linux")
    monkeypatch.setattr(control.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(control.os, "getuid", lambda: 10001)
    monkeypatch.setattr(control.os, "geteuid", lambda: 10001)
    fields = {
        "Uid": "10001 10001 10001 10001",
        "NoNewPrivs": "1",
        "Seccomp": "2",
        "Seccomp_filters": "2",
        "Threads": "1",
        "NSpid": str(control.os.getpid()),
        **{key: "0000000000000000" for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")},
        **changes,
    }
    monkeypatch.setattr(control.boundary, "_status_fields", lambda _path: fields)


def test_inherited_profile_needs_no_new_filter_or_native_syscall(monkeypatch):
    profile_environment(monkeypatch)
    control._inherited_profile()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Uid", "0 0 0 0"),
        ("NoNewPrivs", "0"),
        ("Seccomp", "0"),
        ("Seccomp_filters", "1"),
        ("Seccomp_filters", ""),
        ("Seccomp_filters", "bad"),
        ("Threads", "2"),
        ("NSpid", "1 2"),
    ]
    + [(key, "0000000000000001") for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")],
)
def test_inherited_profile_rejects_missing_or_relaxed_boundary(monkeypatch, field, value):
    profile_environment(monkeypatch, **{field: value})
    with pytest.raises(VerificationFailure):
        control._inherited_profile()


@pytest.mark.parametrize("part", ["os", "architecture", "uid", "euid", "endian"])
def test_inherited_profile_rejects_wrong_host(monkeypatch, part):
    profile_environment(monkeypatch)
    if part == "os":
        monkeypatch.setattr(control.sys, "platform", "darwin")
    elif part == "architecture":
        monkeypatch.setattr(control.platform, "machine", lambda: "x86_64")
    elif part in {"uid", "euid"}:
        monkeypatch.setattr(control.os, "get" + part, lambda: 0)
    else:
        monkeypatch.setattr(control.sys, "byteorder", "big")
    with pytest.raises(VerificationFailure):
        control._inherited_profile()


@pytest.mark.parametrize("fail_at", [None, "namespace", "bind", "probe"])
def test_owned_control_orders_proof_before_socket_and_always_removes_files(monkeypatch, fail_at):
    events, paths = [], []
    native = {"host_elf_header_sha256": "f" * 64, "handshake_exit": 42}
    monkeypatch.setattr(control, "_inherited_profile", lambda: events.append("profile"))

    def namespace():
        events.append("namespace")
        if fail_at == "namespace":
            raise VerificationFailure("route_failed")

    class Listener:
        def __init__(self, family, kind):
            assert family == control.socket.AF_UNIX and kind == control.socket.SOCK_STREAM
            events.append("socket")

        def __enter__(self):
            return self

        def bind(self, path):
            events.append("bind")
            paths.append(Path(path).parent)
            Path(path).touch()  # fake the owned socket file, without a kernel socket
            if fail_at == "bind":
                raise OSError("fake bind failure")

        def listen(self, backlog):
            assert backlog == 1
            events.append("listen")

        def __exit__(self, *_args):
            events.append("closed")

    def probe(path, directory):
        events.append("probe")
        assert path.parent == Path(directory) == paths[0]
        assert path.stat().st_mode & 0o777 == 0o700
        assert ArmMachine(path.read_bytes()).run() == 42
        if fail_at == "probe":
            raise VerificationFailure("probe_failed")
        return native

    monkeypatch.setattr(offline, "kernel_namespace", namespace)
    monkeypatch.setattr(control.socket, "socket", Listener)
    monkeypatch.setattr(control.boundary, "_run_emulation_probe", probe)
    # Keep the fake Unix pathname below the Linux sockaddr_un bound on all hosts.
    monkeypatch.setattr(control.tempfile, "tempdir", "/tmp")
    if fail_at:
        with pytest.raises((VerificationFailure, OSError)):
            control._run_control()
    else:
        assert control._run_control() == {
            "native_emulation": native,
            "external_errno": 101,
            "unix_connected": True,
        }
    assert events[:2] == ["profile", "namespace"]
    if fail_at == "namespace":
        assert events == ["profile", "namespace"]
    else:
        assert events[-1] == "closed"
        assert paths and all(not path.exists() for path in paths)


def test_cli_output_is_exact_and_control_runs_under_ten_second_alarm(monkeypatch, capsys):
    events = []

    @contextlib.contextmanager
    def signals(seconds):
        assert seconds == 10
        events.append("alarm")
        yield
        events.append("released")

    result = {"native_emulation": {"proof": True}, "external_errno": 101, "unix_connected": True}

    def run():
        assert events == ["alarm"]
        return result

    monkeypatch.setattr(control, "verification_signals", signals)
    monkeypatch.setattr(control, "_run_control", run)
    assert control.main([]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == result
    assert captured.err == ""
    assert events == ["alarm", "released"]


@pytest.mark.parametrize(
    "arguments", [["/arbitrary/program"], ["--help"], ["--host", "example.com"]]
)
def test_cli_cannot_select_program_destination_or_interpreter(monkeypatch, capsys, arguments):
    monkeypatch.setattr(
        control, "_run_control", lambda: pytest.fail("arguments must prevent execution")
    )
    assert control.main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "qemu_control_failed\n"


@pytest.mark.parametrize(
    "error", [VerificationFailure("private proof failed"), OSError("private path")]
)
def test_cli_failure_never_emits_success_or_raw_error(monkeypatch, capsys, error):
    monkeypatch.setattr(control, "verification_signals", lambda _seconds: contextlib.nullcontext())

    def fail():
        raise error

    monkeypatch.setattr(control, "_run_control", fail)
    assert control.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "qemu_control_failed\n"
