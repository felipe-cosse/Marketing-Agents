"""Fake-only native-boundary proofs: never load libc, execute ELF, or use Docker."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import signal
import struct
import subprocess
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
    assert boundary._prove_abi() == expected
    assert len(calls) == 2 and all(not path.exists() for path in calls)


@pytest.mark.parametrize("native", [0, 41, 43, -signal.SIGSYS, OSError(errno.ENOEXEC, "synthetic")])
def test_abi_does_not_accept_broken_native_control(monkeypatch, native):
    calls = probe_run(monkeypatch, [native])
    with pytest.raises(VerificationFailure, match=r"(?:native_probe|elf_probe)_failed"):
        boundary._prove_abi()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "compat", [0, 42, -signal.SIGKILL, -signal.SIGSEGV, OSError(errno.EPERM, "synthetic")]
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
    monkeypatch.setattr(boundary, "_prove_abi", record("abi", "ENOEXEC"))
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
        "schema_version": 1,
        "native_abi": "linux_aarch64_little_64",
        "native_guard_installed": True,
        "core_file_limit_zero": True,
        "native_elf_exit": 42,
        "foreign_elf_rejection": "ENOEXEC",
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
