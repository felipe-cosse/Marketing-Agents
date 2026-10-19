"""AC-18 fake-only VMA diagnostics: never create mappings, Docker, or BPF hooks."""

import json
import subprocess
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ac_15_vma_diagnostic as diagnostic
from scripts.ac_15_kernel_client import canonical, command_digest, counter_names, phase_summary
from scripts.del_05_clean_state import VerificationFailure
from tests.tooling import test_ac_15_kernel_protocol as protocol

SOURCE = protocol.SOURCE
IMAGE = "sha256:" + "f" * 64
MANIFEST = {**SOURCE, "files": {"scripts/ac_15_kernel_boundary.py": {"sha256": "e" * 64}}}
CONFIGURATION = protocol.kernel_configuration()


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


def observation(case="untouched", error=22):
    return {
        "case": case,
        "return_code": -1 if error else 0,
        "errno": error,
        "mappings_released": True,
        "kernel_configuration": deepcopy(CONFIGURATION),
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("release", None),
        ("release", "7.0.14\\nSECRET"),
        ("release", True),
        ("config_sha256", None),
        ("config_sha256", "A" * 64),
        ("config_sha256", "1" * 63),
        ("config_sha256", False),
        ("anon_vma_name", "unknown"),
        ("anon_vma_name", "enabled"),
        ("anon_vma_name", []),
        ("policy", "unknown"),
        ("source", "/boot/config"),
        ("reason", "missing_symbol"),
        ("raw_config", "CONFIG_ANON_VMA_NAME=y"),
        ("address", 4096),
    ],
)
def test_probe_requires_current_strict_configuration_projection(key, value):
    body = observation()
    body["kernel_configuration"][key] = value
    with pytest.raises(VerificationFailure, match="kernel_configuration_unqualified"):
        diagnostic.validate_probe(body, "untouched")


@pytest.mark.parametrize("key", tuple(CONFIGURATION))
def test_probe_requires_complete_configuration_metadata(key):
    body = observation()
    del body["kernel_configuration"][key]
    with pytest.raises(VerificationFailure, match="kernel_configuration_unqualified"):
        diagnostic.validate_probe(body, "untouched")


def test_probe_cannot_omit_live_configuration_metadata():
    body = observation()
    del body["kernel_configuration"]
    with pytest.raises(VerificationFailure, match="probe_invalid"):
        diagnostic.validate_probe(body, "untouched")


@pytest.mark.parametrize("case", diagnostic.CASES)
@pytest.mark.parametrize("error", [22])
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
        vma_unsupported_started=1,
        vma_unsupported_completed=1,
        **(
            {"control_read_error": 1, "control_read_vma_exit_result_efault": 1}
            if error
            else {"safe_prctl": 1}
        ),
    )
    return phase_summary(dict.fromkeys(counter_names(), 0), values)


@pytest.mark.parametrize("error", [False, True])
def test_original_strict_rejection_is_never_waived(error):
    result = diagnostic.strict_observation(phase(error), CONFIGURATION)
    assert result == ("kernel_unclassified_or_incomplete_phase" if error else None)
    assert phase(error)["rejected"] == int(error)
    if error:
        with pytest.raises(VerificationFailure, match="call_projection"):
            diagnostic.validate_one_call(phase(error))
    else:
        diagnostic.validate_one_call(phase())


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
        diagnostic_completed=not error,
        probe=observation(),
        summary=summary,
        strict_failure=diagnostic.strict_observation(summary, CONFIGURATION),
        qualification=ready["qualification"],
        kernel_configuration=CONFIGURATION,
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
    assert session.evidence["diagnostic_completed"] is not error
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
            self.evidence["diagnostic_completed"] = True

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
        kernel_configuration=CONFIGURATION,
        strict_failure=None,
    )
    host.validate_result(result, ready, summary, None, CONFIGURATION)
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
            host.validate_result(changed, ready, summary, None, CONFIGURATION)


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
            return {
                "qualification": protocol.qualification(),
                "kernel_configuration": CONFIGURATION,
            }

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
    assert body["diagnostic_completed"] is (mode == "clean")
    assert events.count("source") == (2 if mode in ("clean", "rejected") else 1)
    assert ("finish" in events) is (mode == "clean")
    if mode != "clean":
        assert "hold" in events and clock[0] >= diagnostic.TARGET_SECONDS


@pytest.mark.parametrize("error", [0, 14, 38, True, -22, 2**32 + 22])
def test_only_exact_einval_can_be_reported_for_disabled_vma(error):
    body = observation(error=error)
    with pytest.raises(VerificationFailure, match="probe_invalid"):
        diagnostic.validate_probe(body, "untouched")


@pytest.mark.parametrize("field", ["vma_unsupported_started", "vma_unsupported_completed"])
@pytest.mark.parametrize("count", [0, 2, True, -1, 2**63])
def test_paired_vma_accounting_cannot_be_missing_or_changed(field, count):
    summary = phase()
    summary["counters"][field] = count
    with pytest.raises(VerificationFailure):
        diagnostic.strict_observation(summary, CONFIGURATION)


@pytest.mark.parametrize(
    "change",
    [
        {"anon_vma_name": "enabled"},
        {"anon_vma_name": "unknown"},
        {"source": "/boot/config"},
        {"policy": "read_names"},
        {"config_sha256": None},
    ],
)
def test_strict_observation_never_accepts_unqualified_kernel_configuration(change):
    assert (
        diagnostic.strict_observation(phase(), {**CONFIGURATION, **change})
        == "kernel_configuration_unqualified"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "0" * 64},
        {"rejected": 0},
        {"extra": "secret"},
    ],
)
def test_summary_digest_or_projection_tampering_is_not_evidence(change):
    summary = phase(error=True)
    summary.update(change)
    with pytest.raises(VerificationFailure, match="summary_invalid"):
        diagnostic.strict_observation(summary, CONFIGURATION)


@pytest.mark.parametrize("field", ["release", "config_sha256"])
@pytest.mark.parametrize("location", ["kernel_configuration", "probe"])
def test_post_probe_config_is_bound_to_attached_collector(tmp_path, field, location):
    host = diagnostic.DiagnosticHost(fake_runner(tmp_path), IMAGE, MANIFEST, "untouched")
    ready = {"qualification": protocol.qualification()}
    result = diagnostic.result_body("untouched", MANIFEST)
    result.update(
        diagnostic_completed=True,
        probe=observation(),
        summary=phase(),
        strict_failure=None,
        qualification=ready["qualification"],
        kernel_configuration=deepcopy(CONFIGURATION),
    )
    configuration = result[location]
    if location == "probe":
        configuration = configuration["kernel_configuration"]
    configuration[field] = "different-kernel" if field == "release" else "0" * 64
    with pytest.raises(VerificationFailure, match=r"unbound|config_changed"):
        host.validate_result(result, ready, phase(), None, CONFIGURATION)


@pytest.mark.parametrize("role", ["target", "collector"])
@pytest.mark.parametrize("fault", ["identity", "image", "owner", "network", "capabilities"])
def test_diagnostic_inherits_strict_owned_container_boundary(tmp_path, role, fault):
    host = diagnostic.DiagnosticHost(fake_runner(tmp_path), IMAGE, MANIFEST, "untouched")
    value = protocol.container_value(host, role)
    if fault == "identity":
        value["Name"] = "/foreign-container"
    elif fault == "image":
        value["Image"] = "sha256:" + "0" * 64
    elif fault == "owner":
        value["Config"]["Labels"][diagnostic.offline.LABEL + "owner"] = "foreign"
    elif fault == "network":
        value["HostConfig"]["NetworkMode"] = "default"
    else:
        value["HostConfig"]["CapAdd"] = ["CAP_SYS_ADMIN"]
    with pytest.raises(VerificationFailure):
        host.validate_container(value, role)


@pytest.mark.parametrize(
    "mode", ["complete", "rejected", "source_failure", "image_failure", "head_changed", "dirty"]
)
@pytest.mark.parametrize("explicit_image", [False, True])
def test_run_cli_uses_only_preacquired_source_bound_image(
    tmp_path, monkeypatch, capsys, mode, explicit_image
):
    events = []
    runner = diagnostic.offline.OfflineRun(tmp_path)

    def export(directory):
        events.append("export")
        assert directory.is_absolute() and not directory.is_relative_to(diagnostic.ROOT)
        if mode == "source_failure":
            raise VerificationFailure("exact_head_requires_clean_worktree")
        runner.report.update(SOURCE)
        return MANIFEST

    def image(identity, manifest):
        events.append("image")
        assert identity == IMAGE and manifest == MANIFEST
        if mode == "image_failure":
            raise VerificationFailure("image_source_mismatch")
        return IMAGE

    def resolve(manifest):
        events.append("resolve")
        return image(IMAGE, manifest)

    def run(parent, identity, manifest):
        events.append("run")
        assert parent is runner and identity == IMAGE and manifest == MANIFEST
        parent.report.update(diagnostic_completed=mode != "rejected", cleanup={"ok": True})

    def command(name, argv, **kwargs):
        events.append(name)
        output = SOURCE["source_commit"] if name == "unchanged-head" else ""
        if mode == "head_changed" and name == "unchanged-head":
            output = "0" * 40
        if mode == "dirty" and name == "unchanged-source":
            output = " M changed.py"
        return subprocess.CompletedProcess(argv, 0, output.encode(), b"")

    monkeypatch.setattr(diagnostic.offline, "OfflineRun", lambda root: runner)
    monkeypatch.setattr(runner, "export", export)
    monkeypatch.setattr(runner, "docker", lambda: events.append("docker"))
    monkeypatch.setattr(runner, "image", image)
    monkeypatch.setattr(runner, "resolve_image", resolve)
    monkeypatch.setattr(runner, "command", command)
    monkeypatch.setattr(diagnostic, "run_diagnostics", run)
    deadlines = []

    def signals(seconds):
        deadlines.append(seconds)
        return nullcontext()

    monkeypatch.setattr(diagnostic, "verification_signals", signals)
    report = tmp_path / "diagnostic-report.json"
    argv = ["run", "--report", str(report)]
    if explicit_image:
        argv += ["--image", IMAGE]
    assert diagnostic.main(argv) == (0 if mode == "complete" else 1)
    result = json.loads(report.read_text())
    assert result["ok"] is result["acceptance"] is False
    assert result["diagnostic_only"] is True
    assert result["diagnostic_completed"] is (mode == "complete")
    assert deadlines == [270]
    assert events[0] == "export"
    if mode == "source_failure":
        assert events == ["export"]
    else:
        assert ("resolve" in events) is not explicit_image
    if mode in ("source_failure", "image_failure"):
        assert "run" not in events
    assert "AC18 diagnostic report: " in capsys.readouterr().out


@pytest.mark.parametrize("fault", ["failed", "incomplete"])
def test_one_bad_case_prevents_success_despite_all_cleanup(tmp_path, monkeypatch, fault):
    runner = fake_runner(tmp_path)
    cases = []

    class Session:
        def __init__(self, parent, image, manifest, case):
            self.case, self.evidence, self.cleaned = case, {}, None
            self.records = {"target": {"attempted": True}, "collector": {"attempted": True}}

        def run(self):
            assert runner.kernel_sessions[-1] is self
            cases.append(self.case)
            self.evidence["diagnostic_completed"] = self.case != "pre_read"
            if self.case == "pre_read" and fault == "failed":
                raise OSError("do-not-leak-private-path-or-payload")

        def cleanup(self, deadline):
            self.cleaned = {"ok": True}
            return self.cleaned

    monkeypatch.setattr(diagnostic, "DiagnosticHost", Session)
    diagnostic.run_diagnostics(runner, IMAGE, MANIFEST)
    assert cases == list(diagnostic.CASES)
    assert runner.report["diagnostic_completed"] is False
    assert runner.report["cleanup"]["ok"] is True
    assert "do-not-leak" not in json.dumps([session.evidence for session in runner.kernel_sessions])


def test_default_report_is_external_and_exclusive(tmp_path, monkeypatch, capsys):
    runner = diagnostic.offline.OfflineRun(tmp_path)
    monkeypatch.setattr(diagnostic.offline, "OfflineRun", lambda root: runner)
    monkeypatch.setattr(diagnostic, "verification_signals", lambda seconds: nullcontext())
    monkeypatch.setattr(
        runner, "export", lambda directory: (_ for _ in ()).throw(OSError("SECRET"))
    )
    # Force tempfile output into the owned test directory without weakening the
    # production checks; neither source export nor report may enter the repository.
    monkeypatch.setattr(diagnostic.tempfile, "tempdir", str(tmp_path))
    assert diagnostic.main(["run"]) == 1
    output = capsys.readouterr().out
    report_path = Path(output.split("AC18 diagnostic report: ", 1)[1].strip())
    assert report_path.parent == tmp_path
    assert report_path.is_file()
    report = json.loads(report_path.read_text())
    assert report["failure"] == "OSError" and "SECRET" not in output


def test_default_report_never_writes_into_source_via_tempdir(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(diagnostic, "ROOT", source)
    monkeypatch.setattr(diagnostic.tempfile, "tempdir", str(source))
    monkeypatch.setattr(
        diagnostic.offline,
        "OfflineRun",
        lambda root: pytest.fail("source runner must not start with unsafe report location"),
    )
    with pytest.raises(VerificationFailure, match="report_must_be_external"):
        diagnostic.main(["run"])
    assert list(source.iterdir()) == []


@pytest.mark.parametrize("case", diagnostic.CASES)
def test_cases_have_exactly_one_fixed_probe_command_and_never_enter_suites(case):
    command = diagnostic.probe_command(case)
    assert command == (diagnostic.sys.executable, "-m", diagnostic.MODULE, "probe", "--case", case)
    assert tuple(diagnostic.offline.SUITES) == ("backend", "frontend", "browser", "native")
    assert all(
        diagnostic.MODULE not in item
        for suite in diagnostic.offline.SUITES.values()
        for item in suite
    )


def test_ac18_make_hook_is_diagnostic_only_and_preserves_four_full_suites():
    makefile = (diagnostic.ROOT / "Makefile").read_text()
    recipe = makefile.split("\ndiagnose-ac-15-vma:\n", 1)[1].splitlines()[0]
    assert recipe == (
        "\tPYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m scripts.ac_15_vma_diagnostic run "
        '$(if $(AC15_IMAGE),--image "$(AC15_IMAGE)",) '
        '$(if $(AC15_REPORT),--report "$(AC15_REPORT)",)'
    )
    assert "--suite" not in recipe and "acquire" not in recipe
    assert diagnostic.offline.SUITE_TIMEOUT == 1700
    assert diagnostic.offline.CONTAINER_TIMEOUT == 1720
    assert diagnostic.offline.HOST_TIMEOUT == 1740


def test_ac18_evidence_requires_all_suites_and_separate_bounded_diagnostic():
    manifest = json.loads(
        (diagnostic.ROOT / "docs/verification/requirements/AC-18.json").read_text()
    )
    gates = {gate["id"]: gate for gate in manifest["gates"]}
    assert len(gates) == 13
    for suite in ("backend", "frontend", "browser", "native"):
        gate = gates["ac-18-complete-offline-" + suite]
        assert gate["argv"] == [
            "python3",
            "-B",
            "scripts/ac_15_offline.py",
            "run",
            "--suite",
            suite,
        ]
        assert gate["timeout_seconds"] == 1800
        assert gate["network_requirement"] == "loopback_only"
    diagnostic_gate = gates["ac-18-live-source-bound-vma-diagnostic"]
    assert diagnostic_gate["argv"] == ["python3", "-B", "-m", "scripts.ac_15_vma_diagnostic", "run"]
    assert diagnostic_gate["timeout_seconds"] == 300
    assert diagnostic_gate["network_requirement"] == "loopback_only"
    assert gates["ac-18-exact-source-quality-audit"]["argv"] == [
        "python3",
        "-B",
        "-m",
        "scripts.ac_16_quality",
        "audit",
    ]
    for gate_id, target in (
        ("ac-18-clean-state-production-delivery", "verify-del-01"),
        ("ac-18-legacy-sqlite-recovery", "test-del-05-compose-backup"),
        ("ac-18-postgresql-recovery", "test-del-10-postgresql-backup"),
    ):
        assert gates[gate_id]["argv"] == ["make", "-j1", target]
        assert gates[gate_id]["timeout_seconds"] == 1800
