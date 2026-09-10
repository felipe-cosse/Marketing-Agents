"""Offline gate failures are actionable without exposing runtime payloads."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.del_05_clean_state import Verification, VerificationFailure, verification_signals
from scripts.del_05_offline_diagnostics import (
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


def test_del_05_diagnostics_project_elapsed_stages_slowest_latest_and_active_progress(source):
    method = "tests/test_example.py::TestExample::test_method"
    output = b"".join(
        (
            event("stage_started", stage="mypy", elapsed_ms=0),
            event("stage_finished", stage="mypy", returncode=0, elapsed_ms=51),
            event("stage_started", elapsed_ms=0),
            event("pytest_progress", session_elapsed_ms=4, completed=0, passed=0),
            event("test_started", node_id=NODE, session_elapsed_ms=10, completed=0),
            event(
                "test_finished",
                node_id=NODE,
                started_ms=10,
                elapsed_ms=250,
                session_elapsed_ms=260,
                completed=1,
                passed=1,
            ),
            event("test_started", node_id=method, session_elapsed_ms=265, completed=1),
            event(
                "test_finished",
                node_id=method,
                started_ms=265,
                elapsed_ms=30,
                session_elapsed_ms=295,
                completed=2,
                passed=2,
            ),
            event(
                "test_started",
                node_id=f"{NODE}[{CANARY}]",
                session_elapsed_ms=300,
                completed=2,
                passed=2,
                failed=0,
                skipped=0,
                collection_errors=0,
            ),
        )
    )
    projected = project_diagnostics(output, b"", source)
    assert projected["stages"] == [
        {"stage": "mypy", "status": "passed", "returncode": 0, "elapsed_ms": 51},
        {"stage": "pytest", "status": "running", "elapsed_ms": 0},
    ]
    assert projected["active_test"] == NODE
    assert projected["active_test_started_ms"] == 300
    assert projected["latest_test"] == {
        "node_id": method,
        "started_ms": 265,
        "elapsed_ms": 30,
        "finished_ms": 295,
    }
    assert [test["node_id"] for test in projected["slowest_tests"]] == [NODE, method]
    assert projected["pytest_progress"] == {
        "session_elapsed_ms": 300,
        "completed": 2,
        "passed": 2,
        "failed": 0,
        "skipped": 0,
        "collection_errors": 0,
    }
    assert "pytest_summary" not in projected
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
def test_del_05_diagnostics_reject_noninteger_or_unbounded_timing_fields(source, invalid):
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
    assert projected["stages"] == [{"stage": "pytest", "status": "passed", "returncode": 0}]
    for field in ("active_test_started_ms", "latest_test", "slowest_tests", "pytest_progress"):
        assert field not in projected


@pytest.mark.parametrize("invalid", [True, -1, MAX_COUNT + 1, 0.1, float("inf"), "1"])
def test_del_05_diagnostics_reject_invalid_counts_at_progress_and_completion(source, invalid):
    fields = dict.fromkeys(
        ("completed", "passed", "failed", "skipped", "collection_errors"), invalid
    )
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


def test_del_05_diagnostics_bound_slowest_details_and_deduplicate_parameter_cases(source):
    count = MAX_SLOW_TESTS + 20
    (source / "tests/test_many.py").write_text(
        "\n".join(f"def test_case_{index}():\n    pass\n" for index in range(count))
    )
    output = b"".join(
        event(
            "test_finished",
            node_id=f"tests/test_many.py::test_case_{index}[{CANARY}]",
            started_ms=0,
            elapsed_ms=index,
            session_elapsed_ms=index,
            completed=index + 1,
        )
        for index in range(count)
    )
    output += event(
        "test_finished",
        node_id=f"tests/test_many.py::test_case_{count - 1}[other]",
        started_ms=100,
        elapsed_ms=1,
        session_elapsed_ms=101,
        completed=count + 1,
    )
    projected = project_diagnostics(output, b"", source)
    assert len(projected["slowest_tests"]) == MAX_SLOW_TESTS
    assert [item["elapsed_ms"] for item in projected["slowest_tests"]] == list(
        range(count - 1, count - MAX_SLOW_TESTS - 1, -1)
    )
    assert projected["latest_test"]["elapsed_ms"] == 1
    assert projected["pytest_progress"]["completed"] == count + 1
    assert len(json.dumps(projected)) < 4_000
    assert CANARY not in json.dumps(projected)


def test_del_05_diagnostics_integer_bounds_and_old_event_clear_stale_active_time(source):
    output = b"".join(
        (
            event("stage_finished", returncode=0, elapsed_ms=MAX_ELAPSED_MS),
            event(
                "test_finished",
                node_id=NODE,
                started_ms=0,
                elapsed_ms=MAX_ELAPSED_MS,
                session_elapsed_ms=MAX_ELAPSED_MS,
                completed=MAX_COUNT,
            ),
            event("test_started", node_id=NODE, session_elapsed_ms=MAX_ELAPSED_MS),
            event("test_started", node_id=NODE),
        )
    )
    projected = project_diagnostics(output, b"", source)
    assert projected["stages"][0]["elapsed_ms"] == MAX_ELAPSED_MS
    assert projected["latest_test"]["elapsed_ms"] == MAX_ELAPSED_MS
    assert projected["active_test"] == NODE
    assert "active_test_started_ms" not in projected


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
    assert record["seconds"] >= 0
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
    assert 0 <= record["seconds"] < 5
    assert record["stdout_sha256"] == hashlib.sha256(partial).hexdigest()
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
    assert 0 <= record["seconds"] < 5
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
    assert projected["pytest_summary"]["completed"] == 2
    assert projected["pytest_progress"]["completed"] == 2
    stage = projected["stages"][0]
    assert stage == {
        "stage": "pytest",
        "status": "failed",
        "returncode": 1,
        "elapsed_ms": stage["elapsed_ms"],
    }
    assert type(stage["elapsed_ms"]) is int and 0 <= stage["elapsed_ms"] <= MAX_ELAPSED_MS
    assert {item["node_id"] for item in projected["slowest_tests"]} == {
        NODE,
        "tests/test_example.py::test_success",
    }
    assert "active_test" not in projected
    assert "active_test_started_ms" not in projected
    assert (
        projected["latest_test"]["finished_ms"]
        <= projected["pytest_progress"]["session_elapsed_ms"]
    )
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
    assert projected["pytest_summary"]["completed"] == 0
    assert CANARY not in json.dumps(projected)


def test_del_05_diagnostics_interrupted_real_plugin_keeps_preceding_counts_and_active_start(source):
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
        # Handshake before the deliberate timeout: slow CI imports must not be
        # mistaken for a failure to retain an already-running test's progress.
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
