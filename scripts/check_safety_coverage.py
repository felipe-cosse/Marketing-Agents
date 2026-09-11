#!/usr/bin/env python3
"""DEL-07: require exact statement/branch coverage for the pinned safety inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, NoReturn

REQUIRED_MODULES = (
    "application/policies/approval_authorization.py",
    "application/policies/runtime_guard.py",
    "application/policies/write_authorization.py",
    "application/services/approval_boundaries.py",
    "application/services/approval_decisions.py",
    "application/services/approval_integrity.py",
    "application/services/external_action_dispatcher.py",
    "domain/action_hash.py",
    "domain/action_idempotency.py",
    "domain/approval.py",
    "domain/canonical_json.py",
    "domain/plan_hash.py",
    "domain/run_lifecycle.py",
    "domain/schema_hash.py",
    "domain/step_lifecycle.py",
    "infrastructure/adapters/connectors/dispatch.py",
    "security/approval_digest.py",
)
SOURCE_PREFIX = "apps/api/src/marketing_agents/"
EXCEPTION_MODULE = "domain/approval.py"
EXCEPTION_SOURCE = (
    "        else:  # pragma: no cover - exact enum exhaustiveness\n"
    '            raise AssertionError("unhandled approval status")\n'
)
EXCEPTION_SHA256 = "cccfc33a13dd95549a89491fe39dcdcb21ac4641892c9db284baa5f067a9bc2f"
_PRAGMA = re.compile(r"#\s*pragma\s*:\s*no\s*(?:cover|branch)\b", re.IGNORECASE)
_COUNT_KEYS = (
    "covered_lines",
    "num_statements",
    "missing_lines",
    "excluded_lines",
    "num_branches",
    "num_partial_branches",
    "covered_branches",
    "missing_branches",
)


class SafetyCoverageError(ValueError):
    """A bounded policy/report error without source contents or report payloads."""


def _fail(message: str) -> NoReturn:
    raise SafetyCoverageError(message)


def _object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(f"{label}: expected an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON document has duplicate keys")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> NoReturn:
    _fail("JSON document has a non-finite number")


def _read_json(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            _fail("JSON document exceeds the size limit")
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SafetyCoverageError("JSON document is missing, unreadable, or malformed") from error
    return _object(value, "JSON document")


def _policy(path: Path) -> None:
    policy = _read_json(path, maximum_bytes=131_072)
    if set(policy) != {
        "schema_version",
        "description",
        "required_modules",
        "required_coverage",
        "exclusion_exceptions",
    }:
        _fail("policy: unexpected fields")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        _fail("policy: unsupported schema version")
    if type(policy["description"]) is not str or not policy["description"].strip():
        _fail("policy: description is required")
    modules = policy["required_modules"]
    if (
        type(modules) is not list
        or any(type(module) is not str for module in modules)
        or len(modules) != len(REQUIRED_MODULES)
        or set(modules) != set(REQUIRED_MODULES)
    ):
        _fail("policy: inventory must contain exactly the 17 required safety modules")
    target = _object(policy["required_coverage"], "policy coverage")
    if set(target) != {"statements", "branches"} or any(
        type(value) is not int or value != 100 for value in target.values()
    ):
        _fail("policy: statements and branches must both require 100 percent")
    exceptions = policy["exclusion_exceptions"]
    if type(exceptions) is not list or len(exceptions) != 1:
        _fail("policy: only the pinned exhaustive-enum exclusion is permitted")
    exception = _object(exceptions[0], "policy exclusion")
    if set(exception) != {"module", "source", "sha256", "reason"}:
        _fail("policy exclusion: unexpected fields")
    if (
        exception["module"] != EXCEPTION_MODULE
        or exception["source"] != EXCEPTION_SOURCE
        or exception["sha256"] != EXCEPTION_SHA256
        or hashlib.sha256(EXCEPTION_SOURCE.encode()).hexdigest() != EXCEPTION_SHA256
    ):
        _fail("policy: exclusion differs from the pinned source/content")
    if type(exception["reason"]) is not str or not exception["reason"].strip():
        _fail("policy: exclusion reason is required")


def _source(root: Path, module: str) -> tuple[int, set[int]]:
    path = root / SOURCE_PREFIX / module
    try:
        if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            _fail(f"{module}: source must remain inside the repository")
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SafetyCoverageError(f"{module}: source is missing or unreadable") from error
    allowed: set[int] = set()
    if module == EXCEPTION_MODULE:
        if source.count(EXCEPTION_SOURCE) != 1:
            _fail(f"{module}: pinned exclusion source must occur exactly once")
        start = source[: source.index(EXCEPTION_SOURCE)].count("\n") + 1
        allowed = {start, start + 1}
    for line_number, line in enumerate(source.splitlines(), start=1):
        if _PRAGMA.search(line) and line_number not in allowed:
            _fail(f"{module}: unexpected coverage-exclusion pragma")
    return len(source.splitlines()), allowed


def _lines(value: Any, *, label: str, source_lines: int) -> set[int]:
    if type(value) is not list or any(
        type(line) is not int or not 1 <= line <= source_lines for line in value
    ):
        _fail(f"{label}: expected in-range integer line numbers")
    if len(value) != len(set(value)):
        _fail(f"{label}: duplicate lines")
    return set(value)


def _branches(value: Any, *, label: str, source_lines: int) -> set[tuple[int, int]]:
    if type(value) is not list:
        _fail(f"{label}: expected a branch list")
    result: set[tuple[int, int]] = set()
    for pair in value:
        if (
            type(pair) is not list
            or len(pair) != 2
            or any(type(line) is not int for line in pair)
            or not 1 <= pair[0] <= source_lines
            or pair[1] == 0
            or abs(pair[1]) > source_lines
        ):
            _fail(f"{label}: expected in-range integer branch pairs")
        arc = (pair[0], pair[1])
        if arc in result:
            _fail(f"{label}: duplicate branches")
        result.add(arc)
    return result


def _module(root: Path, module: str, value: Any) -> dict[str, int]:
    entry = _object(value, module)
    summary = _object(entry.get("summary"), f"{module} summary")
    counts: dict[str, int] = {}
    for key in _COUNT_KEYS:
        value = summary.get(key)
        if type(value) is not int or value < 0:
            _fail(f"{module}: {key} must be a nonnegative integer")
        counts[key] = value
    if counts["num_statements"] < 1:
        _fail(f"{module}: statement inventory must be nonempty")
    source_lines, allowed_exclusions = _source(root, module)
    executed = _lines(
        entry.get("executed_lines"), label=f"{module} executed lines", source_lines=source_lines
    )
    missing = _lines(
        entry.get("missing_lines"), label=f"{module} missing lines", source_lines=source_lines
    )
    excluded = _lines(
        entry.get("excluded_lines"), label=f"{module} excluded lines", source_lines=source_lines
    )
    covered_arcs = _branches(
        entry.get("executed_branches"),
        label=f"{module} executed branches",
        source_lines=source_lines,
    )
    missing_arcs = _branches(
        entry.get("missing_branches"), label=f"{module} missing branches", source_lines=source_lines
    )
    if excluded != allowed_exclusions:
        _fail(f"{module}: excluded lines differ from the source-pinned exception")
    if (
        executed & missing
        or executed & excluded
        or missing & excluded
        or covered_arcs & missing_arcs
    ):
        _fail(f"{module}: executed, missing, and excluded evidence must be disjoint")
    expected = {
        "covered_lines": len(executed),
        "num_statements": len(executed) + len(missing),
        "missing_lines": len(missing),
        "excluded_lines": len(excluded),
        "num_branches": len(covered_arcs) + len(missing_arcs),
        "covered_branches": len(covered_arcs),
        "missing_branches": len(missing_arcs),
    }
    if any(counts[key] != expected_value for key, expected_value in expected.items()):
        _fail(f"{module}: summary counts disagree with line/branch evidence")
    if missing or missing_arcs or counts["num_partial_branches"]:
        _fail(f"{module}: every statement and measured branch must be covered")
    return counts


def check_coverage(report_path: Path, *, root: Path, policy_path: Path) -> dict[str, Any]:
    """Check selected modules, allowing unrelated modules in a broader report.

    This validates coverage evidence and current exclusion source, not report
    authenticity or semantic correctness of tests. It never trusts percentages.
    """

    _policy(policy_path)
    report = _read_json(report_path, maximum_bytes=134_217_728)
    metadata = _object(report.get("meta"), "report metadata")
    if type(metadata.get("format")) is not int or metadata["format"] != 3:
        _fail("report: coverage.py JSON format 3 is required")
    if metadata.get("branch_coverage") is not True:
        _fail("report: branch measurement must be enabled")
    files = _object(report.get("files"), "report files")
    absent = [module for module in REQUIRED_MODULES if SOURCE_PREFIX + module not in files]
    if absent:
        _fail("report: missing required safety modules: " + ", ".join(absent))
    results = {
        module: _module(root, module, files[SOURCE_PREFIX + module]) for module in REQUIRED_MODULES
    }
    return {
        "valid": True,
        "module_count": len(results),
        "covered_statements": sum(item["covered_lines"] for item in results.values()),
        "covered_branches": sum(item["covered_branches"] for item in results.values()),
        "excluded_lines": sum(item["excluded_lines"] for item in results.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args(argv)
    policy = args.policy or args.root / "docs/verification/safety-coverage.json"
    try:
        result = check_coverage(args.report, root=args.root, policy_path=policy)
    except SafetyCoverageError as error:
        print(json.dumps({"valid": False, "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
