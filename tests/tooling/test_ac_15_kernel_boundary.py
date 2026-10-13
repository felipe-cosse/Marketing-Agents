"""Fake-only native-boundary proofs: never load libc, execute ELF, or use Docker."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import signal
import struct
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ac_15_kernel_boundary as boundary
from scripts.del_05_clean_state import VerificationFailure


@pytest.fixture(autouse=True)
def forbid_native_execution(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("real native/subprocess execution is forbidden in boundary tests")

    monkeypatch.setattr(boundary.ctypes, "CDLL", forbidden)
    monkeypatch.setattr(boundary.subprocess, "run", forbidden)
    monkeypatch.setattr(boundary.subprocess, "Popen", forbidden)
    monkeypatch.setattr(boundary.resource, "setrlimit", forbidden)
    monkeypatch.setattr(boundary, "_baseline", None)


def baseline(**changes):
    return boundary._Baseline(
        **{
            "runner": 42,
            "starts": (101, 202),
            "pid_namespace": (3, 4),
            "cgroup": (5, 6),
            **changes,
        }
    )


def test_native_guard_is_exact_arch_allow_and_foreign_kill():
    assert boundary.NATIVE_GUARD == (
        (0x20, 0, 0, 4),
        (0x15, 1, 0, 0xC00000B7),
        (0x06, 0, 0, 0x80000000),
        (0x06, 0, 0, 0x7FFF0000),
    )
    for arch in (0xC00000B7, 0x40000028, 0xC000003E, 0, 0xFFFFFFFF):
        accumulator = index = 0
        while True:
            code, yes, no, value = boundary.NATIVE_GUARD[index]
            if code == 0x20:
                assert value == 4
                accumulator = arch
            elif code == 0x15:
                index += yes if accumulator == value else no
            else:
                assert code == 0x06
                assert value == (0x7FFF0000 if arch == boundary.NATIVE_ARCH else 0x80000000)
                break
            index += 1


@pytest.mark.parametrize("result", [0, -1, 1])
def test_guard_install_uses_only_fixed_program(result):
    calls = []

    def prctl(*arguments):
        calls.append(arguments)
        assert arguments[:2] == (22, 2) and arguments[3:] == (0, 0)
        program = ctypes.cast(arguments[2], ctypes.POINTER(boundary._SockFprog)).contents
        assert program.length == 4
        assert (
            tuple(
                (item.code, item.jt, item.jf, item.k) for item in program.filters[: program.length]
            )
            == boundary.NATIVE_GUARD
        )
        return result

    if result == 0:
        boundary._install_guard(SimpleNamespace(prctl=prctl))
    else:
        with pytest.raises(VerificationFailure, match="native_guard_install_failed"):
            boundary._install_guard(SimpleNamespace(prctl=prctl))
    assert len(calls) == 1


@pytest.mark.parametrize("compat", [False, True])
def test_elf_probes_are_exact_static_rx_exit_only(compat):
    value = boundary._elf_probe(compat=compat)
    assert value[:16] == b"\x7fELF" + bytes((1 if compat else 2, 1, 1, 0)) + bytes(8)
    if compat:
        fields = struct.unpack("<16sHHIIIIIHHHHHH", value[:52])
        assert fields[1:] == (2, 40, 1, 0x10000 + 84, 52, 0, 0x05000000, 52, 32, 1, 0, 0, 0)
        assert struct.unpack("<IIIIIIII", value[52:84]) == (
            1,
            0,
            0x10000,
            0x10000,
            96,
            96,
            5,
            0x10000,
        )
        assert value[84:] == struct.pack("<III", 0xE3A0002A, 0xE3A07001, 0xEF000000)
    else:
        fields = struct.unpack("<16sHHIQQQIHHHHHH", value[:64])
        assert fields[1:] == (2, 183, 1, 0x10000 + 120, 64, 0, 0, 64, 56, 1, 0, 0, 0)
        assert struct.unpack("<IIQQQQQQ", value[64:120]) == (
            1,
            5,
            0,
            0x10000,
            0x10000,
            132,
            132,
            0x10000,
        )
        assert value[120:] == struct.pack("<III", 0xD2800540, 0xD2800BA8, 0xD4000001)


def probe_run(monkeypatch, outcomes):
    calls = []

    def run(arguments, **kwargs):
        index = len(calls)
        path = Path(arguments[0])
        assert path.read_bytes() == boundary._elf_probe(compat=bool(index))
        assert path.stat().st_mode & 0o777 == 0o700
        assert kwargs == {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(path.parent),
            "close_fds": True,
            "env": {},
            "timeout": boundary.PROBE_SECONDS,
            "check": False,
        }
        calls.append(path)
        outcome = outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(returncode=outcome)

    monkeypatch.setattr(boundary.subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    "compat,expected",
    [
        (OSError(errno.ENOEXEC, "synthetic"), "ENOEXEC"),
        (-signal.SIGSYS, "SIGSYS"),
    ],
)
def test_abi_requires_native_positive_before_narrow_compat_rejection(monkeypatch, compat, expected):
    calls = probe_run(monkeypatch, [42, compat])
    assert boundary._prove_abi() == {
        "foreign_elf_mode": "rejected",
        "foreign_elf_rejection": expected,
        "native_emulation": None,
    }
    assert len(calls) == 2 and all(not path.exists() for path in calls)


@pytest.mark.parametrize("native", [0, 41, 43, -signal.SIGSYS, OSError(errno.ENOEXEC, "synthetic")])
def test_abi_does_not_accept_broken_native_control(monkeypatch, native):
    calls = probe_run(monkeypatch, [native])
    with pytest.raises(VerificationFailure, match=r"(?:native_probe|elf_probe)_failed"):
        boundary._prove_abi()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "compat", [0, -signal.SIGKILL, -signal.SIGSEGV, OSError(errno.EPERM, "synthetic")]
)
def test_abi_rejects_other_foreign_statuses(monkeypatch, compat):
    probe_run(monkeypatch, [42, compat])
    with pytest.raises(VerificationFailure, match=r"(?:foreign_abi_executed|elf_probe_failed)"):
        boundary._prove_abi()


def test_abi_timeout_is_not_a_denial_proof(monkeypatch):
    probe_run(monkeypatch, [42, subprocess.TimeoutExpired(["synthetic"], 2)])
    with pytest.raises(subprocess.TimeoutExpired):
        boundary._prove_abi()


def uring_call(result, error):
    def syscall(number, entries, params):
        assert number.value == 425 and entries.value == 1
        assert ctypes.sizeof(params._obj) == 120
        assert ctypes.string_at(params, 120) == bytes(120)
        ctypes.set_errno(error)
        return result

    return SimpleNamespace(syscall=syscall)


@pytest.mark.parametrize("error,name", [(errno.EPERM, "EPERM"), (errno.ENOSYS, "ENOSYS")])
def test_uring_requires_valid_input_and_kernel_denial(error, name):
    assert boundary._prove_uring_denied(uring_call(-1, error)) == name


@pytest.mark.parametrize(
    "result,error",
    [(-1, 0), (-1, errno.EFAULT), (-1, errno.EINVAL), (-1, errno.EACCES), (-2, errno.EPERM)],
)
def test_uring_other_errors_do_not_prove_deny(result, error):
    with pytest.raises(VerificationFailure, match="denial_unproven"):
        boundary._prove_uring_denied(uring_call(result, error))


@pytest.mark.parametrize("descriptor", [0, 17])
def test_uring_unexpected_success_closes_only_returned_fd(monkeypatch, descriptor):
    closed = []
    monkeypatch.setattr(boundary.os, "close", closed.append)
    with pytest.raises(VerificationFailure, match="io_uring_available"):
        boundary._prove_uring_denied(uring_call(descriptor, 0))
    assert closed == [descriptor]


@pytest.fixture
def qualification(monkeypatch):
    events = []
    native = object()
    monkeypatch.setattr(boundary.sys, "platform", "linux")
    monkeypatch.setattr(boundary.sys, "byteorder", "little")
    monkeypatch.setattr(boundary.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(boundary.os, "getuid", lambda: 10001)
    monkeypatch.setattr(boundary.os, "geteuid", lambda: 10001)

    def record(name, value):
        def call(*args):
            assert boundary._baseline is None
            if name in {"guard", "uring"}:
                assert args == (native,)
            events.append(name)
            return value

        return call

    monkeypatch.setattr(boundary, "_snapshot", record("snapshot", baseline()))
    monkeypatch.setattr(boundary, "_libc", record("libc", native))
    monkeypatch.setattr(boundary, "_zero_core_file_limit", record("core", None))
    monkeypatch.setattr(boundary, "_install_guard", record("guard", None))
    monkeypatch.setattr(
        boundary,
        "_prove_abi",
        record(
            "abi",
            {
                "foreign_elf_mode": "rejected",
                "foreign_elf_rejection": "ENOEXEC",
                "native_emulation": None,
            },
        ),
    )
    monkeypatch.setattr(boundary, "_prove_uring_denied", record("uring", "EPERM"))
    return events


def test_qualification_order_and_evidence_publish_only_after_all_proofs(qualification):
    result = boundary.qualify_target()
    assert qualification == [
        "snapshot",
        "libc",
        "snapshot",
        "core",
        "guard",
        "abi",
        "uring",
        "snapshot",
    ]
    assert boundary._baseline == baseline()
    assert result == {
        "schema_version": 2,
        "native_abi": "linux_aarch64_little_64",
        "native_guard_installed": True,
        "core_file_limit_zero": True,
        "native_elf_exit": 42,
        "foreign_elf_rejection": "ENOEXEC",
        "foreign_elf_mode": "rejected",
        "native_emulation": None,
        "io_uring_setup_rejection": "EPERM",
        "inherited_sockets": 0,
        "inherited_io_uring": 0,
        "cgroup_inode": 6,
        "cgroup_read_only": True,
        "cgroup_subgroups": 0,
        "trusted_process_count": 2,
        "trusted_thread_count": 2,
        "source_sha256": hashlib.sha256(Path(boundary.__file__).read_bytes()).hexdigest(),
    }
    with pytest.raises(VerificationFailure, match="already_qualified"):
        boundary.qualify_target()


@pytest.mark.parametrize(
    "stage",
    [
        "_snapshot",
        "_libc",
        "_zero_core_file_limit",
        "_install_guard",
        "_prove_abi",
        "_prove_uring_denied",
    ],
)
@pytest.mark.parametrize("error", [VerificationFailure("synthetic_reject"), OSError("synthetic")])
def test_failed_qualification_never_publishes_baseline(qualification, monkeypatch, stage, error):
    def fail(*_args):
        raise error

    monkeypatch.setattr(boundary, stage, fail)
    with pytest.raises(VerificationFailure):
        boundary.qualify_target()
    assert boundary._baseline is None


@pytest.mark.parametrize("changed_at,code", [(1, "startup_changed"), (2, "qualification_changed")])
def test_qualification_rejects_identity_change(qualification, monkeypatch, changed_at, code):
    snapshots = [baseline()] * 3
    snapshots[changed_at] = baseline(cgroup=(5, 99))
    monkeypatch.setattr(boundary, "_snapshot", lambda: snapshots.pop(0))
    with pytest.raises(VerificationFailure, match=code):
        boundary.qualify_target()
    assert boundary._baseline is None


def test_quiescence_requires_prior_qualification():
    with pytest.raises(VerificationFailure, match="not_qualified"):
        boundary.quiescent_target()


@pytest.mark.parametrize(
    "transient",
    [
        VerificationFailure("kernel_boundary_descendants_remaining"),
        VerificationFailure("kernel_boundary_requires_single_thread"),
        VerificationFailure("kernel_boundary_extra_threads"),
    ],
)
def test_quiescence_retries_within_one_second_then_returns_exact_baseline(monkeypatch, transient):
    clock = [10.0]
    sleeps = []
    snapshots = [transient, baseline()]
    monkeypatch.setattr(boundary, "_baseline", baseline())
    monkeypatch.setattr(boundary.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        assert 0 < seconds <= 0.001
        sleeps.append(seconds)
        clock[0] += seconds

    def snapshot():
        value = snapshots.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(boundary.time, "sleep", sleep)
    monkeypatch.setattr(boundary, "_snapshot", snapshot)
    assert boundary.quiescent_target() == {
        "schema_version": 1,
        "cgroup_inode": 6,
        "cgroup_unchanged": True,
        "remaining_tasks": 0,
        "trusted_process_count": 2,
        "trusted_thread_count": 2,
        "baseline_unchanged": True,
    }
    assert len(sleeps) == 1 and sum(sleeps) <= 1


@pytest.mark.parametrize(
    "change",
    [
        {"runner": 43},
        {"starts": (101, 203)},
        {"pid_namespace": (3, 5)},
        {"cgroup": (5, 7)},
    ],
)
def test_quiescence_identity_mismatch_cannot_become_success(monkeypatch, change):
    clock = [0.0]
    monkeypatch.setattr(boundary, "_baseline", baseline())
    monkeypatch.setattr(boundary, "_snapshot", lambda: baseline(**change))
    monkeypatch.setattr(boundary.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        boundary.time, "sleep", lambda _seconds: pytest.fail("identity drift retried")
    )
    with pytest.raises(VerificationFailure, match="not_quiescent"):
        boundary.quiescent_target()
    assert clock[0] == 0


def test_quiescence_persistent_descendant_exhausts_only_one_second(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(boundary, "_baseline", baseline())
    monkeypatch.setattr(boundary.time, "monotonic", lambda: clock[0])

    def snapshot():
        raise VerificationFailure("kernel_boundary_descendants_remaining")

    def sleep(seconds):
        assert 0 < seconds <= 0.001
        clock[0] += seconds

    monkeypatch.setattr(boundary, "_snapshot", snapshot)
    monkeypatch.setattr(boundary.time, "sleep", sleep)
    with pytest.raises(VerificationFailure, match="not_quiescent"):
        boundary.quiescent_target()
    assert clock[0] == pytest.approx(1.0)


def test_quiescence_late_success_is_not_accepted(monkeypatch):
    clock = iter([0.0, 1.001])
    monkeypatch.setattr(boundary, "_baseline", baseline())
    monkeypatch.setattr(boundary, "_snapshot", baseline)
    monkeypatch.setattr(boundary.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        boundary.time, "sleep", lambda _seconds: pytest.fail("late success retried")
    )
    with pytest.raises(VerificationFailure, match="not_quiescent"):
        boundary.quiescent_target()


@pytest.mark.parametrize(
    "error", [OSError("gone"), VerificationFailure("kernel_boundary_socket_inherited")]
)
def test_quiescence_unavailable_or_changed_boundary_is_immediately_fatal(monkeypatch, error):
    monkeypatch.setattr(boundary, "_baseline", baseline())

    def snapshot():
        raise error

    monkeypatch.setattr(boundary, "_snapshot", snapshot)
    monkeypatch.setattr(
        boundary.time, "sleep", lambda _seconds: pytest.fail("unsafe state retried")
    )
    with pytest.raises(VerificationFailure, match="not_quiescent"):
        boundary.quiescent_target()


def test_failed_source_hash_does_not_publish_qualified_baseline(qualification, monkeypatch):
    def unreadable(_path):
        raise OSError("synthetic source unavailable")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(VerificationFailure, match="qualification_unavailable"):
        boundary.qualify_target()
    assert boundary._baseline is None


@pytest.mark.parametrize("limits", [(0, 0), (0, 1024), (1024, 1024)])
def test_core_dump_limit_is_irreversibly_zero_before_any_probe(monkeypatch, limits):
    calls = []
    monkeypatch.setattr(
        boundary.resource, "setrlimit", lambda kind, value: calls.append((kind, value))
    )
    monkeypatch.setattr(boundary.resource, "getrlimit", lambda kind: limits)
    if limits == (0, 0):
        boundary._zero_core_file_limit()
    else:
        with pytest.raises(VerificationFailure, match="core_limit_failed"):
            boundary._zero_core_file_limit()
    assert calls == [(boundary.resource.RLIMIT_CORE, (0, 0))]


def proc_stat(pid, *, state="S", parent=None, start=123):
    parent = (0 if pid == 1 else 1) if parent is None else parent
    return f"{pid} (name with ) spaces) " + " ".join(
        [state, str(parent), *(["0"] * 17), str(start)]
    )


@pytest.mark.parametrize("state", ["S", "R"])
def test_process_stat_parses_last_comm_parenthesis_and_start(tmp_path, monkeypatch, state):
    monkeypatch.setattr(boundary, "PROC", tmp_path)
    process = tmp_path / "42"
    (process / "task/42").mkdir(parents=True)
    (process / "stat").write_text(proc_stat(42, state=state))
    assert boundary._process_start(42) == 123


@pytest.mark.parametrize(
    "state,parent,code",
    [
        ("Z", 1, "process_dead"),
        ("X", 1, "process_dead"),
        ("x", 1, "process_dead"),
        ("S", 7, "parent_mismatch"),
    ],
)
def test_process_stat_rejects_zombies_and_wrong_parent(tmp_path, monkeypatch, state, parent, code):
    monkeypatch.setattr(boundary, "PROC", tmp_path)
    process = tmp_path / "42"
    (process / "task/42").mkdir(parents=True)
    (process / "stat").write_text(proc_stat(42, state=state, parent=parent))
    with pytest.raises(VerificationFailure, match=code):
        boundary._process_start(42)


@pytest.mark.parametrize(
    "target,code",
    [
        ("socket:[123]", "socket_inherited"),
        ("anon_inode:[io_uring]", "ring_inherited"),
        ("anon_inode:io_uring", "ring_inherited"),
    ],
)
def test_inherited_socket_or_ring_descriptor_fails(tmp_path, monkeypatch, target, code):
    monkeypatch.setattr(boundary, "PROC", tmp_path)
    (tmp_path / "self/fd").mkdir(parents=True)
    (tmp_path / "self/fd/3").symlink_to(target)
    with pytest.raises(VerificationFailure, match=code):
        boundary._descriptors()


def test_snapshot_rejects_extra_process_before_other_checks(monkeypatch):
    monkeypatch.setattr(boundary.os, "getpid", lambda: 42)
    monkeypatch.setattr(boundary.os, "getppid", lambda: 1)
    monkeypatch.setattr(boundary, "_names", lambda _path: {"1", "42", "43", "self"})
    monkeypatch.setattr(boundary, "_profile_status", lambda: pytest.fail("unexpected later proof"))
    with pytest.raises(VerificationFailure, match="descendants_remaining"):
        boundary._snapshot()


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("NoNewPrivs", "0", "no_new_privileges_required"),
        ("Seccomp", "0", "seccomp_required"),
        ("Seccomp", "1", "seccomp_required"),
        ("CapEff", "0000000000000001", "capabilities_present"),
        ("Threads", "2", "requires_single_thread"),
        ("NSpid", "7 42", "proc_namespace"),
    ],
)
def test_profile_status_rejects_unqualified_values(tmp_path, monkeypatch, field, value, code):
    fields = {
        "NoNewPrivs": "1",
        "Seccomp": "2",
        "CapEff": "0000000000000000",
        "Threads": "1",
        "NSpid": "42",
    }
    monkeypatch.setattr(boundary, "PROC", tmp_path)
    monkeypatch.setattr(boundary.os, "getpid", lambda: 42)
    (tmp_path / "self").mkdir()
    path = tmp_path / "self/status"
    path.write_text("\n".join(f"{key}:\t{item}" for key, item in fields.items()))
    boundary._profile_status()
    fields[field] = value
    path.write_text("\n".join(f"{key}:\t{item}" for key, item in fields.items()))
    with pytest.raises(VerificationFailure, match=code):
        boundary._profile_status()


def test_profile_status_rejects_duplicate_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(boundary, "PROC", tmp_path)
    (tmp_path / "self").mkdir()
    (tmp_path / "self/status").write_text("NoNewPrivs: 1\nNoNewPrivs: 1\n")
    with pytest.raises(VerificationFailure, match="status_unparsed"):
        boundary._profile_status()


@pytest.mark.parametrize(
    "raw,limit,code", [(b"abcd", 3, "control_file_bound"), (b"\xff", 3, "control_file_unparsed")]
)
def test_control_file_reads_are_bounded_ascii(tmp_path, raw, limit, code):
    path = tmp_path / "control"
    path.write_bytes(raw)
    with pytest.raises(VerificationFailure, match=code):
        boundary._read(path, limit)


def test_directory_enumeration_has_a_fixed_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(boundary, "MAX_ENTRIES", 2)
    for index in range(3):
        (tmp_path / str(index)).touch()
    with pytest.raises(VerificationFailure, match="directory_bound"):
        boundary._names(tmp_path)


def cgroup_fixture(tmp_path, monkeypatch):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (proc / "1").mkdir()
    cgroup.mkdir()
    (proc / "self/cgroup").write_text("0::/\n")
    (proc / "1/cgroup").write_text("0::/\n")
    (proc / "self/mountinfo").write_text("1 0 0:1 / /sys/fs/cgroup ro,nosuid - cgroup2 cgroup ro\n")
    monkeypatch.setattr(boundary, "PROC", proc)
    monkeypatch.setattr(boundary, "CGROUP", cgroup)
    monkeypatch.setattr(
        boundary.os, "statvfs", lambda _path: SimpleNamespace(f_flag=boundary.os.ST_RDONLY)
    )
    return proc, cgroup


def test_cgroup_requires_private_readonly_root_without_subgroups(tmp_path, monkeypatch):
    _proc, cgroup = cgroup_fixture(tmp_path, monkeypatch)
    assert boundary._cgroup_state() == (cgroup.stat().st_dev, cgroup.stat().st_ino)
    (cgroup / "child").mkdir()
    with pytest.raises(VerificationFailure, match="cgroup_subgroup"):
        boundary._cgroup_state()


@pytest.mark.parametrize(
    "kind,code",
    [
        ("writable", "cgroup_read_only"),
        ("writable_stat", "cgroup_read_only"),
        ("nonroot", "cgroup_read_only"),
        ("missing", "cgroup_mount"),
        ("duplicate", "cgroup_mount"),
        ("self_foreign", "private_cgroup"),
        ("init_foreign", "init_cgroup"),
    ],
)
def test_cgroup_rejects_foreign_or_ambiguous_mounts(tmp_path, monkeypatch, kind, code):
    proc, _cgroup = cgroup_fixture(tmp_path, monkeypatch)
    mount = proc / "self/mountinfo"
    text = mount.read_text()
    if kind == "writable":
        mount.write_text(text.replace("ro,nosuid", "rw,nosuid"))
    elif kind == "writable_stat":
        monkeypatch.setattr(boundary.os, "statvfs", lambda _path: SimpleNamespace(f_flag=0))
    elif kind == "nonroot":
        mount.write_text(text.replace("0:1 / /sys", "0:1 /child /sys"))
    elif kind == "missing":
        mount.write_text("")
    elif kind == "duplicate":
        mount.write_text(text + text)
    else:
        (proc / ("self" if kind == "self_foreign" else "1") / "cgroup").write_text("0::/child\n")
    with pytest.raises(VerificationFailure, match=code):
        boundary._cgroup_state()


@pytest.mark.parametrize("mismatch", [False, True])
def test_snapshot_binds_both_process_starts_and_private_namespaces(monkeypatch, mismatch):
    monkeypatch.setattr(boundary.os, "getpid", lambda: 42)
    monkeypatch.setattr(boundary.os, "getppid", lambda: 1)
    monkeypatch.setattr(boundary, "_names", lambda _path: {"1", "42", "self"})
    monkeypatch.setattr(boundary, "_profile_status", lambda: None)
    monkeypatch.setattr(boundary, "_descriptors", lambda: None)
    monkeypatch.setattr(boundary, "_cgroup_state", lambda: (5, 6))
    monkeypatch.setattr(boundary, "_process_start", lambda pid: 101 if pid == 1 else 202)
    monkeypatch.setattr(
        boundary, "_identity", lambda path: (3, 9 if mismatch and "1" in path.parts else 4)
    )
    if mismatch:
        with pytest.raises(VerificationFailure, match="pid_namespace_changed"):
            boundary._snapshot()
    else:
        assert boundary._snapshot() == baseline()


def test_process_stat_rejects_extra_native_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(boundary, "PROC", tmp_path)
    process = tmp_path / "42"
    (process / "task/42").mkdir(parents=True)
    (process / "task/43").mkdir()
    (process / "stat").write_text(proc_stat(42))
    with pytest.raises(VerificationFailure, match="extra_threads"):
        boundary._process_start(42)


def emulator_proof():
    return {
        "guest_elf_class": 1,
        "guest_elf_machine": 40,
        "host_elf_class": 2,
        "host_elf_machine": 183,
        "host_elf_little_endian": True,
        "host_elf_header_sha256": "f" * 64,
        "stable_process_identity": True,
        "same_cgroup": True,
        "same_pid_namespace": True,
        "no_new_privileges": True,
        "seccomp_filter": True,
        "capabilities_zero": True,
        "handshake_exit": 42,
    }


def test_foreign_exit42_is_not_a_live_emulator_proof(monkeypatch):
    probe_run(monkeypatch, [42, 42])

    def unqualified():
        raise VerificationFailure("fake_live_proof_missing")

    monkeypatch.setattr(boundary, "_prove_native_emulation", unqualified)
    with pytest.raises(VerificationFailure, match="fake_live_proof_missing"):
        boundary._prove_abi()


def test_foreign_exit42_requires_separate_complete_proof(monkeypatch):
    calls = probe_run(monkeypatch, [42, 42])
    observed = []

    def qualified():
        assert len(calls) == 2
        observed.append(True)
        return emulator_proof()

    monkeypatch.setattr(boundary, "_prove_native_emulation", qualified)
    assert boundary._prove_abi() == {
        "foreign_elf_mode": "native_emulation",
        "foreign_elf_rejection": None,
        "native_emulation": emulator_proof(),
    }
    assert observed == [True]


def test_qualification_publishes_emulation_mode_only_after_all_existing_proofs(
    qualification, monkeypatch
):
    monkeypatch.setattr(
        boundary,
        "_prove_abi",
        lambda: {
            "foreign_elf_mode": "native_emulation",
            "foreign_elf_rejection": None,
            "native_emulation": emulator_proof(),
        },
    )
    proof = boundary.qualify_target()
    assert proof["schema_version"] == 2 and proof["native_emulation"] == emulator_proof()
    assert (
        proof["foreign_elf_mode"] == "native_emulation" and proof["foreign_elf_rejection"] is None
    )
    assert qualification == ["snapshot", "libc", "snapshot", "core", "guard", "uring", "snapshot"]
    assert boundary._baseline == baseline()


def executable_metadata(**changes):
    return SimpleNamespace(
        **{
            "st_mode": 0o100755,
            "st_dev": 11,
            "st_ino": 22,
            "st_size": 4096,
            "st_mtime_ns": 44,
            "st_ctime_ns": 55,
            **changes,
        }
    )


def fake_executable(
    monkeypatch, *, header=None, before=None, after=None, live=None, read_error=None
):
    before = executable_metadata() if before is None else before
    after = before if after is None else after
    header = boundary._elf_probe(compat=False)[:64] if header is None else header
    stats = iter([before, after])
    events = []
    real_stat = boundary.os.stat

    def stat(path, *args, **kwargs):
        if path == boundary.PROC / "43/exe":
            return before if live is None else live
        return real_stat(path, *args, **kwargs)

    def open_file(path, flags):
        assert path == boundary.PROC / "43/exe"
        assert flags == boundary.os.O_RDONLY | boundary.os.O_CLOEXEC
        events.append("open")
        return 987

    def read(fd, count):
        assert (fd, count) == (987, 64)
        events.append("read64")
        if read_error:
            raise read_error
        return header

    def close(fd):
        assert fd == 987
        events.append("close")

    monkeypatch.setattr(boundary.os, "open", open_file)
    monkeypatch.setattr(boundary.os, "read", read)
    monkeypatch.setattr(boundary.os, "fstat", lambda fd: next(stats))
    monkeypatch.setattr(boundary.os, "stat", stat)
    monkeypatch.setattr(boundary.os, "close", close)
    return events, header


@pytest.mark.parametrize("elf_type", [2, 3])
def test_live_executable_reads_exactly_64_bytes_and_hashes_only_header(monkeypatch, elf_type):
    header = bytearray(boundary._elf_probe(compat=False)[:64])
    struct.pack_into("<H", header, 16, elf_type)
    events, header = fake_executable(monkeypatch, header=bytes(header))
    identity, digest = boundary._native_executable(43)
    assert identity == (11, 22, 4096, 44, 55)
    assert digest == hashlib.sha256(header).hexdigest()
    assert events == ["open", "read64", "close"]


@pytest.mark.parametrize(
    "change",
    [
        {"st_mode": 0o040755},
        {"st_mode": 0o100644},
        {"st_size": 63},
        {"st_size": boundary.EMULATOR_EXECUTABLE_LIMIT + 1},
    ],
)
def test_executable_invalid_metadata_fails_before_read_and_always_closes(monkeypatch, change):
    events, _ = fake_executable(monkeypatch, before=executable_metadata(**change))
    with pytest.raises(VerificationFailure, match="executable_invalid"):
        boundary._native_executable(43)
    assert events == ["open", "close"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("st_dev", 99),
        ("st_ino", 99),
        ("st_size", 5000),
        ("st_mtime_ns", 99),
        ("st_ctime_ns", 99),
    ],
)
def test_executable_identity_metadata_must_be_stable_during_header_read(monkeypatch, field, value):
    events, _ = fake_executable(monkeypatch, after=executable_metadata(**{field: value}))
    with pytest.raises(VerificationFailure, match="executable_changed"):
        boundary._native_executable(43)
    assert events[-1] == "close"


@pytest.mark.parametrize(
    "offset,payload",
    [
        (0, b"NOPE"),
        (4, b"\x01"),
        (5, b"\x02"),
        (6, b"\x00"),
        (16, struct.pack("<H", 1)),
        (18, struct.pack("<H", 40)),
        (20, struct.pack("<I", 0)),
        (52, struct.pack("<H", 52)),
    ],
)
def test_live_executable_header_must_be_native_elf64_little_aarch64(monkeypatch, offset, payload):
    header = bytearray(boundary._elf_probe(compat=False)[:64])
    header[offset : offset + len(payload)] = payload
    events, _ = fake_executable(monkeypatch, header=bytes(header))
    with pytest.raises(VerificationFailure, match="native_elf_required"):
        boundary._native_executable(43)
    assert events[-1] == "close"


@pytest.mark.parametrize("header", [b"", b"\x7fELF", bytes(63), bytes(65)])
def test_live_executable_short_or_nonexact_header_cannot_qualify(monkeypatch, header):
    events, _ = fake_executable(monkeypatch, header=header)
    with pytest.raises(VerificationFailure, match="native_elf_required"):
        boundary._native_executable(43)
    assert events[-1] == "close"


def test_executable_read_failure_still_closes_owned_descriptor(monkeypatch):
    events, _ = fake_executable(monkeypatch, read_error=OSError("synthetic read"))
    with pytest.raises(OSError):
        boundary._native_executable(43)
    assert events[-1] == "close"


@pytest.mark.parametrize(
    "field,value",
    [
        ("st_dev", 99),
        ("st_ino", 99),
        ("st_size", 5000),
        ("st_mtime_ns", 99),
        ("st_ctime_ns", 99),
    ],
)
def test_retained_executable_fd_must_still_match_live_process_exe(monkeypatch, field, value):
    events, _ = fake_executable(monkeypatch, live=executable_metadata(**{field: value}))
    with pytest.raises(VerificationFailure, match="executable_changed"):
        boundary._native_executable(43)
    assert events[-1] == "close"


def test_arm_handshake_elf_contains_only_fixed_ready_release_and_exit_code():
    image = boundary._elf_handshake()
    header = struct.unpack("<16sHHIIIIIHHHHHH", image[:52])
    assert header[0][:7] == b"\x7fELF\x01\x01\x01"
    assert header[1:4] == (2, 40, 1) and header[7] == 0x05000000
    assert struct.unpack("<IIIIIIII", image[52:84]) == (
        1,
        0,
        0x10000,
        0x10000,
        len(image),
        len(image),
        5,
        0x10000,
    )
    assert image[84:] == boundary._emulation_handshake() + struct.pack(
        "<III", 0xE3A0002A, 0xE3A07001, 0xEF000000
    )


def run_arm_handshake(*, write_result=1, read_result=1, release=0x47):
    """Interpret only this fixed ARM instruction subset; never execute ELF."""
    code = boundary._elf_handshake()[84:]
    words = struct.unpack(f"<{len(code) // 4}I", code)
    registers = [0] * 16
    registers[13] = 4096
    memory, calls, zero, pc = {}, [], False, 0
    for _ in range(100):
        instruction = words[pc]
        next_pc = pc + 1
        if instruction == 0xE24DD004:
            registers[13] -= 4
        elif instruction & 0xFFF00000 == 0xE3A00000:
            registers[(instruction >> 12) & 15] = instruction & 255
        elif instruction == 0xE1A0100D:
            registers[1] = registers[13]
        elif instruction == 0xE5CD3000:
            memory[registers[13]] = registers[3] & 255
        elif instruction == 0xE5DD3000:
            registers[3] = memory[registers[13]]
        elif instruction & 0xFFF0F000 == 0xE3500000:
            zero = registers[(instruction >> 16) & 15] == instruction & 255
        elif instruction & 0xFF000000 == 0x0A000000:
            if zero:
                offset = instruction & 0xFFFFFF
                if offset & 0x800000:
                    offset -= 1 << 24
                next_pc = pc + 2 + offset
        elif instruction == 0xEF000000:
            syscall = registers[7]
            if syscall == 1:
                return registers[0], calls
            assert syscall in {3, 4}
            assert registers[1:3] == [registers[13], 1]
            if syscall == 4:
                assert registers[0] == 1
                calls.append(("write", bytes([memory[registers[13]]])))
                registers[0] = write_result
            else:
                assert registers[0] == 0
                calls.append(("read", 1))
                memory[registers[13]] = release
                registers[0] = read_result
        else:
            pytest.fail(f"unreviewed ARM instruction {instruction:#x}")
        pc = next_pc
    pytest.fail("handshake did not terminate within fixed step bound")


@pytest.mark.parametrize(
    "write_result,read_result,release,exit_code",
    [
        (1, 1, ord("G"), 42),
        (0, 1, ord("G"), 43),
        (-1, 1, ord("G"), 43),
        (1, 0, ord("G"), 43),
        (1, -1, ord("G"), 43),
        (1, 1, ord("X"), 43),
    ],
)
def test_arm_handshake_requires_successful_ready_write_exact_release_and_read(
    write_result, read_result, release, exit_code
):
    actual, calls = run_arm_handshake(
        write_result=write_result, read_result=read_result, release=release
    )
    assert actual == exit_code and calls[0] == ("write", b"R")
    assert calls == ([("write", b"R"), ("read", 1)] if write_result == 1 else [("write", b"R")])


def emulation_snapshot():
    return boundary._EmulationSnapshot(
        start=99,
        tasks=((43, 99), (44, 100)),
        pid_namespace=(3, 4),
        cgroup_namespace=(5, 6),
        cgroup=(7, 8),
        executable=(11, 22, 4096, 44, 55),
        header_sha256="f" * 64,
    )


def fake_probe(monkeypatch, *, fault=None, drift=None):
    clock, events = [10.0], []
    ancestors = ((42, 1, 99), (1, 0, 11))

    class Pipe:
        def __init__(self, fd, name):
            self.fd, self.name, self.closed = fd, name, False

        def fileno(self):
            return self.fd

        def close(self):
            events.append(("close", self.name))
            self.closed = True
            if fault == "pipe_close" and self.name == "stdin":
                raise OSError("synthetic close failure")

    class Child:
        pid, returncode = 43, None

        def __init__(self):
            self.stdin, self.stdout = Pipe(11, "stdin"), Pipe(10, "stdout")
            self.waits = 0

        def poll(self):
            return self.returncode

        def kill(self):
            events.append(("kill",))
            if fault == "kill":
                raise OSError("synthetic kill failure")
            self.returncode = -signal.SIGKILL

        def wait(self, timeout):
            self.waits += 1
            events.append(("wait", timeout))
            assert 0 <= timeout <= boundary.PROBE_SECONDS
            if fault == "wait_timeout" and self.waits == 1:
                raise subprocess.TimeoutExpired(["synthetic"], timeout)
            if fault == "reap" and self.waits >= 2:
                raise subprocess.TimeoutExpired(["synthetic"], timeout)
            if self.returncode is None:
                self.returncode = 43 if fault == "exit" else 42
            return self.returncode

    child = Child()

    def popen(argv, **kwargs):
        path = Path(argv[0])
        assert path.name == "arm-handshake" and path.read_bytes() == boundary._elf_handshake()
        assert path.stat().st_mode & 0o777 == 0o700
        assert kwargs == {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "cwd": str(path.parent),
            "close_fds": True,
            "env": {},
            "bufsize": 0,
        }
        events.append(("spawn", path))
        if fault == "spawn":
            raise OSError("synthetic spawn failure")
        if fault == "slow_spawn":
            clock[0] = 11.76
        return child

    selects, reads, snapshots = [0], [0], [0]

    def select(read, write, error, timeout):
        assert (read, write, error) == ([10], [], [])
        selects[0] += 1
        events.append(("select", timeout))
        assert 0 <= timeout <= 1.75
        if selects[0] == 2:
            assert timeout == 0
            return ([10] if fault == "early_extra" else [], [], [])
        if fault in {"ready_timeout", "kill"} and selects[0] == 1:
            return [], [], []
        if fault == "eof_timeout" and selects[0] == 3:
            return [], [], []
        return [10], [], []

    def read(fd, count):
        assert (fd, count) == (10, 2)
        reads[0] += 1
        events.append(("read", count))
        if reads[0] == 1:
            return {"ready_eof": b"", "ready_wrong": b"X", "ready_extra": b"RR"}.get(fault, b"R")
        if fault == "late_success":
            clock[0] = 11.76
        if fault == "late_cleanup":
            clock[0] = 12.01
        return b"X" if fault == "late_extra" else b""

    def snapshot(child_arg, ancestor_arg):
        assert child_arg is child and ancestor_arg == ancestors
        snapshots[0] += 1
        events.append(("snapshot",))
        if fault == "profile":
            raise VerificationFailure("kernel_boundary_emulator_profile_changed")
        if drift and snapshots[0] == 2:
            return replace(emulation_snapshot(), **drift)
        return emulation_snapshot()

    def write(fd, data):
        assert (fd, data) == (11, b"G")
        assert snapshots[0] == 2
        events.append(("release",))
        return 0 if fault == "release" else 1

    monkeypatch.setattr(boundary.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(boundary, "_profile_status", lambda: None)
    monkeypatch.setattr(boundary, "_emulation_ancestors", lambda: ancestors)
    monkeypatch.setattr(boundary, "_names", lambda path: {"1", "42"})
    monkeypatch.setattr(boundary.subprocess, "Popen", popen)
    monkeypatch.setattr(boundary.select, "select", select)
    monkeypatch.setattr(boundary.os, "read", read)
    monkeypatch.setattr(boundary.os, "write", write)
    monkeypatch.setattr(boundary, "_emulation_snapshot", snapshot)
    return child, events, clock


def test_native_emulation_workflow_requires_two_live_proofs_release_exit_and_reap(monkeypatch):
    child, events, clock = fake_probe(monkeypatch)
    assert boundary._prove_native_emulation() == emulator_proof()
    assert clock[0] == 10.0 and child.waits == 2
    assert child.stdin.closed and child.stdout.closed
    assert [entry[0] for entry in events] == [
        "spawn",
        "select",
        "read",
        "snapshot",
        "snapshot",
        "select",
        "release",
        "close",
        "wait",
        "select",
        "read",
        "wait",
        "close",
        "close",
    ]
    assert events[1] == ("select", 1.75)
    assert all(not entry[1].exists() for entry in events if entry[0] == "spawn")


@pytest.mark.parametrize(
    "fault",
    [
        "spawn",
        "slow_spawn",
        "ready_timeout",
        "ready_eof",
        "ready_wrong",
        "ready_extra",
        "early_extra",
        "release",
        "exit",
        "wait_timeout",
        "eof_timeout",
        "late_extra",
        "profile",
        "late_success",
        "late_cleanup",
        "kill",
        "reap",
        "pipe_close",
    ],
)
def test_native_emulation_workflow_cannot_qualify_partial_or_unreaped_execution(monkeypatch, fault):
    child, events, _clock = fake_probe(monkeypatch, fault=fault)
    with pytest.raises(VerificationFailure):
        boundary._prove_native_emulation()
    if fault != "spawn":
        assert child.stdin.closed and child.stdout.closed
        assert any(event[0] == "wait" for event in events)
    if fault in {
        "ready_timeout",
        "ready_eof",
        "ready_wrong",
        "ready_extra",
        "profile",
        "kill",
        "slow_spawn",
    }:
        assert ("release",) not in events
    if fault in {"kill", "reap", "pipe_close", "late_cleanup"}:
        assert events[-1] == ("close", "stdout")


@pytest.mark.parametrize(
    "change",
    [
        {"start": 100},
        {"tasks": ((43, 99), (44, 101))},
        {"pid_namespace": (3, 5)},
        {"cgroup_namespace": (5, 7)},
        {"cgroup": (7, 9)},
        {"executable": (11, 23, 4096, 44, 55)},
        {"header_sha256": "e" * 64},
    ],
)
def test_native_emulation_live_identity_drift_fails_before_release(monkeypatch, change):
    child, events, _clock = fake_probe(monkeypatch, drift=change)
    with pytest.raises(VerificationFailure, match="identity_changed"):
        boundary._prove_native_emulation()
    assert ("release",) not in events and ("kill",) in events
    assert child.stdin.closed and child.stdout.closed


def fake_emulation_tree(monkeypatch, *, nested=False, child=False):
    parents = {42: 1, 1: 0}
    if nested:
        parents = {42: 41, 41: 1, 1: 0}
    tasks = {pid: {str(pid)} for pid in parents}
    if child:
        parents.update({43: 42, 44: 42})
        tasks[43] = {"43", "44"}
    starts = {pid: 1000 + pid for pid in parents}
    profiles = {}
    for pid in parents:
        profiles[pid] = {
            "Pid": str(pid),
            "Tgid": "43" if child and pid in {43, 44} else str(pid),
            "PPid": str(parents[pid]),
            "Uid": "10001 10001 10001 10001",
            "NoNewPrivs": "1",
            "Seccomp": "2",
            "Seccomp_filters": "1" if pid == 1 else "2",
            "NSpid": str(pid),
            "Threads": "2" if child and pid in {43, 44} else "1",
            **dict.fromkeys(("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"), "0000000000000000"),
        }
    pids = set(parents) - ({44} if child else set())
    cgroups, namespaces = {}, {}

    def path_pid(path):
        pieces = path.relative_to(boundary.PROC).parts
        if pieces[0] == "self":
            return 42
        return int(pieces[2] if len(pieces) > 2 and pieces[1] == "task" else pieces[0])

    def names(path):
        if path == boundary.PROC:
            return {str(pid) for pid in pids}
        assert path.name == "task"
        return tasks[int(path.parent.name)]

    def identity(path):
        if path == boundary.CGROUP:
            return 7, 8
        pid = path_pid(path)
        return namespaces.get((pid, path.name), (3, 4) if path.name == "pid" else (5, 6))

    def read(path, limit=boundary.READ_LIMIT):
        assert path.name == "cgroup" and limit == 1024
        return cgroups.get(path_pid(path), "0::/\n")

    monkeypatch.setattr(boundary.os, "getpid", lambda: 42)
    monkeypatch.setattr(boundary, "_identity", identity)
    monkeypatch.setattr(boundary, "_names", names)
    monkeypatch.setattr(boundary, "_status_fields", lambda path: profiles[path_pid(path)])
    monkeypatch.setattr(boundary, "_stat_identity", lambda path, pid: (parents[pid], starts[pid]))
    monkeypatch.setattr(boundary, "_read", read)
    monkeypatch.setattr(
        boundary, "_native_executable", lambda pid: ((11, 22, 4096, 44, 55), "f" * 64)
    )
    return SimpleNamespace(
        parents=parents,
        tasks=tasks,
        starts=starts,
        profiles=profiles,
        pids=pids,
        cgroups=cgroups,
        namespaces=namespaces,
    )


@pytest.mark.parametrize("nested", [False, True])
def test_emulation_ancestry_is_exact_two_or_three_trusted_processes(monkeypatch, nested):
    fake_emulation_tree(monkeypatch, nested=nested)
    assert boundary._emulation_ancestors() == (
        ((42, 41, 1042), (41, 1, 1041), (1, 0, 1001)) if nested else ((42, 1, 1042), (1, 0, 1001))
    )


@pytest.mark.parametrize(
    "fault",
    [
        "loop",
        "too_deep",
        "init_parent",
        "extra_thread",
        "uid",
        "capability",
        "nnp",
        "seccomp",
        "filter_missing",
        "caller_filter",
        "init_filter",
        "namespace",
        "cgroup",
    ],
)
def test_emulation_ancestry_rejects_untrusted_or_changed_chain(monkeypatch, fault):
    tree = fake_emulation_tree(monkeypatch, nested=True)
    if fault == "loop":
        tree.parents[41] = 42
    elif fault == "too_deep":
        tree.parents[41] = 40
        tree.parents[40] = 1
        tree.starts[40] = 1040
        tree.tasks[40] = {"40"}
        tree.profiles[40] = {**tree.profiles[41], "NSpid": "40"}
    elif fault == "init_parent":
        tree.parents[1] = 2
    elif fault == "extra_thread":
        tree.tasks[41].add("99")
    elif fault == "uid":
        tree.profiles[41]["Uid"] = "0 0 0 0"
    elif fault == "capability":
        tree.profiles[41]["CapBnd"] = "0000000000000001"
    elif fault == "nnp":
        tree.profiles[41]["NoNewPrivs"] = "0"
    elif fault == "seccomp":
        tree.profiles[41]["Seccomp"] = "0"
    elif fault == "filter_missing":
        del tree.profiles[42]["Seccomp_filters"]
    elif fault == "caller_filter":
        tree.profiles[41]["Seccomp_filters"] = "1"
    elif fault == "init_filter":
        tree.profiles[1]["Seccomp_filters"] = "2"
    elif fault == "namespace":
        tree.namespaces[41, "pid"] = 3, 9
    else:
        tree.cgroups[41] = "0::/foreign\n"
    with pytest.raises(VerificationFailure):
        boundary._emulation_ancestors()


def test_emulation_snapshot_checks_every_native_thread_and_process_identity(monkeypatch):
    fake_emulation_tree(monkeypatch, child=True)
    child = SimpleNamespace(pid=43, poll=lambda: None)
    snapshot = boundary._emulation_snapshot(child, ((42, 1, 1042), (1, 0, 1001)))
    assert snapshot == replace(emulation_snapshot(), start=1043, tasks=((43, 1043), (44, 1044)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("Pid", "99"),
        ("Tgid", "99"),
        ("PPid", "1"),
        ("Uid", "0 0 0 0"),
        ("NoNewPrivs", "0"),
        ("Seccomp", "0"),
        ("Seccomp_filters", "1"),
        ("CapInh", "0000000000000001"),
        ("CapPrm", "0000000000000001"),
        ("CapEff", "0000000000000001"),
        ("CapBnd", "0000000000000001"),
        ("CapAmb", "0000000000000001"),
        ("NSpid", "43 44"),
        ("Threads", "1"),
    ],
)
def test_emulation_snapshot_rejects_bad_profile_on_nonleader_thread(monkeypatch, field, value):
    tree = fake_emulation_tree(monkeypatch, child=True)
    tree.profiles[44][field] = value
    with pytest.raises(VerificationFailure, match="profile_changed"):
        boundary._emulation_snapshot(
            SimpleNamespace(pid=43, poll=lambda: None), ((42, 1, 1042), (1, 0, 1001))
        )


@pytest.mark.parametrize(
    "fault",
    [
        "unexpected_pid",
        "ancestor_start",
        "task_empty",
        "task_missing_leader",
        "task_not_pid",
        "task_bound",
        "pid_namespace",
        "cgroup_namespace",
        "cgroup",
        "died",
        "start_changed",
    ],
)
def test_emulation_snapshot_rejects_namespace_ancestry_liveness_and_task_drift(monkeypatch, fault):
    tree = fake_emulation_tree(monkeypatch, child=True)
    child = SimpleNamespace(pid=43, poll=lambda: None)
    if fault == "unexpected_pid":
        tree.pids.add(99)
    elif fault == "ancestor_start":
        tree.starts[42] += 1
    elif fault == "task_empty":
        tree.tasks[43] = set()
    elif fault == "task_missing_leader":
        tree.tasks[43] = {"44"}
    elif fault == "task_not_pid":
        tree.tasks[43].add("oops")
    elif fault == "task_bound":
        tree.tasks[43] = {str(pid) for pid in range(43, 43 + boundary.EMULATOR_MAX_THREADS + 1)}
    elif fault == "pid_namespace":
        tree.namespaces[44, "pid"] = 3, 9
    elif fault == "cgroup_namespace":
        tree.namespaces[44, "cgroup"] = 5, 9
    elif fault == "cgroup":
        tree.cgroups[44] = "0::/foreign\n"
    elif fault == "died":
        child.poll = lambda: 42
    else:

        def changed_executable(pid):
            tree.starts[43] += 1
            return (11, 22, 4096, 44, 55), "f" * 64

        monkeypatch.setattr(boundary, "_native_executable", changed_executable)
    with pytest.raises(VerificationFailure):
        boundary._emulation_snapshot(child, ((42, 1, 1042), (1, 0, 1001)))
