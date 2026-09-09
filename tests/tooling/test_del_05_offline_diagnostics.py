"""Offline gate failures are actionable without exposing runtime payloads."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.del_05_clean_state import Verification, VerificationFailure, verification_signals
from scripts.del_05_offline_diagnostics import (
    MAX_FAILURES,
    MAX_LINE_BYTES,
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
    assert projected["stages"] == [{"stage": "pytest", "status": "failed", "returncode": 1}]
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
    assert project_diagnostics(process.stdout, process.stderr, source)["stages"] == [
        {"stage": "architecture", "status": "failed", "returncode": 7}
    ]


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
