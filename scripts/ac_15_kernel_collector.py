"""Bounded AC-15 native ARM64 kernel observer, separate from the target.

This privileged process has only BPF/PERFMON; the target remains unprivileged.
The host owns source/image/cgroup identity and proves target quiescence BEFORE
each snapshot and before STOP. A snapshot is not itself a quiescence proof.
The target stays alive until both hooks detach and this process exits. There
are no counter resets, event payloads, process names, destinations, or pinning.

The generators preserve the reviewed v7.0 native ABI. Live BTF validates the
selected hook, not the exact running kernel's entire source or foreign ABIs.
Mutable userspace buffers remain observations at hook time, not an atomic copy
of the subsequent kernel read. Connected sends require the surrounding gate's
inherited-descriptor and destination-attempt controls.

Counters are atomic u64. Snapshots reject the high half and any decrease, with a
sticky runtime overflow flag. At most 1024 destinations are processed per entry;
wrapping a u64 within the fixed 1720-second lifetime would need over 10^16
observations/second. The half-range threshold is intentionally far below that
unattainable workload, not a claim that modulo counters are generally safe.
"""

import ctypes
import hashlib
import json
import os
import platform
import re
import signal
import struct
import sys
import zlib
from pathlib import Path

from scripts.ac_15_kernel_bytecode import (
    COUNTER_NAMES,
    PENDING_MAX_ENTRIES,
    PENDING_VALUE_SIZE,
    exit_instructions,
    instructions,
)

SCHEMA_VERSION = 1
MAX_LIFETIME = 1720
MAX_COMMANDS = 256
MAX_LINE = 16384
MAX_BTF_BYTES = 64 * 1024**2
MAX_CONFIG_COMPRESSED = 2 * 1024**2
MAX_CONFIG_DECOMPRESSED = 4 * 1024**2
COUNTER_LIMIT = 1 << 63
ROOT = Path(__file__).resolve().parents[1]


def require(value, code):
    if not value:
        raise ValueError(code)


def bounded_read(path, maximum):
    with Path(path).open("rb") as source:
        content = source.read(maximum + 1)
    require(len(content) <= maximum, "bounded_input_exceeded")
    return content


def kernel_configuration():
    """Qualify only the live kernel's bounded, uniquely disabled VMA feature.

    No installed config, alternate path, enabled/unknown fallback, raw config or
    exception text is evidence. The hash identifies the decompressed live bytes.
    """
    release = platform.release()
    require(
        type(release) is str and re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", release),
        "invalid_kernel_release",
    )
    compressed = bounded_read("/proc/config.gz", MAX_CONFIG_COMPRESSED)
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = decoder.decompress(compressed, MAX_CONFIG_DECOMPRESSED + 1)
    except zlib.error as error:
        raise ValueError("invalid_kernel_config_gzip") from error
    require(len(raw) <= MAX_CONFIG_DECOMPRESSED, "kernel_config_expanded_bound")
    require(
        decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data,
        "invalid_kernel_config_gzip",
    )
    content = raw.decode("ascii")
    # Treat even malformed/indented alternate spellings as ambiguity, rather
    # than allowing them to hide beside the sole canonical disabled declaration.
    matches = [line for line in content.splitlines() if "CONFIG_ANON_VMA_NAME" in line]
    require(matches == ["# CONFIG_ANON_VMA_NAME is not set"], "unsupported_anon_vma_config")
    return {
        "source": "/proc/config.gz",
        "release": release,
        "config_sha256": hashlib.sha256(raw).hexdigest(),
        "anon_vma_name": "disabled",
        "policy": "disabled_requires_einval",
    }


def verify_bound_source():
    # Lazy import avoids a cycle with the host bridge. This checks every source
    # file using the same image-baked manifest as the unprivileged suite runner.
    from scripts.ac_15_offline import verify_source

    manifest = json.loads(bounded_read("/opt/ac15/source-manifest.json", 16 * 1024**2))
    verify_source(ROOT, manifest)
    values = {key: manifest[key] for key in ("source_commit", "source_tree", "source_sha256")}
    require(
        all(
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}" if key == "source_sha256" else r"[0-9a-f]{40}", value)
            for key, value in values.items()
        ),
        "invalid_source_identity",
    )
    return values


def collector_boundary(target):
    require(type(target) is int and 0 < target < 2**64, "invalid_target_cgroup")
    require(platform.machine() == "aarch64" and os.getuid() == 0, "native_root_collector_required")
    own = os.stat("/sys/fs/cgroup").st_ino
    require(own != target, "collector_must_be_outside_target")
    require(
        bounded_read("/proc/self/cgroup", 4096).strip() == b"0::/",
        "private_cgroup_namespace_required",
    )
    lines = bounded_read("/proc/self/status", 65536).decode("ascii").splitlines()
    status = dict(line.split(":", 1) for line in lines if ":" in line)
    for field in ("CapEff", "CapPrm", "CapBnd"):
        require(int(status[field], 16) == (1 << 38) | (1 << 39), "collector_capabilities_invalid")
    for field in ("CapInh", "CapAmb"):
        require(int(status[field], 16) == 0, "collector_extra_capabilities")
    require(
        int(status["NoNewPrivs"]) == 1 and int(status["Seccomp"]) == 2,
        "collector_kernel_boundary_invalid",
    )
    namespace_proof()
    return own


def btf_schema(raw):
    """Read bounded public kernel BTF; only return the selected hook's ABI."""
    assert 24 <= len(raw) <= 64 * 1024**2
    magic, version, flags, header, toff, tlen, soff, slen = struct.unpack_from("<HBBIIIII", raw)
    assert (magic, version, flags) == (0xEB9F, 1, 0)
    assert (
        24 <= header <= len(raw)
        and header + toff + tlen <= len(raw)
        and header + soff + slen <= len(raw)
    )
    strings = raw[header + soff : header + soff + slen]
    types = [None]
    pos, end = header + toff, header + toff + tlen
    sizes = {
        1: (4, 0),
        2: (0, 0),
        3: (12, 0),
        4: (0, 12),
        5: (0, 12),
        6: (0, 8),
        7: (0, 0),
        8: (0, 0),
        9: (0, 0),
        10: (0, 0),
        11: (0, 0),
        12: (0, 0),
        13: (0, 8),
        14: (4, 0),
        15: (0, 12),
        16: (0, 0),
        17: (4, 0),
        18: (0, 0),
        19: (0, 12),
    }

    def name(offset):
        assert 0 <= offset < len(strings)
        stop = strings.find(b"\0", offset)
        assert stop >= offset and stop - offset < 65536
        return strings[offset:stop].decode("utf8")

    while pos < end:
        assert pos + 12 <= end and len(types) < 1000000
        noff, info, value = struct.unpack_from("<III", raw, pos)
        kind, count = (info >> 24) & 31, info & 65535
        assert kind in sizes
        fixed, each = sizes[kind]
        extra = fixed + each * count
        assert pos + 12 + extra <= end
        types.append(
            (name(noff), kind, count, value, raw[pos + 12 : pos + 12 + extra], bool(info >> 31))
        )
        pos += 12 + extra
    assert pos == end

    def resolve(index):
        for _ in range(16):
            assert 0 < index < len(types)
            row = types[index]
            if row[1] not in {8, 9, 10, 11, 18}:
                return row
            index = row[3]
        raise ValueError("btf_type_cycle")

    matches = [
        (i, row) for i, row in enumerate(types) if row and row[1] == 12 and row[0] == "do_el0_svc"
    ]
    assert len(matches) == 1, "native_entry_symbol_missing_or_ambiguous"
    identifier, function = matches[0]
    proto = resolve(function[3])
    assert proto[1] == 13 and proto[2] == 1 and proto[3] == 0
    _parameter_name, parameter_type = struct.unpack("<II", proto[4])
    pointer = resolve(parameter_type)
    assert pointer[1] == 2
    regs = resolve(pointer[3])
    assert regs[0] == "pt_regs" and regs[1] == 4

    def fields(row, base=0, depth=0):
        assert depth < 8 and row[1] in {4, 5}
        for offset in range(0, len(row[4]), 12):
            noff, type_id, bits = struct.unpack_from("<III", row[4], offset)
            if row[5]:
                assert (bits >> 24) == 0
                bits &= 0xFFFFFF
            assert bits % 8 == 0
            field_name = name(noff)
            member = resolve(type_id)
            if field_name == "regs":
                yield base + bits // 8, member
            elif not field_name and member[1] in {4, 5}:
                yield from fields(member, base + bits // 8, depth + 1)

    candidates = list(fields(regs))
    assert len(candidates) == 1
    offset, array = candidates[0]
    assert array[1] == 3
    element, _index_type, count = struct.unpack("<III", array[4])
    integer = resolve(element)
    assert integer[1] == 1 and integer[3] == 8 and count == 31 and offset == 0
    assert regs[3] >= 31 * 8
    return {
        "function_btf_id": identifier,
        "regs8_offset": offset + 8 * 8,
        "pt_regs_size": regs[3],
        "btf_sha256": hashlib.sha256(raw).hexdigest(),
        "compat_symbol_present": any(
            row and row[1] == 12 and row[0] == "do_el0_svc_compat" for row in types
        ),
    }


def namespace_proof():
    """Before the synthetic control, recheck local-only kernel state."""

    def bounded(path, limit):
        with open(path, "rb") as source:
            content = source.read(limit + 1)
        assert len(content) <= limit
        return content

    entries = []
    with os.scandir("/sys/class/net") as directory:
        for entry in directory:
            assert len(entries) < 128
            assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", entry.name)
            entries.append(entry)
    found_loopback = False
    for entry in sorted(entries, key=lambda item: item.name):
        if not entry.is_dir():
            assert not entry.is_symlink() and entry.is_file(follow_symlinks=False)
            continue
        flags_text = bounded(entry.path + "/flags", 32).strip()
        assert re.fullmatch(rb"0x[0-9a-fA-F]{1,8}", flags_text)
        flags = int(flags_text, 16)
        state = bounded(entry.path + "/operstate", 32).strip()
        if entry.name == "lo":
            assert flags & 1 and flags & 8 and state in {b"up", b"unknown"}
            found_loopback = True
        else:
            assert not flags & 1 and state == b"down"
    assert found_loopback
    routes4 = bounded("/proc/net/route", 65536).splitlines()
    assert len(routes4) == 1
    assert routes4[0].split() == [
        b"Iface",
        b"Destination",
        b"Gateway",
        b"Flags",
        b"RefCnt",
        b"Use",
        b"Metric",
        b"Mask",
        b"MTU",
        b"Window",
        b"IRTT",
    ]
    for route in bounded("/proc/net/ipv6_route", 65536).splitlines():
        fields = route.split()
        assert len(fields) == 10 and fields[9] == b"lo"
        assert all(re.fullmatch(rb"[0-9a-fA-F]{32}", fields[index]) for index in (0, 2, 4))
        assert all(re.fullmatch(rb"[0-9a-fA-F]{2}", fields[index]) for index in (1, 3))
        assert all(re.fullmatch(rb"[0-9a-fA-F]{8}", fields[index]) for index in (5, 6, 7, 8))
        assert int(fields[1], 16) <= 128 and int(fields[3], 16) <= 128


class BpfAPI:
    """Minimal native UAPI adapter; injectable syscall for dependency-free tests."""

    def __init__(self, syscall=None):
        if syscall is None:
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.syscall.restype = ctypes.c_long
            syscall = self.libc.syscall
        self.syscall = syscall
        self.verifier_log = ctypes.create_string_buffer(65536)
        self.log_required = 0

    def call(self, command, fields):
        attr = ctypes.create_string_buffer(144)
        for offset, fmt, value in fields:
            struct.pack_into(fmt, attr, offset, value)
        ctypes.set_errno(0)
        result = self.syscall(
            ctypes.c_long(280), ctypes.c_int(command), ctypes.byref(attr), ctypes.c_uint(144)
        )
        error_number = ctypes.get_errno()
        if command == 5:
            self.log_required = struct.unpack_from("<I", attr, 140)[0]
        if result < 0:
            raise OSError(error_number, "bounded_bpf_operation_failed")
        return int(result), attr

    def diagnostics(self):
        content = self.verifier_log.value
        matched = re.search(rb"processed ([0-9]{1,10}) insns \(limit ([0-9]{1,10})\)", content)
        return {
            "level": 4,
            "required_bytes": self.log_required,
            "sha256": hashlib.sha256(content).hexdigest(),
            "processed_instructions": int(matched[1]) if matched else None,
            "instruction_limit": int(matched[2]) if matched else None,
        }

    def info(self, fd, size):
        value = ctypes.create_string_buffer(size)
        _, attr = self.call(
            15, [(0, "<I", fd), (4, "<I", size), (8, "<Q", ctypes.addressof(value))]
        )
        require(struct.unpack_from("<I", attr, 4)[0] >= size, "short_bpf_object_info")
        return value

    def program_info(self, fd):
        value = self.info(fd, 232)
        require(struct.unpack_from("<I", value, 0)[0] == 26, "wrong_program_type")
        return (
            struct.unpack_from("<I", value, 4)[0],
            struct.unpack_from("<Q", value, 208)[0],
            struct.unpack_from("<I", value, 220)[0],
            struct.unpack_from("<I", value, 224)[0],
        )

    def validate_map(self, fd, map_type, key_size, maximum, value_size=8):
        value = self.info(fd, 88)
        require(struct.unpack_from("<I", value, 0)[0] == map_type, "wrong_map_type")
        require(
            struct.unpack_from("<IIII", value, 8) == (key_size, value_size, maximum, 0),
            "wrong_map_schema",
        )
        identity = struct.unpack_from("<I", value, 4)[0]
        require(identity > 0, "missing_map_identity")
        return identity

    def counter(self, fd, key):
        k, value = ctypes.c_uint(key), ctypes.c_ulonglong()
        self.call(
            1, [(0, "<I", fd), (8, "<Q", ctypes.addressof(k)), (16, "<Q", ctypes.addressof(value))]
        )
        return value.value

    def pending_empty(self, fd):
        key = ctypes.c_ulonglong()
        try:
            self.call(4, [(0, "<I", fd), (8, "<Q", 0), (16, "<Q", ctypes.addressof(key))])
        except OSError as error:
            if error.errno == 2:
                return True
            raise
        return False


class Collector:
    """Own exact BPF descriptors; never pin, reset, or silently discard state."""

    def __init__(self, target, schema, api=None, close=None, *, configuration):
        require(type(target) is int and 0 < target < 2**64, "invalid_target_cgroup")
        require(
            type(configuration) is dict
            and set(configuration)
            == {"source", "release", "config_sha256", "anon_vma_name", "policy"}
            and configuration["source"] == "/proc/config.gz"
            and configuration["anon_vma_name"] == "disabled"
            and configuration["policy"] == "disabled_requires_einval"
            and type(configuration["release"]) is str
            and re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", configuration["release"])
            and type(configuration["config_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", configuration["config_sha256"]),
            "qualified_kernel_configuration_required",
        )
        self.configuration = configuration.copy()
        self.target, self.schema = target, schema
        self.api = BpfAPI() if api is None else api
        self.close = os.close if close is None else close
        self.descriptors, self.hooks, self.maps = [], {}, {}
        self.stage = "map_create"
        self.last = None
        self.overflow = False
        self.sequence = 0

    def own(self, fd):
        self.descriptors.append(fd)
        return fd

    def create_map(self, map_type, key_size, maximum, value_size=8):
        fd, _ = self.api.call(
            0,
            [(0, "<I", map_type), (4, "<I", key_size), (8, "<I", value_size), (12, "<I", maximum)],
        )
        return self.own(fd)

    def attach(self):
        self.map_fd = self.create_map(2, 4, len(COUNTER_NAMES))
        self.maps["counters"] = self.api.validate_map(self.map_fd, 2, 4, len(COUNTER_NAMES))
        self.stage = "pending_map_create"
        self.pending_fd = self.create_map(1, 8, PENDING_MAX_ENTRIES, PENDING_VALUE_SIZE)
        self.maps["pending"] = self.api.validate_map(
            self.pending_fd, 1, 8, PENDING_MAX_ENTRIES, PENDING_VALUE_SIZE
        )
        require(self.api.pending_empty(self.pending_fd), "pending_not_empty_at_start")
        # Exit must exist before any entry can create pending state.
        self.hooks["exit"] = self.load_hook("exit", 25, exit_instructions)
        self.hooks["entry"] = self.load_hook("entry", 24, instructions)

    def load_hook(self, role, attach_kind, generator):
        bytecode = generator(
            self.map_fd,
            self.target,
            self.schema["regs8_offset"],
            pending_map_fd=self.pending_fd,
            anon_vma_name_disabled=True,
        )
        require(0 < len(bytecode) <= 65536 and len(bytecode) % 8 == 0, "invalid_bytecode")
        insns = ctypes.create_string_buffer(bytecode)
        license_value = ctypes.create_string_buffer(b"GPL\0")
        ctypes.memset(self.api.verifier_log, 0, len(self.api.verifier_log))
        self.api.log_required = 0
        self.stage = role + "_program_load"
        fd, _ = self.api.call(
            5,
            [
                (0, "<I", 26),
                (4, "<I", len(bytecode) // 8),
                (8, "<Q", ctypes.addressof(insns)),
                (16, "<Q", ctypes.addressof(license_value)),
                (24, "<I", 4),
                (28, "<I", len(self.api.verifier_log)),
                (32, "<Q", ctypes.addressof(self.api.verifier_log)),
                (68, "<I", attach_kind),
                (108, "<I", self.schema["function_btf_id"]),
            ],
        )
        self.own(fd)
        stats = self.api.diagnostics()
        require(
            0 < stats["required_bytes"] <= len(self.api.verifier_log), "incomplete_verifier_log"
        )
        self.stage = role + "_link_create"
        link_fd, _ = self.api.call(28, [(0, "<I", fd), (4, "<I", 0), (8, "<I", attach_kind)])
        self.own(link_fd)
        identity = self.api.program_info(fd)
        prog_id, misses, attach_object, attach_btf_id = identity
        require(
            prog_id > 0
            and misses == 0
            and attach_object > 0
            and attach_btf_id == self.schema["function_btf_id"],
            "wrong_program_identity",
        )
        link = self.api.info(link_fd, 28)
        require(
            struct.unpack_from("<I", link, 0)[0] == 2
            and struct.unpack_from("<I", link, 8)[0] == prog_id
            and struct.unpack_from("<III", link, 16) == (attach_kind, attach_object, attach_btf_id),
            "wrong_link_identity",
        )
        return {
            "fd": fd,
            "link": link_fd,
            "identity": identity,
            "evidence": {
                "hook": "f" + role + "/do_el0_svc",
                "bytecode_sha256": hashlib.sha256(bytecode).hexdigest(),
                "instruction_count": len(bytecode) // 8,
                "verifier_log": stats,
                "program_id": prog_id,
                "attach_btf_obj_id": attach_object,
                "attach_btf_id": attach_btf_id,
                "attach_type": attach_kind,
            },
        }

    def snapshot(self):
        """Only the host can establish quiescence; never hide unhealthy values."""
        values = {
            name: self.api.counter(self.map_fd, key) for key, name in enumerate(COUNTER_NAMES)
        }
        require(
            all(type(value) is int and 0 <= value < 2**64 for value in values.values()),
            "invalid_counter_value",
        )
        monotonic = self.last is None or all(
            values[name] >= self.last[name] for name in COUNTER_NAMES
        )
        self.overflow |= any(value >= COUNTER_LIMIT for value in values.values()) or not monotonic
        self.last = values.copy()
        misses = {}
        for role, hook in self.hooks.items():
            identity = self.api.program_info(hook["fd"])
            require(
                (identity[0], identity[2], identity[3])
                == (hook["identity"][0], hook["identity"][2], hook["identity"][3]),
                "program_identity_changed",
            )
            misses[role] = identity[1]
        self.sequence += 1
        return {
            "sequence": self.sequence,
            "counters": values,
            "pending_empty": self.api.pending_empty(self.pending_fd),
            "recursion_misses": misses,
            "overflow": self.overflow,
            "monotonic": monotonic,
            "conservation": values["messages"] == sum(values[name] for name in COUNTER_NAMES[5:12]),
        }

    def detach(self):
        # Target must already be quiescent. Never remove exit observation first.
        for role in ("entry", "exit"):
            fd = self.hooks[role]["link"]
            self.close(fd)
            self.descriptors.remove(fd)
        self.stage = "detached"

    def close_all(self):
        errors = 0
        # Best effort independent closure: one failure must not skip later FDs.
        for fd in reversed(self.descriptors):
            try:
                self.close(fd)
            except OSError:
                errors += 1
        self.descriptors.clear()
        return errors == 0


def own_exclusion(api):
    """Fixed calls from collector cgroup must never contribute target evidence."""
    addr = ctypes.create_string_buffer(
        struct.pack("<H", 2) + struct.pack("!H", 9) + bytes([192, 0, 2, 1]) + bytes(8)
    )
    for _ in range(64):
        ctypes.set_errno(0)
        result = api.syscall(
            ctypes.c_long(203), ctypes.c_long(-1), ctypes.byref(addr), ctypes.c_uint(16)
        )
        require(result == -1 and ctypes.get_errno() == 9, "own_connect_control_failed")
    sizes = ctypes.create_string_buffer(6)
    for _ in range(16):
        require(
            api.syscall(
                ctypes.c_long(167),
                ctypes.c_ulonglong(39),
                *[ctypes.c_ulonglong(0) for _ in range(4)],
            )
            == 1,
            "own_prctl_control_failed",
        )
        require(
            api.syscall(ctypes.c_long(277), ctypes.c_uint(3), ctypes.c_uint(0), ctypes.byref(sizes))
            == 0,
            "own_seccomp_control_failed",
        )
    return {"connect": 64, "prctl": 16, "seccomp": 16}


def emit(stream, event, **values):
    encoded = json.dumps(
        {"schema_version": SCHEMA_VERSION, "event": event, **values},
        sort_keys=True,
        separators=(",", ":"),
    )
    require(len(encoded.encode()) + 1 <= MAX_LINE, "protocol_output_too_large")
    stream.write(encoded + "\n")
    stream.flush()


def serve(
    collector, input_stream, output_stream, source_identity, source_check=verify_bound_source
):
    """The host sends exact commands only after quiescing all target descendants."""
    for _ in range(MAX_COMMANDS):
        command = input_stream.readline(32)
        if command == "SNAPSHOT\n":
            collector.stage = "snapshot"
            emit(output_stream, "SNAPSHOT", **collector.snapshot())
        elif command == "STOP\n":
            collector.stage = "source_recheck"
            require(source_check() == source_identity, "source_identity_changed")
            collector.detach()
            final = collector.snapshot()
            require(collector.close_all(), "descriptor_cleanup_failed")
            emit(output_stream, "DONE", **final, detached=True, all_fds_closed=True)
            return
        else:
            raise ValueError("invalid_collector_command")
    raise ValueError("collector_command_limit")


def _interrupted(signum, _frame):
    raise TimeoutError(
        "collector_deadline" if signum == signal.SIGALRM else "collector_interrupted"
    )


def main(argv=None):
    collector = None
    stage = "startup"
    result = 2
    previous = {}
    try:
        require(__debug__ and sys.flags.optimize == 0, "nonoptimized_python_required")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
            previous[sig] = signal.signal(sig, _interrupted)
        signal.alarm(MAX_LIFETIME)
        arguments = sys.argv[1:] if argv is None else argv
        require(
            len(arguments) == 1 and re.fullmatch(r"[0-9]{1,20}", arguments[0]),
            "invalid_collector_arguments",
        )
        target = int(arguments[0])
        source = verify_bound_source()
        own = collector_boundary(target)
        stage = "kernel_configuration"
        configuration = kernel_configuration()
        stage = "kernel_btf"
        schema = btf_schema(bounded_read("/sys/kernel/btf/vmlinux", MAX_BTF_BYTES))
        collector = Collector(target, schema, configuration=configuration)
        collector.attach()
        collector.stage = "exclude_other_cgroup"
        self_controls = own_exclusion(collector.api)
        baseline = collector.snapshot()
        require(
            not any(baseline["counters"].values())
            and baseline["pending_empty"]
            and not any(baseline["recursion_misses"].values())
            and baseline["conservation"]
            and not baseline["overflow"],
            "collector_scope_control_failed",
        )
        emit(
            sys.stdout,
            "READY",
            attached=True,
            own_cgroup=own,
            target_cgroup=target,
            schema=schema,
            kernel_configuration=collector.configuration,
            programs={role: hook["evidence"] for role, hook in collector.hooks.items()},
            maps=collector.maps,
            counter_names=COUNTER_NAMES,
            source=source,
            self_controls=self_controls,
            baseline=baseline,
        )
        serve(collector, sys.stdin, sys.stdout, source, verify_bound_source)
        result = 0
    except (Exception, KeyboardInterrupt) as error:
        stage = collector.stage if collector is not None else stage
        # Only fixed categories, saved errno, and bounded hash/statistics; never
        # include exceptions, raw verifier logs, target memory or arbitrary paths.
        category = type(error).__name__
        if category not in {
            "ValueError",
            "AssertionError",
            "OSError",
            "TimeoutError",
            "KeyboardInterrupt",
            "KeyError",
            "UnicodeDecodeError",
        }:
            category = "collector_error"
        emit(
            sys.stdout,
            "FAILED",
            failure_stage=stage,
            category=category,
            errno=error.errno if isinstance(error, OSError) else None,
            verifier_log=collector.api.diagnostics() if collector is not None else None,
        )
    finally:
        # A second signal cannot skip independent FD closure. The host still owns
        # its deadline and exact-container forced cleanup if this process stalls.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        if collector is not None and not collector.close_all():
            result = 2
        signal.alarm(0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
