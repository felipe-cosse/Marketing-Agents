"""Fake-only VMA diagnostic tests: never create mappings, Docker, or BPF hooks."""

import gzip
import hashlib
import json
import subprocess
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import ac_15_vma_diagnostic as diagnostic
from scripts.ac_15_kernel_client import canonical, command_digest, counter_names, phase_summary
from scripts.del_05_clean_state import VerificationFailure
from tests.tooling import test_ac_15_kernel_protocol as protocol

SOURCE = protocol.SOURCE
IMAGE = "sha256:" + "f" * 64
MANIFEST = {**SOURCE, "files": {"scripts/ac_15_kernel_boundary.py": {"sha256": "e" * 64}}}
CONFIGURATION = {
    "release": "7.0.14-test",
    "config_sha256": "1" * 64,
    "anon_vma_name": "disabled",
    "reason": None,
}
READ_KERNEL_CONFIGURATION = diagnostic.kernel_configuration


@pytest.fixture(autouse=True)
def no_native_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("native_or_docker_execution_forbidden")

    monkeypatch.setattr(diagnostic.ctypes, "CDLL", forbidden)
    monkeypatch.setattr(diagnostic.subprocess, "run", forbidden)
    monkeypatch.setattr(diagnostic.subprocess, "Popen", forbidden)
    monkeypatch.setattr(diagnostic.boundary, "qualify_target", forbidden)
    monkeypatch.setattr(diagnostic, "bounded_read", forbidden)
    monkeypatch.setattr(diagnostic, "kernel_configuration", lambda: deepcopy(CONFIGURATION))


def fake_runner(tmp_path):
    commands = []

    def command(name, argv, **options):
        commands.append((name, argv, options))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    return SimpleNamespace(
        owner="unit-test",
        root=tmp_path,
        environment={},
        report={},
        kernel_sessions=[],
        command=command,
        commands=commands,
    )


def observation(case="untouched", error=0):
    return {
        "case": case,
        "return_code": -1 if error else 0,
        "errno": error,
        "mappings_released": True,
        "kernel_configuration": deepcopy(CONFIGURATION),
    }


def fake_configuration(monkeypatch, raw, *, compressed=None):
    calls = []

    def read(path, maximum):
        calls.append((path, maximum))
        return gzip.compress(raw, mtime=0) if compressed is None else compressed

    monkeypatch.setattr(diagnostic.platform, "release", lambda: "7.0.14-test")
    monkeypatch.setattr(diagnostic, "bounded_read", read)
    result = READ_KERNEL_CONFIGURATION()
    assert calls == [("/proc/config.gz", 2 * 1024**2)]
    diagnostic.validate_configuration(result)
    return result


@pytest.mark.parametrize(
    "raw,state,reason",
    [
        (b"CONFIG_ANON_VMA_NAME=y\n", "enabled", None),
        (b"# CONFIG_ANON_VMA_NAME is not set\n", "disabled", None),
        (b"CONFIG_OTHER=y\n", "unknown", "missing_symbol"),
        (b"CONFIG_ANON_VMA_NAME_EXTRA=y\n", "unknown", "missing_symbol"),
        (b"CONFIG_ANON_VMA_NAME=y\n" * 2, "unknown", "duplicate_symbol"),
        (
            b"CONFIG_ANON_VMA_NAME=y\n# CONFIG_ANON_VMA_NAME is not set\n",
            "unknown",
            "duplicate_symbol",
        ),
        (b"CONFIG_ANON_VMA_NAME=n\n", "unknown", "invalid_symbol"),
        (b"CONFIG_ANON_VMA_NAME=m\n", "unknown", "invalid_symbol"),
        (b" CONFIG_ANON_VMA_NAME=y\n", "unknown", "invalid_symbol"),
        (b"# CONFIG_ANON_VMA_NAME not set\n", "unknown", "invalid_symbol"),
    ],
)
def test_live_configuration_exact_symbol_and_raw_config_hash(monkeypatch, raw, state, reason):
    result = fake_configuration(monkeypatch, raw)
    assert result == {
        "release": "7.0.14-test",
        "config_sha256": hashlib.sha256(raw).hexdigest(),
        "anon_vma_name": state,
        "reason": reason,
    }


@pytest.mark.parametrize(
    "failure,reason",
    [
        (FileNotFoundError, "config_missing"),
        (PermissionError, "config_denied"),
        (OSError, "config_unavailable"),
        (ValueError, "compressed_bound"),
    ],
)
def test_live_configuration_unavailable_is_unknown_and_sanitized(monkeypatch, failure, reason):
    calls = []

    def read(path, maximum):
        calls.append((path, maximum))
        raise failure("do-not-disclose-config-path-or-content")

    monkeypatch.setattr(diagnostic.platform, "release", lambda: "7.0.14-test")
    monkeypatch.setattr(diagnostic, "bounded_read", read)
    result = READ_KERNEL_CONFIGURATION()
    assert calls == [("/proc/config.gz", 2 * 1024**2)]
    assert result == {
        "release": "7.0.14-test",
        "config_sha256": None,
        "anon_vma_name": "unknown",
        "reason": reason,
    }
    diagnostic.validate_configuration(result)


@pytest.mark.parametrize("release", ["7.0.14\nSECRET", "x" * 129, "", None, True, OSError])
def test_live_configuration_invalid_release_does_not_read_config(monkeypatch, release):
    def read_release():
        if release is OSError:
            raise OSError("do-not-disclose")
        return release

    monkeypatch.setattr(diagnostic.platform, "release", read_release)
    # The autouse guard rejects any config read in this branch.
    result = READ_KERNEL_CONFIGURATION()
    assert result == {
        "release": None,
        "config_sha256": None,
        "anon_vma_name": "unknown",
        "reason": "release_unavailable",
    }
    diagnostic.validate_configuration(result)


@pytest.mark.parametrize("fault", ["invalid", "truncated", "concatenated", "trailing", "text"])
def test_live_configuration_invalid_compression_or_text_is_unknown(monkeypatch, fault):
    raw = b"CONFIG_ANON_VMA_NAME=y\n"
    compressed = gzip.compress(raw, mtime=0)
    if fault == "invalid":
        compressed = b"not-gzip"
    elif fault == "truncated":
        compressed = compressed[:-1]
    elif fault == "concatenated":
        compressed += gzip.compress(raw, mtime=0)
    elif fault == "trailing":
        compressed += b"trailing-content"
    else:
        compressed = gzip.compress(raw + b"\xff", mtime=0)
    result = fake_configuration(monkeypatch, raw, compressed=compressed)
    assert result["anon_vma_name"] == "unknown"
    assert result["config_sha256"] is None
    assert result["reason"] == ("invalid_config_text" if fault == "text" else "invalid_gzip")


@pytest.mark.parametrize("excess", [0, 1, 100000])
def test_live_configuration_decompression_cannot_exceed_bound(monkeypatch, excess):
    raw = b"CONFIG_ANON_VMA_NAME=y\n"
    monkeypatch.setattr(diagnostic, "MAX_CONFIG_DECOMPRESSED", len(raw))
    result = fake_configuration(monkeypatch, raw + b"#" * excess)
    if excess:
        assert result["anon_vma_name"] == "unknown"
        assert result["config_sha256"] is None
        assert result["reason"] == "decompressed_bound"
    else:
        assert result["anon_vma_name"] == "enabled"
        assert result["config_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "key,value",
    [
        ("release", None),
        ("release", "7.0.14\nSECRET"),
        ("release", True),
        ("config_sha256", None),
        ("config_sha256", "A" * 64),
        ("config_sha256", "1" * 63),
        ("config_sha256", False),
        ("anon_vma_name", "unknown"),
        ("anon_vma_name", "yes"),
        ("anon_vma_name", []),
        ("reason", "config_missing"),
        ("reason", []),
        ("raw_config", "CONFIG_ANON_VMA_NAME=y"),
        ("path", "/proc/config.gz"),
    ],
)
def test_configuration_projection_rejects_invalid_known_metadata(key, value):
    body = deepcopy(CONFIGURATION)
    body[key] = value
    with pytest.raises(VerificationFailure, match="config_invalid"):
        diagnostic.validate_configuration(body)


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": None},
        {"reason": "not_fixed_reason"},
        {"reason": "config_missing", "config_sha256": "1" * 64},
        {"reason": "release_unavailable", "release": "7.0.14-test"},
        {"reason": "config_denied", "release": None},
        {"reason": "missing_symbol"},
    ],
)
def test_configuration_unknown_requires_consistent_evidence(changes):
    body = {**CONFIGURATION, "anon_vma_name": "unknown", "config_sha256": None, **changes}
    with pytest.raises(VerificationFailure, match="config_invalid"):
        diagnostic.validate_configuration(body)


@pytest.mark.parametrize("key", ["release", "config_sha256", "anon_vma_name", "reason"])
def test_probe_requires_complete_configuration_metadata(key):
    body = observation()
    del body["kernel_configuration"][key]
    with pytest.raises(VerificationFailure, match="config_invalid"):
        diagnostic.validate_probe(body, "untouched")


def test_probe_cannot_omit_live_configuration_metadata():
    body = observation()
    del body["kernel_configuration"]
    with pytest.raises(VerificationFailure, match="probe_invalid"):
        diagnostic.validate_probe(body, "untouched")


@pytest.mark.parametrize("case", diagnostic.CASES)
@pytest.mark.parametrize("error", [0, 14, 22, 38])
def test_probe_makes_one_fixed_native_call_and_unmaps_only_owned_pages(monkeypatch, case, error):
    calls = []
    addresses = iter((0x1000, 0x2000))

    def function(name, action):
        def execute(*args):
            calls.append((name, args))
            return action(*args)

        return execute

    libc = SimpleNamespace(
        mmap=function("mmap", lambda *args: next(addresses)),
        munmap=function("munmap", lambda *args: 0),
        syscall=function("syscall", lambda *args: -1 if error else 0),
    )
    monkeypatch.setattr(diagnostic, "inherited_profile", lambda: calls.append(("profile",)))
    monkeypatch.setattr(diagnostic.os, "sysconf", lambda key: 4096)
    monkeypatch.setattr(diagnostic.ctypes, "CDLL", lambda *args, **kwargs: libc)
    monkeypatch.setattr(diagnostic.ctypes, "set_errno", lambda value: None)
    monkeypatch.setattr(diagnostic.ctypes, "get_errno", lambda: error)

    def read(address):
        calls.append(("read", address))
        return SimpleNamespace(value=0)

    monkeypatch.setattr(diagnostic.ctypes, "c_ubyte", SimpleNamespace(from_address=read))

    def configuration():
        assert [item for item in calls if item[0] == "munmap"] == [
            ("munmap", (0x2000, 4096)),
            ("munmap", (0x1000, 4096)),
        ]
        return deepcopy(CONFIGURATION)

    monkeypatch.setattr(diagnostic, "kernel_configuration", configuration)
    result = diagnostic.probe(case)
    assert result == observation(case, error)
    assert calls[0] == ("profile",)
    assert [item for item in calls if item[0] == "syscall"] == [
        ("syscall", (167, 0x53564D41, 0, 0x1000, 4096, 0x2000))
    ]
    assert [item for item in calls if item[0] == "munmap"] == [
        ("munmap", (0x2000, 4096)),
        ("munmap", (0x1000, 4096)),
    ]
    assert [item for item in calls if item[0] == "read"] == (
        [("read", 0x2000)] if case == "pre_read" else []
    )
    assert [item[1][2] for item in calls if item[0] == "mmap"] == [
        3,
        0 if case == "inaccessible" else 3,
    ]
    assert "4096" not in canonical(result).decode() and "8192" not in canonical(result).decode()


@pytest.mark.parametrize("fault", ["second_mapping", "syscall", "first_unmap"])
def test_probe_cleanup_runs_on_mapping_or_call_failures(monkeypatch, fault):
    mappings, unmapped = [], []

    def mmap(*args):
        if mappings and fault == "second_mapping":
            return diagnostic.ctypes.c_void_p(-1).value
        result = 0x1000 * (len(mappings) + 1)
        mappings.append(result)
        return result

    def munmap(address, size):
        unmapped.append(address)
        return -1 if fault == "first_unmap" and len(unmapped) == 1 else 0

    def syscall(*args):
        if fault == "syscall":
            raise OSError("synthetic")
        return 0

    libc = SimpleNamespace(mmap=mmap, munmap=munmap, syscall=syscall)
    monkeypatch.setattr(diagnostic, "inherited_profile", lambda: None)
    monkeypatch.setattr(diagnostic.os, "sysconf", lambda key: 4096)
    monkeypatch.setattr(diagnostic.ctypes, "CDLL", lambda *args, **kwargs: libc)
    monkeypatch.setattr(diagnostic.ctypes, "get_errno", lambda: 0)
    with pytest.raises((VerificationFailure, OSError)):
        diagnostic.probe("untouched")
    assert unmapped == list(reversed(mappings))


def test_complete_manifest_is_not_limited_by_protocol_line_size(monkeypatch):
    manifest = {"files": {"synthetic-" + str(index): "x" * 100 for index in range(1000)}}
    raw = json.dumps(manifest).encode()
    assert len(raw) > 65536
    calls = []

    def read(path, limit):
        calls.append((path, limit))
        return raw

    monkeypatch.setattr(diagnostic, "bounded_read", read)
    assert diagnostic.read_manifest() == manifest
    assert calls == [("/opt/ac15/source-manifest.json", 16 * 1024**2)]


@pytest.mark.parametrize(
    "key,value",
    [
        ("case", "other"),
        ("return_code", True),
        ("errno", False),
        ("errno", 4096),
        ("errno", -1),
        ("errno", 14),
        ("mappings_released", False),
        ("address", "hidden"),
    ],
)
def test_probe_projection_rejects_malformed_or_address_fields(key, value):
    body = observation()
    body[key] = value
    with pytest.raises(VerificationFailure, match="probe_invalid"):
        diagnostic.validate_probe(body, "untouched")


@pytest.mark.parametrize("case", diagnostic.CASES)
def test_argv_adapter_preserves_existing_boundary_flags_and_collector(tmp_path, case):
    runner = fake_runner(tmp_path)
    session = diagnostic.DiagnosticHost(runner, IMAGE, MANIFEST, case)
    prefix = [
        "docker",
        "create",
        "--name",
        session.records["target"]["name"],
        "--network",
        "none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        "10001:10001",
    ]
    session.command("kernel-create-target", [*prefix, IMAGE, "--suite", session.suite])
    assert runner.commands[0][1] == [
        *prefix,
        "--entrypoint",
        "python",
        IMAGE,
        "-m",
        diagnostic.MODULE,
        "inside",
        "--case",
        case,
    ]
    collector = ["docker", "create", "--read-only", "--cap-add=BPF", "--cap-add=PERFMON"]
    session.command("kernel-create-collector", collector)
    assert runner.commands[1][1] == collector
    assert diagnostic.offline.SUITES.keys() == {"backend", "frontend", "browser", "native"}


@pytest.mark.parametrize("fault", ["suffix", "entrypoint", "name", "verb"])
def test_argv_adapter_rejects_changed_launch_contract(tmp_path, fault):
    runner = fake_runner(tmp_path)
    session = diagnostic.DiagnosticHost(runner, IMAGE, MANIFEST, "untouched")
    argv = [
        "docker",
        "create",
        "--name",
        session.records["target"]["name"],
        IMAGE,
        "--suite",
        session.suite,
    ]
    if fault == "suffix":
        argv[-1] = "native"
    elif fault == "entrypoint":
        argv[2:2] = ["--entrypoint", "sh"]
    elif fault == "name":
        argv[3] = "foreign-container"
    else:
        argv[1] = "run"
    with pytest.raises(VerificationFailure, match="launch_changed"):
        session.command("kernel-create-target", argv)
    assert not runner.commands


def phase(error=False):
    values = protocol.counters(
        entry_prctl=1,
        **(
            {"control_read_error": 1, "control_read_vma_name_efault": 1}
            if error
            else {"safe_prctl": 1}
        ),
    )
    return phase_summary(dict.fromkeys(counter_names(), 0), values)


@pytest.mark.parametrize("error", [False, True])
def test_original_strict_rejection_is_never_waived(error):
    result = diagnostic.strict_observation(phase(error))
    assert result == ("kernel_unclassified_or_incomplete_phase" if error else None)
    assert phase(error)["rejected"] == int(error)
    diagnostic.validate_one_call(phase(error))


@pytest.mark.parametrize(
    "extra",
    [
        {"entry_prctl": 2},
        {"entry_seccomp": 1},
        {"external": 1},
        {"control_queries": 1},
        {"filter_changes": 1},
        {"safe_prctl": 0},
    ],
)
def test_unrelated_activity_makes_diagnostic_attribution_incomplete(extra):
    summary = phase()
    summary["counters"].update(extra)
    with pytest.raises(VerificationFailure, match="call_projection"):
        diagnostic.validate_one_call(summary)


@pytest.mark.parametrize("error", [False, True])
def test_host_lifecycle_keeps_rejected_target_held_and_never_claims_acceptance(
    tmp_path, monkeypatch, error
):
    runner = fake_runner(tmp_path)
    session = diagnostic.DiagnosticHost(runner, IMAGE, MANIFEST, "untouched")
    summary = phase(error)
    ready = {
        "event": "READY",
        "suite": session.suite,
        "cgroup_inode": 123,
        "source": SOURCE,
        "qualification": protocol.qualification(),
    }
    result = diagnostic.result_body("untouched", MANIFEST)
    result.update(
        diagnostic_completed=True,
        probe=observation(error=14 if error else 0),
        summary=summary,
        strict_failure=diagnostic.strict_observation(summary),
        qualification=ready["qualification"],
    )
    transcript = [
        ("target", "READY", ready),
        ("collector", "READY", protocol.ready()),
        ("collector", "SNAPSHOT", protocol.snapshot(2)),
        (
            "target",
            "BEGIN",
            {
                "event": "BEGIN",
                "phase": 1,
                "command_sha256": command_digest(diagnostic.probe_command("untouched")),
                "quiescence": protocol.quiescence(),
            },
        ),
        ("collector", "SNAPSHOT", protocol.snapshot(3)),
        ("target", "END", {"event": "END", "phase": 1, "quiescence": protocol.quiescence()}),
        ("collector", "SNAPSHOT", protocol.snapshot(4, counters=summary["counters"])),
    ]
    if not error:
        transcript += [
            ("target", "HOLD", {"event": "HOLD", "phase": 1, "quiescence": protocol.quiescence()}),
            ("collector", "DONE", protocol.snapshot(5, "DONE", counters=summary["counters"])),
        ]
    transcript.append(("target", "RESULT", result))
    actions, events = protocol.wire_host(session, monkeypatch, transcript)
    session.run()
    assert list(events) == []
    assert session.evidence["ok"] is False
    assert session.evidence["acceptance"] is False
    assert session.evidence["diagnostic_completed"] is True
    assert session.evidence["result"]["ok"] is False
    assert (("send", "collector", "STOP") in actions) is not error
    assert (("send", "target", {"event": "RELEASE"}) in actions) is not error


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_three_fresh_sessions_register_before_run_and_share_cleanup_budget(
    tmp_path, monkeypatch, cleanup_ok
):
    runner = fake_runner(tmp_path)
    clock = [0.0]
    deadlines = []
    cases = []

    class Session:
        def __init__(self, parent, image, manifest, case):
            self.case, self.evidence, self.cleaned = case, {}, None
            self.records = {"target": {"attempted": True}, "collector": {"attempted": True}}

        def run(self):
            assert runner.kernel_sessions[-1] is self
            cases.append(self.case)
            clock[0] += 5

        def cleanup(self, deadline):
            deadlines.append(deadline - clock[0])
            clock[0] += 2
            self.cleaned = {"ok": cleanup_ok}
            return self.cleaned

    monkeypatch.setattr(diagnostic, "DiagnosticHost", Session)
    monkeypatch.setattr(diagnostic.time, "monotonic", lambda: clock[0])
    if cleanup_ok:
        diagnostic.run_diagnostics(runner, IMAGE, MANIFEST)
        assert cases == list(diagnostic.CASES)
        assert deadlines == [30, 28, 26]
        assert runner.report["diagnostic_completed"] is True
    else:
        with pytest.raises(VerificationFailure, match="cleanup_failed"):
            diagnostic.run_diagnostics(runner, IMAGE, MANIFEST)
        assert cases == ["untouched"]
        assert runner.report["cleanup"]["ok"] is False


@pytest.mark.parametrize("case", [None, "native", "../path", "", 1, True])
def test_no_arbitrary_diagnostic_case_or_program(case):
    with pytest.raises(VerificationFailure, match="case_invalid"):
        diagnostic.probe_command(case)


def test_source_bound_result_rejects_metadata_tampering(tmp_path):
    runner = fake_runner(tmp_path)
    host = diagnostic.DiagnosticHost(runner, IMAGE, MANIFEST, "untouched")
    ready, summary = {"qualification": protocol.qualification()}, phase()
    result = diagnostic.result_body("untouched", MANIFEST)
    result.update(
        diagnostic_completed=True,
        probe=observation(),
        summary=summary,
        qualification=ready["qualification"],
        strict_failure=None,
    )
    host.validate_result(result, ready, summary, None)
    for key, value in (
        ("ok", True),
        ("acceptance", True),
        ("source_commit", "0" * 40),
        ("diagnostic_completed", 1),
        ("extra", "address"),
    ):
        changed = deepcopy(result)
        changed[key] = value
        with pytest.raises(VerificationFailure, match="result_unbound"):
            host.validate_result(changed, ready, summary, None)


@pytest.mark.parametrize("mode", ["clean", "rejected", "begin_failure", "probe_failure"])
def test_target_preserves_source_checks_and_failed_hold_even_before_attached(
    monkeypatch, capsys, mode
):
    clock = [0.0]
    events = []
    monkeypatch.setattr(diagnostic.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        assert 0 <= seconds <= 0.1
        clock[0] += seconds
        events.append("hold")

    monkeypatch.setattr(diagnostic.time, "sleep", sleep)
    monkeypatch.setattr(diagnostic, "verification_signals", lambda seconds: nullcontext())
    monkeypatch.setattr(diagnostic, "read_manifest", lambda: MANIFEST)
    monkeypatch.setattr(diagnostic.offline, "verify_source", lambda *args: events.append("source"))
    monkeypatch.setattr(diagnostic.offline, "kernel_namespace", lambda: {})
    for key, value in diagnostic.offline.SAFE_ENV.items():
        monkeypatch.setenv(key, value)
    for key, value in SOURCE.items():
        monkeypatch.setenv("AC15_" + key.upper(), value)

    class Observer:
        started = False
        closed = False

        def __init__(self, suite, deadline):
            assert suite == "diagnostic-vma-untouched"
            assert deadline == diagnostic.TARGET_SECONDS - 10

        def begin(self, manifest):
            events.append("begin")
            if mode == "begin_failure":
                raise VerificationFailure("synthetic_begin_failure")
            self.started = True
            return {"qualification": protocol.qualification()}

        def collect(self, command, timeout):
            events.append("collect")
            assert command == diagnostic.probe_command("untouched")
            assert timeout == diagnostic.PROBE_SECONDS
            if mode == "probe_failure":
                raise VerificationFailure("synthetic_probe_failure")
            return subprocess.CompletedProcess(command, 0, canonical(observation()), b""), phase(
                mode == "rejected"
            )

        def finish(self):
            assert mode == "clean"
            events.append("finish")
            self.closed = True

    monkeypatch.setattr(diagnostic, "KernelAuditClient", Observer)
    assert diagnostic.inside("untouched") == (0 if mode == "clean" else 1)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith(diagnostic.offline.PREFIX)
    body = json.loads(lines[0][len(diagnostic.offline.PREFIX) :])
    assert body["ok"] is False and body["acceptance"] is False
    assert body["diagnostic_completed"] is (mode in ("clean", "rejected"))
    assert events.count("source") == (2 if mode in ("clean", "rejected") else 1)
    assert ("finish" in events) is (mode == "clean")
    if mode != "clean":
        assert "hold" in events and clock[0] >= diagnostic.TARGET_SECONDS
