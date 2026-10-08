"""Fixed emulated-guest control of actual native kernel connect observation.

This does not qualify arbitrary guest-ABI semantics or authenticate an emulator
binary. The held-child proof establishes its native host ELF/profile; the two
source-fixed guest connects must then appear in the unchanged kernel audit.
There is no CLI option for a program, destination, or alternate interpreter.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import struct
import sys
import tempfile
from pathlib import Path

from scripts import ac_15_kernel_boundary as boundary
from scripts.del_05_clean_state import VerificationFailure, require, verification_signals

CONTROL_SECONDS = 10
EXTERNAL_ERRNO = 101
_BASE = 0x10000
_HEADER_SIZE = 84


def _guest_image(unix_path: bytes) -> bytes:
    """Build one static ARM EABI5 RX image; writable handshake storage is on SP.

    Linux v7.0 ARM table: socket281, connect283, close6, exit1 (read3/write4
    belong to the shared fixed handshake). No socketcall multiplexing or data
    transmission. Only the owned UNIX listener pathname varies internally.
    https://github.com/torvalds/linux/blob/v7.0/arch/arm/tools/syscall.tbl
    """
    require(
        isinstance(unix_path, bytes)
        and unix_path.startswith(b"/")
        and 1 <= len(unix_path) <= 107
        and b"\0" not in unix_path,
        "qemu_control_unix_path_invalid",
    )
    prefix = boundary._emulation_handshake()
    words = list(struct.unpack(f"<{len(prefix) // 4}I", prefix))
    literals: list[tuple[int, int, int | str]] = []
    failures: list[tuple[int, int]] = []

    def emit(*instructions: int) -> None:
        words.extend(instructions)

    def load(register: int, value: int | str) -> None:
        literals.append((len(words), register, value))
        emit(0)  # PC-relative LDR, fixed after the literal pool is placed.

    def fail_if(condition: int) -> None:
        failures.append((len(words), condition))
        emit(0)

    for family, address, length in ((2, "external", 16), (1, "unix", len(unix_path) + 3)):
        emit(0xE3A00000 | family, 0xE3A01001, 0xE3A02000)  # socket(family, STREAM, 0)
        load(7, 281)
        emit(0xEF000000, 0xE3500000)  # svc; cmp r0, #0
        fail_if(0xB)  # blt: no fd on any negative socket result
        emit(0xE1A04000)  # mov r4, r0: preserve the owned descriptor
        load(1, address)
        emit(0xE3A02000 | length)  # sockaddr length is <=110, no rotation needed
        load(7, 283)
        emit(0xEF000000)
        emit(0xE3700065 if family == 2 else 0xE3500000)  # cmn r0, #101 / cmp r0, #0
        fail_if(1)  # bne: exactly -ENETUNREACH or exactly successful UNIX connect
        emit(0xE1A00004, 0xE3A07006, 0xEF000000, 0xE3500000)  # close(r4), require zero
        fail_if(1)
    emit(0xE3A0002A, 0xE3A07001, 0xEF000000)  # exit(42)
    failure = len(words)
    emit(0xE3A0002B, 0xE3A07001, 0xEF000000, 0xEAFFFFFB)  # exit(43), never fall into data
    pool_offset = _HEADER_SIZE + 4 * len(words)
    data_offset = pool_offset + 4 * len(literals)
    external = b"\x02\x00\x00\x09\xc0\x00\x02\x01" + bytes(8)
    unix = b"\x01\x00" + unix_path + b"\0"
    addresses = {"external": _BASE + data_offset, "unix": _BASE + data_offset + len(external)}
    pool = bytearray()
    for index, (instruction, register, value) in enumerate(literals):
        distance = pool_offset + 4 * index - (_HEADER_SIZE + 4 * instruction + 8)
        require(0 <= distance <= 4095, "qemu_control_literal_range")
        words[instruction] = 0xE59F0000 | (register << 12) | distance
        pool.extend(struct.pack("<I", addresses[value] if isinstance(value, str) else value))
    for instruction, condition in failures:
        distance = failure - instruction - 2
        require(0 <= distance < (1 << 23), "qemu_control_branch_range")
        words[instruction] = (condition << 28) | 0x0A000000 | distance
    image = bytearray(
        boundary._elf_probe(compat=True)[:_HEADER_SIZE]
        + struct.pack(f"<{len(words)}I", *words)
        + pool
        + external
        + unix
    )
    require(len(image) <= 4096, "qemu_control_image_bound")
    struct.pack_into("<II", image, 52 + 16, len(image), len(image))
    return bytes(image)


def _inherited_profile() -> None:
    require(
        not sys.flags.optimize
        and sys.platform == "linux"
        and sys.byteorder == "little"
        and platform.machine() == "aarch64"
        and struct.calcsize("P") == 8,
        "qemu_control_native_arm64_required",
    )
    require(os.getuid() == os.geteuid() == 10001, "qemu_control_unprivileged_uid_required")
    boundary._profile_status()
    fields = boundary._status_fields(boundary.PROC / "self/status")
    require(
        fields.get("Uid", "").split() == ["10001"] * 4
        and all(
            fields.get(key) == "0000000000000000"
            for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
        and fields.get("Seccomp_filters", "").isdecimal()
        and int(fields["Seccomp_filters"]) >= 2,
        "qemu_control_inherited_profile_required",
    )


def _run_control() -> dict:
    # Late import avoids the runner's integration import cycle. The route proof
    # precedes every real probe; no network or filter setting is changed here.
    from scripts.ac_15_offline import kernel_namespace

    _inherited_profile()
    kernel_namespace()
    with tempfile.TemporaryDirectory(prefix="ac15-qemu-") as directory:
        root = Path(directory)
        listener_path = root / "local.sock"
        guest = root / "fixed-arm-control"
        image = _guest_image(os.fsencode(listener_path))
        with guest.open("xb") as stream:
            stream.write(image)
        guest.chmod(0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(listener_path))
            listener.listen(1)
            # No accept thread/process: one connected socket fits the backlog.
            # The helper proves the blocked native child, releases G, checks42,
            # and kills/reaps/closes its pipes within its shared two-second cap.
            proof = boundary._run_emulation_probe(guest, directory)
    return {"native_emulation": proof, "external_errno": EXTERNAL_ERRNO, "unix_connected": True}


def main(argv: list[str] | None = None) -> int:
    try:
        require(not (sys.argv[1:] if argv is None else argv), "qemu_control_no_arguments")
        with verification_signals(CONTROL_SECONDS):
            result = _run_control()
            print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
        return 0
    except (VerificationFailure, OSError):
        # Never disclose temporary paths, emulator argv, or an arbitrary error.
        print("qemu_control_failed", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
