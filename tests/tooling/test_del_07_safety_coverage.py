"""DEL-07: strict safety coverage rejects omissions, rounding and new exclusions."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts import check_safety_coverage as checker

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_safety_coverage.py"
POLICY = ROOT / "docs/verification/safety-coverage.json"
MODULE = checker.REQUIRED_MODULES[0]


@pytest.fixture
def coverage_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Synthetic complete coverage evidence, not execution/acceptance evidence."""

    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(POLICY.read_bytes())
    files: dict[str, Any] = {}
    for module in checker.REQUIRED_MODULES:
        path = tmp_path / checker.SOURCE_PREFIX / module
        path.parent.mkdir(parents=True, exist_ok=True)
        if module == checker.EXCEPTION_MODULE:
            source = (
                "class Boundary:\n    def check(self):\n        if True:\n            return None\n"
                + checker.EXCEPTION_SOURCE
            )
            executed, excluded, arcs = [1, 2, 3, 4], [5, 6], []
        else:
            source = "if condition:\n    value = 1\n"
            executed, excluded, arcs = [1, 2], [], [[1, 2], [1, -1]]
        path.write_text(source)
        files[checker.SOURCE_PREFIX + module] = {
            "executed_lines": executed,
            "missing_lines": [],
            "excluded_lines": excluded,
            "executed_branches": arcs,
            "missing_branches": [],
            "summary": {
                "covered_lines": len(executed),
                "num_statements": len(executed),
                "missing_lines": 0,
                "excluded_lines": len(excluded),
                "num_branches": len(arcs),
                "num_partial_branches": 0,
                "covered_branches": len(arcs),
                "missing_branches": 0,
                "percent_covered": 100.0,
                "percent_covered_display": "100",
            },
        }
    report: dict[str, Any] = {
        "meta": {"format": 3, "version": "7.15.4", "branch_coverage": True},
        "files": files,
    }
    report_path = tmp_path / "coverage.json"
    report_path.write_text(json.dumps(report))
    return tmp_path, policy_path, report_path, report


def _entry(report: dict[str, Any], module: str = MODULE) -> dict[str, Any]:
    return report["files"][checker.SOURCE_PREFIX + module]


def _check(fixture: tuple[Path, Path, Path, dict[str, Any]]) -> dict[str, Any]:
    root, policy, report_path, report = fixture
    report_path.write_text(json.dumps(report))
    return checker.check_coverage(report_path, root=root, policy_path=policy)


def test_del_07_complete_synthetic_inventory_passes(coverage_fixture: Any) -> None:
    result = _check(coverage_fixture)
    assert result == {
        "valid": True,
        "module_count": 17,
        "covered_statements": 36,
        "covered_branches": 32,
        "excluded_lines": 2,
    }


def test_del_07_broader_report_may_include_unrelated_modules(coverage_fixture: Any) -> None:
    coverage_fixture[3]["files"]["unrelated/module.py"] = {}
    assert _check(coverage_fixture)["module_count"] == 17


@pytest.mark.parametrize("modules", [[], [MODULE], list(checker.REQUIRED_MODULES[:-1])])
def test_del_07_policy_cannot_reduce_required_inventory(
    coverage_fixture: Any, modules: list[str]
) -> None:
    _, policy_path, _, _ = coverage_fixture
    policy = json.loads(policy_path.read_text())
    policy["required_modules"] = modules
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(checker.SafetyCoverageError, match="exactly the 17"):
        _check(coverage_fixture)


def test_del_07_policy_cannot_duplicate_or_substitute_a_required_module(
    coverage_fixture: Any,
) -> None:
    _, policy_path, _, _ = coverage_fixture
    policy = json.loads(policy_path.read_text())
    policy["required_modules"][-1] = policy["required_modules"][0]
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(checker.SafetyCoverageError, match="exactly the 17"):
        _check(coverage_fixture)


@pytest.mark.parametrize("value", [0, 99, True, 100.0])
def test_del_07_policy_cannot_weaken_or_coerce_the_target(
    coverage_fixture: Any, value: Any
) -> None:
    _, policy_path, _, _ = coverage_fixture
    policy = json.loads(policy_path.read_text())
    policy["required_coverage"]["branches"] = value
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(checker.SafetyCoverageError, match="both require 100"):
        _check(coverage_fixture)


@pytest.mark.parametrize(
    "field, value",
    [
        ("meta", None),
        ("files", []),
        ("files", {}),
        ("meta", {"format": True, "branch_coverage": True}),
        ("meta", {"format": 3, "branch_coverage": 1}),
    ],
)
def test_del_07_missing_or_malformed_report_metadata_fails(
    coverage_fixture: Any, field: str, value: Any
) -> None:
    coverage_fixture[3][field] = value
    with pytest.raises(checker.SafetyCoverageError):
        _check(coverage_fixture)


@pytest.mark.parametrize("count", [1, 8, 17])
def test_del_07_every_required_module_must_be_reported(coverage_fixture: Any, count: int) -> None:
    report = coverage_fixture[3]
    for module in checker.REQUIRED_MODULES[:count]:
        del report["files"][checker.SOURCE_PREFIX + module]
    with pytest.raises(checker.SafetyCoverageError, match="missing required safety modules"):
        _check(coverage_fixture)


@pytest.mark.parametrize("field", list(checker._COUNT_KEYS))
@pytest.mark.parametrize("value", [-1, True, 1.0, "1", None])
def test_del_07_counts_are_exact_nonnegative_integers(
    coverage_fixture: Any, field: str, value: Any
) -> None:
    _entry(coverage_fixture[3])["summary"][field] = value
    with pytest.raises(checker.SafetyCoverageError, match="nonnegative integer"):
        _check(coverage_fixture)


def test_del_07_zero_statement_report_cannot_satisfy_a_module(coverage_fixture: Any) -> None:
    entry = _entry(coverage_fixture[3])
    entry["summary"]["num_statements"] = 0
    with pytest.raises(checker.SafetyCoverageError, match="nonempty"):
        _check(coverage_fixture)


@pytest.mark.parametrize("kind", ["statement", "branch"])
def test_del_07_rounded_hundred_percent_cannot_hide_missing_execution(
    coverage_fixture: Any, kind: str
) -> None:
    entry = _entry(coverage_fixture[3])
    if kind == "statement":
        entry["missing_lines"] = [entry["executed_lines"].pop()]
        entry["summary"].update(covered_lines=1, missing_lines=1)
    else:
        entry["missing_branches"] = [entry["executed_branches"].pop()]
        entry["summary"].update(covered_branches=1, missing_branches=1, num_partial_branches=1)
    assert entry["summary"]["percent_covered"] == 100.0
    with pytest.raises(checker.SafetyCoverageError, match="every statement and measured branch"):
        _check(coverage_fixture)


@pytest.mark.parametrize(
    "field",
    [
        "covered_lines",
        "num_statements",
        "excluded_lines",
        "num_branches",
        "covered_branches",
        "missing_branches",
        "missing_lines",
    ],
)
def test_del_07_summary_cannot_disagree_with_evidence(coverage_fixture: Any, field: str) -> None:
    _entry(coverage_fixture[3])["summary"][field] += 1
    with pytest.raises(checker.SafetyCoverageError, match="counts disagree"):
        _check(coverage_fixture)


@pytest.mark.parametrize(
    "field, value",
    [
        ("executed_lines", [True]),
        ("executed_lines", [1, 1]),
        ("missing_lines", [0]),
        ("excluded_lines", [999]),
        ("executed_branches", [[1, True]]),
        ("executed_branches", [[1, 0]]),
        ("executed_branches", [[1, 999]]),
        ("executed_branches", [[-1, 1]]),
        ("executed_branches", [[1, 2], [1, 2]]),
        ("missing_branches", "none"),
    ],
)
def test_del_07_line_and_branch_arrays_require_unique_valid_integers(
    coverage_fixture: Any, field: str, value: Any
) -> None:
    _entry(coverage_fixture[3])[field] = value
    with pytest.raises(checker.SafetyCoverageError):
        _check(coverage_fixture)


@pytest.mark.parametrize("kind", ["line", "branch"])
def test_del_07_executed_and_missing_evidence_cannot_overlap(
    coverage_fixture: Any, kind: str
) -> None:
    entry = _entry(coverage_fixture[3])
    if kind == "line":
        entry["missing_lines"] = [1]
    else:
        entry["missing_branches"] = [[1, 2]]
    with pytest.raises(checker.SafetyCoverageError, match="disjoint"):
        _check(coverage_fixture)


@pytest.mark.parametrize("module", [MODULE, checker.EXCEPTION_MODULE])
def test_del_07_added_report_exclusions_fail(coverage_fixture: Any, module: str) -> None:
    entry = _entry(coverage_fixture[3], module)
    entry["excluded_lines"].append(1)
    entry["summary"]["excluded_lines"] += 1
    with pytest.raises(checker.SafetyCoverageError, match="source-pinned exception"):
        _check(coverage_fixture)


@pytest.mark.parametrize(
    "pragma", ["# pragma: no cover", "# pragma: no branch", "# PRAGMA: NO COVER"]
)
def test_del_07_new_source_exclusion_pragmas_fail(coverage_fixture: Any, pragma: str) -> None:
    root = coverage_fixture[0]
    path = root / checker.SOURCE_PREFIX / MODULE
    path.write_text(path.read_text() + f"unused = 2  {pragma}\n")
    with pytest.raises(checker.SafetyCoverageError, match="unexpected coverage-exclusion pragma"):
        _check(coverage_fixture)


def test_del_07_exception_tracks_exact_source_instead_of_fixed_line_numbers(
    coverage_fixture: Any,
) -> None:
    root, _, _, report = coverage_fixture
    path = root / checker.SOURCE_PREFIX / checker.EXCEPTION_MODULE
    path.write_text("\n\n" + path.read_text())
    entry = _entry(report, checker.EXCEPTION_MODULE)
    entry["executed_lines"] = [line + 2 for line in entry["executed_lines"]]
    entry["excluded_lines"] = [line + 2 for line in entry["excluded_lines"]]
    assert _check(coverage_fixture)["excluded_lines"] == 2


def test_del_07_modified_exception_source_fails_even_with_same_line_numbers(
    coverage_fixture: Any,
) -> None:
    root = coverage_fixture[0]
    path = root / checker.SOURCE_PREFIX / checker.EXCEPTION_MODULE
    path.write_text(path.read_text().replace("unhandled approval status", "hidden new behavior"))
    with pytest.raises(checker.SafetyCoverageError, match="pinned exclusion source"):
        _check(coverage_fixture)


@pytest.mark.parametrize("change", ["append", "source", "hash"])
def test_del_07_policy_cannot_authorize_a_new_exception(coverage_fixture: Any, change: str) -> None:
    _, policy_path, _, _ = coverage_fixture
    policy = json.loads(policy_path.read_text())
    if change == "append":
        policy["exclusion_exceptions"].append(deepcopy(policy["exclusion_exceptions"][0]))
    elif change == "source":
        policy["exclusion_exceptions"][0]["source"] += "pass\n"
    else:
        policy["exclusion_exceptions"][0]["sha256"] = "0" * 64
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(checker.SafetyCoverageError, match="exclusion"):
        _check(coverage_fixture)


@pytest.mark.parametrize(
    "payload", ['{"files":{},"files":{}}', '{"secret":"canary",', '{"count":NaN}', "[]"]
)
def test_del_07_malformed_duplicate_and_nonfinite_json_fails_without_echoing_payload(
    coverage_fixture: Any, payload: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root, policy, report_path, _ = coverage_fixture
    report_path.write_text(payload)
    assert (
        checker.main(["--root", str(root), "--policy", str(policy), "--report", str(report_path)])
        == 1
    )
    output = capsys.readouterr().out
    assert json.loads(output)["valid"] is False
    assert "canary" not in output


def test_del_07_absent_report_and_source_fail_closed(coverage_fixture: Any) -> None:
    root, policy, report_path, _ = coverage_fixture
    with pytest.raises(checker.SafetyCoverageError, match="missing, unreadable"):
        checker.check_coverage(report_path.with_name("absent.json"), root=root, policy_path=policy)
    (root / checker.SOURCE_PREFIX / MODULE).unlink()
    with pytest.raises(checker.SafetyCoverageError, match="source is missing"):
        _check(coverage_fixture)


def test_del_07_cli_reports_success_and_failure_exit_codes(coverage_fixture: Any) -> None:
    root, policy, report_path, report = coverage_fixture
    command = [
        sys.executable,
        str(SCRIPT),
        "--root",
        str(root),
        "--policy",
        str(policy),
        "--report",
        str(report_path),
    ]
    passed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
    assert passed.returncode == 0, passed.stderr
    assert json.loads(passed.stdout)["module_count"] == 17
    del report["files"][checker.SOURCE_PREFIX + MODULE]
    report_path.write_text(json.dumps(report))
    failed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
    assert failed.returncode == 1
    assert json.loads(failed.stdout)["valid"] is False
