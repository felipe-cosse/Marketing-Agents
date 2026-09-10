"""Bounded, source-allowlisted diagnostics for otherwise opaque offline CI gates.

The CLI wraps an existing command without changing its arguments or exit status.
The same module can be loaded with pytest's ``-p`` option. Only fixed protocol
fields, source-declared test names (never parameter IDs), and known exception
classes survive the outer verifier's projection. Raw output remains hash-only.
Integer timings use monotonic milliseconds: stages measure their wrapped command;
tests start relative to pytest configuration and include setup/call/teardown.
Progress reports completed items separately from existing outcome/phase counts.
Only the latest and eight slowest distinct source tests are retained, taking the
longest parameter case per source node. An active start is not a live stopwatch.
"""

from __future__ import annotations

import argparse
import ast
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
MAX_NODE_BYTES = 512
MAX_SOURCE_BYTES = 1024 * 1024
MAX_COUNT = 1_000_000
MAX_ELAPSED_MS = 24 * 60 * 60 * 1000
MAX_SLOW_TESTS = 8
COUNT_FIELDS = ("completed", "passed", "failed", "skipped", "collection_errors")


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
    failures: set[tuple[str, str, str]] = set()
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
                event = json.loads(line[marker + len(PREFIX) :])
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
                    result["active_test"] = node
                    result.pop("active_test_started_ms", None)
                    if (
                        started := _bounded_integer(event.get("session_elapsed_ms"), MAX_ELAPSED_MS)
                    ) is not None:
                        result["active_test_started_ms"] = started
                elif kind == "test_finished":
                    if result.get("active_test") == node:
                        result.pop("active_test", None)
                        result.pop("active_test_started_ms", None)
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
                    failure = (node, phase, exception)
                    if failure in failures:
                        continue
                    if len(failures) >= MAX_FAILURES:
                        result["truncated"] = True
                        continue
                    failures.add(failure)
                    result["failed_tests"].append(
                        {"node_id": node, "phase": phase, "exception": exception}
                    )
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
    return result


def _emit(event: str, stage: str, **fields: object) -> None:
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
_session_started_ns = 0
_active_test: tuple[str, int] | None = None


def _elapsed_ms(started_ns: int, now_ns: int | None = None) -> int:
    elapsed = ((time.monotonic_ns() if now_ns is None else now_ns) - started_ns) // 1_000_000
    return max(0, min(elapsed, MAX_ELAPSED_MS))


def _current_progress(now_ns: int | None = None) -> dict:
    return {
        **{field: min(count, MAX_COUNT) for field, count in _counts.items()},
        "session_elapsed_ms": _elapsed_ms(_session_started_ns, now_ns),
    }


def pytest_configure(config) -> None:
    global _allowed_nodes, _session_started_ns, _active_test
    _session_started_ns = time.monotonic_ns()
    _active_test = None
    _allowed_nodes = source_test_nodes(Path(config.rootpath))
    for key in _counts:
        _counts[key] = 0
    _emit("pytest_progress", "pytest", **_current_progress())


def pytest_runtest_logstart(nodeid, location) -> None:
    global _active_test
    del location
    now = time.monotonic_ns()
    _active_test = (nodeid, now)
    if (node := safe_node(nodeid, _allowed_nodes)) is not None:
        _emit("test_started", "pytest", node_id=node, **_current_progress(now))


def pytest_runtest_logfinish(nodeid, location) -> None:
    global _active_test
    del location
    now = time.monotonic_ns()
    _counts["completed"] += 1
    timing = {}
    if _active_test is not None and _active_test[0] == nodeid:
        timing = {
            "started_ms": _elapsed_ms(_session_started_ns, _active_test[1]),
            "elapsed_ms": _elapsed_ms(_active_test[1], now),
        }
    _active_test = None
    if (node := safe_node(nodeid, _allowed_nodes)) is not None:
        _emit("test_finished", "pytest", node_id=node, **timing, **_current_progress(now))
    else:
        _emit("pytest_progress", "pytest", **_current_progress(now))


def pytest_runtest_logreport(report) -> None:
    if report.failed:
        _counts["failed"] += 1
        if (node := safe_node(report.nodeid, _allowed_nodes)) is not None:
            _emit(
                "test_failed",
                "pytest",
                node_id=node,
                phase=report.when,
                exception=_exception_class(report),
                **_current_progress(),
            )
    elif report.skipped:
        _counts["skipped"] += 1
    elif report.when == "call" and report.passed:
        _counts["passed"] += 1


def pytest_collectreport(report) -> None:
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
