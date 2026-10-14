"""AC-15 offline gate failures are actionable without exposing runtime payloads."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.del_05_clean_state import Verification, VerificationFailure, verification_signals
from scripts.del_05_offline_diagnostics import (
    AC16_DIAGNOSTIC_CASES,
    AC16_DIAGNOSTIC_ERRORS,
    AC16_DIAGNOSTIC_MAX_DELTA,
    AC16_DIAGNOSTIC_NODE,
    AC16_DIAGNOSTIC_PREFIX,
    COUNT_FIELDS,
    MAX_COUNT,
    MAX_ELAPSED_MS,
    MAX_FAILURES,
    MAX_LINE_BYTES,
    MAX_SLOW_TESTS,
    MAX_STREAM_BYTES,
    PREFIX,
    project_diagnostics,
    safe_node,
    source_test_nodes,
)

ROOT = Path(__file__).resolve().parents[2]
NODE = "tests/test_example.py::test_failure"
CANARY = "private-canary-provider-key-12345"


def event(kind: str, *, stage: str = "pytest", **fields: object) -> bytes:
    return (PREFIX + json.dumps({"event": kind, "stage": stage, **fields}) + "\n").encode()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").write_text(
        "def test_failure():\n    pass\n\n"
        "class TestExample:\n    def test_method(self):\n        pass\n"
    )
    return tmp_path


def test_del_05_diagnostics_project_stages_failures_counts_and_source_names(source):
    output = b"".join(
        (
            event("stage_started", stage="mypy"),
            event("stage_finished", stage="mypy", returncode=0),
            event("stage_started"),
            event("test_started", node_id=NODE),
            event("test_failed", node_id=NODE, phase="call", exception="AssertionError"),
            event("test_finished", node_id=NODE),
            event("pytest_finished", passed=19, failed=1, skipped=2, collection_errors=0),
            event("stage_finished", returncode=1),
        )
    )
    projected = project_diagnostics(output, b"", source)
    assert projected["stages"] == [
        {"stage": "mypy", "status": "passed", "returncode": 0},
        {"stage": "pytest", "status": "failed", "returncode": 1},
    ]
    assert projected["last_stage"] == "pytest"
    assert "active_test" not in projected
    assert projected["failed_tests"] == [
        {"node_id": NODE, "phase": "call", "exception": "AssertionError"}
    ]
    assert projected["pytest_summary"] == {
        "passed": 19,
        "failed": 1,
        "skipped": 2,
        "collection_errors": 0,
    }
    assert not projected["truncated"]


def test_del_05_diagnostics_never_copy_payload_parameters_paths_or_unknown_fields(source):
    output = b"".join(
        (
            CANARY.encode() + b"\n",
            event("stage_started", stage=CANARY),
            event("test_failed", node_id=CANARY, phase="call", exception="AssertionError"),
            event(
                "test_failed",
                node_id=f"{NODE}[{CANARY}]",
                phase="call",
                exception=CANARY,
                traceback=CANARY,
                message=CANARY,
            ),
            event("test_failed", node_id=NODE, phase=CANARY, exception="AssertionError"),
            event("test_started", node_id="/private/" + CANARY),
            event("test_started", node_id="tests/../" + CANARY),
            event("pytest_finished", passed=True, failed=-1, skipped=CANARY),
            event("stage_finished", returncode=True),
        )
    )
    projected = project_diagnostics(output, CANARY, source)
    assert CANARY not in json.dumps(projected)
    assert projected["failed_tests"] == [
        {"node_id": NODE, "phase": "call", "exception": "OtherError"}
    ]
    assert "pytest_summary" not in projected
    assert projected["stages"] == []


def test_del_05_diagnostics_project_timings_progress_latest_and_distinct_slowest(source):
    count = MAX_SLOW_TESTS + 20
    (source / "tests/test_many.py").write_text(
        "\n".join(
            f"class TestGroup{index}:\n    def test_case(self): pass" for index in range(count)
        )
    )
    nodes = [f"tests/test_many.py::TestGroup{index}::test_case" for index in range(count)]
    output = event("stage_started", elapsed_ms=0)
    output += b"".join(
        event(
            "test_finished",
            node_id=f"{node}[{CANARY}]",
            started_ms=0,
            elapsed_ms=index,
            session_elapsed_ms=index,
            completed=index + 1,
        )
        for index, node in enumerate(nodes)
    )
    output += event(
        "test_finished",
        node_id=f"{nodes[-1]}[other]",
        started_ms=100,
        elapsed_ms=1,
        session_elapsed_ms=101,
        completed=count + 1,
    )
    output += event("test_started", node_id=NODE, session_elapsed_ms=102, completed=count + 1)
    output += event("stage_finished", returncode=0, elapsed_ms=103)
    projected = project_diagnostics(output, b"", source)
    assert projected["stages"] == [
        {"stage": "pytest", "status": "passed", "returncode": 0, "elapsed_ms": 103}
    ]
    assert projected["active_test"] == NODE
    assert projected["active_test_started_ms"] == 102
    assert projected["latest_test"] == {
        "node_id": nodes[-1],
        "started_ms": 100,
        "elapsed_ms": 1,
        "finished_ms": 101,
    }
    assert [item["elapsed_ms"] for item in projected["slowest_tests"]] == list(
        range(count - 1, count - MAX_SLOW_TESTS - 1, -1)
    )
    assert projected["pytest_progress"] == {"completed": count + 1, "session_elapsed_ms": 102}
    assert len(json.dumps(projected)) < 4_000
    assert CANARY not in json.dumps(projected)


@pytest.mark.parametrize(
    "invalid",
    [
        True,
        False,
        -1,
        1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        MAX_ELAPSED_MS + 1,
        "12",
        None,
        [],
        {},
    ],
)
def test_del_05_diagnostics_reject_invalid_timing_fields(source, invalid):
    output = b"".join(
        (
            event("stage_started", elapsed_ms=invalid),
            event("stage_finished", returncode=0, elapsed_ms=invalid),
            event("test_started", node_id=NODE, session_elapsed_ms=invalid),
            event(
                "test_finished",
                node_id=NODE,
                started_ms=invalid,
                elapsed_ms=invalid,
                session_elapsed_ms=invalid,
            ),
            event("pytest_progress", session_elapsed_ms=invalid, completed=invalid),
        )
    )
    projected = project_diagnostics(output, b"", source)
    # Nonfinite JSON is rejected in full by the existing strict decoder.
    assert all("elapsed_ms" not in stage for stage in projected["stages"])
    for field in ("active_test_started_ms", "latest_test", "slowest_tests", "pytest_progress"):
        assert field not in projected


@pytest.mark.parametrize("invalid", [True, -1, MAX_COUNT + 1, 0.1, float("inf"), "1"])
def test_del_05_diagnostics_reject_invalid_counts_at_progress_and_completion(source, invalid):
    fields = dict.fromkeys(COUNT_FIELDS, invalid)
    projected = project_diagnostics(
        event("pytest_progress", **fields) + event("pytest_finished", **fields),
        b"",
        source,
    )
    assert "pytest_progress" not in projected
    assert "pytest_summary" not in projected


@pytest.mark.parametrize("started,elapsed,finished", [(20, 0, 19), (10, 12, 20)])
def test_del_05_diagnostics_reject_inconsistent_test_timing(source, started, elapsed, finished):
    projected = project_diagnostics(
        event(
            "test_finished",
            node_id=NODE,
            started_ms=started,
            elapsed_ms=elapsed,
            session_elapsed_ms=finished,
        ),
        b"",
        source,
    )
    assert "latest_test" not in projected
    assert "slowest_tests" not in projected


def test_del_05_diagnostics_active_timing_omits_ambiguous_and_legacy_starts(source):
    output = event("test_started", node_id=NODE, session_elapsed_ms=MAX_ELAPSED_MS)
    projected = project_diagnostics(output, b"", source)
    assert projected["active_test_started_ms"] == MAX_ELAPSED_MS
    output += event("test_started", node_id=NODE)
    projected = project_diagnostics(output, b"", source)
    assert projected["active_tests"] == [{"node_id": NODE, "count": 2}]
    assert "active_test_started_ms" not in projected
    output += event("test_finished", node_id=NODE)
    projected = project_diagnostics(output, b"", source)
    assert projected["active_tests"] == [{"node_id": NODE, "count": 1}]
    assert "active_test_started_ms" not in projected


def test_del_05_diagnostics_unknown_source_cannot_introduce_timing_or_progress(source):
    projected = project_diagnostics(
        event(
            "test_finished",
            node_id=f"tests/test_example.py::test_{CANARY}",
            started_ms=0,
            elapsed_ms=10,
            session_elapsed_ms=10,
            completed=1,
        ),
        b"",
        source,
    )
    assert "latest_test" not in projected
    assert "slowest_tests" not in projected
    assert "pytest_progress" not in projected
    assert CANARY not in json.dumps(projected)


def test_del_05_diagnostics_reject_untrusted_protocol_shapes(source):
    malformed = [None, [], 42, {"stage": []}, {"stage": "pytest", "event": []}]
    output = b"".join((PREFIX + json.dumps(item) + "\n").encode() for item in malformed)
    output += PREFIX.encode() + b"not-json\n"
    output += PREFIX.encode() + b"[" * 1500 + b"]" * 1500 + b"\n"
    output += event("test_failed", node_id=NODE, phase=[], exception=[])
    output += b"\xff" + PREFIX.encode() + b"\xff\n"
    projected = project_diagnostics(output, None, source)
    assert projected["failed_tests"] == []
    assert projected["stages"] == []


def test_del_05_diagnostics_source_allowlist_ignores_unknown_and_linked_tests(source, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def test_outside():\n    pass\n")
    (source / "tests/test_link.py").symlink_to(outside)
    (source / "tests/test_syntax.py").write_text("def test_broken(:")
    allowed = source_test_nodes(source)
    assert NODE in allowed
    assert "tests/test_example.py::TestExample::test_method" in allowed
    assert "tests/test_syntax.py" in allowed
    assert "tests/test_link.py" not in allowed
    assert safe_node(NODE + "[" + CANARY + "]", allowed) == NODE
    assert safe_node("tests/test_example.py::test_" + CANARY, allowed) is None
    assert safe_node(NODE + "[" + "x" * MAX_LINE_BYTES + "]", allowed) is None


def test_del_05_diagnostics_bound_stream_lines_unique_failures_and_encoded_result(source):
    count = MAX_FAILURES + 10
    (source / "tests/test_many.py").write_text(
        "\n".join(f"def test_case_{index}():\n    pass\n" for index in range(count))
    )
    output = b"x" * (MAX_STREAM_BYTES + 1) + b"\n"
    output += event("stage_started")
    for index in range(count):
        output += event(
            "test_failed",
            node_id=f"tests/test_many.py::test_case_{index}[{CANARY}]",
            phase="call",
            exception="AssertionError",
        )
    # Oversized, valid JSON is still rejected instead of copied.
    output += event("test_started", node_id=NODE, message="x" * MAX_LINE_BYTES)
    projected = project_diagnostics(output, b"", source)
    assert projected["truncated"]
    assert len(projected["failed_tests"]) == MAX_FAILURES
    assert len(json.dumps(projected)) < 48_000
    assert CANARY not in json.dumps(projected)
    assert "active_test" not in projected


@pytest.mark.parametrize("phase", ["setup", "call", "teardown", "collection"])
def test_del_05_diagnostics_handles_failure_phases_and_deduplicates_parameters(source, phase):
    output = b"".join(
        event("test_failed", node_id=f"{NODE}[{parameter}]", phase=phase, exception="ValueError")
        for parameter in ("first", "second", CANARY)
    )
    projected = project_diagnostics(output, b"", source)
    assert projected["failed_tests"] == [
        {"node_id": NODE, "phase": phase, "exception": "ValueError"}
    ]


def test_del_05_diagnostics_command_failure_keeps_hashes_and_safe_projection(source):
    output = (
        event("stage_started")
        + event(
            "test_failed", node_id=f"{NODE}[{CANARY}]", phase="call", exception="AssertionError"
        )
        + event("stage_finished", returncode=1)
    )
    result = subprocess.CompletedProcess(["docker"], 2, output, CANARY.encode())
    verifier = Verification(source, "HEAD")
    with (
        patch("scripts.del_05_clean_state.subprocess.run", return_value=result),
        pytest.raises(VerificationFailure, match="command_failed:offline-backend"),
    ):
        verifier.command("offline-backend", ["docker"])
    record = verifier.report["commands"][-1]
    assert record["stdout_sha256"] == hashlib.sha256(output).hexdigest()
    assert record["stderr_sha256"] == hashlib.sha256(CANARY.encode()).hexdigest()
    assert record["offline_diagnostics"]["failed_tests"][0]["node_id"] == NODE
    assert CANARY not in json.dumps(verifier.report)


def test_del_05_diagnostics_timeout_keeps_active_test_partial_hashes_and_no_payload(source):
    partial = event("stage_started") + event("test_started", node_id=f"{NODE}[{CANARY}]")
    verifier = Verification(source, "HEAD")
    with (
        patch(
            "scripts.del_05_clean_state.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                ["docker"], 1, output=partial, stderr=CANARY.encode()
            ),
        ),
        pytest.raises(VerificationFailure, match="command_timeout:offline-backend"),
    ):
        verifier.command("offline-backend", ["docker"], timeout=1)
    record = verifier.report["commands"][-1]
    assert record["status"] == "timeout"
    assert type(record["seconds"]) is float and record["seconds"] >= 0
    assert record["stdout_sha256"] == hashlib.sha256(partial).hexdigest()
    assert record["stderr_sha256"] == hashlib.sha256(CANARY.encode()).hexdigest()
    assert record["offline_diagnostics"]["active_test"] == NODE
    assert CANARY not in json.dumps(verifier.report)


def test_del_05_diagnostics_never_parse_runtime_secret_output(source):
    output = event("stage_started") + event("test_started", node_id=NODE)
    verifier = Verification(source, "HEAD")
    with patch(
        "scripts.del_05_clean_state.subprocess.run",
        return_value=subprocess.CompletedProcess(["docker"], 0, output, CANARY.encode()),
    ):
        verifier.command("snapshot-before-restart", ["docker"])
    assert "offline_diagnostics" not in verifier.report["commands"][-1]
    assert CANARY not in json.dumps(verifier.report)


def test_del_05_diagnostics_real_command_timeout_retains_only_safe_partial_evidence(source):
    partial = event("stage_started") + event("test_started", node_id=f"{NODE}[{CANARY}]")
    code = (
        f"import sys,time; sys.stdout.buffer.write({partial!r}); sys.stdout.flush(); time.sleep(30)"
    )
    verifier = Verification(source, "HEAD")
    with pytest.raises(VerificationFailure, match="command_timeout:offline-backend"):
        verifier.command("offline-backend", [sys.executable, "-c", code], timeout=1)
    record = verifier.report["commands"][-1]
    assert record["stdout_sha256"] == hashlib.sha256(partial).hexdigest()
    assert type(record["seconds"]) is float and record["seconds"] >= 1
    assert record["offline_diagnostics"]["active_test"] == NODE
    assert record["offline_diagnostics"]["stages"] == [{"stage": "pytest", "status": "running"}]
    assert CANARY not in json.dumps(verifier.report)


def test_del_05_diagnostics_aggregate_deadline_keeps_active_test_without_changing_deadlines(source):
    partial = event("stage_started") + event("test_started", node_id=f"{NODE}[{CANARY}]")
    code = (
        f"import sys,time; sys.stdout.buffer.write({partial!r}); sys.stdout.flush(); time.sleep(30)"
    )
    verifier = Verification(source, "HEAD")
    with (
        pytest.raises(VerificationFailure, match="verification_execution_deadline"),
        verification_signals(1),
    ):
        verifier.command("offline-backend", [sys.executable, "-c", code], timeout=10)
    record = verifier.report["commands"][-1]
    assert record["status"] == "interrupted"
    assert type(record["seconds"]) is float and record["seconds"] >= 0
    assert record["stdout_sha256"] == hashlib.sha256(partial).hexdigest()
    assert record["offline_diagnostics"]["active_test"] == NODE
    assert CANARY not in json.dumps(verifier.report)


def test_del_05_diagnostics_actual_pytest_plugin_identifies_failure_without_secret(source):
    (source / "pytest.ini").write_text("[pytest]\n")
    (source / "tests/test_example.py").write_text(
        "import pytest\n"
        f"@pytest.mark.parametrize('value', ['{CANARY}'], ids=['{CANARY}'])\n"
        "def test_failure(value):\n"
        "    raise ValueError(value)\n"
        "def test_success():\n"
        "    pass\n"
    )
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.del_05_offline_diagnostics",
            "pytest",
            "--",
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.del_05_offline_diagnostics",
        ],
        cwd=source,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 1
    assert CANARY.encode() in process.stdout  # The raw pytest stream really is sensitive.
    projected = project_diagnostics(process.stdout, process.stderr, source)
    assert projected["failed_tests"] == [
        {"node_id": NODE, "phase": "call", "exception": "ValueError"}
    ]
    assert projected["pytest_summary"]["passed"] == 1
    assert projected["pytest_summary"]["failed"] == 1
    stage = projected["stages"][0]
    assert stage == {
        "stage": "pytest",
        "status": "failed",
        "returncode": 1,
        "elapsed_ms": stage["elapsed_ms"],
    }
    assert type(stage["elapsed_ms"]) is int and 0 <= stage["elapsed_ms"] <= MAX_ELAPSED_MS
    assert projected["pytest_summary"]["completed"] == 2
    assert len(projected["slowest_tests"]) == 2
    assert CANARY not in json.dumps(projected)


def test_del_05_diagnostics_wrapper_preserves_command_exit_status_and_failure_stage(source):
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.del_05_offline_diagnostics",
            "architecture",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert process.returncode == 7
    stage = project_diagnostics(process.stdout, process.stderr, source)["stages"][0]
    assert stage == {
        "stage": "architecture",
        "status": "failed",
        "returncode": 7,
        "elapsed_ms": stage["elapsed_ms"],
    }
    assert type(stage["elapsed_ms"]) is int and 0 <= stage["elapsed_ms"] <= MAX_ELAPSED_MS


@pytest.mark.parametrize("code,expected", [(0, 0), (7, 7), (-15, 143), (None, 127)])
def test_del_05_diagnostics_wrapper_timing_never_changes_argv_or_exit_status(
    source, monkeypatch, code, expected
):
    module = _isolated_diagnostics_module()
    emitted = []
    clock = iter((1_000_000, 252_000_000))
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: next(clock))
    monkeypatch.setattr(
        module,
        "_emit",
        lambda kind, stage, **fields: emitted.append(event(kind, stage=stage, **fields)),
    )
    command = ["existing-command", "--unchanged", CANARY]
    with patch.object(module.subprocess, "run") as run:
        if code is None:
            run.side_effect = OSError(CANARY)
        else:
            run.return_value = subprocess.CompletedProcess(command, code)
        assert module.main(["architecture", "--", *command]) == expected
        run.assert_called_once_with(command, check=False)
    projected = project_diagnostics(b"".join(emitted), b"", source)
    assert projected["stages"] == [
        {
            "stage": "architecture",
            "status": "passed" if code == 0 else "failed",
            "returncode": 127 if code is None else code,
            "elapsed_ms": 251,
        }
    ]
    assert CANARY not in json.dumps(projected)


def test_del_05_diagnostics_interrupted_real_plugin_keeps_progress_and_active_start(source):
    (source / "pytest.ini").write_text("[pytest]\n")
    entered = source / "entered-active-test"
    (source / "tests/test_example.py").write_text(
        "import time, pytest\nfrom pathlib import Path\n"
        "def test_success():\n    pass\n"
        f"@pytest.mark.parametrize('value', ['{CANARY}'], ids=['{CANARY}'])\n"
        "def test_failure(value):\n"
        f"    Path({str(entered)!r}).touch()\n"
        "    time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "scripts.del_05_offline_diagnostics"],
        cwd=source,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Handshake so import time cannot masquerade as missing test progress.
        deadline = time.monotonic() + 20
        while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists()
        with pytest.raises(subprocess.TimeoutExpired) as failure:
            process.communicate(timeout=0.1)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    projected = project_diagnostics(failure.value.stdout, failure.value.stderr, source)
    assert projected["active_test"] == NODE
    assert projected["pytest_progress"]["completed"] == 1
    assert projected["pytest_progress"]["passed"] == 1
    assert 0 <= projected["active_test_started_ms"] <= MAX_ELAPSED_MS
    assert projected["active_test_started_ms"] >= projected["latest_test"]["finished_ms"]
    assert projected["latest_test"]["node_id"] == "tests/test_example.py::test_success"
    assert "pytest_summary" not in projected
    assert CANARY not in json.dumps(projected)


def test_del_05_diagnostics_actual_collection_error_retains_source_file_not_message(source):
    (source / "pytest.ini").write_text("[pytest]\n")
    (source / "tests/test_example.py").write_text(f"raise ImportError('{CANARY}')\n")
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.del_05_offline_diagnostics",
        ],
        cwd=source,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 2
    assert CANARY.encode() in process.stdout
    projected = project_diagnostics(process.stdout, process.stderr, source)
    assert len(projected["failed_tests"]) == 1
    failure = projected["failed_tests"][0]
    assert failure["node_id"] == "tests/test_example.py"
    assert failure["phase"] == "collection"
    assert projected["pytest_summary"]["collection_errors"] == 1
    assert CANARY not in json.dumps(projected)


def _isolated_diagnostics_module():
    # Never change the real registered plugin's controller/worker globals while
    # these very tests are executing inside an offline xdist worker.
    spec = importlib.util.spec_from_file_location(
        "standalone_diagnostics_probe", project_diagnostics.__code__.co_filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_del_05_diagnostics_worker_hooks_never_emit_or_aggregate(source, monkeypatch):
    module = _isolated_diagnostics_module()

    def forbidden(*args, **kwargs):
        pytest.fail("a worker must not scan controller sources or emit protocol")

    monkeypatch.setattr(module, "source_test_nodes", forbidden)
    module.pytest_configure(SimpleNamespace(rootpath=source, workerinput={"workerid": CANARY}))
    assert module._is_worker and module._allowed_nodes == frozenset()
    monkeypatch.setattr(module, "_emit", forbidden)
    module.pytest_runtest_logstart(NODE + "[" + CANARY + "]", None)
    module.pytest_runtest_logfinish(NODE + "[" + CANARY + "]", None)
    for outcome in ("passed", "failed", "skipped"):
        report = SimpleNamespace(
            nodeid=NODE,
            passed=outcome == "passed",
            failed=outcome == "failed",
            skipped=outcome == "skipped",
            when="call",
        )
        module.pytest_runtest_logreport(report)
        module.pytest_collectreport(report)
    module.pytest_sessionfinish(None, 1)
    assert module._counts == dict.fromkeys(COUNT_FIELDS, 0)
    assert module._active_timings == {}


def test_del_05_diagnostics_worker_emitter_has_independent_safety_backstop(source, capsys):
    module = _isolated_diagnostics_module()
    module.pytest_configure(SimpleNamespace(rootpath=source, workerinput={}))
    module._emit("pytest_finished", "pytest", private=CANARY)
    assert capsys.readouterr().out == ""


def test_del_05_diagnostics_controller_counts_parameters_without_emitting_ids(source, monkeypatch):
    module = _isolated_diagnostics_module()
    module.pytest_configure(SimpleNamespace(rootpath=source))
    emitted = []
    monkeypatch.setattr(
        module,
        "_emit",
        lambda kind, stage, **fields: emitted.append({"event": kind, "stage": stage, **fields}),
    )
    for index, outcome in enumerate(("passed", "passed", "failed", "skipped")):
        node = f"{NODE}[{CANARY}-{index}]"
        module.pytest_runtest_logstart(node, None)
        module.pytest_runtest_logreport(
            SimpleNamespace(
                nodeid=node,
                passed=outcome == "passed",
                failed=outcome == "failed",
                skipped=outcome == "skipped",
                when="call",
            )
        )
        module.pytest_runtest_logfinish(node, None)
    module.pytest_collectreport(SimpleNamespace(failed=True, nodeid="tests/test_example.py"))
    module.pytest_sessionfinish(None, 1)
    assert emitted[-1] == {
        "event": "pytest_finished",
        "stage": "pytest",
        "completed": 4,
        "passed": 2,
        "failed": 1,
        "skipped": 1,
        "collection_errors": 1,
        "session_elapsed_ms": emitted[-1]["session_elapsed_ms"],
    }
    assert sum(item["event"] == "test_started" for item in emitted) == 4
    assert sum(item["event"] == "test_finished" for item in emitted) == 4
    assert CANARY not in json.dumps(emitted)


def test_del_05_diagnostics_controller_times_overlapping_cases_independently(source, monkeypatch):
    module = _isolated_diagnostics_module()
    clock = iter(value * 1_000_000 for value in (0, 0, 10, 20, 100, 150))
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: next(clock))
    emitted = []
    monkeypatch.setattr(
        module,
        "_emit",
        lambda kind, stage, **fields: emitted.append(event(kind, stage=stage, **fields)),
    )
    module.pytest_configure(SimpleNamespace(rootpath=source))
    first, second = f"{NODE}[{CANARY}-1]", f"{NODE}[{CANARY}-2]"
    module.pytest_runtest_logstart(first, None)
    module.pytest_runtest_logstart(second, None)
    assert len(module._active_timings) == 2
    assert CANARY not in repr(module._active_timings)
    module.pytest_runtest_logfinish(first, None)
    module.pytest_runtest_logfinish(second, None)
    assert module._active_timings == {}
    projected = project_diagnostics(b"".join(emitted), b"", source)
    assert projected["latest_test"] == {
        "node_id": NODE,
        "started_ms": 20,
        "elapsed_ms": 130,
        "finished_ms": 150,
    }
    assert projected["slowest_tests"] == [projected["latest_test"]]
    assert projected["pytest_progress"]["completed"] == 2
    assert "active_tests" not in projected
    assert CANARY not in json.dumps(projected)


def test_del_05_diagnostics_controller_timing_inventory_and_values_remain_bounded(
    source, monkeypatch
):
    module = _isolated_diagnostics_module()
    monkeypatch.setattr(module, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "MAX_ACTIVE_NODES", 2)
    module.pytest_configure(SimpleNamespace(rootpath=source))
    for index in range(3):
        module.pytest_runtest_logstart(f"{NODE}[{CANARY}-{index}]", None)
    assert len(module._active_timings) == 2
    for index in range(3):
        module.pytest_runtest_logfinish(f"{NODE}[{CANARY}-{index}]", None)
    assert module._active_timings == {}
    assert module._counts["completed"] == 3
    assert module._elapsed_ms(100, 99) == 0
    assert module._elapsed_ms(0, (MAX_ELAPSED_MS + 1) * 1_000_000) == MAX_ELAPSED_MS
    module._counts["completed"] = MAX_COUNT + 1
    assert module._current_progress()["completed"] == MAX_COUNT


def test_del_05_diagnostics_overlapping_parameter_instances_keep_the_remaining_count(source):
    other = "tests/test_example.py::TestExample::test_method"
    output = b"".join(
        (
            event("test_started", node_id=f"{NODE}[{CANARY}-first]"),
            event("test_started", node_id=f"{NODE}[{CANARY}-second]"),
            event("test_started", node_id=other),
            event("test_finished", node_id=f"{NODE}[{CANARY}-first]"),
        )
    )
    projected = project_diagnostics(output, None, source)
    assert projected["active_tests"] == [
        {"node_id": NODE, "count": 1},
        {"node_id": other, "count": 1},
    ]
    assert projected["active_test"] == other
    assert CANARY not in json.dumps(projected)
    output += event("test_finished", node_id=other)
    projected = project_diagnostics(output, None, source)
    assert projected["active_tests"] == [{"node_id": NODE, "count": 1}]
    assert projected["active_test"] == NODE
    output += event("test_finished", node_id=f"{NODE}[{CANARY}-second]")
    projected = project_diagnostics(output, None, source)
    assert "active_test" not in projected and "active_tests" not in projected


def test_del_05_diagnostics_active_inventory_and_counts_are_bounded(source, monkeypatch):
    module = _isolated_diagnostics_module()
    nodes = [
        f"tests/test_many.py::test_case_{index}" for index in range(module.MAX_ACTIVE_NODES + 1)
    ]
    (source / "tests/test_many.py").write_text(
        "\n".join(f"def test_case_{index}(): pass" for index in range(len(nodes)))
    )
    output = b"".join(event("test_started", node_id=node) for node in nodes)
    projected = module.project_diagnostics(output, None, source)
    assert projected["truncated"]
    assert len(projected["active_tests"]) == module.MAX_ACTIVE_NODES
    assert len(json.dumps(projected)) < 48_000
    monkeypatch.setattr(module, "MAX_COUNT", 2)
    projected = module.project_diagnostics(event("test_started", node_id=NODE) * 3, None, source)
    assert projected["active_tests"] == [{"node_id": NODE, "count": 2}]
    assert projected["truncated"]


def _xdist_diagnostics_fixture(source):
    # Missing acquisition is a failure, never an optional/skipped proof.
    assert importlib.metadata.version("pytest-xdist") == "3.8.0"
    (source / "pytest.ini").write_text("[pytest]\n")
    (source / "conftest.py").write_text(
        "from scripts import del_05_offline_diagnostics as diagnostics\n"
        "def pytest_configure(config):\n"
        "    if hasattr(config, 'workerinput'):\n"
        "        def forbidden(*args, **kwargs):\n"
        "            raise AssertionError('worker attempted diagnostic protocol')\n"
        "        diagnostics._emit = forbidden\n"
    )


def _run_xdist_diagnostics(source):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
    }
    environment.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.del_05_offline_diagnostics",
            "pytest",
            "--",
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "2",
            "--dist=loadfile",
            "--max-worker-restart=0",
            "-p",
            "scripts.del_05_offline_diagnostics",
        ],
        cwd=source,
        env=environment,
        capture_output=True,
        timeout=40,
        check=False,
    )


def _diagnostic_events(output):
    events = []
    for line in output.splitlines():
        marker = line.find(PREFIX.encode())
        if marker >= 0:
            events.append(json.loads(line[marker + len(PREFIX) :]))
    return events


def test_del_05_diagnostics_actual_two_workers_emit_one_redacted_aggregate(source):
    _xdist_diagnostics_fixture(source)
    (source / "tests/test_example.py").unlink()
    (source / "barrier").mkdir()
    for name in ("a", "b"):
        (source / f"tests/test_{name}.py").write_text(
            "from pathlib import Path\nimport time\nimport pytest\n"
            "@pytest.mark.parametrize('value', [0, 1, 2], "
            f"ids=['{CANARY}-0', '{CANARY}-1', '{CANARY}-2'])\n"
            "def test_shared(value, worker_id):\n"
            "    if value == 0:\n"
            "        assert worker_id in {'gw0', 'gw1'}\n"
            "        (Path('barrier') / worker_id).write_text('ready')\n"
            "        deadline = time.monotonic() + 10\n"
            "        while len(list(Path('barrier').glob('gw[01]'))) != 2:\n"
            "            assert time.monotonic() < deadline, 'two workers did not overlap'\n"
            "            time.sleep(0.01)\n"
            f"    if value == 1 and {name!r} == 'a':\n"
            f"        raise ValueError('{CANARY}')\n"
            f"    if value == 2 and {name!r} == 'b':\n"
            f"        pytest.skip('{CANARY}')\n"
        )
    process = _run_xdist_diagnostics(source)
    assert process.returncode == 1
    assert {path.name for path in (source / "barrier").iterdir()} == {"gw0", "gw1"}
    assert CANARY.encode() in process.stdout
    emitted = _diagnostic_events(process.stdout)
    assert CANARY not in json.dumps(emitted)
    assert sum(item["event"] == "test_started" for item in emitted) == 6
    assert sum(item["event"] == "test_finished" for item in emitted) == 6
    assert sum(item["event"] == "pytest_finished" for item in emitted) == 1
    projected = project_diagnostics(process.stdout, process.stderr, source)
    assert projected["pytest_summary"] == {
        "completed": 6,
        "passed": 4,
        "failed": 1,
        "skipped": 1,
        "collection_errors": 0,
    }
    assert projected["failed_tests"] == [
        {"node_id": "tests/test_a.py::test_shared", "phase": "call", "exception": "ValueError"}
    ]
    assert "active_test" not in projected and "active_tests" not in projected
    stage = projected["stages"][0]
    assert stage == {
        "stage": "pytest",
        "status": "failed",
        "returncode": 1,
        "elapsed_ms": stage["elapsed_ms"],
    }
    assert len(projected["slowest_tests"]) == 2
    assert all(item["elapsed_ms"] >= 0 for item in projected["slowest_tests"])
    assert projected["pytest_progress"]["completed"] == 6
    assert CANARY not in json.dumps(projected)
    partial = b""
    maximum_active = 0
    for item in emitted:
        partial += (PREFIX + json.dumps(item) + "\n").encode()
        active = project_diagnostics(partial, None, source).get("active_tests", [])
        maximum_active = max(maximum_active, sum(item["count"] for item in active))
    assert maximum_active == 2


def test_del_05_diagnostics_actual_two_worker_collection_error_is_not_duplicated(source):
    _xdist_diagnostics_fixture(source)
    (source / "tests/test_example.py").write_text(f"raise ImportError('{CANARY}')\n")
    process = _run_xdist_diagnostics(source)
    assert process.returncode != 0
    emitted = _diagnostic_events(process.stdout)
    assert sum(item["event"] == "pytest_finished" for item in emitted) == 1
    assert CANARY not in json.dumps(emitted)
    projected = project_diagnostics(process.stdout, process.stderr, source)
    assert projected["pytest_summary"] == {
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "collection_errors": 1,
    }
    assert len(projected["failed_tests"]) == 1
    assert projected["failed_tests"][0]["node_id"] == "tests/test_example.py"
    assert projected["failed_tests"][0]["phase"] == "collection"
    assert CANARY not in json.dumps(projected)


def _fixture_diagnostic_payload(case="mode_backend"):
    return {
        "case": case,
        "actual_error": "artifact_not_fresh",
        "artifact_times": {
            name: {
                "mtime_minus_started_ns": -42,
                "finished_minus_mtime_ns": AC16_DIAGNOSTIC_MAX_DELTA,
            }
            for name in (".coverage", "coverage.json", "verification.json")
        },
    }


@pytest.fixture
def ac16_source(source):
    directory = source / "tests/tooling"
    directory.mkdir()
    (directory / "test_ac_16_quality.py").write_text(
        "class QualityRecordTests:\n"
        "    def test_summary_rejects_partial_modes_false_success_and_boolean_counters(self):\n"
        "        pass\n"
    )
    return source


def _fixture_event(payload, *, node=AC16_DIAGNOSTIC_NODE, phase="call", exception="AssertionError"):
    return event(
        "test_failed", node_id=node, phase=phase, exception=exception, fixture_diagnostic=payload
    )


def test_del_05_ac16_fixture_diagnostic_is_fixed_typed_and_independently_copied(ac16_source):
    payload = _fixture_diagnostic_payload()
    payload["artifact_times"][".coverage"] = {
        "mtime_minus_started_ns": None,
        "finished_minus_mtime_ns": None,
    }
    payload["artifact_times"]["coverage.json"][
        "mtime_minus_started_ns"
    ] = -AC16_DIAGNOSTIC_MAX_DELTA
    projected = project_diagnostics(_fixture_event(payload), CANARY, ac16_source)
    assert projected["failed_tests"] == [
        {
            "node_id": AC16_DIAGNOSTIC_NODE,
            "phase": "call",
            "exception": "AssertionError",
            "fixture_diagnostic": payload,
        }
    ]
    assert "pytest_summary" not in projected
    assert CANARY not in json.dumps(projected)
    module = _isolated_diagnostics_module()
    copied = module._fixture_diagnostic(payload)
    payload["artifact_times"]["coverage.json"]["mtime_minus_started_ns"] = CANARY
    assert copied["artifact_times"]["coverage.json"]["mtime_minus_started_ns"] == (
        -AC16_DIAGNOSTIC_MAX_DELTA
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "not_dict",
        "extra_field",
        "missing_field",
        "unknown_case",
        "case_list",
        "unknown_error",
        "error_list",
        "missing_artifact",
        "extra_artifact",
        "artifact_list",
        "times_list",
        "missing_time",
        "extra_time",
        "boolean_time",
        "float_time",
        "string_time",
        "high_time",
        "low_time",
        "half_missing",
    ),
)
def test_del_05_ac16_fixture_diagnostic_rejects_malformed_without_payload_leak(
    ac16_source, mutation
):
    payload = _fixture_diagnostic_payload()
    times = payload["artifact_times"][".coverage"]
    if mutation == "not_dict":
        payload = [CANARY]
    elif mutation == "extra_field":
        payload["private"] = CANARY
    elif mutation == "missing_field":
        payload.pop("case")
    elif mutation == "unknown_case":
        payload["case"] = CANARY
    elif mutation == "case_list":
        payload["case"] = [CANARY]
    elif mutation == "unknown_error":
        payload["actual_error"] = CANARY
    elif mutation == "error_list":
        payload["actual_error"] = [CANARY]
    elif mutation == "missing_artifact":
        payload["artifact_times"].pop(".coverage")
    elif mutation == "extra_artifact":
        payload["artifact_times"][CANARY] = times.copy()
    elif mutation == "artifact_list":
        payload["artifact_times"] = [CANARY]
    elif mutation == "times_list":
        payload["artifact_times"][".coverage"] = [CANARY]
    elif mutation == "missing_time":
        times.pop("mtime_minus_started_ns")
    elif mutation == "extra_time":
        times["private"] = CANARY
    else:
        times["mtime_minus_started_ns"] = {
            "boolean_time": True,
            "float_time": 1.0,
            "string_time": CANARY,
            "high_time": AC16_DIAGNOSTIC_MAX_DELTA + 1,
            "low_time": -AC16_DIAGNOSTIC_MAX_DELTA - 1,
            "half_missing": None,
        }[mutation]
    assert _isolated_diagnostics_module()._fixture_diagnostic(payload) is None
    projected = project_diagnostics(_fixture_event(payload), CANARY, ac16_source)
    assert projected["failed_tests"] == [
        {"node_id": AC16_DIAGNOSTIC_NODE, "phase": "call", "exception": "AssertionError"}
    ]
    assert CANARY not in json.dumps(projected)


@pytest.mark.parametrize(
    ("node", "phase", "exception"),
    (
        (NODE, "call", "AssertionError"),
        (AC16_DIAGNOSTIC_NODE, "setup", "AssertionError"),
        (AC16_DIAGNOSTIC_NODE, "call", "ValueError"),
    ),
)
def test_del_05_ac16_fixture_diagnostic_only_attaches_to_exact_failure(
    ac16_source, node, phase, exception, monkeypatch
):
    payload = _fixture_diagnostic_payload()
    output = _fixture_event(payload, node=node, phase=phase, exception=exception)
    projected = project_diagnostics(output, None, ac16_source)
    assert all("fixture_diagnostic" not in item for item in projected["failed_tests"])
    module = _isolated_diagnostics_module()
    module.pytest_configure(SimpleNamespace(rootpath=ac16_source))
    emitted = []
    monkeypatch.setattr(module, "_emit", lambda *args, **kwargs: emitted.append(kwargs))
    module.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid=node,
            when=phase,
            failed=True,
            longrepr=SimpleNamespace(
                reprcrash=SimpleNamespace(
                    message=exception + ": failed\n" + AC16_DIAGNOSTIC_PREFIX + json.dumps(payload)
                )
            ),
        )
    )
    assert all("fixture_diagnostic" not in item for item in emitted)
    assert module._counts["failed"] == 1


def test_del_05_ac16_fixture_diagnostic_keeps_distinct_cases_and_bounds_dedup(ac16_source):
    first = _fixture_diagnostic_payload()
    second = _fixture_diagnostic_payload("valid_integer")
    output = _fixture_event(first) * 3 + _fixture_event(second) * 2
    projected = project_diagnostics(output, None, ac16_source)
    assert [item["fixture_diagnostic"] for item in projected["failed_tests"]] == [first, second]
    assert not projected["truncated"]
    output = b""
    for index in range(MAX_FAILURES + 2):
        payload = _fixture_diagnostic_payload()
        payload["artifact_times"][".coverage"]["mtime_minus_started_ns"] = index
        output += _fixture_event(payload) * 2
    projected = project_diagnostics(output, None, ac16_source)
    assert len(projected["failed_tests"]) == MAX_FAILURES
    assert projected["truncated"]
    assert len(json.dumps(projected)) < 64_000


def test_del_05_ac16_fixture_note_extraction_is_bounded_strict_and_not_rendered(ac16_source):
    module = _isolated_diagnostics_module()
    payload = _fixture_diagnostic_payload()
    note = AC16_DIAGNOSTIC_PREFIX + json.dumps(payload)

    class NeverRender:
        def __init__(self, message):
            self.reprcrash = SimpleNamespace(message=message)

        def __str__(self):
            raise AssertionError("must not render traceback")

    valid = SimpleNamespace(longrepr=NeverRender(CANARY + "\n" + note + "\n" + CANARY))
    assert module._fixture_note(valid) == payload
    for message in (
        None,
        42,
        CANARY,
        "prefix " + note,
        note + "\n" + note,
        note + CANARY,
        note + "\n" + "x" * MAX_LINE_BYTES,
        "é" * (MAX_LINE_BYTES // 2) + "\n" + note,
        AC16_DIAGNOSTIC_PREFIX + "[" * 1500 + "]" * 1500,
        AC16_DIAGNOSTIC_PREFIX + '{"case":"mode_backend","case":"valid_integer"}',
        AC16_DIAGNOSTIC_PREFIX + '{"case":NaN}',
        note.replace('"mtime_minus_started_ns": -42', '"mtime_minus_started_ns": true'),
    ):
        assert module._fixture_note(SimpleNamespace(longrepr=NeverRender(message))) is None
    duplicate = _fixture_event(payload).replace(b'"case":', b'"case":"mode_backend","case":')
    nonfinite = _fixture_event(payload).replace(
        b'"mtime_minus_started_ns": -42', b'"mtime_minus_started_ns": NaN'
    )
    assert project_diagnostics(duplicate + nonfinite, None, ac16_source)["failed_tests"] == []


def test_del_05_ac16_fixture_diagnostic_all_reviewed_enums_are_accepted():
    module = _isolated_diagnostics_module()
    for case in AC16_DIAGNOSTIC_CASES:
        assert module._fixture_diagnostic(_fixture_diagnostic_payload(case)) is not None
    for code in AC16_DIAGNOSTIC_ERRORS:
        payload = _fixture_diagnostic_payload()
        payload["actual_error"] = code
        assert module._fixture_diagnostic(payload) is not None


@pytest.mark.parametrize("should_fail", (False, True), ids=("passing-subtest", "failing-subtest"))
def test_del_05_ac16_fixture_note_survives_real_xdist_unittest_without_secrets(
    ac16_source, should_fail
):
    _xdist_diagnostics_fixture(ac16_source)
    (ac16_source / "tests/test_example.py").unlink()
    payload = _fixture_diagnostic_payload()
    (ac16_source / "tests/tooling/test_ac_16_quality.py").write_text(
        "import json\nimport unittest\n"
        "class QualityRecordTests(unittest.TestCase):\n"
        "    def test_summary_rejects_partial_modes_false_success_and_boolean_counters(self):\n"
        f"        with self.subTest(field='mode', value={CANARY!r}):\n"
        "            try:\n"
        f"                self.assertFalse({should_fail!r}, {CANARY!r})\n"
        "            except AssertionError as error:\n"
        f"                error.add_note({CANARY!r})\n"
        f"                error.add_note({AC16_DIAGNOSTIC_PREFIX!r} + json.dumps({payload!r}))\n"
        "                raise\n"
    )
    process = _run_xdist_diagnostics(ac16_source)
    assert process.returncode == int(should_fail)
    if should_fail:
        assert CANARY.encode() in process.stdout
    emitted = _diagnostic_events(process.stdout)
    assert CANARY not in json.dumps(emitted)
    assert sum(item["event"] == "pytest_finished" for item in emitted) == 1
    projected = project_diagnostics(process.stdout, process.stderr, ac16_source)
    expected_failures = (
        [
            {
                "node_id": AC16_DIAGNOSTIC_NODE,
                "phase": "call",
                "exception": "AssertionError",
                "fixture_diagnostic": payload,
            }
        ]
        if should_fail
        else []
    )
    assert projected["failed_tests"] == expected_failures
    # Pytest reports the containing method plus its subtest; diagnostics must
    # preserve those hook counts instead of inventing a different aggregate.
    assert projected["pytest_summary"] == {
        "completed": 1,
        "passed": 1 if should_fail else 2,
        "failed": int(should_fail),
        "skipped": 0,
        "collection_errors": 0,
    }
    assert projected["stages"] == [
        {
            "stage": "pytest",
            "status": "failed" if should_fail else "passed",
            "returncode": int(should_fail),
            "elapsed_ms": projected["stages"][0]["elapsed_ms"],
        }
    ]
    assert projected["latest_test"]["node_id"] == AC16_DIAGNOSTIC_NODE
    assert projected["slowest_tests"] == [projected["latest_test"]]
    assert CANARY not in json.dumps(projected)
