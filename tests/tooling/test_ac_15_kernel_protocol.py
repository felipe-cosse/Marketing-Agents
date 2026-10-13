"""Fake-only tests: protocol evidence can never start Docker or native probes."""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import subprocess
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import ac_15_kernel_boundary as boundary
from scripts import ac_15_kernel_client as client
from scripts import ac_15_kernel_host as host
from scripts import ac_15_offline as offline
from scripts.del_05_clean_state import VerificationFailure

IMAGE = "sha256:" + "a" * 64
SOURCE = {"source_commit": "b" * 40, "source_tree": "c" * 40, "source_sha256": "d" * 64}
MANIFEST = {**SOURCE, "files": {"scripts/ac_15_kernel_boundary.py": {"sha256": "e" * 64}}}


@pytest.fixture(autouse=True)
def forbid_native_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected native execution")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(boundary, "qualify_target", forbidden)
    monkeypatch.setattr(boundary, "quiescent_target", forbidden)
    monkeypatch.setattr(offline, "ACTIVE_OBSERVER", None)


def counters(**changes):
    value = dict.fromkeys(client.counter_names(), 0)
    value.update(changes)
    value["messages"] = sum(
        value[key]
        for key in (
            "loopback",
            "external",
            "unix",
            "kernel_ipc",
            "connected_send",
            "unclassified",
            "read_error",
        )
    )
    return value


def snapshot(sequence=1, event="SNAPSHOT", **changes):
    value = {
        "schema_version": 1,
        "event": event,
        "counters": counters(),
        "pending_empty": True,
        "recursion_misses": {"entry": 0, "exit": 0},
        "overflow": False,
        "conservation": True,
        "monotonic": True,
        "sequence": sequence,
    }
    if event == "DONE":
        value.update(detached=True, all_fds_closed=True)
    value.update(changes)
    return value


def kernel_configuration():
    return {
        "source": "/proc/config.gz",
        "release": "7.0.9-linuxkit",
        "config_sha256": "3" * 64,
        "anon_vma_name": "disabled",
        "policy": "disabled_requires_einval",
    }


def ready():
    value = {
        "event": "READY",
        "schema_version": 1,
        "attached": True,
        "own_cgroup": 456,
        "target_cgroup": 123,
        "counter_names": list(client.counter_names()),
        "kernel_configuration": kernel_configuration(),
        "schema": {
            "function_btf_id": 88,
            "regs8_offset": 64,
            "pt_regs_size": 336,
            "btf_sha256": "f" * 64,
            "compat_symbol_present": True,
        },
        "maps": {"counters": 11, "pending": 12},
        "source": SOURCE,
        "self_controls": {"connect": 64, "prctl": 16, "seccomp": 16},
        "baseline": {
            key: value
            for key, value in snapshot().items()
            if key not in {"event", "schema_version"}
        },
        "programs": {},
    }
    for role, attach, identity in (("entry", 24, 21), ("exit", 25, 22)):
        value["programs"][role] = {
            "hook": f"f{role}/do_el0_svc",
            "attach_type": attach,
            "attach_btf_id": 88,
            "attach_btf_obj_id": 9,
            "program_id": identity,
            "instruction_count": 500,
            "bytecode_sha256": "1" * 64,
            "verifier_log": {
                "level": 4,
                "required_bytes": 200,
                "sha256": "2" * 64,
                "processed_instructions": 1000,
                "instruction_limit": 1000000,
            },
        }
    return value


def emulation_proof():
    return {
        "guest_elf_class": 1,
        "guest_elf_machine": 40,
        "host_elf_class": 2,
        "host_elf_machine": 183,
        "host_elf_little_endian": True,
        "host_elf_header_sha256": "7" * 64,
        "stable_process_identity": True,
        "same_cgroup": True,
        "same_pid_namespace": True,
        "no_new_privileges": True,
        "seccomp_filter": True,
        "capabilities_zero": True,
        "handshake_exit": 42,
    }


def qualification(*, emulation=False):
    return {
        "schema_version": 2,
        "native_abi": "linux_aarch64_little_64",
        "native_guard_installed": True,
        "native_elf_exit": 42,
        "core_file_limit_zero": True,
        "inherited_sockets": 0,
        "inherited_io_uring": 0,
        "cgroup_inode": 123,
        "cgroup_read_only": True,
        "cgroup_subgroups": 0,
        "trusted_process_count": 2,
        "trusted_thread_count": 2,
        "source_sha256": "e" * 64,
        "foreign_elf_mode": "native_emulation" if emulation else "rejected",
        "foreign_elf_rejection": None if emulation else "SIGSYS",
        "native_emulation": emulation_proof() if emulation else None,
        "io_uring_setup_rejection": "EPERM",
    }


def quiescence():
    return {
        "schema_version": 1,
        "cgroup_inode": 123,
        "cgroup_unchanged": True,
        "remaining_tasks": 0,
        "trusted_process_count": 2,
        "trusted_thread_count": 2,
        "baseline_unchanged": True,
    }


@pytest.mark.parametrize(
    "raw,code",
    [
        ('{"x":1,"x":2}', "duplicate_key"),
        ('{"x":{"a":1,"a":1}}', "duplicate_key"),
        ('{"x":NaN}', "nonfinite_number"),
        ('{"x":Infinity}', "nonfinite_number"),
        ('{"x":-Infinity}', "nonfinite_number"),
        ("[]", "object_required"),
        ("null", "object_required"),
        ("1", "object_required"),
        (b" " * (client.LINE_LIMIT + 1), "line_bound"),
    ],
)
def test_json_rejects_ambiguous_nonfinite_and_unbounded_input(raw, code):
    with pytest.raises(VerificationFailure, match=code):
        client.parse_json(raw)


def test_canonical_evidence_and_python_digest():
    assert client.parse_json(b'{"b":2,"a":1}') == {"a": 1, "b": 2}
    assert client.canonical({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    with pytest.raises(ValueError):
        client.canonical({"x": float("nan")})
    command = (client.sys.executable, "-I", "-c", "pass")
    expected = hashlib.sha256(client.canonical(["{qualified-python}", *command[1:]])).hexdigest()
    assert client.command_digest(command) == expected
    assert client.command_digest(("python-other", *command[1:])) != expected


@pytest.mark.parametrize("value", [-1, True, False, 1.0, "1", None, 2**63, float("inf")])
def test_counter_types_and_unsigned_finite_bound(value):
    invalid = counters()
    invalid["entry_connect"] = value
    with pytest.raises(VerificationFailure, match="overflow_or_type"):
        client.validate_counters(invalid)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 0},
        {"messages": 1},
        {"output_queries_started": 1},
        {"vma_unsupported_started": 1},
        {"vma_unsupported_completed": 1},
    ],
)
def test_counter_schema_conservation_and_output_pairing(change):
    value = counters()
    value.update(change)
    with pytest.raises(VerificationFailure):
        client.validate_counters(value)


def test_counter_missing_and_regression_fail_closed():
    value = counters()
    del value["entry_connect"]
    with pytest.raises(VerificationFailure, match="counter_schema"):
        client.validate_counters(value)
    with pytest.raises(VerificationFailure, match="counter_regression"):
        client.phase_summary(counters(loopback=1), counters())


@pytest.mark.parametrize("field", ["vma_unsupported_started", "vma_unsupported_completed"])
@pytest.mark.parametrize("value", [-1, True, 1.0, "1", None, 2**63])
def test_vma_unsupported_counter_requires_exact_bounded_unsigned_value(field, value):
    invalid = counters(vma_unsupported_started=1, vma_unsupported_completed=1)
    invalid[field] = value
    with pytest.raises(VerificationFailure, match="overflow_or_type"):
        client.validate_counters(invalid)


@pytest.mark.parametrize("field", ["vma_unsupported_started", "vma_unsupported_completed"])
def test_vma_unsupported_counter_is_required(field):
    invalid = counters()
    del invalid[field]
    with pytest.raises(VerificationFailure, match="counter_schema"):
        client.validate_counters(invalid)


def test_vma_unsupported_counter_balance_regression_and_hash_binding():
    before = counters(vma_unsupported_started=3, vma_unsupported_completed=3)
    after = counters(vma_unsupported_started=7, vma_unsupported_completed=7)
    summary = client.phase_summary(before, after)
    assert summary["counters"]["vma_unsupported_started"] == 4
    assert summary["counters"]["vma_unsupported_completed"] == 4
    assert summary["rejected"] == 0
    evidence = {key: summary[key] for key in ("schema_version", "observer", "counters")}
    assert summary["sha256"] == hashlib.sha256(client.canonical(evidence)).hexdigest()
    with pytest.raises(VerificationFailure, match="counter_regression"):
        client.phase_summary(after, before)
    for field in ("vma_unsupported_started", "vma_unsupported_completed"):
        invalid = dict(after)
        invalid[field] += 1
        with pytest.raises(VerificationFailure, match="vma_unsupported_incomplete"):
            client.phase_summary(before, invalid)


@pytest.mark.parametrize("event", ["SNAPSHOT", "DONE"])
@pytest.mark.parametrize("pending_empty", [True, False])
def test_vma_balance_never_overrides_snapshot_pending_requirement(event, pending_empty):
    value = snapshot(
        event=event,
        counters=counters(vma_unsupported_started=2, vma_unsupported_completed=2),
        pending_empty=pending_empty,
    )
    if not pending_empty:
        with pytest.raises(VerificationFailure, match="snapshot_incomplete"):
            host.validate_snapshot(value, event)
    else:
        assert host.validate_snapshot(value, event) == value["counters"]
    value["pending_empty"] = True
    value["counters"]["vma_unsupported_completed"] -= 1
    with pytest.raises(VerificationFailure, match="vma_unsupported_incomplete"):
        host.validate_snapshot(value, event)


def test_phase_delta_all_rejections_and_hash_bind_exact_counters():
    before = counters(entry_connect=10, unix=3)
    after = dict(before)
    rejecting = (
        "external",
        "unclassified",
        "read_error",
        "vector_error",
        "regs_read_error",
        "filter_changes",
        "control_error",
        "control_read_error",
        "control_state_error",
    )
    for key in rejecting:
        after[key] += 1
    after["messages"] += 3
    summary = client.phase_summary(before, after)
    assert summary["rejected"] == 9 and summary["unix"] == 0
    evidence = {key: summary[key] for key in ("schema_version", "observer", "counters")}
    assert summary["sha256"] == hashlib.sha256(client.canonical(evidence)).hexdigest()
    assert all(summary["counters"][key] == 1 for key in rejecting)
    client.validate_counters(counters(entry_connect=2**63 - 1))


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 0},
        {"event": "READY"},
        {"schema_version": True},
        {"sequence": True},
        {"sequence": 0},
        {"sequence": 257},
        {"pending_empty": 1},
        {"pending_empty": False},
        {"overflow": True},
        {"overflow": 0},
        {"conservation": False},
        {"monotonic": False},
        {"recursion_misses": {"entry": 1, "exit": 0}},
        {"recursion_misses": {"entry": False, "exit": 0}},
        {"recursion_misses": {"entry": 0}},
    ],
)
def test_snapshot_requires_complete_exact_evidence(change):
    with pytest.raises(VerificationFailure):
        host.validate_snapshot(snapshot(**change))


@pytest.mark.parametrize("field", ["detached", "all_fds_closed"])
@pytest.mark.parametrize("value", [False, 1, None])
def test_done_never_accepts_incomplete_cleanup(field, value):
    with pytest.raises(VerificationFailure, match="detach_incomplete"):
        host.validate_snapshot(snapshot(event="DONE", **{field: value}), "DONE")


def test_valid_snapshot_ready_and_done():
    assert host.validate_snapshot(snapshot()) == counters()
    assert host.validate_snapshot(snapshot(256, "DONE"), "DONE") == counters()
    host.validate_ready(ready(), 123)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": False},
        {"source": "/boot/config"},
        {"source": None},
        {"release": ""},
        {"release": "x" * 129},
        {"release": "7.0\n"},
        {"release": "../kernel"},
        {"release": 7},
        {"config_sha256": ""},
        {"config_sha256": "g" * 64},
        {"config_sha256": "A" * 64},
        {"config_sha256": "1" * 63},
        {"config_sha256": "1" * 65},
        {"config_sha256": None},
        {"anon_vma_name": "enabled"},
        {"anon_vma_name": "unknown"},
        {"anon_vma_name": "m"},
        {"anon_vma_name": False},
        {"anon_vma_name": None},
        {"policy": "allow_prctl"},
        {"policy": True},
    ],
)
def test_collector_configuration_has_no_ambiguous_or_unqualified_fallback(change):
    proof = {**kernel_configuration(), **change}
    with pytest.raises(VerificationFailure, match="configuration_unqualified"):
        client.validate_kernel_configuration(proof)
    record = ready()
    record["kernel_configuration"] = proof
    with pytest.raises(VerificationFailure, match="configuration_unqualified"):
        host.validate_ready(record, 123)


@pytest.mark.parametrize("field", tuple(kernel_configuration()))
def test_collector_configuration_requires_every_bound_field(field):
    proof = kernel_configuration()
    del proof[field]
    with pytest.raises(VerificationFailure, match="configuration_unqualified"):
        client.validate_kernel_configuration(proof)


@pytest.mark.parametrize("value", [None, True, [], "disabled", {}])
def test_collector_configuration_requires_exact_object(value):
    with pytest.raises(VerificationFailure, match="configuration_unqualified"):
        client.validate_kernel_configuration(value)


def test_collector_ready_cannot_omit_configuration():
    record = ready()
    del record["kernel_configuration"]
    with pytest.raises(VerificationFailure, match="collector_ready_invalid"):
        host.validate_ready(record, 123)


@pytest.mark.parametrize(
    "path,value",
    [
        (("extra",), 0),
        (("schema_version",), True),
        (("attached",), 1),
        (("own_cgroup",), 123),
        (("own_cgroup",), 2**64),
        (("target_cgroup",), 124),
        (("maps", "pending"), 11),
        (("maps", "counters"), True),
        (("schema", "regs8_offset"), 72),
        (("schema", "pt_regs_size"), 247),
        (("schema", "compat_symbol_present"), 1),
        (("schema", "btf_sha256"), "bad"),
        (("schema", "btf_sha256"), 123),
        (("programs", "entry", "extra"), 1),
        (("programs", "entry", "verifier_log", "extra"), 1),
        (("baseline", "event"), "SNAPSHOT"),
        (("baseline", "schema_version"), 1),
        (("programs", "entry", "hook"), "fentry/wrong"),
        (("programs", "exit", "program_id"), 21),
        (("programs", "exit", "attach_btf_obj_id"), 10),
        (("programs", "entry", "instruction_count"), 8193),
        (("programs", "entry", "attach_type"), 25),
        (("programs", "entry", "verifier_log", "required_bytes"), 65537),
        (("programs", "entry", "verifier_log", "processed_instructions"), 1000001),
        (("self_controls", "connect"), 63),
        (("baseline", "sequence"), 2),
    ],
)
def test_ready_rejects_broken_identity_or_qualification(path, value):
    record = ready()
    at = record
    for key in path[:-1]:
        at = at[key]
    at[path[-1]] = value
    with pytest.raises(VerificationFailure):
        host.validate_ready(record, 123)


@pytest.mark.parametrize(
    "suite,offset", [("backend", 0), ("frontend", 0), ("native", 0), ("browser", 3)]
)
def test_expected_fault_canaries_never_leak_to_regular_phases(suite, offset):
    samples = [
        counters(external=1),
        counters(unix=1),
        counters(external=1),
        counters(external=1, filter_changes=1),
    ]
    for index, value in enumerate(samples):
        summary = client.phase_summary(counters(), value)
        host.validate_phase(summary, index + offset, suite)
        if summary["rejected"]:
            with pytest.raises(VerificationFailure, match="rejection_mismatch"):
                host.validate_phase(summary, 4 + offset, suite)
    host.validate_phase(client.phase_summary(counters(), counters()), 4 + offset, suite)


@pytest.mark.parametrize(
    "field",
    [
        "unclassified",
        "read_error",
        "vector_error",
        "regs_read_error",
        "control_error",
        "control_read_error",
        "control_state_error",
    ],
)
def test_incomplete_classifier_evidence_is_never_canary_allowance(field):
    summary = client.phase_summary(counters(), counters(external=1, **{field: 1}))
    with pytest.raises(VerificationFailure, match="incomplete_phase"):
        host.validate_phase(summary, 0, "backend")


def test_vma_phase_requires_collector_disabled_proof_and_balanced_counters():
    summary = client.phase_summary(
        counters(), counters(vma_unsupported_started=3, vma_unsupported_completed=3)
    )
    with pytest.raises(VerificationFailure, match="vma_configuration_missing"):
        host.validate_phase(summary, 4, "backend")
    host.validate_phase(summary, 4, "backend", kernel_configuration=kernel_configuration())
    for state in ("enabled", "unknown", None, False):
        proof = {**kernel_configuration(), "anon_vma_name": state}
        with pytest.raises(VerificationFailure, match="configuration_unqualified"):
            host.validate_phase(summary, 4, "backend", kernel_configuration=proof)
    summary["counters"]["vma_unsupported_completed"] -= 1
    with pytest.raises(VerificationFailure, match="vma_unsupported_incomplete"):
        host.validate_phase(summary, 4, "backend", kernel_configuration=kernel_configuration())


@pytest.mark.parametrize(
    "field",
    [
        "external",
        "unclassified",
        "read_error",
        "vector_error",
        "regs_read_error",
        "filter_changes",
        "control_error",
        "control_read_error",
        "control_state_error",
    ],
)
def test_balanced_disabled_vma_never_excuses_original_rejections(field):
    summary = client.phase_summary(
        counters(),
        counters(vma_unsupported_started=3, vma_unsupported_completed=3, **{field: 1}),
    )
    assert summary["rejected"] == 1
    with pytest.raises(VerificationFailure):
        host.validate_phase(summary, 4, "backend", kernel_configuration=kernel_configuration())


def test_browser_health_requires_each_transport():
    for missing in ("loopback", "unix", "kernel_ipc"):
        values = {"loopback": 1, "unix": 1, "kernel_ipc": 1}
        values[missing] = 0
        with pytest.raises(VerificationFailure, match="transport_health_missing"):
            host.validate_phase(client.phase_summary(counters(), counters(**values)), 2, "browser")
    host.validate_phase(
        client.phase_summary(counters(), counters(loopback=1, unix=1, kernel_ipc=1)), 2, "browser"
    )


def make_host(tmp_path):
    runner = offline.OfflineRun(tmp_path)
    session = host.KernelHost(runner, IMAGE, MANIFEST, "backend")
    session.target_cgroup = 123
    return session


@pytest.mark.parametrize("sequence", [1, 3, True])
def test_host_snapshot_rejects_duplicate_skipped_or_boolean_sequence(
    tmp_path, monkeypatch, sequence
):
    session = make_host(tmp_path)
    monkeypatch.setattr(session, "send", lambda *args: None)
    monkeypatch.setattr(session, "event", lambda *args: snapshot(sequence))
    with pytest.raises(VerificationFailure):
        session.snapshot()


def test_host_snapshot_sequence_is_monotonic(tmp_path, monkeypatch):
    session = make_host(tmp_path)
    sent = []
    events = iter([snapshot(2), snapshot(3)])
    monkeypatch.setattr(session, "send", lambda *args: sent.append(args))
    monkeypatch.setattr(session, "event", lambda *args: next(events))
    assert session.snapshot() == session.snapshot() == counters()
    assert session.sequence == 3 and sent == [("collector", "SNAPSHOT")] * 2


@pytest.mark.parametrize(
    "change",
    [
        {"remaining_tasks": 1},
        {"trusted_thread_count": 3},
        {"cgroup_inode": 124},
        {"baseline_unchanged": 1},
        {"schema_version": True},
        {"extra": 0},
    ],
)
def test_host_rejects_unquiescent_or_changed_target(tmp_path, change):
    session = make_host(tmp_path)
    session.quiescence(quiescence())
    with pytest.raises(VerificationFailure, match="not_quiescent"):
        session.quiescence({**quiescence(), **change})


def test_collector_failure_retains_hash_not_unknown_payload(tmp_path, monkeypatch):
    session = make_host(tmp_path)
    event = {"event": "FAILED", "private": "secret"}
    monkeypatch.setattr(session, "line", lambda role: client.canonical(event))
    with pytest.raises(VerificationFailure, match="unexpected_event"):
        session.event("collector", "SNAPSHOT")
    assert session.evidence["collector_failed"] is True
    assert (
        session.evidence["collector_failure_sha256"]
        == hashlib.sha256(client.canonical(event)).hexdigest()
    )
    assert "secret" not in json.dumps(session.evidence) and session.evidence["ok"] is False


@pytest.mark.parametrize("field", list(SOURCE))
def test_host_source_binding_fails_before_collector_launch(tmp_path, monkeypatch, field):
    session = make_host(tmp_path)
    value = {
        "event": "READY",
        "suite": "backend",
        "cgroup_inode": 123,
        "source": {**SOURCE, field: "wrong"},
        "qualification": qualification(),
    }
    launched = []
    monkeypatch.setattr(session, "launch", lambda *args: launched.append(args))
    monkeypatch.setattr(session, "event", lambda *args: value)
    with pytest.raises(VerificationFailure, match="target_ready_invalid"):
        session.run()
    assert launched == [("target",)] and not session.evidence["ok"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha256", "0" * 64),
        ("native_guard_installed", 1),
        ("inherited_sockets", 1),
        ("core_file_limit_zero", False),
        ("foreign_elf_rejection", "allowed"),
        ("io_uring_setup_rejection", "EINVAL"),
    ],
)
def test_host_rejects_missing_target_boundary_proof(tmp_path, monkeypatch, field, value):
    session = make_host(tmp_path)
    event = {
        "event": "READY",
        "suite": "backend",
        "cgroup_inode": 123,
        "source": SOURCE,
        "qualification": {**qualification(), field: value},
    }
    monkeypatch.setattr(session, "launch", lambda *args: None)
    monkeypatch.setattr(session, "event", lambda *args: event)
    with pytest.raises(VerificationFailure, match="target_qualification_invalid"):
        session.run()
    assert not session.evidence["ok"]


def fake_client(monkeypatch):
    audit = client.KernelAuditClient("backend", 100)
    audit.started = True
    monkeypatch.setattr(client.time, "monotonic", lambda: 90)
    monkeypatch.setattr(boundary, "quiescent_target", quiescence)
    return audit


def test_missing_observer_never_runs_unaudited_child():
    with pytest.raises(VerificationFailure, match="kernel_observer_missing"):
        offline.collect_trace(("never",), timeout=1)
    with pytest.raises(VerificationFailure, match="not_active"):
        client.KernelAuditClient("backend", 100).collect(("never",), timeout=1)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), "1"])
def test_client_rejects_invalid_timeout_before_child(monkeypatch, timeout):
    audit = fake_client(monkeypatch)
    with pytest.raises(VerificationFailure, match="timeout_invalid"):
        audit.collect(("never",), timeout=timeout)


def test_client_handshake_sequences_and_caps_child_budget(monkeypatch):
    audit = fake_client(monkeypatch)
    events, children = [], []
    summary = client.phase_summary(counters(), counters(loopback=1))

    def exchange(value, expected):
        events.append(value)
        if expected == "END":
            return {"event": "END", "phase": value["phase"], "summary": summary}
        if expected == "RELEASE":
            return {"event": "RELEASE"}
        return {"event": "BEGIN", "phase": value["phase"]}

    def run(argv, **kwargs):
        assert events[-1]["event"] == "BEGIN"
        assert kwargs == {
            "capture_output": True,
            "stdin": subprocess.DEVNULL,
            "timeout": 10,
            "check": False,
        }
        children.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"private output", b"")

    monkeypatch.setattr(audit, "exchange", exchange)
    monkeypatch.setattr(subprocess, "run", run)
    for command in (("first",), ("second",)):
        result, actual = audit.collect(command, timeout=30)
        assert actual == summary and result.stdout == b"private output"
    audit.finish()
    assert [event["event"] for event in events] == ["BEGIN", "END", "BEGIN", "END", "HOLD"]
    assert [event["phase"] for event in events] == [1, 1, 2, 2, 2]
    assert events[0]["command_sha256"] == client.command_digest(("first",))
    assert children == [("first",), ("second",)] and audit.closed
    with pytest.raises(VerificationFailure, match="not_active"):
        audit.collect(("never",), timeout=1)


@pytest.mark.parametrize(
    "reply",
    [
        {"event": "BEGIN", "phase": 2},
        {"event": "BEGIN", "phase": 1, "extra": 1},
        {"event": "BEGIN", "phase": True},
    ],
)
def test_bad_begin_ack_never_runs_child(monkeypatch, reply):
    audit = fake_client(monkeypatch)
    monkeypatch.setattr(audit, "exchange", lambda *args: reply)
    with pytest.raises(VerificationFailure, match="begin_reply_invalid"):
        audit.collect(("never",), timeout=1)


@pytest.mark.parametrize("bad", ["hash", "counter", "phase", "extra"])
def test_post_child_protocol_failure_retains_capture_without_acceptance(monkeypatch, bad):
    audit = fake_client(monkeypatch)
    result = subprocess.CompletedProcess(("child",), 3, b"private output", b"private stderr")
    summary = client.phase_summary(counters(), counters())
    reply = {"event": "END", "phase": 1, "summary": summary}
    if bad == "hash":
        summary["sha256"] = "0" * 64
    elif bad == "counter":
        summary["counters"]["external"] = 1
    elif bad == "phase":
        reply["phase"] = True
    else:
        reply["extra"] = "secret"
    monkeypatch.setattr(
        audit,
        "exchange",
        lambda value, expected: {"event": "BEGIN", "phase": 1} if expected == "BEGIN" else reply,
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(client.AuditedCommandFailure) as caught:
        audit.collect(("child",), timeout=1)
    assert caught.value.result is result and "private" not in str(caught.value)


@pytest.mark.parametrize("qualified", [True, False])
def test_client_vma_summary_requires_acknowledged_collector_configuration(monkeypatch, qualified):
    audit = fake_client(monkeypatch)
    if qualified:
        audit.kernel_configuration = kernel_configuration()
    result = subprocess.CompletedProcess(("child",), 0, b"private output", b"")
    summary = client.phase_summary(
        counters(), counters(vma_unsupported_started=2, vma_unsupported_completed=2)
    )
    monkeypatch.setattr(
        audit,
        "exchange",
        lambda _value, expected: (
            {"event": "BEGIN", "phase": 1}
            if expected == "BEGIN"
            else {"event": "END", "phase": 1, "summary": summary}
        ),
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: result)
    if qualified:
        assert audit.collect(("child",), timeout=1) == (result, summary)
    else:
        with pytest.raises(
            client.AuditedCommandFailure, match="configuration_unqualified"
        ) as caught:
            audit.collect(("child",), timeout=1)
        assert caught.value.result is result and "private" not in str(caught.value)


def test_exchange_framing_and_control_reserve_do_not_extend_command_budget(monkeypatch):
    audit = client.KernelAuditClient("backend", 100)
    output = io.BytesIO()
    monkeypatch.setattr(client.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(
        client.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"event":"ATTACHED"}\n'))
    )
    monkeypatch.setattr(client.time, "monotonic", lambda: 119)
    assert audit.exchange({"event": "READY"}, "ATTACHED") == {"event": "ATTACHED"}
    assert output.getvalue() == b'AC15_AUDIT {"event":"READY"}\n'
    monkeypatch.setattr(client.time, "monotonic", lambda: 120)
    with pytest.raises(VerificationFailure, match="control_deadline"):
        audit.exchange({}, "ATTACHED")
    audit = fake_client(monkeypatch)
    monkeypatch.setattr(client.time, "monotonic", lambda: 100)
    monkeypatch.setattr(audit, "exchange", lambda *args: {"event": "BEGIN", "phase": 1})
    with pytest.raises(VerificationFailure, match="suite_deadline"):
        audit.collect(("never",), timeout=1)


@pytest.mark.parametrize(
    "reply",
    [b"", b'{"event":"ATTACHED"}', b" " * client.LINE_LIMIT + b"\n", b'{"event":"WRONG"}\n'],
)
def test_exchange_rejects_missing_oversized_unterminated_or_wrong_reply(monkeypatch, reply):
    audit = fake_client(monkeypatch)
    monkeypatch.setattr(client.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(client.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(reply)))
    with pytest.raises(VerificationFailure):
        audit.exchange({}, "ATTACHED")


def host_transcript(suite, *, emulation=False, vma_unsupported=0):
    """Use the real fixed inventory, but synthetic independent collector counters."""
    transcript = [
        (
            "target",
            "READY",
            {
                "event": "READY",
                "suite": suite,
                "cgroup_inode": 123,
                "source": deepcopy(SOURCE),
                "qualification": qualification(emulation=emulation),
            },
        ),
        ("collector", "READY", ready()),
        ("collector", "SNAPSHOT", snapshot(2)),
    ]
    cumulative, sequence, summaries = counters(), 2, []
    commands = offline.audit_phase_commands(suite, emulation=emulation)
    for index, command in enumerate(commands):
        phase = index + 1
        transcript.append(
            (
                "target",
                "BEGIN",
                {
                    "event": "BEGIN",
                    "phase": phase,
                    "command_sha256": client.command_digest(command),
                    "quiescence": quiescence(),
                },
            )
        )
        sequence += 1
        transcript.append(
            ("collector", "SNAPSHOT", snapshot(sequence, counters=deepcopy(cumulative)))
        )
        transcript.append(
            ("target", "END", {"event": "END", "phase": phase, "quiescence": quiescence()})
        )
        offset = index - (3 if suite == "browser" else 0)
        change = (
            {"external": 1, "filter_changes": 1}
            if offset == 3
            else {"external": 1}
            if offset in (0, 2)
            else {"unix": 1}
            if offset == 1
            else {}
        )
        if suite == "browser" and index == 2:
            change = {"loopback": 1, "unix": 1, "kernel_ipc": 1}
        if emulation and offset == 4:
            change = {"external": 1, "unix": 1}
        if index == len(commands) - 1:
            change.update(
                vma_unsupported_started=vma_unsupported,
                vma_unsupported_completed=vma_unsupported,
            )
        delta = counters(**change)
        summary = client.phase_summary(counters(), delta)
        summaries.append(summary)
        cumulative = {key: cumulative[key] + delta[key] for key in cumulative}
        sequence += 1
        transcript.append(
            ("collector", "SNAPSHOT", snapshot(sequence, counters=deepcopy(cumulative)))
        )
    transcript.append(
        ("target", "HOLD", {"event": "HOLD", "phase": len(commands), "quiescence": quiescence()})
    )
    transcript.append(
        ("collector", "DONE", snapshot(sequence + 1, "DONE", counters=deepcopy(cumulative)))
    )
    actual = offline.SUITES[suite]
    result = {
        **SOURCE,
        "suite": suite,
        "ok": True,
        "kernel": {
            "kernel_observer": {
                "observer": "kernel-cgroup-v1",
                "qualification": qualification(emulation=emulation),
                "kernel_configuration": kernel_configuration(),
            }
        },
        "commands": [
            {"argv": list(command), "network_syscalls": deepcopy(summary)}
            for command, summary in zip(actual, summaries[-len(actual) :], strict=True)
        ],
    }
    if emulation:
        result["kernel"]["native_emulation_control"] = {
            "native_emulation": emulation_proof(),
            "external_errno": 101,
            "unix_connected": True,
            "network_syscalls": deepcopy(summaries[(3 if suite == "browser" else 0) + 4]),
        }
    transcript.append(("target", "RESULT", result))
    return transcript


def wire_host(session, monkeypatch, transcript):
    events = iter(transcript)
    actions = []

    def event(role, expected):
        actual_role, actual_event, value = next(events)
        assert (role, expected) == (actual_role, actual_event)
        actions.append(("event", role, expected))
        return value

    monkeypatch.setattr(session, "launch", lambda *args: actions.append(("launch", *args)))
    monkeypatch.setattr(session, "event", event)
    monkeypatch.setattr(session, "send", lambda *args: actions.append(("send", *args)))
    monkeypatch.setattr(session, "finish_process", lambda role: actions.append(("finish", role)))
    return actions, events


@pytest.mark.parametrize("suite", tuple(offline.SUITES))
@pytest.mark.parametrize("emulation", [False, True])
def test_full_host_inventory_source_and_detach_release_order(
    tmp_path, monkeypatch, suite, emulation
):
    runner = offline.OfflineRun(tmp_path)
    session = host.KernelHost(runner, IMAGE, MANIFEST, suite)
    actions, events = wire_host(session, monkeypatch, host_transcript(suite, emulation=emulation))
    session.run()
    assert next(events, None) is None and session.evidence["ok"]
    commands = offline.audit_phase_commands(suite, emulation=emulation)
    assert [value["phase"] for value in session.evidence["phases"]] == list(
        range(1, len(commands) + 1)
    )
    assert [value["command_sha256"] for value in session.evidence["phases"]] == [
        client.command_digest(command) for command in commands
    ]
    assert actions.index(("event", "collector", "DONE")) < actions.index(("finish", "collector"))
    assert actions.index(("finish", "collector")) < actions.index(
        ("send", "target", {"event": "RELEASE"})
    )
    assert actions[-1] == ("finish", "target")
    assert runner.report["suites"][0]["ok"] is True
    assert [item["argv"] for item in runner.report["suites"][0]["commands"]] == [
        list(command) for command in offline.SUITES[suite]
    ]


def test_host_binds_collector_config_ack_and_final_vma_phase(tmp_path, monkeypatch):
    runner = offline.OfflineRun(tmp_path)
    session = host.KernelHost(runner, IMAGE, MANIFEST, "backend")
    transcript = host_transcript("backend", vma_unsupported=7)
    actions, _ = wire_host(session, monkeypatch, transcript)
    session.run()
    assert (
        "send",
        "target",
        {"event": "ATTACHED", "kernel_configuration": kernel_configuration()},
    ) in actions
    assert session.evidence["collector"]["kernel_configuration"] == kernel_configuration()
    assert session.evidence["phases"][-1]["summary"]["counters"]["vma_unsupported_completed"] == 7
    assert session.evidence["phases"][-1]["summary"]["rejected"] == 0
    assert runner.report["suites"][0]["kernel"]["kernel_observer"]["kernel_configuration"] == (
        kernel_configuration()
    )


@pytest.mark.parametrize("fault", ["missing", "changed-digest", "changed-release", "extra"])
def test_host_rejects_final_configuration_not_identical_to_collector(tmp_path, monkeypatch, fault):
    runner = offline.OfflineRun(tmp_path)
    session = host.KernelHost(runner, IMAGE, MANIFEST, "backend")
    transcript = host_transcript("backend", vma_unsupported=2)
    observer = transcript[-1][2]["kernel"]["kernel_observer"]
    if fault == "missing":
        del observer["kernel_configuration"]
    elif fault == "changed-digest":
        observer["kernel_configuration"]["config_sha256"] = "4" * 64
    elif fault == "changed-release":
        observer["kernel_configuration"]["release"] = "7.0.10-linuxkit"
    else:
        observer["kernel_configuration"]["untrusted"] = True
    wire_host(session, monkeypatch, transcript)
    with pytest.raises(VerificationFailure, match="suite_qualification_mismatch"):
        session.run()
    assert not session.evidence["ok"]


def test_host_rejects_unknown_collector_before_target_release(tmp_path, monkeypatch):
    runner = offline.OfflineRun(tmp_path)
    session = host.KernelHost(runner, IMAGE, MANIFEST, "backend")
    transcript = host_transcript("backend")
    transcript[1][2]["kernel_configuration"]["anon_vma_name"] = "unknown"
    actions, _ = wire_host(session, monkeypatch, transcript)
    with pytest.raises(VerificationFailure, match="configuration_unqualified"):
        session.run()
    assert not any(action[0] == "send" for action in actions)
    assert not session.evidence["ok"]


@pytest.mark.parametrize(
    "fault,code",
    [
        ("collector-source", "collector_source_mismatch"),
        ("phase-hash", "phase_inventory_mismatch"),
        ("phase-boolean", "phase_inventory_mismatch"),
        ("phase-skip", "phase_inventory_mismatch"),
        ("end-phase", "phase_end_mismatch"),
        ("between-phase", "activity_outside_phase"),
        ("final-counter", "final_counters_changed"),
        ("final-sequence", "final_sequence_mismatch"),
        ("hold-short", "hold_incomplete"),
        ("missing-detach", "detach_incomplete"),
        ("target-source", "source_changed"),
        ("target-inventory", "command_inventory_mismatch"),
        ("target-summary", "suite_evidence_mismatch"),
    ],
)
def test_host_cannot_accept_partial_forged_or_unbound_phase_history(
    tmp_path, monkeypatch, fault, code
):
    session = make_host(tmp_path)
    transcript = host_transcript("backend")

    def selected(role, event):
        return next(
            value
            for actual_role, actual_event, value in transcript
            if (actual_role, actual_event) == (role, event)
        )

    if fault == "collector-source":
        selected("collector", "READY")["source"] = {**SOURCE, "source_tree": "wrong"}
    elif fault == "phase-hash":
        selected("target", "BEGIN")["command_sha256"] = "0" * 64
    elif fault in {"phase-boolean", "phase-skip"}:
        selected("target", "BEGIN")["phase"] = True if fault == "phase-boolean" else 2
    elif fault == "end-phase":
        selected("target", "END")["phase"] = 2
    elif fault == "between-phase":
        snapshots = [value for role, event, value in transcript if event == "SNAPSHOT"]
        snapshots[1]["counters"] = counters(safe_prctl=1)
    elif fault == "final-counter":
        selected("collector", "DONE")["counters"]["safe_prctl"] += 1
    elif fault == "final-sequence":
        selected("collector", "DONE")["sequence"] += 1
    elif fault == "hold-short":
        selected("target", "HOLD")["phase"] -= 1
    elif fault == "missing-detach":
        selected("collector", "DONE")["detached"] = False
    elif fault == "target-source":
        selected("target", "RESULT")["source_tree"] = "wrong"
    elif fault == "target-inventory":
        selected("target", "RESULT")["commands"] = []
    else:
        selected("target", "RESULT")["commands"][0]["network_syscalls"]["sha256"] = "0" * 64
    actions, _ = wire_host(session, monkeypatch, transcript)
    with pytest.raises(VerificationFailure, match=code):
        session.run()
    assert not session.evidence["ok"]
    if not fault.startswith("target-"):
        assert ("send", "target", {"event": "RELEASE"}) not in actions


def container_value(session, role):
    name = session.records[role]["name"]
    return {
        "Id": hashlib.sha256(name.encode()).hexdigest(),
        "Name": "/" + name,
        "Image": IMAGE,
        "Config": {
            "User": "0:0" if role == "collector" else "10001:10001",
            "Labels": {offline.LABEL + "owner": session.runner.owner},
        },
        "HostConfig": {
            "Init": True,
            "Privileged": False,
            "NetworkMode": "none",
            "PortBindings": {},
            "Binds": [],
            "Devices": [],
            "DeviceRequests": [],
            "PidMode": "",
            "UTSMode": "",
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "PidsLimit": 1024,
            "ShmSize": 1024**3,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "ReadonlyRootfs": role == "collector",
            "CapAdd": ["CAP_BPF", "CAP_PERFMON"] if role == "collector" else [],
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {"none": {}}},
        "State": {"Running": False, "ExitCode": 0},
    }


def test_role_launch_and_owned_cleanup_use_only_exact_isolated_ids(tmp_path, monkeypatch):
    session = make_host(tmp_path)
    containers = {role: container_value(session, role) for role in session.records}
    calls, attached, removed = [], [], set()
    monkeypatch.setattr(host.time, "monotonic", lambda: 100)

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            attached.append((argv, kwargs))
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout):
            assert 0 < timeout <= 2
            return self.returncode

    def command(name, argv, **kwargs):
        calls.append((name, argv, kwargs))
        if argv[1] == "create":
            role = name.removeprefix("kernel-create-")
            return subprocess.CompletedProcess(argv, 0, containers[role]["Id"].encode(), b"")
        identity = argv[-1]
        value = next(value for value in containers.values() if value["Id"] == identity)
        if argv[1:3] == ["container", "rm"]:
            assert argv == ["docker", "container", "rm", "--force", value["Id"]]
            removed.add(identity)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert argv[:3] == ["docker", "container", "inspect"]
        if identity in removed:
            return subprocess.CompletedProcess(
                argv, 1, b"", ("Error: No such container: " + identity).encode()
            )
        return subprocess.CompletedProcess(argv, 0, json.dumps([value]).encode(), b"")

    monkeypatch.setattr(session, "command", command)
    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    session.launch("target")
    session.launch("collector", 123)
    creates = [argv for _, argv, _ in calls if argv[1] == "create"]
    assert len(creates) == 2 and len(attached) == 2
    for role, argv in zip(("target", "collector"), creates, strict=True):
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--user") + 1] == ("0:0" if role == "collector" else "10001:10001")
        assert {
            "--pull=never",
            "--init",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--cgroupns=private",
            "--ipc=private",
            "--pids-limit=1024",
            "--shm-size=1g",
        } <= set(argv)
        assert not {
            "--volume",
            "--mount",
            "--publish",
            "--privileged",
            "--pid=host",
            "--network=host",
        } & set(argv)
        assert ("--read-only" in argv) == (role == "collector")
        assert ("--cap-add=BPF" in argv) == (role == "collector")
        assert ("--cap-add=PERFMON" in argv) == (role == "collector")
        assert IMAGE in argv and all(
            "AC15_" + key.upper() + "=" + value in argv for key, value in SOURCE.items()
        )
    for argv, kwargs in attached:
        assert argv[:-1] == ["docker", "start", "--attach", "--interactive"]
        assert argv[-1] in {value["Id"] for value in containers.values()}
        assert kwargs["start_new_session"] is True
    outcome = session.cleanup(130)
    assert outcome["ok"] and removed == {value["Id"] for value in containers.values()}
    assert all(value["attached_client_reaped"] for value in outcome["roles"].values())
    count = len(calls)
    assert session.cleanup(130) is outcome and len(calls) == count


@pytest.mark.parametrize(
    "fault",
    ["owner", "image", "identity", "network", "privileged", "capabilities", "mount", "unconfined"],
)
def test_cleanup_refuses_changed_or_foreign_container(tmp_path, monkeypatch, fault):
    session = make_host(tmp_path)
    value = container_value(session, "collector")
    record = session.records["collector"]
    record.update(attempted=True, identity=value["Id"])
    if fault == "owner":
        value["Config"]["Labels"][offline.LABEL + "owner"] = "foreign"
    elif fault == "image":
        value["Image"] = "sha256:" + "0" * 64
    elif fault == "identity":
        value["Id"] = "0" * 64
    elif fault == "network":
        value["HostConfig"]["NetworkMode"] = "host"
    elif fault == "privileged":
        value["HostConfig"]["Privileged"] = True
    elif fault == "capabilities":
        value["HostConfig"]["CapAdd"].append("CAP_SYS_ADMIN")
    elif fault == "mount":
        value["Mounts"] = [{"Destination": "/host"}]
    else:
        value["HostConfig"]["SecurityOpt"].append("seccomp=unconfined")
    calls = []

    def command(name, argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ["docker", "container", "inspect"]
        return subprocess.CompletedProcess(argv, 0, json.dumps([value]).encode(), b"")

    monkeypatch.setattr(session, "command", command)
    monkeypatch.setattr(host.time, "monotonic", lambda: 100)
    assert session.cleanup(130)["ok"] is False
    assert len(calls) == 1


def test_client_begin_publishes_only_after_qualification_and_ack(monkeypatch):
    audit = client.KernelAuditClient("backend", 100)
    events = []
    monkeypatch.setattr(boundary, "qualify_target", lambda: qualification())

    def exchange(value, expected):
        assert not audit.started
        assert value == {
            "event": "READY",
            "suite": "backend",
            "source": SOURCE,
            "qualification": qualification(),
            "cgroup_inode": 123,
        }
        assert expected == "ATTACHED"
        events.append(value)
        return {"event": "ATTACHED", "kernel_configuration": kernel_configuration()}

    monkeypatch.setattr(audit, "exchange", exchange)
    proof = audit.begin(MANIFEST)
    assert proof == {
        "observer": "kernel-cgroup-v1",
        "qualification": qualification(),
        "kernel_configuration": kernel_configuration(),
    }
    assert audit.started and len(events) == 1
    with pytest.raises(VerificationFailure, match="already_started"):
        audit.begin(MANIFEST)


@pytest.mark.parametrize("fault", ["missing", "unknown", "extra", "enabled"])
def test_client_begin_fails_closed_before_started_on_unqualified_ack(monkeypatch, fault):
    audit = client.KernelAuditClient("backend", 100)
    monkeypatch.setattr(boundary, "qualify_target", lambda: qualification())
    reply = {"event": "ATTACHED", "kernel_configuration": kernel_configuration()}
    if fault == "missing":
        del reply["kernel_configuration"]
    elif fault == "extra":
        reply["allow_vma"] = True
    else:
        reply["kernel_configuration"]["anon_vma_name"] = fault
    monkeypatch.setattr(audit, "exchange", lambda *_args: reply)
    with pytest.raises(
        VerificationFailure, match=r"(?:attach_reply_invalid|configuration_unqualified)"
    ):
        audit.begin(MANIFEST)
    assert audit.started is False and audit.kernel_configuration is None


def test_original_1800_second_bound_is_not_expanded():
    assert offline.SUITE_TIMEOUT == 1700
    assert offline.CONTAINER_TIMEOUT == 1720
    assert offline.HOST_TIMEOUT == 1740
    assert offline.HOST_TIMEOUT + 30 + 30 == 1800


def test_offline_observer_lifetime_registered_before_ack_and_cleared_after_detach(monkeypatch):
    events = []

    class Observer:
        def __init__(self, suite, deadline):
            assert (suite, deadline) == ("backend", 100)

        def begin(self, manifest):
            assert manifest == MANIFEST and offline.ACTIVE_OBSERVER is self
            events.append("begin")
            return {"observer": "fake"}

        def collect(self, command, *, timeout):
            assert offline.ACTIVE_OBSERVER is self
            assert (command, timeout) == (("child",), 5)
            events.append("collect")
            return "sentinel"

        def finish(self):
            assert offline.ACTIVE_OBSERVER is self
            events.append("finish")

    monkeypatch.setattr(offline, "KernelAuditClient", Observer)
    assert offline.start_kernel_observer("backend", 100, MANIFEST) == {"observer": "fake"}
    assert offline.collect_trace(("child",), timeout=5) == "sentinel"
    with pytest.raises(VerificationFailure, match="already_active"):
        offline.start_kernel_observer("backend", 100, MANIFEST)
    offline.finish_kernel_observer()
    assert offline.ACTIVE_OBSERVER is None and events == ["begin", "collect", "finish"]


def test_offline_failed_begin_retains_lifetime_without_authorizing_commands(monkeypatch):
    def begin(manifest):
        assert offline.ACTIVE_OBSERVER is observer
        raise VerificationFailure("fake_attach_failed")

    observer = client.KernelAuditClient("backend", 100)
    monkeypatch.setattr(observer, "begin", begin)
    monkeypatch.setattr(offline, "KernelAuditClient", lambda *args: observer)
    with pytest.raises(VerificationFailure, match="fake_attach_failed"):
        offline.start_kernel_observer("backend", 100, MANIFEST)
    assert offline.ACTIVE_OBSERVER is observer and observer.started is False
    with pytest.raises(VerificationFailure, match="not_active"):
        offline.collect_trace(("never",), timeout=1)


def test_offline_failed_detach_retains_observer_authority(monkeypatch):
    def finish():
        raise VerificationFailure("fake_detach_failed")

    observer = SimpleNamespace(finish=finish)
    monkeypatch.setattr(offline, "ACTIVE_OBSERVER", observer)
    with pytest.raises(VerificationFailure, match="fake_detach_failed"):
        offline.finish_kernel_observer()
    assert offline.ACTIVE_OBSERVER is observer


@pytest.mark.parametrize(
    "fault,code",
    [
        ("eof", "observer_or_target_eof"),
        ("stderr", "process_stderr"),
        ("other-role", "unsolicited_process_output"),
        ("line-bound", "protocol_line_bound"),
        ("total-bound", "protocol_output_bound"),
        ("deadline", "host_deadline"),
    ],
)
def test_host_io_rejects_incomplete_cross_role_and_unbounded_output(
    tmp_path, monkeypatch, fault, code
):
    session = make_host(tmp_path)
    session.deadline = 100
    monkeypatch.setattr(host.time, "monotonic", lambda: 100 if fault == "deadline" else 90)

    # Use hashable identity objects for the stream map without creating real descriptors.
    class Pipe:
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

    for index, role in enumerate(session.records):
        session.records[role]["client"] = SimpleNamespace(
            stdout=Pipe(index * 2), stderr=Pipe(index * 2 + 1)
        )
    role = "collector" if fault == "other-role" else "target"
    kind = "stderr" if fault == "stderr" else "stdout"
    stream = getattr(session.records[role]["client"], kind)
    data = b"" if fault == "eof" else b"private diagnostic\n"
    if fault == "line-bound":
        session.records[role]["stdout"] = bytearray(b" " * client.LINE_LIMIT)
    if fault == "total-bound":
        session.records[role]["stdout_bytes"] = 1024**2
    monkeypatch.setattr(host.select, "select", lambda *args: ([stream], [], []))
    monkeypatch.setattr(host.os, "read", lambda fd, size: data)
    with pytest.raises(VerificationFailure, match=code):
        session.line("target")
    assert "private" not in json.dumps(session.evidence)


def test_host_buffered_lines_preserve_exact_frame_boundaries(tmp_path):
    session = make_host(tmp_path)
    session.records["target"]["stdout"] = bytearray(b"first\nsecond\n")
    assert session.line("target") == b"first"
    assert session.line("target") == b"second"
    assert session.records["target"]["stdout"] == bytearray()


@pytest.mark.parametrize(
    "tail,stderr,returncode,code",
    [
        (b"extra", b"", 0, "failed_or_extra_output"),
        (b"", b"private", 0, "process_stderr"),
        (b"", b"", 1, "failed_or_extra_output"),
    ],
)
def test_host_process_finish_never_ignores_trailing_output_or_failed_exit(
    tmp_path, monkeypatch, tail, stderr, returncode, code
):
    session = make_host(tmp_path)
    session.deadline = 100
    monkeypatch.setattr(host.time, "monotonic", lambda: 90)

    class Pipe:
        def __init__(self, descriptor):
            self.descriptor = descriptor

        def fileno(self):
            return self.descriptor

    stdout_pipe, stderr_pipe = Pipe(10), Pipe(11)
    session.records["collector"]["client"] = SimpleNamespace(
        stdout=stdout_pipe,
        stderr=stderr_pipe,
        poll=lambda: returncode,
        wait=lambda timeout: returncode,
        returncode=returncode,
    )
    monkeypatch.setattr(host.select, "select", lambda streams, *args: (streams, [], []))
    monkeypatch.setattr(
        host.os, "read", lambda descriptor, size: tail if descriptor == 10 else stderr
    )
    with pytest.raises(VerificationFailure, match=code):
        session.finish_process("collector")
    assert not session.records["collector"]["inactive"]
    record = session.records["collector"]
    assert record["stdout_bytes"] == len(tail)
    assert record["stdout_sha256"].hexdigest() == hashlib.sha256(tail).hexdigest()
    if not tail:
        assert record["stderr_bytes"] == len(stderr)
        assert record["stderr_sha256"].hexdigest() == hashlib.sha256(stderr).hexdigest()


@pytest.mark.parametrize("source_valid", [True, False])
def test_rejected_actual_phase_preserves_only_source_bound_failed_diagnostics(
    tmp_path, monkeypatch, source_valid
):
    session = make_host(tmp_path)
    transcript = host_transcript("backend")
    # The first four phases are expected-fault canaries; phase five is real work.
    begin_at = next(
        index
        for index, (_, event, value) in enumerate(transcript)
        if event == "BEGIN" and value["phase"] == 5
    )
    bad_snapshot = transcript[begin_at + 3][2]
    bad_snapshot["counters"]["external"] += 1
    bad_snapshot["counters"]["messages"] += 1
    failure = {
        **SOURCE,
        "suite": "backend",
        "ok": False,
        "commands": [],
        "failure": "external_network_syscall_observed",
        "failed_command": {
            "argv": list(offline.SUITES["backend"][0]),
            "exit_code": 0,
            "output_complete": True,
            "stdout_sha256": "3" * 64,
            "stderr_sha256": "4" * 64,
        },
    }
    if not source_valid:
        failure["source_tree"] = "wrong"
    transcript = [*transcript[: begin_at + 4], ("target", "RESULT", failure)]
    actions, _ = wire_host(session, monkeypatch, transcript)
    with pytest.raises(VerificationFailure, match="kernel_phase_rejection_mismatch"):
        session.run()
    assert not session.evidence["ok"]
    assert session.runner.report["suites"] == ([failure] if source_valid else [])
    assert ("event", "target", "RESULT") in actions
    assert ("send", "target", {"event": "RELEASE"}) not in actions


@pytest.mark.parametrize("collector_running", [True, False])
def test_cleanup_target_lifetime_requires_confirmed_collector_cessation(
    tmp_path, monkeypatch, collector_running
):
    session = make_host(tmp_path)
    values = {role: container_value(session, role) for role in session.records}
    values["collector"]["State"] = {"Running": collector_running, "ExitCode": 1}
    for role, value in values.items():
        session.records[role].update(attempted=True, identity=value["Id"])
    calls, removed = [], set()

    def command(name, argv, **kwargs):
        calls.append(argv)
        identity = argv[-1]
        role = next(role for role, value in values.items() if value["Id"] == identity)
        if argv[1:3] == ["container", "rm"]:
            if role == "collector":
                raise subprocess.CalledProcessError(1, argv)
            removed.add(identity)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert argv[:3] == ["docker", "container", "inspect"]
        if identity in removed:
            return subprocess.CompletedProcess(
                argv, 1, b"", ("Error: No such container: " + identity).encode()
            )
        return subprocess.CompletedProcess(argv, 0, json.dumps([values[role]]).encode(), b"")

    monkeypatch.setattr(session, "command", command)
    monkeypatch.setattr(host.time, "monotonic", lambda: 100)
    result = session.cleanup(130)
    assert result["ok"] is False
    assert (values["target"]["Id"] in removed) is (not collector_running)
    assert values["collector"]["Id"] not in removed


@pytest.mark.parametrize("deadline,expected", [(100, 92), (91, 91)])
def test_failed_phase_diagnostic_grace_never_extends_shared_deadline(
    tmp_path, monkeypatch, deadline, expected
):
    session = make_host(tmp_path)
    session.deadline = deadline
    monkeypatch.setattr(host.time, "monotonic", lambda: 90)

    def event(role, name):
        assert (role, name) == ("target", "RESULT")
        assert session.deadline == expected
        raise VerificationFailure("fake_missing_result")

    monkeypatch.setattr(session, "event", event)
    session.retain_failed_phase()
    assert session.deadline == deadline
    assert session.evidence["failed_phase_result_retained"] is False
    assert not session.evidence["ok"] and session.runner.report["suites"] == []


def test_collector_finalization_monitors_target_still_held(tmp_path, monkeypatch):
    session = make_host(tmp_path)
    session.deadline = 100
    monkeypatch.setattr(host.time, "monotonic", lambda: 90)

    class Pipe:
        def fileno(self):
            return 5

    target_stdout = Pipe()
    target_stderr = Pipe()
    collector_stdout = Pipe()
    collector_stderr = Pipe()
    session.records["target"]["client"] = SimpleNamespace(
        stdout=target_stdout, stderr=target_stderr
    )
    session.records["collector"]["client"] = SimpleNamespace(
        stdout=collector_stdout, stderr=collector_stderr, poll=lambda: None
    )
    monkeypatch.setattr(host.select, "select", lambda *args: ([target_stdout], [], []))
    monkeypatch.setattr(host.os, "read", lambda *args: b"")
    with pytest.raises(VerificationFailure, match="observer_or_target_eof"):
        session.finish_process("collector")
    assert session.records["target"]["stdout_eof"] is True
    assert session.records["collector"]["cessation_confirmed"] is False


def test_failed_target_hold_survives_stdin_eof_but_never_extends_original_deadline(
    monkeypatch, capsys
):
    clock, sleeps, alarms = [100.0], [], []
    observer = client.KernelAuditClient("backend", 82)
    # This is also the pre-ACK failure path: startup ownership exists but no
    # command was ever authorized by an attachment acknowledgment.
    assert observer.started is False
    monkeypatch.setattr(offline, "ACTIVE_OBSERVER", observer)
    monkeypatch.setattr(offline, "inside_result", lambda suite: {"suite": suite, "ok": False})
    monkeypatch.setattr(offline.time, "monotonic", lambda: clock[0])
    closed = io.BytesIO()
    closed.close()
    monkeypatch.setattr(offline.sys, "stdin", SimpleNamespace(buffer=closed))

    @contextmanager
    def alarm(duration):
        alarms.append(duration)
        yield

    def sleep(duration):
        sleeps.append(duration)
        clock[0] += duration

    monkeypatch.setattr(offline, "verification_signals", alarm)
    monkeypatch.setattr(offline.time, "sleep", sleep)
    assert offline.main(["inside", "--suite", "backend"]) == 1
    assert clock[0] == observer.deadline + 20 == 102
    assert sleeps == [1, 1] and alarms == [2]
    assert offline.ACTIVE_OBSERVER is observer
    assert json.loads(capsys.readouterr().out.removeprefix(offline.PREFIX)) == {
        "suite": "backend",
        "ok": False,
    }


@pytest.mark.parametrize("rejection", ["ENOEXEC", "SIGSYS"])
def test_qualification_schema2_narrow_rejection_variant(rejection):
    proof = qualification()
    proof["foreign_elf_rejection"] = rejection
    assert host.validate_qualification(proof, 123, "e" * 64) is False


def test_qualification_schema2_native_emulation_variant_requires_full_proof():
    assert host.validate_qualification(qualification(emulation=True), 123, "e" * 64) is True


@pytest.mark.parametrize("emulation", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 1),
        ("schema_version", True),
        ("extra", 1),
        ("native_elf_exit", 42.0),
        ("cgroup_inode", 124),
        ("source_sha256", "0" * 64),
        ("foreign_elf_mode", "qemu"),
        ("foreign_elf_mode", True),
        ("foreign_elf_mode", []),
        ("io_uring_setup_rejection", []),
        ("io_uring_setup_rejection", "EINVAL"),
    ],
)
def test_qualification_shared_fields_remain_exact_and_source_bound(emulation, field, value):
    proof = qualification(emulation=emulation)
    proof[field] = value
    with pytest.raises(VerificationFailure, match="target_qualification_invalid"):
        host.validate_qualification(proof, 123, "e" * 64)


@pytest.mark.parametrize(
    "emulation,field,value",
    [
        (False, "foreign_elf_rejection", None),
        (False, "foreign_elf_rejection", 42),
        (False, "native_emulation", {}),
        (False, "native_emulation", emulation_proof()),
        (True, "foreign_elf_rejection", "ENOEXEC"),
        (True, "foreign_elf_rejection", "SIGSYS"),
        (True, "native_emulation", None),
        (True, "native_emulation", True),
        (True, "native_emulation", 42),
        (True, "native_emulation", {}),
    ],
)
def test_qualification_discriminated_variants_cannot_be_mixed_or_reduced_to_exit42(
    emulation, field, value
):
    proof = qualification(emulation=emulation)
    proof[field] = value
    with pytest.raises(VerificationFailure):
        host.validate_qualification(proof, 123, "e" * 64)


@pytest.mark.parametrize("field", tuple(emulation_proof()))
@pytest.mark.parametrize("mutation", ["missing", "wrong_type", "wrong_value"])
def test_every_native_emulation_field_is_required_and_strict(field, mutation):
    proof = qualification(emulation=True)
    native = proof["native_emulation"]
    original = native[field]
    if mutation == "missing":
        del native[field]
    elif mutation == "wrong_type":
        native[field] = 1 if type(original) is bool else True if type(original) is int else 123
    else:
        native[field] = (
            False
            if type(original) is bool
            else original + 1
            if type(original) is int
            else "invalid"
        )
    with pytest.raises(VerificationFailure, match="emulation_qualification_invalid"):
        host.validate_qualification(proof, 123, "e" * 64)


@pytest.mark.parametrize("change", [{"extra": True}, {"host_elf_header_sha256": "A" * 64}])
def test_native_emulation_proof_rejects_extra_fields_and_noncanonical_header_hash(change):
    proof = qualification(emulation=True)
    proof["native_emulation"].update(change)
    with pytest.raises(VerificationFailure, match="emulation_qualification_invalid"):
        host.validate_qualification(proof, 123, "e" * 64)


@pytest.mark.parametrize(
    "suite,offset", [("backend", 0), ("frontend", 0), ("browser", 3), ("native", 0)]
)
def test_emulation_health_phase_is_additional_and_never_an_ordinary_command_allowance(
    suite, offset
):
    summary = client.phase_summary(counters(), counters(external=1, unix=1))
    host.validate_phase(summary, offset + 4, suite, emulation=True)
    with pytest.raises(VerificationFailure, match="rejection_mismatch"):
        host.validate_phase(summary, offset + 4, suite, emulation=False)
    with pytest.raises(VerificationFailure, match="rejection_mismatch"):
        host.validate_phase(summary, offset + 5, suite, emulation=True)
    host.validate_phase(
        client.phase_summary(counters(), counters()), offset + 5, suite, emulation=True
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"external": 0, "unix": 1},
        {"external": 2, "unix": 1},
        {"external": 1, "unix": 0},
        {"external": 1, "unix": 2},
        {"external": 1, "unix": 1, "filter_changes": 1},
        *(
            {"external": 1, "unix": 1, field: 1}
            for field in (
                "unclassified",
                "read_error",
                "vector_error",
                "regs_read_error",
                "control_error",
                "control_read_error",
                "control_state_error",
            )
        ),
    ],
)
def test_emulation_control_never_waives_missed_external_unix_or_error_evidence(changes):
    summary = client.phase_summary(counters(), counters(**changes))
    with pytest.raises(VerificationFailure):
        host.validate_phase(summary, 4, "backend", emulation=True)


@pytest.mark.parametrize("suite", tuple(offline.SUITES))
def test_emulation_inventory_inserts_exactly_one_fixed_control_before_ordinary_suite(suite):
    ordinary = offline.audit_phase_commands(suite)
    emulated = offline.audit_phase_commands(suite, emulation=True)
    index = (3 if suite == "browser" else 0) + 4
    expected = (client.sys.executable, "-m", "scripts.ac_15_qemu_control")
    assert emulated == (*ordinary[:index], expected, *ordinary[index:])
    assert ordinary[index:] == offline.SUITES[suite]
    assert emulated[index + 1 :] == offline.SUITES[suite]


@pytest.mark.parametrize("emulation", [False, True])
def test_host_cannot_skip_or_invent_optional_control_based_on_target_command_claim(
    tmp_path, monkeypatch, emulation
):
    session = make_host(tmp_path)
    transcript = host_transcript("backend", emulation=emulation)
    begin = next(
        value for _, event, value in transcript if event == "BEGIN" and value["phase"] == 5
    )
    # In emulation mode try to omit the extra control; in rejected mode try to
    # introduce it. READY qualification, not the target's next claim, decides.
    command = (
        offline.SUITES["backend"][0]
        if emulation
        else (client.sys.executable, "-m", "scripts.ac_15_qemu_control")
    )
    begin["command_sha256"] = client.command_digest(command)
    actions, _ = wire_host(session, monkeypatch, transcript)
    with pytest.raises(VerificationFailure, match="phase_inventory_mismatch"):
        session.run()
    assert not session.evidence["ok"]
    assert ("send", "target", {"event": "BEGIN", "phase": 5}) not in actions


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "hash",
        "proof_type",
        "errno",
        "unix",
        "summary",
        "summary_type",
        "extra",
        "qualification",
    ],
)
def test_host_binds_returned_emulation_control_to_initial_proof_and_observed_phase(
    tmp_path, monkeypatch, fault
):
    session = make_host(tmp_path)
    transcript = host_transcript("backend", emulation=True)
    result = transcript[-1][2]
    kernel = result["kernel"]
    control = kernel["native_emulation_control"]
    if fault == "missing":
        del kernel["native_emulation_control"]
    elif fault == "hash":
        control["native_emulation"]["host_elf_header_sha256"] = "0" * 64
    elif fault == "proof_type":
        control["native_emulation"]["guest_elf_class"] = True
    elif fault == "errno":
        control["external_errno"] = 1
    elif fault == "unix":
        control["unix_connected"] = 1
    elif fault == "summary":
        control["network_syscalls"]["sha256"] = "0" * 64
    elif fault == "summary_type":
        control["network_syscalls"]["counters"]["external"] = True
    elif fault == "extra":
        control["extra"] = 1
    else:
        kernel["kernel_observer"]["qualification"]["schema_version"] = 2.0
    wire_host(session, monkeypatch, transcript)
    with pytest.raises(
        VerificationFailure, match=r"(?:emulation_control|suite_qualification)_mismatch"
    ):
        session.run()
    assert not session.evidence["ok"]


def test_rejected_abi_mode_cannot_claim_an_emulation_control_in_final_result(tmp_path, monkeypatch):
    session = make_host(tmp_path)
    transcript = host_transcript("backend")
    transcript[-1][2]["kernel"]["native_emulation_control"] = {}
    wire_host(session, monkeypatch, transcript)
    with pytest.raises(VerificationFailure, match="unexpected_emulation_control"):
        session.run()
    assert not session.evidence["ok"]
