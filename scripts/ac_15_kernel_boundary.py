"""AC-15 trusted target qualification, before and between observed commands.

The host must first attest this container's private PID/cgroup namespaces,
Docker init, non-root UID, dropped capabilities and default seccomp profile.
This module adds a fixed native-only guard BEFORE observer attachment. It does
not claim CONFIG_COMPAT is absent. An ARM ELF must either fail with ENOEXEC,
be killed by the inherited architecture guard (SIGSYS), or pass a separate
live native-emulator handshake. Merely exiting with the expected code does
not qualify emulation, nor does the proof identify an emulator by its name.

No prctl/seccomp calls occur in quiescent_target. A live trusted parent keeps
the cgroup identity occupied until the collector has detached. No descendant,
including an init-reparented process or a zombie, is permitted at a snapshot.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import resource
import select
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.del_05_clean_state import VerificationFailure, require

PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
MAX_ENTRIES = 4096
READ_LIMIT = 65536
DRAIN_SECONDS = 1.0
PROBE_SECONDS = 2.0
PROBE_CLEANUP_SECONDS = 0.25
EMULATOR_MAX_THREADS = 32
EMULATOR_EXECUTABLE_LIMIT = 256 * 1024 * 1024
NATIVE_ARCH = 0xC00000B7
NATIVE_GUARD = (
    (0x20, 0, 0, 4),  # seccomp_data.arch, not the syscall number.
    (0x15, 1, 0, NATIVE_ARCH),
    (0x06, 0, 0, 0x80000000),  # KILL_PROCESS for every foreign audit ABI.
    (0x06, 0, 0, 0x7FFF0000),  # ALLOW: never mask a native syscall attempt.
)


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(_SockFilter))]


@dataclass(frozen=True)
class _Baseline:
    runner: int
    starts: tuple[int, int]
    pid_namespace: tuple[int, int]
    cgroup: tuple[int, int]


_baseline: _Baseline | None = None


def _read(path: Path, limit: int = READ_LIMIT) -> str:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    require(len(value) <= limit, "kernel_boundary_control_file_bound")
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as error:
        raise VerificationFailure("kernel_boundary_control_file_unparsed") from error


def _names(path: Path) -> set[str]:
    result: set[str] = set()
    with os.scandir(path) as entries:
        for entry in entries:
            require(len(result) < MAX_ENTRIES, "kernel_boundary_directory_bound")
            result.add(entry.name)
    return result


def _identity(path: Path) -> tuple[int, int]:
    value = path.stat()
    require(value.st_ino > 0, "kernel_boundary_identity_invalid")
    return value.st_dev, value.st_ino


def _descriptors() -> None:
    # Keep scandir's FD alive: a listdir snapshot can include a closed directory FD.
    with os.scandir(PROC / "self/fd") as entries:
        for index, entry in enumerate(entries):
            require(index < MAX_ENTRIES, "kernel_boundary_descriptor_bound")
            require(entry.name.isdecimal(), "kernel_boundary_descriptor_unparsed")
            target = os.readlink(entry.path)
            require(not target.startswith("socket:"), "kernel_boundary_socket_inherited")
            require(
                target not in {"anon_inode:[io_uring]", "anon_inode:io_uring"},
                "kernel_boundary_ring_inherited",
            )


def _status_fields(path: Path) -> dict[str, str]:
    fields = {}
    for line in _read(path).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            require(key not in fields, "kernel_boundary_status_unparsed")
            fields[key] = value.strip()
    return fields


def _profile_status() -> None:
    fields = _status_fields(PROC / "self/status")
    require(fields.get("NoNewPrivs") == "1", "kernel_boundary_no_new_privileges_required")
    require(fields.get("Seccomp") == "2", "kernel_boundary_seccomp_required")
    require(fields.get("CapEff") == "0000000000000000", "kernel_boundary_capabilities_present")
    require(fields.get("Threads") == "1", "kernel_boundary_requires_single_thread")
    require(fields.get("NSpid", "").split() == [str(os.getpid())], "kernel_boundary_proc_namespace")


def _cgroup_state() -> tuple[int, int]:
    require(_read(PROC / "self/cgroup", 1024) == "0::/\n", "kernel_boundary_private_cgroup")
    require(_read(PROC / "1/cgroup", 1024) == "0::/\n", "kernel_boundary_init_cgroup")
    mounts = []
    for line in _read(PROC / "self/mountinfo").splitlines():
        before, separator, after = line.partition(" - ")
        left, right = before.split(), after.split()
        require(separator and len(left) >= 6 and len(right) >= 3, "kernel_boundary_mount_unparsed")
        if left[4] == "/sys/fs/cgroup":
            mounts.append((left, right))
    require(len(mounts) == 1, "kernel_boundary_cgroup_mount")
    left, right = mounts[0]
    require(
        left[3] == "/" and right[0] == "cgroup2" and "ro" in left[5].split(","),
        "kernel_boundary_cgroup_read_only",
    )
    require(bool(os.statvfs(CGROUP).f_flag & os.ST_RDONLY), "kernel_boundary_cgroup_read_only")
    with os.scandir(CGROUP) as entries:
        for index, entry in enumerate(entries):
            require(index < MAX_ENTRIES, "kernel_boundary_cgroup_entry_bound")
            require(not entry.is_dir(follow_symlinks=False), "kernel_boundary_cgroup_subgroup")
    return _identity(CGROUP)


def _stat_identity(path: Path, pid: int) -> tuple[int, int]:
    value = _read(path, 4096)
    prefix, separator, suffix = value.rpartition(") ")
    fields = suffix.split()
    require(
        separator and prefix.startswith(f"{pid} (") and len(fields) >= 20,
        "kernel_boundary_process_stat_unparsed",
    )
    require(fields[0] not in {"Z", "X", "x"}, "kernel_boundary_process_dead")
    require(fields[19].isdecimal(), "kernel_boundary_process_start_unparsed")
    require(fields[1].isdecimal(), "kernel_boundary_parent_mismatch")
    return int(fields[1]), int(fields[19])


def _stat_start(path: Path, pid: int, expected_parent: int) -> int:
    parent, start = _stat_identity(path, pid)
    require(parent == expected_parent, "kernel_boundary_parent_mismatch")
    return start


def _process_start(pid: int) -> int:
    start = _stat_start(PROC / str(pid) / "stat", pid, 0 if pid == 1 else 1)
    require(_names(PROC / str(pid) / "task") == {str(pid)}, "kernel_boundary_extra_threads")
    return start


def _snapshot() -> _Baseline:
    runner = os.getpid()
    require(runner > 1 and os.getppid() == 1, "kernel_boundary_init_parent_required")
    pids = {name for name in _names(PROC) if name.isdecimal()}
    require(pids == {"1", str(runner)}, "kernel_boundary_descendants_remaining")
    _profile_status()
    _descriptors()
    namespace = _identity(PROC / "self/ns/pid")
    require(namespace == _identity(PROC / "1/ns/pid"), "kernel_boundary_pid_namespace_changed")
    return _Baseline(
        runner, (_process_start(1), _process_start(runner)), namespace, _cgroup_state()
    )


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    libc.prctl.restype = ctypes.c_int
    libc.syscall.restype = ctypes.c_long
    return libc


def _install_guard(libc) -> None:
    filters = (_SockFilter * len(NATIVE_GUARD))(*(_SockFilter(*row) for row in NATIVE_GUARD))
    program = _SockFprog(len(filters), filters)
    require(
        libc.prctl(22, 2, ctypes.cast(ctypes.pointer(program), ctypes.c_void_p).value, 0, 0) == 0,
        "kernel_boundary_native_guard_install_failed",
    )


def _zero_core_file_limit() -> None:
    # This suppresses ordinary core files, not host-configured piped handlers.
    # Fresh static probes contain only the fixed ELF bytes and an empty env;
    # their cwd is owned temporary storage, never the source checkout.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    require(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0), "kernel_boundary_core_limit_failed")


def _elf_probe(*, compat: bool) -> bytes:
    """Static RX, no interpreter/relocations; only exit(42), for 4/16/64-KiB pages.

    ARM EABI5 sets the EABI mask required by compat_elf_check_arch. The native
    positive control shares the load-address/alignment construction. Source:
    https://github.com/torvalds/linux/blob/v7.0/arch/arm64/kernel/process.c
    https://github.com/torvalds/linux/blob/v7.0/fs/binfmt_elf.c
    """
    ident = b"\x7fELF" + bytes((1 if compat else 2, 1, 1, 0)) + bytes(8)
    base = 0x10000
    if compat:
        code = struct.pack("<III", 0xE3A0002A, 0xE3A07001, 0xEF000000)
        size = 52 + 32 + len(code)
        header = struct.pack(
            "<16sHHIIIIIHHHHHH",
            ident,
            2,
            40,
            1,
            base + 84,
            52,
            0,
            0x05000000,
            52,
            32,
            1,
            0,
            0,
            0,
        )
        segment = struct.pack("<IIIIIIII", 1, 0, base, base, size, size, 5, 0x10000)
    else:
        code = struct.pack("<III", 0xD2800540, 0xD2800BA8, 0xD4000001)
        size = 64 + 56 + len(code)
        header = struct.pack(
            "<16sHHIQQQIHHHHHH",
            ident,
            2,
            183,
            1,
            base + 120,
            64,
            0,
            0,
            64,
            56,
            1,
            0,
            0,
            0,
        )
        segment = struct.pack("<IIQQQQQQ", 1, 5, 0, base, base, size, size, 0x10000)
    return header + segment + code


def _emulation_handshake() -> bytes:
    """Fixed ARM EABI prefix: write R, read G, then fall through; bad I/O exits43.

    The caller may append only its own source-fixed diagnostic instructions.
    SP has four bytes reserved. ARM syscall numbers3/4/1 are read/write/exit:
    https://github.com/torvalds/linux/blob/v7.0/arch/arm/tools/syscall.tbl
    """
    fail = [0xE3A0002B, 0xE3A07001, 0xEF000000]
    words = [
        0xE24DD004,  # sub sp, sp, #4
        0xE3A03052,  # mov r3, #'R'
        0xE5CD3000,  # strb r3, [sp]
        0xE3A00001,  # write(1, sp, 1)
        0xE1A0100D,
        0xE3A02001,
        0xE3A07004,
        0xEF000000,
        0xE3500001,  # cmp r0, #1
        0x0A000002,  # beq past the three-instruction failure exit
        *fail,
        0xE3A00000,  # read(0, sp, 1)
        0xE1A0100D,
        0xE3A02001,
        0xE3A07003,
        0xEF000000,
        0xE3500001,
        0x0A000002,
        *fail,
        0xE5DD3000,  # ldrb r3, [sp]
        0xE3530047,  # cmp r3, #'G'
        0x0A000002,
        *fail,
    ]
    return struct.pack(f"<{len(words)}I", *words)


def _elf_handshake() -> bytes:
    code = _emulation_handshake() + struct.pack("<III", 0xE3A0002A, 0xE3A07001, 0xEF000000)
    image = bytearray(_elf_probe(compat=True)[:84] + code)
    struct.pack_into("<II", image, 52 + 16, len(image), len(image))
    return bytes(image)


def _native_executable(pid: int) -> tuple[tuple[int, ...], str]:
    """Read only a bounded ELF header from the live owned process's executable.

    No readlink, pathname, process name, or full binary authenticity claim.
    The retained FD and metadata comparison bind each read to one object.
    """
    descriptor = os.open(PROC / str(pid) / "exe", os.O_RDONLY | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_mode & 0o111
            and 64 <= before.st_size <= EMULATOR_EXECUTABLE_LIMIT,
            "kernel_boundary_emulator_executable_invalid",
        )
        header = os.read(descriptor, 64)
        after = os.fstat(descriptor)

        def identity(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

        require(
            identity(before) == identity(after) == identity(os.stat(PROC / str(pid) / "exe")),
            "kernel_boundary_emulator_executable_changed",
        )
        require(
            len(header) == 64
            and header[:7] == b"\x7fELF\x02\x01\x01"
            and struct.unpack_from("<HHI", header, 16) in {(2, 183, 1), (3, 183, 1)}
            and struct.unpack_from("<H", header, 52)[0] == 64,
            "kernel_boundary_emulator_native_elf_required",
        )
        return identity(before), hashlib.sha256(header).hexdigest()
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _EmulationSnapshot:
    start: int
    tasks: tuple[tuple[int, int], ...]
    pid_namespace: tuple[int, int]
    cgroup_namespace: tuple[int, int]
    cgroup: tuple[int, int]
    executable: tuple[int, ...]
    header_sha256: str


def _emulation_ancestors() -> tuple[tuple[int, int, int], ...]:
    """Only the trusted caller's live ancestor chain; never absorb other work."""
    chain = []
    pid = os.getpid()
    pid_namespace = _identity(PROC / "self/ns/pid")
    cgroup_namespace = _identity(PROC / "self/ns/cgroup")
    filters = _status_fields(PROC / "self/status").get("Seccomp_filters", "")
    require(filters.isdecimal() and int(filters) >= 2, "kernel_boundary_emulator_guard_unproven")
    while True:
        require(
            pid >= 1 and len(chain) < 3 and pid not in {row[0] for row in chain},
            "kernel_boundary_emulator_ancestors",
        )
        root = PROC / str(pid)
        parent, start = _stat_identity(root / "stat", pid)
        require(
            _names(root / "task") == {str(pid)},
            "kernel_boundary_emulator_ancestor_threads",
        )
        status = _status_fields(root / "status")
        ancestor_filters = status.get("Seccomp_filters", "")
        require(
            status.get("Uid", "").split() == ["10001"] * 4
            and status.get("NoNewPrivs") == "1"
            and status.get("Seccomp") == "2"
            and ancestor_filters.isdecimal()
            and (
                1 <= int(ancestor_filters) < int(filters)
                if pid == 1
                else ancestor_filters == filters
            )
            and all(
                status.get(key) == "0000000000000000"
                for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
            )
            and status.get("NSpid", "").split() == [str(pid)]
            and status.get("Threads") == "1",
            "kernel_boundary_emulator_ancestor_profile",
        )
        require(
            _read(root / "cgroup", 1024) == "0::/\n"
            and _identity(root / "ns/pid") == pid_namespace
            and _identity(root / "ns/cgroup") == cgroup_namespace,
            "kernel_boundary_emulator_ancestor_namespace",
        )
        chain.append((pid, parent, start))
        if pid == 1:
            require(parent == 0 and len(chain) in {2, 3}, "kernel_boundary_emulator_ancestors")
            return tuple(chain)
        pid = parent


def _emulation_snapshot(
    child: subprocess.Popen, ancestors: tuple[tuple[int, int, int], ...]
) -> _EmulationSnapshot:
    require(child.poll() is None, "kernel_boundary_emulator_died")
    pid, runner = child.pid, os.getpid()
    require(type(pid) is int and pid > 1 and pid != runner, "kernel_boundary_emulator_pid")
    require(
        _emulation_ancestors() == ancestors
        and {name for name in _names(PROC) if name.isdecimal()}
        == {str(row[0]) for row in ancestors} | {str(pid)},
        "kernel_boundary_emulator_unexpected_process",
    )
    root = PROC / str(pid)
    start = _stat_start(root / "stat", pid, runner)
    pid_namespace = _identity(PROC / "self/ns/pid")
    cgroup_namespace = _identity(PROC / "self/ns/cgroup")
    cgroup = _identity(CGROUP)
    require(_read(PROC / "self/cgroup", 1024) == "0::/\n", "kernel_boundary_private_cgroup")
    parent_status = _status_fields(PROC / "self/status")
    filters = parent_status.get("Seccomp_filters", "")
    require(filters.isdecimal() and int(filters) >= 2, "kernel_boundary_emulator_guard_unproven")
    tasks = _names(root / "task")
    require(
        1 <= len(tasks) <= EMULATOR_MAX_THREADS
        and str(pid) in tasks
        and all(t.isdecimal() for t in tasks),
        "kernel_boundary_emulator_task_bound",
    )
    identities = []
    for tid in sorted(map(int, tasks)):
        task = root / "task" / str(tid)
        status = _status_fields(task / "status")
        require(
            status.get("Pid") == str(tid)
            and status.get("Tgid") == str(pid)
            and status.get("PPid") == str(runner)
            and status.get("Uid", "").split() == ["10001"] * 4
            and status.get("NoNewPrivs") == "1"
            and status.get("Seccomp") == "2"
            and status.get("Seccomp_filters") == filters
            and all(
                status.get(key) == "0000000000000000"
                for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
            )
            and status.get("NSpid", "").split() == [str(tid)]
            and status.get("Threads") == str(len(tasks)),
            "kernel_boundary_emulator_profile_changed",
        )
        require(
            _read(task / "cgroup", 1024) == "0::/\n"
            and _identity(task / "ns/pid") == pid_namespace
            and _identity(task / "ns/cgroup") == cgroup_namespace,
            "kernel_boundary_emulator_namespace_changed",
        )
        identities.append((tid, _stat_start(task / "stat", tid, runner)))
    executable, header_sha256 = _native_executable(pid)
    require(
        _stat_start(root / "stat", pid, runner) == start and child.poll() is None,
        "kernel_boundary_emulator_identity_changed",
    )
    return _EmulationSnapshot(
        start, tuple(identities), pid_namespace, cgroup_namespace, cgroup, executable, header_sha256
    )


def _probe_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    require(remaining > 0, "kernel_boundary_emulator_deadline")
    return remaining


def _run_emulation_probe(path: Path, directory: str | Path) -> dict:
    """Run a source-fixed R/G guest, not a public arbitrary-code runner.

    Both qualification and the separately observed fixed syscall canary reuse
    this live-native-ABI proof. The caller owns and binds the guest source.
    """
    deadline = time.monotonic() + PROBE_SECONDS
    work_deadline = deadline - PROBE_CLEANUP_SECONDS
    child = None
    proof = None
    cleanup_ok = True
    try:
        _profile_status()
        ancestors = _emulation_ancestors()
        require(
            {name for name in _names(PROC) if name.isdecimal()}
            == {str(row[0]) for row in ancestors},
            "kernel_boundary_emulator_unexpected_process",
        )
        child = subprocess.Popen(
            [str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(directory),
            close_fds=True,
            env={},
            bufsize=0,
        )
        require(
            child.stdin is not None and child.stdout is not None, "kernel_boundary_emulator_pipes"
        )
        descriptor = child.stdout.fileno()
        readable, _, _ = select.select([descriptor], [], [], _probe_remaining(work_deadline))
        require(bool(readable) and os.read(descriptor, 2) == b"R", "kernel_boundary_emulator_ready")
        before = _emulation_snapshot(child, ancestors)
        after = _emulation_snapshot(child, ancestors)
        require(before == after, "kernel_boundary_emulator_identity_changed")
        _probe_remaining(work_deadline)
        require(
            not select.select([descriptor], [], [], 0)[0], "kernel_boundary_emulator_extra_output"
        )
        require(os.write(child.stdin.fileno(), b"G") == 1, "kernel_boundary_emulator_release")
        child.stdin.close()
        require(
            child.wait(timeout=_probe_remaining(work_deadline)) == 42,
            "kernel_boundary_emulator_exit",
        )
        readable, _, _ = select.select([descriptor], [], [], _probe_remaining(work_deadline))
        require(
            bool(readable) and os.read(descriptor, 2) == b"",
            "kernel_boundary_emulator_extra_output",
        )
        _probe_remaining(work_deadline)
        proof = {
            "guest_elf_class": 1,
            "guest_elf_machine": 40,
            "host_elf_class": 2,
            "host_elf_machine": 183,
            "host_elf_little_endian": True,
            "host_elf_header_sha256": before.header_sha256,
            "stable_process_identity": True,
            "same_cgroup": True,
            "same_pid_namespace": True,
            "no_new_privileges": True,
            "seccomp_filter": True,
            "capabilities_zero": True,
            "handshake_exit": 42,
        }
    except (OSError, subprocess.SubprocessError) as error:
        raise VerificationFailure("kernel_boundary_emulator_probe_failed") from error
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
            except (OSError, subprocess.SubprocessError):
                cleanup_ok = False
            try:
                child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (OSError, subprocess.SubprocessError):
                cleanup_ok = False
            for stream in (child.stdin, child.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    cleanup_ok = False
        require(
            cleanup_ok and time.monotonic() <= deadline, "kernel_boundary_emulator_cleanup_failed"
        )
    require(proof is not None, "kernel_boundary_emulator_proof_incomplete")
    return proof


def _prove_native_emulation() -> dict:
    with tempfile.TemporaryDirectory(prefix="ac15-native-emulation-") as directory:
        path = Path(directory) / "arm-handshake"
        with path.open("xb") as stream:
            stream.write(_elf_handshake())
        path.chmod(0o700)
        return _run_emulation_probe(path, directory)


def _prove_abi() -> dict:
    with tempfile.TemporaryDirectory(prefix="ac15-native-abi-") as directory:
        for compat in (False, True):
            path = Path(directory) / ("arm-exit" if compat else "aarch64-exit")
            with path.open("xb") as stream:
                stream.write(_elf_probe(compat=compat))
            path.chmod(0o700)
            try:
                result = subprocess.run(
                    [str(path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=directory,
                    close_fds=True,
                    env={},
                    timeout=PROBE_SECONDS,
                    check=False,
                )
            except OSError as error:
                if compat and error.errno == errno.ENOEXEC:
                    return {
                        "foreign_elf_mode": "rejected",
                        "foreign_elf_rejection": "ENOEXEC",
                        "native_emulation": None,
                    }
                raise VerificationFailure("kernel_boundary_elf_probe_failed") from error
            if not compat:
                require(result.returncode == 42, "kernel_boundary_native_probe_failed")
            else:
                if result.returncode == 42:
                    return {
                        "foreign_elf_mode": "native_emulation",
                        "foreign_elf_rejection": None,
                        "native_emulation": _prove_native_emulation(),
                    }
                require(result.returncode == -signal.SIGSYS, "kernel_boundary_foreign_abi_executed")
                return {
                    "foreign_elf_mode": "rejected",
                    "foreign_elf_rejection": "SIGSYS",
                    "native_emulation": None,
                }
    raise VerificationFailure("kernel_boundary_abi_probe_incomplete")


def _prove_uring_denied(libc) -> str:
    # Native UAPI io_uring_params: ten u32 words plus two 40-byte offset structs.
    # No flags, no reserved bits, one entry; EFAULT/EINVAL are NOT denial proofs.
    params = (ctypes.c_uint64 * 15)()
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(425), ctypes.c_uint(1), ctypes.byref(params))
    saved_errno = ctypes.get_errno()
    if result >= 0:
        os.close(result)
        raise VerificationFailure("kernel_boundary_io_uring_available")
    require(
        result == -1 and saved_errno in {errno.EPERM, errno.ENOSYS},
        "kernel_boundary_io_uring_denial_unproven",
    )
    return "EPERM" if saved_errno == errno.EPERM else "ENOSYS"


def qualify_target() -> dict:
    """Run once before observer READY; no untrusted child may precede this call."""
    global _baseline
    require(_baseline is None, "kernel_boundary_already_qualified")
    require(
        sys.platform == "linux"
        and sys.byteorder == "little"
        and platform.machine() == "aarch64"
        and ctypes.sizeof(ctypes.c_void_p) == ctypes.sizeof(ctypes.c_ulong) == 8,
        "kernel_boundary_native_platform_required",
    )
    require(os.getuid() == os.geteuid() == 10001, "kernel_boundary_target_uid_required")
    try:
        before = _snapshot()
        libc = _libc()
        require(_snapshot() == before, "kernel_boundary_startup_changed")
        _zero_core_file_limit()
        _install_guard(libc)
        abi_result = _prove_abi()
        uring_result = _prove_uring_denied(libc)
        require(_snapshot() == before, "kernel_boundary_qualification_changed")
        proof = {
            "schema_version": 2,
            "native_abi": "linux_aarch64_little_64",
            "native_guard_installed": True,
            "core_file_limit_zero": True,
            "native_elf_exit": 42,
            **abi_result,
            "io_uring_setup_rejection": uring_result,
            "inherited_sockets": 0,
            "inherited_io_uring": 0,
            "cgroup_inode": before.cgroup[1],
            "cgroup_read_only": True,
            "cgroup_subgroups": 0,
            "trusted_process_count": 2,
            "trusted_thread_count": 2,
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        _baseline = before
        return proof
    except OSError as error:
        raise VerificationFailure("kernel_boundary_qualification_unavailable") from error


def quiescent_target() -> dict:
    """Exact baseline after child wait; at most one second for native task drain."""
    require(_baseline is not None, "kernel_boundary_not_qualified")
    deadline = time.monotonic() + DRAIN_SECONDS
    while True:
        try:
            require(_snapshot() == _baseline, "kernel_boundary_identity_changed")
            require(time.monotonic() <= deadline, "kernel_boundary_drain_deadline")
            return {
                "schema_version": 1,
                "cgroup_inode": _baseline.cgroup[1],
                "cgroup_unchanged": True,
                "remaining_tasks": 0,
                "trusted_process_count": 2,
                "trusted_thread_count": 2,
                "baseline_unchanged": True,
            }
        except (OSError, VerificationFailure) as error:
            # Only a just-exited native task may drain. Never forgive an observed
            # changed identity/profile/descriptor or unavailable baseline file.
            if not isinstance(error, VerificationFailure) or str(error) not in {
                "kernel_boundary_descendants_remaining",
                "kernel_boundary_requires_single_thread",
                "kernel_boundary_extra_threads",
            }:
                raise VerificationFailure("kernel_boundary_not_quiescent") from error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VerificationFailure("kernel_boundary_not_quiescent") from error
            time.sleep(min(0.001, remaining))
