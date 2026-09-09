"""Bounded, source-allowlisted diagnostics for otherwise opaque offline CI gates.

The CLI wraps an existing command without changing its arguments or exit status.
The same module can be loaded with pytest's ``-p`` option. Only fixed protocol
fields, source-declared test names (never parameter IDs), and known exception
classes survive the outer verifier's projection. Raw output remains hash-only.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
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
                result["last_stage"] = stage
            elif kind == "stage_finished":
                code = event.get("returncode")
                if type(code) is int and -255 <= code <= 255:
                    stages[stage] = {
                        "stage": stage,
                        "status": "passed" if code == 0 else "failed",
                        "returncode": code,
                    }
                    result["last_stage"] = stage
            elif stage == "pytest" and kind in {"test_started", "test_finished", "test_failed"}:
                node = safe_node(event.get("node_id"), allowed)
                if node is None:
                    continue
                result["last_stage"] = "pytest"
                if kind == "test_started":
                    result["active_test"] = node
                elif kind == "test_finished":
                    if result.get("active_test") == node:
                        result.pop("active_test", None)
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
            elif stage == "pytest" and kind == "pytest_finished":
                summary = {}
                for key in ("passed", "failed", "skipped", "collection_errors"):
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
_counts = {"passed": 0, "failed": 0, "skipped": 0, "collection_errors": 0}


def pytest_configure(config) -> None:
    global _allowed_nodes
    _allowed_nodes = source_test_nodes(Path(config.rootpath))
    for key in _counts:
        _counts[key] = 0


def pytest_runtest_logstart(nodeid, location) -> None:
    del location
    if (node := safe_node(nodeid, _allowed_nodes)) is not None:
        _emit("test_started", "pytest", node_id=node)


def pytest_runtest_logfinish(nodeid, location) -> None:
    del location
    if (node := safe_node(nodeid, _allowed_nodes)) is not None:
        _emit("test_finished", "pytest", node_id=node)


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
            )


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    _emit("pytest_finished", "pytest", **_counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an existing gate command is required")
    _emit("stage_started", args.stage)
    try:
        code = subprocess.run(command, check=False).returncode
    except OSError:
        code = 127
    _emit("stage_finished", args.stage, returncode=code)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
