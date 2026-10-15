"""DEL-01 connects delivery qualification to the complete committed product."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ac_16_quality as quality
from scripts import verify_local as local
from tests.tooling import test_exec_05_product_gate as reviewed

ROOT = Path(__file__).resolve().parents[2]
TARGET = "verify-del-01"
ENTRY = "\t$(MAKE) -j1 verify-clean REF=HEAD"
TEST_TARGET = "test-del-01"
TEST_ENTRIES = [
    "\tPYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B -m pytest -q "
    "tests/tooling/test_del_01_delivery_entrypoint.py",
    "\tnode --test apps/web/scripts/del-01-product-journey.test.mjs",
]


def _recipe(text: str, target: str) -> list[str]:
    lines = text.splitlines()
    assert lines.count(target + ":") == 1, f"DEL-01 requires one {target} definition"
    result = []
    for line in lines[lines.index(target + ":") + 1 :]:
        if line.startswith("\t"):
            result.append(line)
        elif line and not line.startswith("#"):
            break
    return result


def _assert_live_hook(text: str) -> None:
    assert any(
        line.startswith(".PHONY:") and TARGET in line.split()[1:] for line in text.splitlines()
    ), "DEL-01 requires its phony delivery target"
    assert _recipe(text, TARGET) == [ENTRY], (
        "DEL-01 requires exactly the serial complete clean-state command at HEAD"
    )


def test_del_01_live_make_target_requires_complete_committed_delivery():
    # Restoring only Makefile keeps these tests and every imported module.
    # The witness fails an ordinary hook assertion, not an import or service error.
    _assert_live_hook((ROOT / "Makefile").read_text())


def test_del_01_focused_target_runs_both_unfiltered_test_leaves():
    text = (ROOT / "Makefile").read_text()
    assert any(
        line.startswith(".PHONY:") and TEST_TARGET in line.split()[1:] for line in text.splitlines()
    ), "DEL-01 requires its phony focused-test target"
    assert _recipe(text, TEST_TARGET) == TEST_ENTRIES, (
        "DEL-01 focused tests must run both complete leaves without ignored failures"
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "removed",
        "duplicate_command",
        "duplicate_target",
        "ignore",
        "shell_ignore",
        "disconnected",
        "not_phony",
        "caller_ref",
        "quality_only",
        "audit_only",
        "parallel",
        "deadline_override",
    ),
)
def test_del_01_missing_or_bypassed_delivery_hook_fails_ordinary_assertion(mutation):
    text = (ROOT / "Makefile").read_text()
    _assert_live_hook(text)
    block = TARGET + ":\n" + ENTRY
    replacements = {
        "removed": "",
        "duplicate_command": block + "\n" + ENTRY,
        "duplicate_target": block + "\n\n" + block,
        "ignore": TARGET + ":\n\t-" + ENTRY[1:],
        "shell_ignore": block + " || true",
        "disconnected": TARGET + ":\n\ndel01-disconnected:\n" + ENTRY,
        "caller_ref": block.replace("REF=HEAD", 'REF="$(REF)"'),
        "quality_only": block.replace("verify-clean", "verify-ac-16"),
        "audit_only": block.replace("verify-clean", "audit-ac-16"),
        "parallel": block.replace("-j1", "-j2"),
        "deadline_override": block + " EXECUTION_SECONDS=3600",
    }
    changed = (
        "\n".join(
            " ".join(token for token in line.split() if token != TARGET)
            if line.startswith(".PHONY:")
            else line
            for line in text.splitlines()
        )
        if mutation == "not_phony"
        else text.replace(block, replacements[mutation], 1)
    )
    with pytest.raises(AssertionError, match="DEL-01"):
        _assert_live_hook(changed)


@pytest.mark.parametrize("returncode", (0, 7))
def test_del_01_real_make_pins_head_serializes_and_propagates_failure(tmp_path, returncode):
    makefile = tmp_path / "Makefile"
    makefile.write_text((ROOT / "Makefile").read_text())
    _assert_live_hook(makefile.read_text())
    # The phony hook must still run when a same-named file is present.
    (tmp_path / TARGET).touch()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    recorder = scripts / "record_clean_call.py"
    recorder.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], "
        "'makeflags': os.environ.get('MAKEFLAGS', '')}))\n"
        f"raise SystemExit({returncode})\n"
    )
    # Only the owned terminal leaf is replaced. Both Make invocations and the
    # existing verify-clean recipe execute; no Docker or application is started.
    (scripts / "verify_clean_state.sh").write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$@"\n'
    )
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    result = subprocess.run(
        (
            "make",
            "-s",
            "--no-print-directory",
            "-j8",
            "-f",
            str(makefile),
            TARGET,
            "REF=refs/heads/not-the-candidate",
            "REPORT=",
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == (0 if returncode == 0 else 2)
    observed = json.loads(result.stdout)
    assert observed["argv"] == ["--ref", "HEAD"]
    flags = shlex.split(observed["makeflags"])
    # GNU Make 3.81 omits its default single-job setting from MAKEFLAGS; newer
    # versions retain -j1. Neither may inherit the caller's -j8 or jobserver.
    jobs = [flag for flag in flags if flag.startswith(("-j", "--jobs"))]
    assert jobs in ([], ["-j1"], ["--jobs=1"])
    assert not any(flag.startswith("--jobserver-") for flag in flags)


@pytest.mark.parametrize("python_result,node_result", ((0, 0), (7, 0), (0, 9)))
def test_del_01_real_focused_make_orders_leaves_and_stops_on_failure(
    tmp_path, python_result, node_result
):
    makefile = tmp_path / "Makefile"
    makefile.write_text((ROOT / "Makefile").read_text())
    assert _recipe(makefile.read_text(), TEST_TARGET) == TEST_ENTRIES
    executables = tmp_path / "bin"
    executables.mkdir()
    for name, returncode in (("python-fixture", python_result), ("node", node_result)):
        launcher = executables / name
        launcher.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"print(json.dumps({{'leaf': {name!r}, 'argv': sys.argv[1:], "
            "'bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE')}))\n"
            f"raise SystemExit({returncode})\n"
        )
        launcher.chmod(0o700)
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    environment["PATH"] = str(executables) + os.pathsep + environment.get("PATH", os.defpath)
    result = subprocess.run(
        (
            "make",
            "-s",
            "--no-print-directory",
            "-j8",
            "-f",
            str(makefile),
            TEST_TARGET,
            f"PYTHON={executables / 'python-fixture'}",
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == (0 if python_result == node_result == 0 else 2)
    observed = [json.loads(line) for line in result.stdout.splitlines()]
    assert observed[0] == {
        "leaf": "python-fixture",
        "argv": ["-B", "-m", "pytest", "-q", "tests/tooling/test_del_01_delivery_entrypoint.py"],
        "bytecode": "1",
    }
    if python_result:
        assert len(observed) == 1
    else:
        assert len(observed) == 2
        assert observed[1] == {
            "leaf": "node",
            "argv": ["--test", "apps/web/scripts/del-01-product-journey.test.mjs"],
            "bytecode": None,
        }


def test_del_01_existing_acceptance_keeps_all_twenty_two_gates_and_deadlines():
    coverage = Path("/external/fresh/coverage.json")
    candidate = "1234567890abcdef1234567890abcdef12345678"
    # Reuse the earlier independent reviewed expectations, not a production
    # function approving another production function's identical drift.
    full_quality = reviewed._reviewed_gates(coverage)
    assert len(full_quality) == 20
    assert quality.expected_gates(coverage) == local.gates(coverage) == full_quality
    expected = (
        *full_quality,
        local.Gate(
            "acceptance-clean",
            (
                "make",
                "-j1",
                f"PYTHON={sys.executable}",
                f"REF={candidate}",
                "verify-clean",
            ),
            1800,
        ),
        local.Gate(
            "acceptance-backup",
            ("make", "-j1", f"PYTHON={sys.executable}", "test-del-05-compose-backup"),
            1800,
        ),
    )
    assert len(expected) == 22
    assert local.gates(coverage, acceptance=True, ref=candidate) == expected
