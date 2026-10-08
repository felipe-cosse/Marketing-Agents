"""AC-15 offline gate failures are actionable without exposing runtime payloads."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
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
    assert module._counts == {"passed": 0, "failed": 0, "skipped": 0, "collection_errors": 0}


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
        "passed": 2,
        "failed": 1,
        "skipped": 1,
        "collection_errors": 1,
    }
    assert sum(item["event"] == "test_started" for item in emitted) == 4
    assert sum(item["event"] == "test_finished" for item in emitted) == 4
    assert CANARY not in json.dumps(emitted)


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
        "passed": 4,
        "failed": 1,
        "skipped": 1,
        "collection_errors": 0,
    }
    assert projected["failed_tests"] == [
        {"node_id": "tests/test_a.py::test_shared", "phase": "call", "exception": "ValueError"}
    ]
    assert "active_test" not in projected and "active_tests" not in projected
    assert projected["stages"] == [{"stage": "pytest", "status": "failed", "returncode": 1}]
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
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "collection_errors": 1,
    }
    assert len(projected["failed_tests"]) == 1
    assert projected["failed_tests"][0]["node_id"] == "tests/test_example.py"
    assert projected["failed_tests"][0]["phase"] == "collection"
    assert CANARY not in json.dumps(projected)
