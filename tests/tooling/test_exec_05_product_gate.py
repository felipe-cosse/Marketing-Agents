"""EXEC-05 reuses the complete source-bound quality control, never a partial runner."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ac_16_quality as quality
from scripts import verify_local as local

ROOT = Path(__file__).resolve().parents[2]
ENTRY = (
    "\tPYTHONDONTWRITEBYTECODE=1 $(UV) run --offline --frozen python -m scripts.ac_16_quality run"
)
BROWSER_RUNNERS = (
    "run-web-01-e2e.mjs",
    "run-web-02-e2e.mjs",
    "run-web-03-e2e.mjs",
    "run-web-04-e2e.mjs",
    "run-web-05-e2e.mjs",
    "run-web-06-e2e.mjs",
    "run-web-07-e2e.mjs",
    "run-web-08-e2e.mjs",
    "run-web-09-e2e.mjs",
    "run-arch-02-e2e.mjs",
    "run-demo-01-e2e.mjs",
    "run-demo-02-e2e.mjs",
    "run-demo-03-e2e.mjs",
    "run-demo-04-e2e.mjs",
    "run-demo-05-e2e.mjs",
    "run-obj-06-e2e.mjs",
)


def _recipe(text: str, target: str) -> list[str]:
    lines = text.splitlines()
    assert lines.count(target + ":") == 1, f"EXEC-05 requires one {target} definition"
    result = []
    for line in lines[lines.index(target + ":") + 1 :]:
        if line.startswith("\t"):
            result.append(line)
        elif line and not line.startswith("#"):
            break
    return result


def _assert_live_hook(text: str) -> None:
    assert ".PHONY: verify-exec-05" in text.splitlines(), "EXEC-05 requires its phony target"
    assert _recipe(text, "verify-exec-05") == [ENTRY], (
        "EXEC-05 requires exactly one complete frozen/offline quality run without ignored failure"
    )


def _reviewed_gates(coverage: Path) -> tuple[local.Gate, ...]:
    """Independent full contract; neither production plan can approve its own drift."""

    def make(name: str, target: str, timeout: int = 300) -> local.Gate:
        return local.Gate(name, ("make", "-j1", f"PYTHON={sys.executable}", target), timeout)

    return (
        make("catalog-validate", "catalog-validate"),
        make("catalog-release", "verify-catalog-release"),
        local.Gate(
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
        local.Gate(
            "api-contract-tests", ("node", "--test", "tools/api-contract/generate.test.mjs"), 300
        ),
        local.Gate(
            "browser-inventory-tests",
            ("node", "--test", "tests/network/web_e2e_inventory.test.mjs"),
            300,
        ),
        local.Gate(
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
        local.Gate(
            "safety-coverage",
            (sys.executable, "-m", "scripts.check_safety_coverage", "--report", str(coverage)),
            60,
        ),
        make("web-format", "web-format-check"),
        make("web-lint", "web-lint"),
        make("web-types", "web-typecheck", 600),
        make("web-unit", "web-test", 1800),
        make("web-build", "web-build", 600),
        local.Gate(
            "browser-network-canary",
            ("node", "apps/web/scripts/run-browser-network-canary.mjs"),
            300,
        ),
        make("web-browser", "web-test-e2e", 1800),
    )


def test_exec_05_make_target_requires_live_complete_quality_run():
    # This assertion survives restoring Makefile to the base. All imports above
    # are existing modules, so the connection witness is not an import failure.
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
def test_exec_05_missing_or_bypassed_hook_fails_an_ordinary_assertion(mutation):
    text = (ROOT / "Makefile").read_text()
    _assert_live_hook(text)
    block = "verify-exec-05:\n" + ENTRY
    replacements = {
        "removed": "",
        "duplicate_command": block + "\n" + ENTRY,
        "duplicate_target": block + "\n\n" + block,
        "ignore": "verify-exec-05:\n\t-" + ENTRY[1:],
        "shell_ignore": block + " || true",
        "moved": "verify-exec-05:\n\nexec05-disconnected:\n" + ENTRY,
        "audit_only": block.removesuffix("run") + "audit",
        "partial_runner": "verify-exec-05:\n\tpython -m scripts.verify_local --backend",
        "selector": block + " --backend",
    }
    changed = (
        text.replace(".PHONY: verify-exec-05\n", "", 1)
        if mutation == "not_phony"
        else text.replace(block, replacements[mutation], 1)
    )
    with pytest.raises(AssertionError, match="EXEC-05"):
        _assert_live_hook(changed)


@pytest.mark.parametrize("returncode", (0, 7))
def test_exec_05_actual_make_recipe_forwards_complete_argv_and_failure(tmp_path, returncode):
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
        ("make", "-s", "-f", str(makefile), "verify-exec-05", f"UV={launcher}"),
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


def test_exec_05_reuses_exact_twenty_gate_coverage_inventory_order_and_deadlines():
    coverage = Path("/external/fresh/coverage.json")
    expected = _reviewed_gates(coverage)
    assert len(expected) == 20
    assert quality.expected_gates(coverage) == expected
    assert local.gates(coverage) == expected
    assert quality.runner is local
    assert _recipe((ROOT / "Makefile").read_text(), "web-test-e2e") == [
        "\tnode apps/web/scripts/run-web-e2e.mjs"
    ]


@pytest.mark.parametrize(
    "arguments",
    (("--backend",), ("--acceptance",), ("--suite", "api"), ("-k", "one"), ("--command", "true")),
)
def test_exec_05_shared_wrapper_rejects_partial_or_replacement_commands(monkeypatch, arguments):
    monkeypatch.setattr(
        quality, "run_quality", lambda _: pytest.fail("must reject before execution")
    )
    with pytest.raises(SystemExit) as error:
        quality.main(["run", *arguments])
    assert error.value.code == 2


def test_exec_05_shared_wrapper_dispatches_only_complete_runner(monkeypatch, tmp_path):
    class ReachedCompleteRunner(Exception):
        pass

    source = {"tree": "0" * 40}
    publications = []
    monkeypatch.setattr(quality, "source_identity", lambda _: source)
    monkeypatch.setattr(quality, "_publish", lambda path, value: publications.append(value))
    monkeypatch.setattr(quality, "_environment_preflight", lambda: None)
    monkeypatch.setattr(quality, "_tools", lambda: {})

    def verify(root, **kwargs):
        assert root == tmp_path
        assert set(kwargs) == {"executor"}
        assert callable(kwargs["executor"])
        raise ReachedCompleteRunner

    monkeypatch.setattr(local, "verify_local", verify)
    with pytest.raises(ReachedCompleteRunner):
        quality.run_quality(tmp_path)
    assert publications == [{"valid": False, "error": "run_in_progress", "source": source}]


def test_exec_05_shared_wrapper_rejects_uncommitted_source_before_any_gate(monkeypatch, tmp_path):
    root = tmp_path / "source"
    root.mkdir()

    def git(*arguments):
        subprocess.run(("git", *arguments), cwd=root, capture_output=True, check=True, timeout=10)

    git("init", "-q")
    # Reflogs must never trigger implicit hostname resolution under AC-15.
    git("config", "--local", "user.name", "Fixture")
    git("config", "--local", "user.email", "fixture@example.invalid")
    for relative in (*quality.LOCKS, "source.txt"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("committed fixture\n")
    git("add", ".")
    git("commit", "-qm", "synthetic fixture")
    store = tmp_path / "reports"
    monkeypatch.setattr(quality, "STORE", store)
    monkeypatch.setattr(
        local, "verify_local", lambda *_args, **_kwargs: pytest.fail("must fail before any gate")
    )
    (root / "source.txt").write_text("caller edit remains\n")
    with pytest.raises(local.VerificationError):
        quality.run_quality(root)
    assert (root / "source.txt").read_text() == "caller edit remains\n"
    assert not store.exists()


def test_exec_05_complete_browser_inventory_is_sixteen_unfiltered_owned_runners():
    result = subprocess.run(
        (
            "node",
            "--input-type=module",
            "-e",
            "import { BROWSER_RUNNERS, readBrowserInventory } from "
            "'./apps/web/scripts/browser-evidence-inventory.mjs'; "
            "console.log(JSON.stringify({runners:BROWSER_RUNNERS, "
            "inventory:readBrowserInventory('./apps/web')}));",
        ),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "runners": list(BROWSER_RUNNERS),
        "inventory": {"runnerCount": 16, "specCount": 16},
    }
    source = (ROOT / "apps/web/scripts/run-web-e2e.mjs").read_text()
    executable = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )
    assert 'from "./browser-evidence-inventory.mjs"' in executable
    assert "readBrowserInventory(resolve(scriptsRoot" in executable
    assert "for (const script of BROWSER_RUNNERS)" in executable
    assert "spawnSync(process.execPath, [resolve(scriptsRoot, script)]" in executable
    assert "if (result.status !== 0) process.exit(result.status ?? 2)" in executable
    assert not re.search(
        r"--grep|--list|--pass-with-no-tests|BROWSER_RUNNERS\.(?:filter|slice)", executable
    )


def test_exec_05_complete_backend_includes_new_durable_api_integration_evidence():
    # The whole tests/ root, not only tests/integration/api, is mandatory: real
    # service/SQLite/HTTP joins also live under tests/integration/db.
    for relative in (
        "tests/integration/db/test_exec_05_campaign_briefs.py",
        "tests/integration/api/test_exec_05_schedule_control.py",
    ):
        path = ROOT / relative
        assert path.is_file(), f"EXEC-05 requires {relative}"
        source = path.read_text()
        assert "EXEC-05" in source or "exec_05" in source
        assert any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
            for node in ast.walk(ast.parse(source))
        )
    frontend = ROOT / "apps/web/src/api/apiContract.test.ts"
    assert frontend.is_file(), "EXEC-05 requires generated-wire adapter regressions"
    assert "EXEC-05" in frontend.read_text()


def test_exec_05_native_browser_requires_real_runs_index_join_and_public_history():
    source = (ROOT / "apps/web/e2e/obj-06-control-surface.spec.ts").read_text()
    assert "EXEC-05" in source
    assert source.count("await openPersistedRunFromIndex(page, completed, info,") == 2
    assert source.count("await assertCompletePublicTimeline(page, completed") == 2
    assert 'name: "Runs & audit", exact: true' in source
    assert 'page.locator(".runs-list > li")' in source
    assert "`/api/v1/audit-events?run_id=${encodeURIComponent(run.id)}`" in source
    assert "`/api/v1/runs/${encodeURIComponent(run.id)}/timeline`" in source
    assert "run.transitions" in source and "run.artifact_summaries" in source
    assert "receipt_id: action.receipt_id" in source
    assert 'page.locator(".run-timeline > .run-event")' in source
    assert not re.search(r"\b(?:page|context)\.route\s*\(|\.fulfill\s*\(", source)
