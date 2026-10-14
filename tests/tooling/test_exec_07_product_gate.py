"""EXEC-07 connects the unchanged clean-state verifier to committed HEAD."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TARGET = "verify-exec-07"
ENTRY = "\t$(MAKE) -j1 verify-clean REF=HEAD"


def _recipe(text: str, target: str) -> list[str]:
    lines = text.splitlines()
    assert lines.count(target + ":") == 1, f"EXEC-07 requires one {target} definition"
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
    ), "EXEC-07 requires its phony target"
    assert _recipe(text, TARGET) == [ENTRY], (
        "EXEC-07 requires exactly the serial complete clean-state command at HEAD"
    )


def test_exec_07_make_target_requires_live_complete_clean_state_run():
    # Restoring Makefile alone retains this test and all imports. The witness
    # therefore fails on the ordinary hook assertion, not a missing dependency.
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
        "caller_ref",
        "quality_only",
        "parallel",
        "deadline_override",
    ),
)
def test_exec_07_missing_or_bypassed_hook_fails_ordinary_assertion(mutation):
    text = (ROOT / "Makefile").read_text()
    _assert_live_hook(text)
    block = TARGET + ":\n" + ENTRY
    replacements = {
        "removed": "",
        "duplicate_command": block + "\n" + ENTRY,
        "duplicate_target": block + "\n\n" + block,
        "ignore": TARGET + ":\n\t-" + ENTRY[1:],
        "shell_ignore": block + " || true",
        "moved": TARGET + ":\n\nexec07-disconnected:\n" + ENTRY,
        "caller_ref": block.replace("REF=HEAD", 'REF="$(REF)"'),
        "quality_only": block.replace("verify-clean", "verify-ac-16"),
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
    with pytest.raises(AssertionError, match="EXEC-07"):
        _assert_live_hook(changed)


@pytest.mark.parametrize("returncode", (0, 7))
def test_exec_07_actual_make_pins_head_and_propagates_clean_state_failure(tmp_path, returncode):
    makefile = tmp_path / "Makefile"
    makefile.write_text((ROOT / "Makefile").read_text())
    _assert_live_hook(makefile.read_text())
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    recorder = scripts / "record_clean_call.py"
    recorder.write_text(
        "import json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:]}))\n"
        f"raise SystemExit({returncode})\n"
    )
    # Only the final owned leaf is replaced. Both Make invocations and the real
    # verify-clean recipe run, including its forwarding into the shell entrypoint.
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
            "-j1",
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
    assert json.loads(result.stdout) == {"argv": ["--ref", "HEAD"]}
