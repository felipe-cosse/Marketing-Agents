"""Pure collector ABI/protocol tests: never open a BPF object or use Docker."""

import ctypes
import hashlib
import io
import json
import struct

import pytest

from scripts import ac_15_kernel_collector as collector
from scripts.ac_15_kernel_bytecode import COUNTER_NAMES


def native_btf(
    *, return_type=0, regs_count=31, regs_offset=0, regs_size=272, duplicate=False, compat=False
):
    strings = bytearray(b"\0")

    def name(value):
        offset = len(strings)
        strings.extend(value.encode() + b"\0")
        return offset

    def record(label, kind, value, data=b"", count=0):
        return struct.pack("<III", name(label), kind << 24 | count, value) + data

    records = [
        record("unsigned long", 1, 8, struct.pack("<I", 64)),
        record("", 3, 0, struct.pack("<III", 1, 1, regs_count)),
        record("pt_regs", 4, regs_size, struct.pack("<III", name("regs"), 2, regs_offset), 1),
        record("", 2, 3),
        record("", 13, return_type, struct.pack("<II", name("regs"), 4), 1),
        record("do_el0_svc", 12, 5),
    ]
    if duplicate:
        records.append(record("do_el0_svc", 12, 5))
    if compat:
        records.append(record("do_el0_svc_compat", 12, 5))
    types = b"".join(records)
    return (
        struct.pack("<HBBIIIII", 0xEB9F, 1, 0, 24, 0, len(types), len(types), len(strings))
        + types
        + strings
    )


def test_btf_qualifies_native_void_pointer_register_layout_only():
    raw = native_btf(compat=True)
    assert collector.btf_schema(raw) == {
        "function_btf_id": 6,
        "regs8_offset": 64,
        "pt_regs_size": 272,
        "btf_sha256": hashlib.sha256(raw).hexdigest(),
        "compat_symbol_present": True,
    }


@pytest.mark.parametrize(
    "options",
    [
        {"return_type": 1},
        {"regs_count": 30},
        {"regs_offset": 8},
        {"regs_size": 247},
        {"duplicate": True},
    ],
)
def test_btf_rejects_unsupported_or_ambiguous_hook_abi(options):
    with pytest.raises((AssertionError, ValueError)):
        collector.btf_schema(native_btf(**options))


@pytest.mark.parametrize("raw", [b"", bytes(24), native_btf()[:-1], b"\0\0" + native_btf()[2:]])
def test_btf_truncated_or_invalid_input_fails_closed(raw):
    with pytest.raises((AssertionError, ValueError)):
        collector.btf_schema(raw)


def test_bpf_load_attr_is_native_and_saves_true_log_size_and_errno():
    observed = []

    def syscall(number, command, attribute, size):
        observed.append((number.value, command.value, size.value, bytes(attribute._obj)))
        struct.pack_into("<I", attribute._obj, 140, 99999)
        ctypes.set_errno(28)
        return -1

    api = collector.BpfAPI(syscall)
    with pytest.raises(OSError) as error:
        api.call(5, [(0, "<I", 26), (24, "<I", 4), (68, "<I", 24), (108, "<I", 6)])
    assert error.value.errno == 28
    assert api.log_required == 99999
    assert observed[0][:3] == (280, 5, 144)
    assert struct.unpack_from("<I", observed[0][3], 24)[0] == 4
    assert struct.unpack_from("<I", observed[0][3], 68)[0] == 24
    assert struct.unpack_from("<I", observed[0][3], 108)[0] == 6


def test_verifier_diagnostics_are_bounded_projection_not_raw_content():
    api = collector.BpfAPI(lambda *_: 0)
    raw = b"private verifier material\nprocessed 288197 insns (limit 1000000)\n"
    api.verifier_log.value = raw
    api.log_required = len(raw) + 1
    diagnostics = api.diagnostics()
    assert diagnostics == {
        "level": 4,
        "required_bytes": len(raw) + 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "processed_instructions": 288197,
        "instruction_limit": 1000000,
    }
    assert "private" not in json.dumps(diagnostics)


def test_program_info_uses_public_recursion_and_btf_offsets():
    api = collector.BpfAPI(lambda *_: 0)
    data = ctypes.create_string_buffer(232)
    for offset, fmt, value in (
        (0, "<I", 26),
        (4, "<I", 91),
        (208, "<Q", 7),
        (220, "<I", 13),
        (224, "<I", 99),
    ):
        struct.pack_into(fmt, data, offset, value)
    api.info = lambda fd, size: data
    assert api.program_info(7) == (91, 7, 13, 99)
    struct.pack_into("<I", data, 0, 0)
    with pytest.raises(ValueError, match="wrong_program_type"):
        api.program_info(7)


@pytest.mark.parametrize("error,empty", [(None, False), (2, True), (1, None), (5, None)])
def test_pending_empty_accepts_only_enoent(error, empty):
    api = collector.BpfAPI(lambda *_: 0)

    def call(command, fields):
        assert command == 4
        assert dict((offset, value) for offset, _, value in fields)[8] == 0
        if error:
            raise OSError(error, "synthetic")
        return 0, None

    api.call = call
    if empty is None:
        with pytest.raises(OSError):
            api.pending_empty(7)
    else:
        assert api.pending_empty(7) is empty


class FakeAPI:
    """UAPI-shaped fake; numeric descriptors have no operating-system meaning."""

    def __init__(self):
        self.calls, self.objects = [], {}
        self.verifier_log = ctypes.create_string_buffer(65536)
        self.log_required = 1
        self.counts = [0] * len(COUNTER_NAMES)
        self.empty = True
        self.misses = 0
        self.identity_change = False
        self.bad_link = False

    def call(self, command, fields):
        values = {offset: value for offset, _, value in fields}
        self.calls.append((command, values))
        fd = 10 + len(self.objects)
        self.objects[fd] = command, values
        if command == 5:
            self.log_required = 1
        return fd, None

    def validate_map(self, fd, map_type, key_size, maximum):
        command, values = self.objects[fd]
        assert command == 0
        assert values == {0: map_type, 4: key_size, 8: 8, 12: maximum}
        return fd + 100

    def pending_empty(self, fd):
        assert self.objects[fd][0] == 0
        return self.empty

    def diagnostics(self):
        return {"level": 4, "required_bytes": self.log_required}

    def program_info(self, fd):
        assert self.objects[fd][0] == 5
        return fd + 100 + int(self.identity_change), self.misses, 7, 6

    def info(self, fd, size):
        command, values = self.objects[fd]
        assert command == 28 and size == 28
        result = ctypes.create_string_buffer(size)
        struct.pack_into("<I", result, 0, 2)
        struct.pack_into("<I", result, 8, values[0] + 100)
        struct.pack_into("<III", result, 16, values[8], 7 + int(self.bad_link), 6)
        return result

    def counter(self, fd, key):
        return self.counts[key]


def attached():
    api = FakeAPI()
    closed = []
    instance = collector.Collector(123, collector.btf_schema(native_btf()), api, closed.append)
    instance.attach()
    return instance, api, closed


def test_attach_loads_exit_first_with_fixed_maps_full_verifier_and_link_binding():
    instance, api, _ = attached()
    assert [command for command, _ in api.calls] == [0, 0, 5, 28, 5, 28]
    loads = [fields for command, fields in api.calls if command == 5]
    assert [fields[68] for fields in loads] == [25, 24]
    assert all(fields[24] == 4 and fields[28] == 65536 and fields[108] == 6 for fields in loads)
    assert all(fields[0] == 26 and 0 < fields[4] < 1024 for fields in loads)
    assert len(instance.descriptors) == 6


def test_link_failure_still_leaves_every_created_fd_owned_for_cleanup():
    api = FakeAPI()
    api.bad_link = True
    closed = []
    instance = collector.Collector(123, collector.btf_schema(native_btf()), api, closed.append)
    with pytest.raises(ValueError, match="wrong_link_identity"):
        instance.attach()
    allocated = instance.descriptors.copy()
    assert len(allocated) == 4
    assert instance.close_all()
    assert closed == allocated[::-1]


def test_snapshots_are_monotonic_no_reset_and_report_conservation_and_pending():
    instance, api, _ = attached()
    first = instance.snapshot()
    assert first["sequence"] == 1 and first["pending_empty"]
    api.counts[0] = api.counts[4] = api.counts[6] = 3
    second = instance.snapshot()
    assert second["counters"]["external"] == 3
    assert second["conservation"] and second["monotonic"] and not second["overflow"]
    assert first["counters"]["external"] == 0
    api.counts[4] = 4
    api.empty = False
    third = instance.snapshot()
    assert not third["conservation"] and not third["pending_empty"]
    assert api.counts[6] == 3


def test_counter_high_half_or_decrease_marks_overflow_sticky():
    instance, api, _ = attached()
    api.counts[0] = collector.COUNTER_LIMIT
    assert instance.snapshot()["overflow"]
    api.counts[0] = 0
    second = instance.snapshot()
    assert second["overflow"] and not second["monotonic"]
    assert instance.snapshot()["overflow"]


def test_snapshot_reports_each_recursion_miss_but_rejects_identity_change():
    instance, api, _ = attached()
    api.misses = 2
    assert instance.snapshot()["recursion_misses"] == {"entry": 2, "exit": 2}
    api.identity_change = True
    with pytest.raises(ValueError, match="program_identity_changed"):
        instance.snapshot()


def test_stop_detaches_entry_before_exit_then_final_snapshot_and_closes_all():
    instance, _, closed = attached()
    links = [instance.hooks[role]["link"] for role in ("entry", "exit")]
    source = {"source_sha256": "a" * 64}
    output = io.StringIO()
    checks = []

    def source_check():
        checks.append(not closed)
        return source

    collector.serve(instance, io.StringIO("SNAPSHOT\nSTOP\n"), output, source, source_check)
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [value["event"] for value in events] == ["SNAPSHOT", "DONE"]
    assert all(value["schema_version"] == 1 for value in events)
    assert events[0]["counters"] == events[1]["counters"]
    assert events[1]["detached"] and events[1]["all_fds_closed"]
    assert closed[:2] == links and len(closed) == 6 and not instance.descriptors
    assert checks == [True]


@pytest.mark.parametrize("command", ["", "STOP", "REPORT\n", "SNAPSHOT extra\n", "x" * 32])
def test_invalid_or_truncated_protocol_does_not_detach_or_claim_success(command):
    instance, _, closed = attached()
    output = io.StringIO()
    with pytest.raises(ValueError, match="invalid_collector_command"):
        collector.serve(instance, io.StringIO(command), output, {})
    assert not closed and not output.getvalue()


def test_protocol_is_bounded_and_source_mismatch_prevents_done(monkeypatch):
    instance, _, closed = attached()
    monkeypatch.setattr(collector, "MAX_COMMANDS", 2)
    with pytest.raises(ValueError, match="collector_command_limit"):
        collector.serve(instance, io.StringIO("SNAPSHOT\n" * 3), io.StringIO(), {})
    with pytest.raises(ValueError, match="source_identity_changed"):
        collector.serve(instance, io.StringIO("STOP\n"), io.StringIO(), {}, lambda: {"changed": 1})
    assert not closed


def test_close_all_continues_after_one_failure_and_done_cannot_be_emitted():
    instance, _, _ = attached()
    attempted = []

    def close(fd):
        attempted.append(fd)
        if len(attempted) == 3:
            raise OSError(5, "synthetic")

    instance.close = close
    output = io.StringIO()
    with pytest.raises(ValueError, match="descriptor_cleanup_failed"):
        collector.serve(instance, io.StringIO("STOP\n"), output, {}, lambda: {})
    assert len(attempted) == 6 and not output.getvalue()
    assert not instance.descriptors


def test_emit_never_writes_oversized_message():
    output = io.StringIO()
    with pytest.raises(ValueError, match="protocol_output_too_large"):
        collector.emit(output, "SNAPSHOT", too_big="a" * collector.MAX_LINE)
    assert not output.getvalue()


def main_harness(monkeypatch, commands="SNAPSHOT\nSTOP\n", bad_link=False):
    api, closed, alarms, checked = FakeAPI(), [], [], []
    api.bad_link = bad_link
    source = {"source_commit": "1" * 40, "source_tree": "2" * 40, "source_sha256": "3" * 64}
    instance = collector.Collector(123, collector.btf_schema(native_btf()), api, closed.append)
    monkeypatch.setattr(collector, "Collector", lambda *_: instance)
    monkeypatch.setattr(collector, "collector_boundary", lambda _: 456)
    monkeypatch.setattr(collector, "bounded_read", lambda *_: native_btf())

    def source_check():
        checked.append(True)
        return source

    monkeypatch.setattr(collector, "verify_bound_source", source_check)
    monkeypatch.setattr(
        collector, "own_exclusion", lambda _: {"connect": 64, "prctl": 16, "seccomp": 16}
    )
    monkeypatch.setattr(collector.signal, "signal", lambda *_: None)
    monkeypatch.setattr(collector.signal, "alarm", alarms.append)
    output = io.StringIO()
    monkeypatch.setattr(collector.sys, "stdin", io.StringIO(commands))
    monkeypatch.setattr(collector.sys, "stdout", output)
    return instance, api, closed, alarms, checked, output


def test_main_source_bound_startup_ready_protocol_and_bounded_lifetime(monkeypatch):
    instance, _, closed, alarms, checked, output = main_harness(monkeypatch)
    assert collector.main(["123"]) == 0
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [value["event"] for value in events] == ["READY", "SNAPSHOT", "DONE"]
    ready = events[0]
    assert ready["own_cgroup"] == 456 and ready["target_cgroup"] == 123
    assert ready["schema_version"] == 1 and ready["attached"]
    assert ready["counter_names"] == list(COUNTER_NAMES)
    assert ready["self_controls"] == {"connect": 64, "prctl": 16, "seccomp": 16}
    assert ready["baseline"]["sequence"] == 1
    assert not any(ready["baseline"]["counters"].values())
    assert set(ready["programs"]) == {"entry", "exit"}
    assert set(ready["maps"]) == {"counters", "pending"}
    assert len(closed) == 6 and not instance.descriptors
    assert alarms == [1720, 0] and checked == [True, True]


def test_main_attach_failure_is_sanitized_and_closes_partial_allocation(monkeypatch):
    instance, _, closed, alarms, checked, output = main_harness(monkeypatch, bad_link=True)
    assert collector.main(["123"]) == 2
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(events) == 1 and events[0]["event"] == "FAILED"
    assert events[0]["failure_stage"] == "exit_link_create"
    assert events[0]["category"] == "ValueError"
    assert "wrong_link_identity" not in output.getvalue()
    assert len(closed) == 4 and not instance.descriptors
    assert alarms == [1720, 0] and checked == [True]


def test_main_eof_never_claims_done_and_cleans_all(monkeypatch):
    instance, _, closed, _, _, output = main_harness(monkeypatch, commands="")
    assert collector.main(["123"]) == 2
    assert [json.loads(line)["event"] for line in output.getvalue().splitlines()] == [
        "READY",
        "FAILED",
    ]
    assert len(closed) == 6 and not instance.descriptors


def test_unquiesced_startup_baseline_fails_before_ready(monkeypatch):
    instance, api, closed, _, _, output = main_harness(monkeypatch)
    api.counts[0] = api.counts[4] = api.counts[5] = 1
    assert collector.main(["123"]) == 2
    assert [json.loads(line)["event"] for line in output.getvalue().splitlines()] == ["FAILED"]
    assert len(closed) == 6 and not instance.descriptors
