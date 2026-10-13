"""EXEC-04: dedicated ordered local foundation gates, without changing full verify."""

from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_local as local

ROOT = Path(__file__).resolve().parents[2]
ENTRY = "\tPYTHONDONTWRITEBYTECODE=1 $(UV) run --offline --frozen python -m scripts.verify_exec_04"
NAMES = ("m3-domain-persistence", "m4-read-runtime-mocks", "m5-approval-safety")
FILES = (
    (
        "tests/unit/domain",
        "tests/integration/db/test_run_01_lifecycle_persistence.py",
        "tests/integration/db/test_del_04_migrations.py",
        "tests/integration/db/test_del_04_catalog_seed.py",
        "tests/integration/db/test_del_04_readiness.py",
        "tests/integration/db/test_del_04_trigger_projection.py",
        "tests/integration/db/test_del_04_postgresql_fixture.py",
        "tests/integration/db/test_del_04_database_cli.py",
    ),
    (
        "tests/contract/test_del_03_durable_mock_composition.py",
        "tests/contract/test_arch_06_llm_provider.py",
        "tests/contract/test_arch_07_connector_contract_matrix.py",
        "tests/integration/db/test_orch_06_controlled_read_executor.py",
        "tests/integration/db/test_orch_09_audited_step_state.py",
        "tests/integration/runtime/test_del_05_process_composition.py",
        "tests/acceptance/test_exec_04_foundation_restart.py",
    ),
    (
        "tests/acceptance/test_ac_08_dispatch_approval_boundary.py",
        "tests/acceptance/test_ac_09_approval_rejection.py",
        "tests/acceptance/test_ac_11_mock_crash_recovery.py",
    ),
)


def _recipe(text: str, target: str) -> list[str]:
    lines = text.splitlines()
    assert lines.count(target + ":") == 1, f"EXEC-04 requires one {target} definition"
    result = []
    for line in lines[lines.index(target + ":") + 1 :]:
        if line.startswith("\t"):
            result.append(line)
        elif line and not line.startswith("#"):
            break
    return result


def _assert_live_hook(text: str) -> None:
    assert _recipe(text, "verify-exec-04") == [ENTRY], (
        "EXEC-04 must invoke the actual runner once, without ignored failure"
    )
    assert text.splitlines().count(ENTRY) == 1, "EXEC-04 requires one dedicated runner invocation"
    assert ".PHONY: verify-exec-04" in text.splitlines(), "EXEC-04 must be a phony target"


def _load_runner():
    # Check the real connection before importing new code. The restored-base
    # witness must fail an assertion, never merely fail module collection.
    _assert_live_hook((ROOT / "Makefile").read_text())
    assert (ROOT / "scripts/verify_exec_04.py").is_file(), "EXEC-04 runner is required"
    return importlib.import_module("scripts.verify_exec_04")


@pytest.fixture
def runner():
    return _load_runner()


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-q")
    # Persistent identity also covers implicit checkout/reflog operations; it
    # must not fall back to host-name resolution in AC-15's offline full suite.
    _git(root, "config", "--local", "user.name", "Fixture")
    _git(root, "config", "--local", "user.email", "fixture@example.invalid")
    (root / "source.txt").write_text("original\n")
    (root / ".gitignore").write_text("*.ignored\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "synthetic fixture")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(local.tempfile, "gettempdir", lambda: str(artifacts))
    return root


def _passed(gate, _root, _environment):
    return local.GateResult(gate.name, "passed", 0, 1)


def test_exec_04_standalone_target_requires_live_foundation_runner():
    _assert_live_hook((ROOT / "Makefile").read_text())
    runner = _load_runner()
    assert tuple(gate.name for gate in runner.gates()) == NAMES


@pytest.mark.parametrize(
    "mutation",
    ("removed", "duplicate", "ignore", "shell_ignore", "moved", "duplicate_target", "not_phony"),
)
def test_exec_04_hook_removal_or_bypass_fails_an_ordinary_assertion(mutation):
    text = (ROOT / "Makefile").read_text()
    _assert_live_hook(text)
    if mutation == "removed":
        text = text.replace("verify-exec-04:\n" + ENTRY + "\n", "", 1)
    elif mutation == "duplicate":
        text = text.replace(ENTRY, ENTRY + "\n" + ENTRY, 1)
    elif mutation == "ignore":
        text = text.replace(ENTRY, "\t-" + ENTRY[1:], 1)
    elif mutation == "shell_ignore":
        text = text.replace(ENTRY, ENTRY + " || true", 1)
    elif mutation == "moved":
        text = text.replace(ENTRY + "\n", "", 1).replace(
            "verify-repository:\n", "verify-repository:\n" + ENTRY + "\n", 1
        )
    elif mutation == "duplicate_target":
        text += "\nverify-exec-04:\n" + ENTRY + "\n"
    else:
        text = text.replace(".PHONY: verify-exec-04\n", "", 1)
    with pytest.raises(AssertionError, match="EXEC-04"):
        _assert_live_hook(text)


def test_exec_04_missing_runner_fails_assertion_before_import(tmp_path, monkeypatch):
    (tmp_path / "Makefile").write_text((ROOT / "Makefile").read_text())
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="EXEC-04 runner is required"):
        _load_runner()


def test_exec_04_whole_file_inventory_order_and_deadlines_are_independent(runner):
    prefix = (sys.executable, "-B", "-m", "pytest", "-q", "--disable-socket", "--allow-unix-socket")
    assert runner.gates() == tuple(
        local.Gate(name, prefix + files, 240) for name, files in zip(NAMES, FILES, strict=True)
    )
    assert all("::" not in name for files in FILES for name in files)
    assert len({name for files in FILES for name in files}) == sum(map(len, FILES))


def test_exec_04_complete_twenty_gate_inventory_coverage_and_deadlines_remain_unchanged():
    gates = local.gates(Path("/synthetic/coverage.json"))
    assert [(gate.name, gate.timeout_seconds) for gate in gates] == [
        ("catalog-validate", 300),
        ("catalog-release", 300),
        ("catalog-tests", 600),
        ("backend-format", 300),
        ("backend-lint", 300),
        ("backend-types", 600),
        ("repository", 900),
        ("api-contract", 300),
        ("network-canaries", 300),
        ("api-contract-tests", 300),
        ("browser-inventory-tests", 300),
        ("backend-tests", 1800),
        ("safety-coverage", 60),
        ("web-format", 300),
        ("web-lint", 300),
        ("web-types", 600),
        ("web-unit", 1800),
        ("web-build", 600),
        ("browser-network-canary", 300),
        ("web-browser", 1800),
    ]
    assert gates[6].argv == ("make", "-j1", f"PYTHON={sys.executable}", "verify-repository")
    assert gates[2].argv == (
        sys.executable,
        "-m",
        "pytest",
        "tests/catalog",
        "--cov=marketing_agents",
        "--cov-branch",
        "--cov-report=",
    )
    assert gates[11].argv == (
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "--ignore=tests/catalog",
        "--cov=marketing_agents",
        "--cov-branch",
        "--cov-append",
        "--cov-report=json:/synthetic/coverage.json",
    )
    assert gates[-1].argv == ("make", "-j1", f"PYTHON={sys.executable}", "web-test-e2e")


def test_exec_04_success_is_serial_preserves_source_and_cleans_temporary_storage(runner, source):
    baseline = local.source_snapshot(source)
    seen, storage = [], []

    def execute(gate, root, environment):
        assert root == source
        seen.append(gate.name)
        path = Path(environment["COVERAGE_FILE"])
        assert not path.is_relative_to(source)
        storage.append(path.parent)
        return _passed(gate, root, environment)

    result = runner.verify(source, executor=execute)
    assert result["valid"] is True and result["error"] is None
    assert seen == list(NAMES)
    assert [item["name"] for item in result["milestones"]] == list(NAMES)
    assert local.source_snapshot(source) == baseline
    assert len(set(storage)) == 1 and not storage[0].exists()


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize(
    ("status", "code"),
    (("failed", 7), ("timed_out", -15), ("command_unavailable", None), ("interrupted", -15)),
)
def test_exec_04_every_failure_stops_all_later_milestones(runner, source, index, status, code):
    seen = []

    def execute(gate, root, environment):
        seen.append(gate.name)
        if gate.name == NAMES[index]:
            return local.GateResult(gate.name, status, code, 1)
        return _passed(gate, root, environment)

    result = runner.verify(source, executor=execute)
    assert result["valid"] is False and result["error"] == "foundation_gate_failed"
    assert seen == list(NAMES[: index + 1])
    assert result["milestones"][-1]["status"] == status
    assert result["milestones"][-1]["returncode"] == code


@pytest.mark.parametrize("kind", ("wrong_name", "false_status", "nonzero"))
def test_exec_04_false_success_result_is_rejected(runner, source, kind):
    seen = []

    def execute(gate, _root, _environment):
        seen.append(gate.name)
        return local.GateResult(
            "wrong" if kind == "wrong_name" else gate.name,
            "failed" if kind == "false_status" else "passed",
            9 if kind == "nonzero" else 0,
            1,
        )

    result = runner.verify(source, executor=execute)
    assert not result["valid"] and result["error"] == "foundation_gate_failed"
    assert seen == [NAMES[0]]


@pytest.mark.parametrize("error_type", (KeyboardInterrupt, local.VerificationInterrupted))
def test_exec_04_interruption_stops_gates_and_restores_signal_handlers(runner, source, error_type):
    handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    seen = []

    def execute(gate, _root, _environment):
        seen.append(gate.name)
        raise error_type()

    result = runner.verify(source, executor=execute)
    assert not result["valid"] and result["error"] == "verification_interrupted"
    assert result["milestones"] == [] and seen == [NAMES[0]]
    assert {number: signal.getsignal(number) for number in handlers} == handlers


@pytest.mark.parametrize("outcome", ("passed", "failed", "timeout", "unavailable"))
def test_exec_04_default_executor_runs_real_process_controls(runner, source, monkeypatch, outcome):
    marker = source.parent / "must-not-run"
    if outcome == "unavailable":
        argv = (str(source.parent / "missing-executable"),)
    else:
        code = (
            "import time; time.sleep(30)"
            if outcome == "timeout"
            else f"raise SystemExit({7 if outcome == 'failed' else 0})"
        )
        argv = (sys.executable, "-B", "-c", code)
    first = local.Gate(NAMES[0], argv, 1 if outcome == "timeout" else 5)
    second = local.Gate(
        NAMES[1],
        (
            sys.executable,
            "-B",
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ),
        5,
    )
    third = local.Gate(NAMES[2], (sys.executable, "-B", "-c", "pass"), 5)
    monkeypatch.setattr(runner, "gates", lambda: (first, second, third))
    result = runner.verify(source)
    if outcome == "passed":
        assert result["valid"] and marker.read_text() == "ran"
    else:
        assert not result["valid"] and not marker.exists()
        assert len(result["milestones"]) == 1
        assert (
            result["milestones"][0]["status"]
            == {"failed": "failed", "timeout": "timed_out", "unavailable": "command_unavailable"}[
                outcome
            ]
        )


@pytest.mark.parametrize("mutation", ("change", "add", "delete", "mode", "symlink"))
@pytest.mark.parametrize("failed", (False, True))
def test_exec_04_source_drift_stops_immediately_and_does_not_rollback(
    runner, source, mutation, failed
):
    target = source / "source.txt"
    link = source / "link"
    link.symlink_to("source.txt")
    seen = []

    def execute(gate, root, environment):
        seen.append(gate.name)
        if mutation == "change":
            target.write_text("caller-visible change\n")
        elif mutation == "add":
            (source / "new.txt").write_text("new source\n")
        elif mutation == "delete":
            target.unlink()
        elif mutation == "mode":
            target.chmod(target.stat().st_mode ^ 0o100)
        else:
            link.unlink()
            link.symlink_to("different")
        return local.GateResult(gate.name, "failed" if failed else "passed", int(failed), 1)

    result = runner.verify(source, executor=execute)
    assert not result["valid"] and result["error"] == "source_drift_detected"
    assert seen == [NAMES[0]]
    if mutation == "change":
        assert target.read_text() == "caller-visible change\n"
    elif mutation == "delete":
        assert not target.exists()
    elif mutation == "add":
        assert (source / "new.txt").read_text() == "new source\n"
    elif mutation == "symlink":
        assert os.readlink(link) == "different"


def test_exec_04_initial_dirty_deleted_and_untracked_source_is_preserved(runner, source):
    (source / "source.txt").unlink()
    (source / ".gitignore").write_text("*.ignored\n# caller edit\n")
    (source / "untracked.txt").write_text("caller work\n")
    before = local.source_snapshot(source)
    assert runner.verify(source, executor=_passed)["valid"]
    assert local.source_snapshot(source) == before


@pytest.mark.parametrize(
    "key", ("PYTHONPATH", "PYTHONHOME", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD")
)
def test_exec_04_loader_overrides_fail_before_any_milestone(runner, source, monkeypatch, key):
    monkeypatch.setenv(key, "private-canary")
    result = runner.verify(source, executor=lambda *_args: pytest.fail("unsafe child launched"))
    assert not result["valid"] and result["error"] == "unsafe_foundation_environment"
    assert result["milestones"] == [] and "private-canary" not in json.dumps(result)


@pytest.mark.parametrize(
    "key",
    (
        "APP_ENV",
        "AUTH_MODE",
        "LLM_PROVIDER",
        "CONNECTOR_MODE",
        "ALLOW_EXTERNAL_NETWORK",
        "REAL_LLM_OPT_IN",
        "REAL_CONNECTOR_OPT_IN",
    ),
)
def test_exec_04_unsafe_runtime_modes_are_not_silently_overridden(runner, source, monkeypatch, key):
    monkeypatch.setenv(key, "unsafe")
    result = runner.verify(source, executor=lambda *_args: pytest.fail("unsafe child launched"))
    assert not result["valid"] and result["error"] == "unsafe_verification_environment"
    assert result["milestones"] == [] and os.environ[key] == "unsafe"


def test_exec_04_filter_scheduling_and_coverage_overrides_do_not_reach_children(
    runner, source, monkeypatch
):
    stripped = (
        "MAKEFLAGS",
        "MFLAGS",
        "MAKEOVERRIDES",
        "PYTEST_ADDOPTS",
        "COVERAGE_PROCESS_START",
        "COVERAGE_RCFILE",
    )
    for key in stripped:
        monkeypatch.setenv(key, "private-selection-canary")
    monkeypatch.setenv("UV_OFFLINE", "0")
    monkeypatch.setenv("UV_FROZEN", "0")

    def execute(gate, root, environment):
        assert not set(stripped) & environment.keys()
        assert environment["UV_OFFLINE"] == environment["UV_FROZEN"] == "1"
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert (
            tuple(gate.argv[-len(FILES[NAMES.index(gate.name)]) :]) == FILES[NAMES.index(gate.name)]
        )
        return _passed(gate, root, environment)

    assert runner.verify(source, executor=execute)["valid"]
    assert all(os.environ[key] == "private-selection-canary" for key in stripped)
    assert os.environ["UV_OFFLINE"] == os.environ["UV_FROZEN"] == "0"


@pytest.mark.parametrize(
    "arguments",
    (
        ("--suite", "m3"),
        ("--list",),
        ("--ref", "HEAD"),
        ("--backend",),
        ("--command", "true"),
        ("-k", "test"),
    ),
)
def test_exec_04_cli_exposes_no_partial_or_replacement_command(runner, monkeypatch, arguments):
    monkeypatch.setattr(runner, "verify", lambda: pytest.fail("selector reached execution"))
    with pytest.raises(SystemExit) as caught:
        runner.main(list(arguments))
    assert caught.value.code == 2


@pytest.mark.parametrize("valid", (False, True))
def test_exec_04_cli_propagates_actual_aggregate_status(runner, monkeypatch, capsys, valid):
    summary = {
        "valid": valid,
        "error": None if valid else "foundation_gate_failed",
        "milestones": [],
    }
    monkeypatch.setattr(runner, "verify", lambda: summary)
    assert runner.main([]) == (0 if valid else 1)
    assert json.loads(capsys.readouterr().out) == summary
