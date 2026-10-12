"""AC-16: source-bound full local quality run and trusted-local record consistency.

The audit checks consistency, not authenticity: a host owner can forge unsigned
reports. AC-16 also requires an independently observed successful full run.
No CLI accepts replacement gates, executors, partial inventories or input reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts import verify_local as runner
from scripts.check_safety_coverage import SafetyCoverageError, check_coverage

ROOT = Path(__file__).resolve().parents[1]
STORE = Path("/tmp/marketing-agents-ac16")
LOCKS = ("uv.lock", "pnpm-lock.yaml", "package.json", "apps/web/package.json")
ARTIFACTS = ("verification.json", "coverage.json", ".coverage")
MAX_BYTES = 128 * 1024 * 1024
TRUST = "trusted-local-consistency-not-authenticated-execution"
COMMAND_OVERRIDES = {
    "UV",
    "PYTHON",
    "MAKEFILES",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTEST_PLUGINS",
    "NODE_OPTIONS",
    "NODE_PATH",
    "BASH_ENV",
    "ENV",
    "OBJ06_NATIVE_READY",
}
ENVIRONMENT_KEYS = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "UV_CACHE_DIR",
    "COVERAGE_FILE",
    "PYTHONDONTWRITEBYTECODE",
    "UV_OFFLINE",
    "UV_FROZEN",
    "APP_ENV",
    "AUTH_MODE",
    "LLM_PROVIDER",
    "CONNECTOR_MODE",
    "ALLOW_EXTERNAL_NETWORK",
    "REAL_LLM_OPT_IN",
    "REAL_CONNECTOR_OPT_IN",
}


class QualityError(ValueError):
    """Bounded errors exclude raw command output, source and environment values."""


def expected_gates(coverage: Path) -> tuple[runner.Gate, ...]:
    """Independent reviewed contract; do not derive this from runner.gates()."""

    def make(name: str, target: str, timeout: int = 300) -> runner.Gate:
        return runner.Gate(name, ("make", "-j1", f"PYTHON={sys.executable}", target), timeout)

    return (
        make("catalog-validate", "catalog-validate"),
        make("catalog-release", "verify-catalog-release"),
        runner.Gate(
            "catalog-tests",
            (
                sys.executable,
                "-m",
                "pytest",
                "tests/catalog",
                "--cov=marketing_agents",
                "--cov-branch",
                "--cov-report=",
            ),
            600,
        ),
        make("backend-format", "format-check"),
        make("backend-lint", "lint"),
        make("backend-types", "typecheck", 600),
        make("repository", "verify-repository", 900),
        make("api-contract", "api-contract-check"),
        make("network-canaries", "test-network"),
        runner.Gate(
            "api-contract-tests", ("node", "--test", "tools/api-contract/generate.test.mjs"), 300
        ),
        runner.Gate(
            "browser-inventory-tests",
            ("node", "--test", "tests/network/web_e2e_inventory.test.mjs"),
            300,
        ),
        runner.Gate(
            "backend-tests",
            (
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "--ignore=tests/catalog",
                "--cov=marketing_agents",
                "--cov-branch",
                "--cov-append",
                f"--cov-report=json:{coverage}",
            ),
            1800,
        ),
        runner.Gate(
            "safety-coverage",
            (sys.executable, "-m", "scripts.check_safety_coverage", "--report", str(coverage)),
            60,
        ),
        make("web-format", "web-format-check"),
        make("web-lint", "web-lint"),
        make("web-types", "web-typecheck", 600),
        make("web-unit", "web-test", 1800),
        make("web-build", "web-build", 600),
        runner.Gate(
            "browser-network-canary",
            ("node", "apps/web/scripts/run-browser-network-canary.mjs"),
            300,
        ),
        make("web-browser", "web-test-e2e", 1800),
    )


def _bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_BYTES:
            raise QualityError("artifact_type_or_size")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
                raise QualityError("artifact_changed")
            if not stat.S_ISREG(before.st_mode):
                raise QualityError("artifact_type_or_size")
            raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise QualityError("artifact_type_or_size")
            after = os.fstat(stream.fileno())
        if before != after:
            # atime can change while reading; content identity excludes it.
            fields = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_ino", "st_dev")
            if any(getattr(before, field) != getattr(after, field) for field in fields):
                raise QualityError("artifact_changed")
        return raw
    except OSError as error:
        raise QualityError("artifact_unreadable") from error


def _digest(path: Path) -> str:
    return hashlib.sha256(_bytes(path)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualityError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise QualityError("nonfinite_json")


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = _bytes(path)
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QualityError("invalid_json") from error
    if type(value) is not dict:
        raise QualityError("json_not_object")
    return value


def source_identity(root: Path) -> dict[str, Any]:
    source = runner.source_snapshot(root)
    commit = runner._resolve_ref(root, "HEAD")
    runner.require_matching_commit(root, commit, source)
    tree = runner._git(root, "rev-parse", f"{commit}^{{tree}}").strip().decode("ascii")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
        raise QualityError("source_tree_invalid")
    normalized = {
        name: {
            "kind": item.kind,
            "mode": 0o120000
            if item.kind == "symlink"
            else (0o100755 if item.mode & stat.S_IXUSR else 0o100644),
            "digest": item.digest,
        }
        for name, item in sorted(source.items())
    }
    try:
        locks = {name: source[name].digest for name in LOCKS}
    except KeyError as error:
        raise QualityError("source_locks_missing") from error
    return {
        "commit": commit,
        "tree": tree,
        "files": len(source),
        "sha256": hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest(),
        "locks": locks,
    }


def _plan(coverage: Path) -> list[dict[str, Any]]:
    # Interpreter and fresh artifact paths vary between run and audit; only these
    # exact generated positions are normalized. All other arguments remain bound.
    result = []
    for gate in expected_gates(coverage):
        argv = [
            "<python>"
            if arg == sys.executable
            else "PYTHON=<python>"
            if arg == f"PYTHON={sys.executable}"
            else arg.replace(str(coverage), "<coverage-report>")
            for arg in gate.argv
        ]
        result.append({"name": gate.name, "argv": argv, "timeout_seconds": gate.timeout_seconds})
    return result


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def validate_report(report: dict[str, Any], expected_source: dict[str, Any], root: Path) -> None:
    """Check record/source/artifact consistency, never claim signed provenance."""
    fields = {
        "schema_version",
        "valid",
        "trust",
        "source",
        "started_ns",
        "finished_ns",
        "tools",
        "plan",
        "observed",
        "artifact_directory",
        "artifacts",
        "coverage",
    }
    if (
        set(report) != fields
        or type(report["schema_version"]) is not int
        or report["schema_version"] != 1
    ):
        raise QualityError("record_schema")
    if report["valid"] is not True or report["trust"] != TRUST:
        raise QualityError("record_not_successful")
    recorded_source = report["source"]
    if type(recorded_source) is not dict or set(recorded_source) != set(expected_source):
        raise QualityError("source_identity_mismatch")
    # An identical-tree non-fast-forward merge may consume feature-run evidence;
    # the recorded commit must still resolve to that exact committed source.
    if any(recorded_source[k] != expected_source[k] for k in expected_source if k != "commit"):
        raise QualityError("source_identity_mismatch")
    commit = recorded_source.get("commit")
    if type(commit) is not str or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise QualityError("source_commit_invalid")
    if runner._resolve_ref(root, commit) != commit:
        raise QualityError("source_commit_invalid")
    if commit != expected_source["commit"]:
        parents = (
            runner._git(root, "show", "-s", "--format=%P", expected_source["commit"])
            .decode()
            .split()
        )
        if len(parents) != 2 or parents[1] != commit:
            raise QualityError("source_commit_not_feature_or_merge")
    runner.require_matching_commit(root, commit, runner.source_snapshot(root))
    if not (
        _integer(report["started_ns"], 1)
        and _integer(report["finished_ns"], 1)
        and report["finished_ns"] >= report["started_ns"]
    ):
        raise QualityError("record_times")
    if type(report["artifact_directory"]) is not str:
        raise QualityError("artifact_directory_invalid")
    directory = Path(report["artifact_directory"])
    if (
        not directory.is_absolute()
        or directory.is_symlink()
        or not directory.is_dir()
        or directory.resolve().is_relative_to(root.resolve())
    ):
        raise QualityError("artifact_directory_invalid")
    if report["plan"] != _plan(directory / "coverage.json"):
        raise QualityError("gate_plan_mismatch")
    if type(report["artifacts"]) is not dict or set(report["artifacts"]) != set(ARTIFACTS):
        raise QualityError("artifact_inventory")
    for name in ARTIFACTS:
        if report["artifacts"][name] != _digest(directory / name):
            raise QualityError("artifact_digest_mismatch")
        modified = (directory / name).stat().st_mtime_ns
        if not report["started_ns"] <= modified <= report["finished_ns"]:
            raise QualityError("artifact_not_fresh")
    saved = _read(directory / "verification.json")
    if set(saved) != {"summary", "gates"} or report["observed"] != saved["gates"]:
        raise QualityError("saved_result_mismatch")
    summary = saved["summary"]
    if type(summary) is not dict or set(summary) != {
        "valid",
        "mode",
        "error",
        "completed_gates",
        "passed_gates",
        "source_files",
        "changed_source_files",
        "elapsed_ms",
        "artifact_directory",
    }:
        raise QualityError("summary_schema")
    if (
        summary["valid"] is not True
        or summary["mode"] != "local"
        or summary["error"] is not None
        or summary["artifact_directory"] != str(directory)
        or any(
            type(summary[k]) is not int or summary[k] != v
            for k, v in {
                "completed_gates": 20,
                "passed_gates": 20,
                "changed_source_files": 0,
                "source_files": expected_source["files"],
            }.items()
        )
        or not _integer(summary["elapsed_ms"])
    ):
        raise QualityError("incomplete_quality_run")
    observed = report["observed"]
    if type(observed) is not list or len(observed) != 20:
        raise QualityError("observed_inventory")
    for result, gate in zip(observed, expected_gates(directory / "coverage.json"), strict=True):
        if (
            type(result) is not dict
            or set(result) != {"name", "status", "returncode", "elapsed_ms"}
            or result["name"] != gate.name
            or result["status"] != "passed"
            or type(result["returncode"]) is not int
            or result["returncode"] != 0
            or not _integer(result["elapsed_ms"])
        ):
            raise QualityError("observed_gate_failure")
    tools = report["tools"]
    if (
        type(tools) is not dict
        or set(tools) != {"python", "node", "pnpm", "uv", "platform"}
        or any(
            type(value) is not str or not re.fullmatch(r"[a-zA-Z0-9 .+_()/-]{1,100}", value)
            for value in tools.values()
        )
    ):
        raise QualityError("tool_record_invalid")
    coverage = check_coverage(
        directory / "coverage.json",
        root=root,
        policy_path=root / "docs/verification/safety-coverage.json",
    )
    if report["coverage"] != coverage:
        raise QualityError("coverage_record_mismatch")


def _publish(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise QualityError("record_symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".ac16-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _tools() -> dict[str, str]:
    result = {
        "python": platform.python_version(),
        "platform": platform.system() + "/" + platform.machine(),
    }
    for name in ("node", "pnpm", "uv"):
        try:
            process = subprocess.run(
                (name, "--version"),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise QualityError("tool_unavailable") from error
        value = process.stdout.decode("ascii", errors="replace").strip()
        if process.returncode or not re.fullmatch(r"[a-zA-Z0-9 .+_()/-]{1,100}", value):
            raise QualityError("tool_version_invalid")
        result[name] = value
    return result


def _environment_preflight() -> None:
    for key in os.environ:
        upper = key.upper()
        if upper == "UV":
            executable = shutil.which("uv")
            supplied = Path(os.environ[key])
            if (
                executable
                and supplied.is_absolute()
                and supplied.resolve() == Path(executable).resolve()
            ):
                # uv injects its own executable. Match the tool whose version is
                # recorded; do not forward this Make override to child gates.
                continue
        if upper == "UV_RUN_RECURSION_DEPTH" and os.environ[key] in {
            str(value) for value in range(1, 9)
        }:
            # uv 0.10.7 adds this internal launch-depth counter. It is neither a
            # command selector nor propagated to child gates by the allowlist.
            continue
        if upper in COMMAND_OVERRIDES or (
            upper.startswith(("UV_", "PNPM_", "NPM_CONFIG_", "PLAYWRIGHT_", "PW_TEST_"))
            and upper not in ENVIRONMENT_KEYS
        ):
            raise QualityError("unsupported_quality_environment_override")


def run_quality(root: Path) -> dict[str, Any]:
    before = source_identity(root)
    path = STORE / (before["tree"] + ".json")
    # Invalidate any previous pass before tool setup or gate execution. A failed
    # retry cannot leave a stale successful record eligible for audit.
    _publish(path, {"valid": False, "error": "run_in_progress", "source": before})
    _environment_preflight()
    started = time.time_ns()
    observed: list[dict[str, Any]] = []
    tool_versions = _tools()
    artifact_directory: Path | None = None

    def execute(gate: runner.Gate, work: Path, environment: Any) -> runner.GateResult:
        nonlocal artifact_directory
        directory = Path(environment["COVERAGE_FILE"]).parent
        expected = expected_gates(directory / "coverage.json")
        if len(observed) >= len(expected) or gate != expected[len(observed)]:
            raise runner.VerificationError("ac16_actual_gate_plan_mismatch")
        if artifact_directory is None:
            artifact_directory = directory
            if any(directory.iterdir()):
                raise runner.VerificationError("ac16_artifacts_not_fresh")
        elif artifact_directory != directory:
            raise runner.VerificationError("ac16_artifact_directory_changed")
        # Retain the verifier's validated mock/safety selections and fresh
        # coverage binding. Do not pass credentials or unrelated caller config
        # into child test processes. This is not an OS network-isolation claim.
        selected_environment = {
            key: value for key, value in environment.items() if key in ENVIRONMENT_KEYS
        }
        result = runner.execute_gate(gate, work, selected_environment)
        observed.append(asdict(result))
        return result

    # No acceptance/backend mode and no CLI filtering: this is the unchanged
    # full aggregate used by make verify, with observation around real execution.
    summary = runner.verify_local(root, executor=execute)
    after = source_identity(root)
    if before != after:
        raise QualityError("source_changed_during_run")
    if summary.get("valid") is not True or artifact_directory is None:
        raise QualityError("full_quality_run_failed")
    report: dict[str, Any] = {
        "schema_version": 1,
        "valid": True,
        "trust": TRUST,
        "source": before,
        "started_ns": started,
        "finished_ns": time.time_ns(),
        "tools": tool_versions,
        "plan": _plan(artifact_directory / "coverage.json"),
        "observed": observed,
        "artifact_directory": str(artifact_directory),
        "artifacts": {name: _digest(artifact_directory / name) for name in ARTIFACTS},
        "coverage": check_coverage(
            artifact_directory / "coverage.json",
            root=root,
            policy_path=root / "docs/verification/safety-coverage.json",
        ),
    }
    validate_report(report, after, root)
    _publish(path, report)
    return report


def audit(root: Path) -> dict[str, Any]:
    before = source_identity(root)
    report = _read(STORE / (before["tree"] + ".json"))
    validate_report(report, before, root)
    if source_identity(root) != before:
        raise QualityError("source_changed_during_audit")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "audit"))
    args = parser.parse_args(argv)
    try:
        report = run_quality(ROOT) if args.command == "run" else audit(ROOT)
        print(
            json.dumps(
                {
                    "valid": True,
                    "source": report["source"],
                    "trust": TRUST,
                    "artifact_directory": report["artifact_directory"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (QualityError, runner.VerificationError, SafetyCoverageError, OSError) as error:
        code = (
            str(error)
            if isinstance(error, (QualityError, runner.VerificationError))
            else "quality_storage_or_coverage_error"
        )
        print(json.dumps({"valid": False, "error": code}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
