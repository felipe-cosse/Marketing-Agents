"""EXEC-06 connects to the reviewed full quality control without weakening it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ac_16_quality as quality
from scripts import check_safety_coverage as safety
from scripts import verify_local as local
from tests.tooling import test_exec_05_product_gate as reviewed

ROOT = Path(__file__).resolve().parents[2]
ENTRY = reviewed.ENTRY
TARGET = "verify-exec-06"
SAFETY_MODULES = (
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


def _assert_live_hook(text: str) -> None:
    assert f".PHONY: {TARGET}" in text.splitlines(), "EXEC-06 requires its phony target"
    assert text.splitlines().count(TARGET + ":") == 1, "EXEC-06 requires one target definition"
    assert reviewed._recipe(text, TARGET) == [ENTRY], (
        "EXEC-06 requires exactly the complete frozen/offline run without ignored failure"
    )


def test_exec_06_make_target_requires_live_complete_quality_run():
    # Imports exist on the approved base. Restoring just Makefile produces an
    # ordinary connection assertion, never a missing new-module/import failure.
    _assert_live_hook((ROOT / "Makefile").read_text())


@pytest.mark.parametrize(
    "mutation",
    (
        "removed",
        "duplicate_command",
        "duplicate_target",
        "ignore",
        "shell_ignore",
        "moved",
        "not_phony",
        "audit_only",
        "partial_runner",
        "selector",
    ),
)
def test_exec_06_missing_or_bypassed_hook_fails_ordinary_assertion(mutation):
    text = (ROOT / "Makefile").read_text()
    _assert_live_hook(text)
    block = TARGET + ":\n" + ENTRY
    replacements = {
        "removed": "",
        "duplicate_command": block + "\n" + ENTRY,
        "duplicate_target": block + "\n\n" + block,
        "ignore": TARGET + ":\n\t-" + ENTRY[1:],
        "shell_ignore": block + " || true",
        "moved": TARGET + ":\n\nexec06-disconnected:\n" + ENTRY,
        "audit_only": block.removesuffix("run") + "audit",
        "partial_runner": TARGET + ":\n\tpython -m scripts.verify_local --backend",
        "selector": block + " --backend",
    }
    changed = (
        text.replace(f".PHONY: {TARGET}\n", "", 1)
        if mutation == "not_phony"
        else text.replace(block, replacements[mutation], 1)
    )
    with pytest.raises(AssertionError, match="EXEC-06"):
        _assert_live_hook(changed)


@pytest.mark.parametrize("returncode", (0, 7))
def test_exec_06_actual_make_forwards_complete_argv_and_failure(tmp_path, returncode):
    makefile = tmp_path / "Makefile"
    makefile.write_text((ROOT / "Makefile").read_text())
    _assert_live_hook(makefile.read_text())
    launcher = tmp_path / "fixture-uv"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'bytecode': "
        "os.environ.get('PYTHONDONTWRITEBYTECODE')}))\n"
        f"raise SystemExit({returncode})\n"
    )
    launcher.chmod(0o700)
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    result = subprocess.run(
        ("make", "-s", "-f", str(makefile), TARGET, f"UV={launcher}"),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == (0 if returncode == 0 else 2)
    assert json.loads(result.stdout) == {
        "argv": ["run", "--offline", "--frozen", "python", "-m", "scripts.ac_16_quality", "run"],
        "bytecode": "1",
    }


def test_exec_06_unchanged_twenty_gate_order_commands_coverage_and_deadlines():
    coverage = Path("/external/fresh/coverage.json")
    # Reuse the previous requirement's independent reviewed expectations rather
    # than introduce a second gate framework or ask production to approve itself.
    expected = reviewed._reviewed_gates(coverage)
    assert len(expected) == 20
    assert quality.expected_gates(coverage) == local.gates(coverage) == expected
    assert quality.runner is local
    assert reviewed._recipe((ROOT / "Makefile").read_text(), "web-test-e2e") == [
        "\tnode apps/web/scripts/run-web-e2e.mjs"
    ]


def test_exec_06_keeps_all_sixteen_unfiltered_owned_browser_runners():
    reviewed.test_exec_05_complete_browser_inventory_is_sixteen_unfiltered_owned_runners()


def test_exec_06_safety_inventory_and_full_statement_branch_thresholds_unchanged():
    policy = json.loads((ROOT / "docs/verification/safety-coverage.json").read_text())
    assert tuple(policy["required_modules"]) == safety.REQUIRED_MODULES == SAFETY_MODULES
    assert policy["required_coverage"] == {"statements": 100, "branches": 100}
    assert len(policy["exclusion_exceptions"]) == 1
    assert policy["exclusion_exceptions"][0]["module"] == "domain/approval.py"
    assert policy["exclusion_exceptions"][0]["sha256"] == (
        "cccfc33a13dd95549a89491fe39dcdcb21ac4641892c9db284baa5f067a9bc2f"
    )
    safety._policy(ROOT / "docs/verification/safety-coverage.json")


@pytest.mark.parametrize(
    "arguments",
    (
        ("--backend",),
        ("--acceptance",),
        ("--suite", "api"),
        ("-k", "one"),
        ("--command", "true"),
    ),
)
def test_exec_06_wrapper_rejects_partial_or_replacement_commands(monkeypatch, arguments):
    reviewed.test_exec_05_shared_wrapper_rejects_partial_or_replacement_commands(
        monkeypatch, arguments
    )


def test_exec_06_wrapper_dispatches_only_complete_runner(monkeypatch, tmp_path):
    reviewed.test_exec_05_shared_wrapper_dispatches_only_complete_runner(monkeypatch, tmp_path)


def test_exec_06_wrapper_rejects_uncommitted_source(monkeypatch, tmp_path):
    reviewed.test_exec_05_shared_wrapper_rejects_uncommitted_source_before_any_gate(
        monkeypatch, tmp_path
    )


def test_exec_06_docs_keep_full_quality_separate_from_acceptance_and_disabled_ci():
    text = (ROOT / "docs/testing.md").read_text()
    assert f"make {TARGET}" in text
    assert "all 20 gates, 17 safety-coverage modules, and 16 browser runners" in text
    assert "does not establish a current full pass" in text
    assert "CI remains disabled by operator direction" in text
    assert "`make verify-clean REF=<commit>` / `make acceptance REF=<commit>`" in text
