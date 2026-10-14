"""Bounded, source-allowlisted diagnostics for otherwise opaque offline CI gates.

The CLI wraps an existing command without changing its arguments or exit status.
The same module can be loaded with pytest's ``-p`` option. Only fixed protocol
fields, source-declared test names (never parameter IDs), and known exception
classes survive the outer verifier's projection. Raw output remains hash-only.
Integer timings use monotonic milliseconds: stages measure their wrapped command;
tests are measured by the controller from start through setup/call/teardown.
Progress counts completed items separately from existing outcome/phase counts.
Only the latest and eight slowest distinct source tests are retained, taking the
longest parameter case per source node. An active start is not a live stopwatch.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

PREFIX = "DEL05_OFFLINE_DIAGNOSTIC "
STAGES = (
    "ruff-format-source",
    "ruff-check-source",
    "ruff-format-tooling",
    "ruff-check-tooling",
    "mypy",
    "architecture",
    "catalog",
    "pytest",
)
EXCEPTIONS = frozenset(
    {
        "AssertionError",
        "AttributeError",
        "ImportError",
        "IndexError",
        "KeyError",
        "ModuleNotFoundError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "SyntaxError",
        "TimeoutError",
        "TypeError",
        "ValueError",
        "OtherError",
    }
)
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_LINE_BYTES = 4096
MAX_FAILURES = 64
MAX_ACTIVE_NODES = 64
MAX_NODE_BYTES = 512
MAX_SOURCE_BYTES = 1024 * 1024
MAX_COUNT = 1_000_000
MAX_ELAPSED_MS = 24 * 60 * 60 * 1000
MAX_SLOW_TESTS = 8
COUNT_FIELDS = ("completed", "passed", "failed", "skipped", "collection_errors")
AC16_DIAGNOSTIC_PREFIX = "AC16_FIXTURE_DIAGNOSTIC "
AC16_DIAGNOSTIC_NODE = (
    "tests/tooling/test_ac_16_quality.py::QualityRecordTests::"
    "test_summary_rejects_partial_modes_false_success_and_boolean_counters"
)
AC16_DIAGNOSTIC_CASES = frozenset(
    {
        "mode_backend",
        "mode_local_plus_acceptance",
        "valid_integer",
        "error_string",
        "completed_gates_short",
        "passed_gates_short",
        "changed_source_files_nonzero",
        "completed_gates_boolean",
        "passed_gates_boolean",
        "changed_source_files_boolean",
        "source_files_boolean",
        "elapsed_ms_boolean",
    }
)
# Reviewed literal codes from validate_report and its bounded artifact readers.
# Never forward an exception's arbitrary message or import the quality runner.
AC16_DIAGNOSTIC_ERRORS = frozenset(
    {
        "artifact_type_or_size",
        "artifact_changed",
        "artifact_unreadable",
        "duplicate_json_key",
        "nonfinite_json",
        "invalid_json",
        "json_not_object",
        "record_schema",
        "record_not_successful",
        "source_identity_mismatch",
        "source_commit_invalid",
        "source_commit_not_feature_or_merge",
        "record_times",
        "artifact_directory_invalid",
        "gate_plan_mismatch",
        "artifact_inventory",
        "artifact_digest_mismatch",
        "artifact_not_fresh",
        "saved_result_mismatch",
        "summary_schema",
        "incomplete_quality_run",
        "observed_inventory",
        "observed_gate_failure",
        "tool_record_invalid",
        "coverage_record_mismatch",
        "no_quality_error",
        "other_quality_error",
    }
)
AC16_DIAGNOSTIC_MAX_DELTA = 1_000_000_000_000
_AC16_ARTIFACTS = frozenset({".coverage", "coverage.json", "verification.json"})
_AC16_TIME_FIELDS = frozenset({"mtime_minus_started_ns", "finished_minus_mtime_ns"})


def _unique_pairs(items: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate diagnostic field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    del value
    raise ValueError("nonfinite diagnostic number")


def _fixture_diagnostic(value: object) -> dict | None:
    """Copy only this fixture's fixed enums and bounded relative timestamps."""
    if type(value) is not dict or set(value) != {"case", "actual_error", "artifact_times"}:
        return None
    if (
        type(value["case"]) is not str
        or value["case"] not in AC16_DIAGNOSTIC_CASES
        or type(value["actual_error"]) is not str
        or value["actual_error"] not in AC16_DIAGNOSTIC_ERRORS
    ):
        return None
    artifacts = value["artifact_times"]
    if type(artifacts) is not dict or set(artifacts) != _AC16_ARTIFACTS:
        return None
    times = {}
    for name in sorted(_AC16_ARTIFACTS):
        fields = artifacts[name]
        if type(fields) is not dict or set(fields) != _AC16_TIME_FIELDS:
            return None
        deltas = list(fields.values())
        if not (
            all(delta is None for delta in deltas)
            or all(
                type(delta) is int and abs(delta) <= AC16_DIAGNOSTIC_MAX_DELTA for delta in deltas
            )
        ):
            return None
        times[name] = {key: fields[key] for key in sorted(_AC16_TIME_FIELDS)}
    return {"case": value["case"], "actual_error": value["actual_error"], "artifact_times": times}


def _fixture_note(report: object) -> dict | None:
    # Pytest includes PEP 678 notes in this structured field; xdist preserves it.
    # Do not render longrepr, visit traceback locals, or copy arbitrary note text.
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    message = getattr(crash, "message", None)
    if (
        type(message) is not str
        or len(message) > MAX_LINE_BYTES
        or len(message.encode("utf-8", errors="replace")) > MAX_LINE_BYTES
    ):
        return None
    notes = [line for line in message.splitlines() if line.startswith(AC16_DIAGNOSTIC_PREFIX)]
    if len(notes) != 1:
        return None
    try:
        value = json.loads(
            notes[0][len(AC16_DIAGNOSTIC_PREFIX) :],
            object_pairs_hook=_unique_pairs,
            parse_constant=_invalid_constant,
        )
    except (ValueError, RecursionError):
        return None
    return _fixture_diagnostic(value)


def source_test_nodes(root: Path) -> frozenset[str]:
    """Exclude arbitrary messages, parameter values, paths, and dynamic names."""
    nodes: set[str] = set()
    for source in (root / "tests").rglob("test*.py"):
        if source.is_symlink() or not source.is_file():
            continue
        try:
            relative = source.relative_to(root).as_posix()
            if len(relative.encode()) > MAX_NODE_BYTES:
                continue
            # Resolve parent symlinks as well as the final path before reading.
            if not source.resolve().is_relative_to(root.resolve()):
                continue
            nodes.add(relative)  # Collection errors can precede a successful AST parse.
            if source.stat().st_size > MAX_SOURCE_BYTES:
                continue
            module = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError):
            continue

        def visit(body: list[ast.stmt], prefix: str) -> None:
            for statement in body:
                if isinstance(statement, ast.ClassDef):
                    visit(statement.body, prefix + "::" + statement.name)
                elif isinstance(
                    statement, ast.FunctionDef | ast.AsyncFunctionDef
                ) and statement.name.startswith("test"):
                    node = prefix + "::" + statement.name
                    if len(node.encode()) <= MAX_NODE_BYTES:
                        nodes.add(node)

        visit(module.body, relative)
    return frozenset(nodes)


def safe_node(node: object, allowed: frozenset[str]) -> str | None:
    if not isinstance(node, str) or len(node) > MAX_LINE_BYTES:
        return None
    # Parametrization IDs are unconstrained runtime data, including credentials.
    candidate = node.split("[", 1)[0]
    return candidate if candidate in allowed else None


def _bytes(value: bytes | str | None) -> bytes:
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace") if isinstance(value, str) else b""


def _bounded_integer(value: object, maximum: int) -> int | None:
    # bool is an int subclass; floats (including NaN/infinity) are never timings.
    return value if type(value) is int and 0 <= value <= maximum else None


def _progress(event: dict) -> dict:
    progress = {}
    for field in COUNT_FIELDS:
        if (count := _bounded_integer(event.get(field), MAX_COUNT)) is not None:
            progress[field] = count
    if (elapsed := _bounded_integer(event.get("session_elapsed_ms"), MAX_ELAPSED_MS)) is not None:
        progress["session_elapsed_ms"] = elapsed
    return progress


def _test_timing(event: dict, node: str) -> dict | None:
    values = {
        field: _bounded_integer(event.get(field), MAX_ELAPSED_MS)
        for field in ("started_ms", "elapsed_ms", "session_elapsed_ms")
    }
    if any(value is None for value in values.values()):
        return None
    started = values["started_ms"]
    elapsed = values["elapsed_ms"]
    finished = values["session_elapsed_ms"]
    # Independent millisecond rounding permits a one-millisecond discrepancy.
    if started > finished or elapsed > finished - started + 1:
        return None
    return {"node_id": node, "started_ms": started, "elapsed_ms": elapsed, "finished_ms": finished}


def project_diagnostics(stdout: bytes | str | None, stderr: bytes | str | None, root: Path) -> dict:
    """Parse a bounded tail and copy only validated protocol fields, never text."""
    allowed = source_test_nodes(root)
    result: dict = {"schema_version": 1, "stages": [], "failed_tests": [], "truncated": False}
    stages: dict[str, dict] = {}
    failures: set[tuple[str, str, str, str | None]] = set()
    active: dict[str, int] = {}
    active_started: dict[str, int] = {}
    for stream in (stdout, stderr):
        data = _bytes(stream)
        if len(data) > MAX_STREAM_BYTES:
            result["truncated"] = True
            data = data[-MAX_STREAM_BYTES:]
        for line in data.splitlines():
            # Pytest progress characters can precede a plugin's flushed marker.
            marker = line.find(PREFIX.encode())
            if marker < 0:
                continue
            if len(line) > MAX_LINE_BYTES:
                result["truncated"] = True
                continue
            try:
                event = json.loads(
                    line[marker + len(PREFIX) :],
                    object_pairs_hook=_unique_pairs,
                    parse_constant=_invalid_constant,
                )
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            stage = event.get("stage")
            if not isinstance(stage, str) or stage not in STAGES:
                continue
            kind = event.get("event")
            if not isinstance(kind, str):
                continue
            if kind == "stage_started":
                stages[stage] = {"stage": stage, "status": "running"}
                if (
                    elapsed := _bounded_integer(event.get("elapsed_ms"), MAX_ELAPSED_MS)
                ) is not None:
                    stages[stage]["elapsed_ms"] = elapsed
                result["last_stage"] = stage
            elif kind == "stage_finished":
                code = event.get("returncode")
                if type(code) is int and -255 <= code <= 255:
                    stages[stage] = {
                        "stage": stage,
                        "status": "passed" if code == 0 else "failed",
                        "returncode": code,
                    }
                    if (
                        elapsed := _bounded_integer(event.get("elapsed_ms"), MAX_ELAPSED_MS)
                    ) is not None:
                        stages[stage]["elapsed_ms"] = elapsed
                    result["last_stage"] = stage
            elif stage == "pytest" and kind in {"test_started", "test_finished", "test_failed"}:
                node = safe_node(event.get("node_id"), allowed)
                if node is None:
                    continue
                result["last_stage"] = "pytest"
                if progress := _progress(event):
                    result["pytest_progress"] = progress
                if kind == "test_started":
                    count = active.get(node, 0)
                    if count >= MAX_COUNT or (not count and len(active) >= MAX_ACTIVE_NODES):
                        result["truncated"] = True
                        continue
                    # Parameters are never retained. Count overlapping starts
                    # of the same source-declared test instead of losing one
                    # when another parameter instance finishes first.
                    active.pop(node, None)
                    active[node] = count + 1
                    # Source-only projection cannot identify which parameter
                    # finished first. Omit ambiguous active timing, not counts.
                    active_started.pop(node, None)
                    if (
                        not count
                        and (
                            started := _bounded_integer(
                                event.get("session_elapsed_ms"), MAX_ELAPSED_MS
                            )
                        )
                        is not None
                    ):
                        active_started[node] = started
                elif kind == "test_finished":
                    count = active.get(node, 0)
                    if count > 1:
                        active[node] = count - 1
                    else:
                        active.pop(node, None)
                    active_started.pop(node, None)
                    if (timing := _test_timing(event, node)) is not None:
                        result["latest_test"] = timing
                        slowest = result.setdefault("slowest_tests", [])
                        prior = next((item for item in slowest if item["node_id"] == node), None)
                        if prior is None or timing["elapsed_ms"] > prior["elapsed_ms"]:
                            if prior is not None:
                                slowest.remove(prior)
                            slowest.append(timing)
                            slowest.sort(key=lambda item: (-item["elapsed_ms"], item["node_id"]))
                            del slowest[MAX_SLOW_TESTS:]
                else:
                    phase = event.get("phase")
                    exception = event.get("exception")
                    if not isinstance(phase, str) or phase not in {
                        "setup",
                        "call",
                        "teardown",
                        "collection",
                    }:
                        continue
                    if not isinstance(exception, str) or exception not in EXCEPTIONS:
                        exception = "OtherError"
                    diagnostic = (
                        _fixture_diagnostic(event.get("fixture_diagnostic"))
                        if node == AC16_DIAGNOSTIC_NODE
                        and phase == "call"
                        and exception == "AssertionError"
                        else None
                    )
                    diagnostic_key = (
                        json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))
                        if diagnostic is not None
                        else None
                    )
                    failure = (node, phase, exception, diagnostic_key)
                    if failure in failures:
                        continue
                    if len(failures) >= MAX_FAILURES:
                        result["truncated"] = True
                        continue
                    failures.add(failure)
                    record = {"node_id": node, "phase": phase, "exception": exception}
                    if diagnostic is not None:
                        record["fixture_diagnostic"] = diagnostic
                    result["failed_tests"].append(record)
            elif stage == "pytest" and kind in {"pytest_progress", "pytest_finished"}:
                if progress := _progress(event):
                    result["pytest_progress"] = progress
                if kind != "pytest_finished":
                    continue
                summary = {}
                for key in COUNT_FIELDS:
                    count = event.get(key)
                    if type(count) is int and 0 <= count <= MAX_COUNT:
                        summary[key] = count
                if summary:
                    result["pytest_summary"] = summary
    result["stages"] = list(stages.values())
    if active:
        # Retain the previous scalar for serial consumers; the complete bounded
        # multiset is authoritative when more than one instance is active.
        result["active_test"] = next(reversed(active))
        if result["active_test"] in active_started:
            result["active_test_started_ms"] = active_started[result["active_test"]]
        result["active_tests"] = [
            {"node_id": node, "count": count} for node, count in active.items()
        ]
    return result


def _emit(event: str, stage: str, **fields: object) -> None:
    if _is_worker:
        return
    print(
        PREFIX + json.dumps({"event": event, "stage": stage, **fields}),
        file=sys.__stdout__,
        flush=True,
    )


def _exception_class(report: object) -> str:
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    message = getattr(crash, "message", "")
    if not isinstance(message, str):
        return "OtherError"
    if message.startswith("assert "):
        return "AssertionError"
    match = re.match(r"^([A-Za-z_][A-Za-z_0-9]*)(?=:|$)", message)
    name = match.group(1) if match else "OtherError"
    return name if name in EXCEPTIONS else "OtherError"


_allowed_nodes: frozenset[str] = frozenset()
_counts = dict.fromkeys(COUNT_FIELDS, 0)
_is_worker = False
_session_started_ns = 0
_active_timings: dict[bytes, int] = {}


def _elapsed_ms(started_ns: int, now_ns: int | None = None) -> int:
    elapsed = ((time.monotonic_ns() if now_ns is None else now_ns) - started_ns) // 1_000_000
    return max(0, min(elapsed, MAX_ELAPSED_MS))


def _current_progress(now_ns: int | None = None) -> dict:
    return {
        **{field: min(count, MAX_COUNT) for field, count in _counts.items()},
        "session_elapsed_ms": _elapsed_ms(_session_started_ns, now_ns),
    }


def _timing_key(nodeid: str) -> bytes:
    # Parameter IDs distinguish concurrent cases but never persist in diagnostics.
    return hashlib.sha256(nodeid.encode("utf-8", errors="replace")).digest()


def pytest_configure(config) -> None:
    global _allowed_nodes, _is_worker, _session_started_ns
    _session_started_ns = time.monotonic_ns()
    _active_timings.clear()
    # xdist 3.8.0 replays these hooks on its controller. Workers must not emit
    # duplicate protocol or write directly to their execnet transport stdout.
    _is_worker = hasattr(config, "workerinput")
    _allowed_nodes = frozenset() if _is_worker else source_test_nodes(Path(config.rootpath))
    for key in _counts:
        _counts[key] = 0
    if not _is_worker:
        _emit("pytest_progress", "pytest", **_current_progress())


def pytest_runtest_logstart(nodeid, location) -> None:
    del location
    if _is_worker:
        return
    now = time.monotonic_ns()
    if (node := safe_node(nodeid, _allowed_nodes)) is not None:
        if len(_active_timings) < MAX_ACTIVE_NODES:
            _active_timings[_timing_key(nodeid)] = now
        _emit("test_started", "pytest", node_id=node, **_current_progress(now))


def pytest_runtest_logfinish(nodeid, location) -> None:
    del location
    if _is_worker:
        return
    now = time.monotonic_ns()
    _counts["completed"] += 1
    timing = {}
    if (started := _active_timings.pop(_timing_key(nodeid), None)) is not None:
        timing = {
            "started_ms": _elapsed_ms(_session_started_ns, started),
            "elapsed_ms": _elapsed_ms(started, now),
        }
    if (node := safe_node(nodeid, _allowed_nodes)) is not None:
        _emit("test_finished", "pytest", node_id=node, **timing, **_current_progress(now))
    else:
        _emit("pytest_progress", "pytest", **_current_progress(now))


def pytest_runtest_logreport(report) -> None:
    if _is_worker:
        return
    if report.failed:
        _counts["failed"] += 1
        if (node := safe_node(report.nodeid, _allowed_nodes)) is not None:
            exception = _exception_class(report)
            diagnostic = (
                _fixture_note(report)
                if node == AC16_DIAGNOSTIC_NODE
                and report.when == "call"
                and exception == "AssertionError"
                else None
            )
            _emit(
                "test_failed",
                "pytest",
                node_id=node,
                phase=report.when,
                exception=exception,
                **({"fixture_diagnostic": diagnostic} if diagnostic is not None else {}),
                **_current_progress(),
            )
    elif report.skipped:
        _counts["skipped"] += 1
    elif report.when == "call" and report.passed:
        _counts["passed"] += 1


def pytest_collectreport(report) -> None:
    if _is_worker:
        return
    if report.failed:
        _counts["collection_errors"] += 1
        if (node := safe_node(report.nodeid, _allowed_nodes)) is not None:
            _emit(
                "test_failed",
                "pytest",
                node_id=node,
                phase="collection",
                exception=_exception_class(report),
                **_current_progress(),
            )


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    if _is_worker:
        return
    _emit("pytest_finished", "pytest", **_current_progress())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an existing gate command is required")
    started = time.monotonic_ns()
    _emit("stage_started", args.stage, elapsed_ms=0)
    try:
        code = subprocess.run(command, check=False).returncode
    except OSError:
        code = 127
    _emit("stage_finished", args.stage, returncode=code, elapsed_ms=_elapsed_ms(started))
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
