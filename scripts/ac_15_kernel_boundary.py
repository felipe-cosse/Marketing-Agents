"""AC-15 trusted target qualification, before and between observed commands.

The host must first attest this container's private PID/cgroup namespaces,
Docker init, non-root UID, dropped capabilities and default seccomp profile.
This module adds a fixed native-only guard BEFORE observer attachment. It does
not claim CONFIG_COMPAT is absent: an exit-only ARM ELF must either fail exec
with ENOEXEC or be killed by the inherited architecture guard (SIGSYS).

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
import signal
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


def _profile_status() -> None:
    fields = {}
    for line in _read(PROC / "self/status").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            require(key not in fields, "kernel_boundary_status_unparsed")
            fields[key] = value.strip()
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


def _process_start(pid: int) -> int:
    value = _read(PROC / str(pid) / "stat", 4096)
    prefix, separator, suffix = value.rpartition(") ")
    fields = suffix.split()
    require(
        separator and prefix.startswith(f"{pid} (") and len(fields) >= 20,
        "kernel_boundary_process_stat_unparsed",
    )
    require(fields[0] not in {"Z", "X", "x"}, "kernel_boundary_process_dead")
    require(fields[19].isdecimal(), "kernel_boundary_process_start_unparsed")
    expected_parent = 0 if pid == 1 else 1
    require(fields[1] == str(expected_parent), "kernel_boundary_parent_mismatch")
    require(_names(PROC / str(pid) / "task") == {str(pid)}, "kernel_boundary_extra_threads")
    return int(fields[19])


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


def _prove_abi() -> str:
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
                    return "ENOEXEC"
                raise VerificationFailure("kernel_boundary_elf_probe_failed") from error
            if not compat:
                require(result.returncode == 42, "kernel_boundary_native_probe_failed")
            else:
                require(result.returncode == -signal.SIGSYS, "kernel_boundary_foreign_abi_executed")
                return "SIGSYS"
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
            "schema_version": 1,
            "native_abi": "linux_aarch64_little_64",
            "native_guard_installed": True,
            "core_file_limit_zero": True,
            "native_elf_exit": 42,
            "foreign_elf_rejection": abi_result,
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
