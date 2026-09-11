"""DEL-07: serial fail-fast gates, fresh coverage, and caller-preserving drift checks."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import verify_local as runner


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True, timeout=10
    )
    return result.stdout.strip()


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "source.txt").write_text("original source\n")
    (root / ".gitignore").write_text("*.ignored\n.coverage\n")
    _git(root, "add", "source.txt", ".gitignore")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(artifacts))
    return root


def _pass(gate: runner.Gate, _root: Path, _environment: Any) -> runner.GateResult:
    return runner.GateResult(gate.name, "passed", 0, 1)


def _commit(source: Path) -> None:
    _git(source, "add", "--all")
    _git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )


def test_del_07_catalog_hard_gate_and_all_remaining_gates_are_serial(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAKEFLAGS", "-j8 --jobserver-auth=3,4")
    monkeypatch.setenv("MFLAGS", "-j8")
    monkeypatch.setenv("MAKEOVERRIDES", "PYTHON=untrusted")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=tests")
    seen: list[runner.Gate] = []

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        seen.append(gate)
        assert root == source
        assert not {"MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "PYTEST_ADDOPTS"} & environment.keys()
        if gate.argv[0] == "make":
            assert gate.argv[1:3] == ("-j1", f"PYTHON={sys.executable}")
        return _pass(gate, root, environment)

    result = runner.verify_local(source, executor=execute)
    assert result["valid"] is True
    assert [gate.name for gate in seen] == [
        "catalog-validate",
        "catalog-release",
        "catalog-tests",
        "backend-format",
        "backend-lint",
        "backend-types",
        "repository",
        "api-contract",
        "network-canaries",
        "api-contract-tests",
        "browser-inventory-tests",
        "backend-tests",
        "safety-coverage",
        "web-format",
        "web-lint",
        "web-types",
        "web-unit",
        "web-build",
        "browser-network-canary",
        "web-browser",
    ]
    assert os.environ["MAKEFLAGS"] == "-j8 --jobserver-auth=3,4"
    assert result["completed_gates"] == result["passed_gates"] == 20


@pytest.mark.parametrize(
    "failed_gate",
    [
        "catalog-validate",
        "catalog-release",
        "catalog-tests",
        "repository",
        "backend-tests",
        "safety-coverage",
        "browser-network-canary",
        "web-browser",
    ],
)
def test_del_07_any_gate_failure_prevents_every_later_gate(source: Path, failed_gate: str) -> None:
    seen: list[str] = []

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        seen.append(gate.name)
        if gate.name == failed_gate:
            return runner.GateResult(gate.name, "failed", 5, 1)
        return _pass(gate, root, environment)

    result = runner.verify_local(source, executor=execute)
    assert result["valid"] is False
    assert result["error"] == "gate_failed"
    assert seen[-1] == failed_gate
    assert result["passed_gates"] == len(seen) - 1


@pytest.mark.parametrize("backend", [False, True])
def test_del_07_coverage_is_fresh_shared_append_only_and_retained(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: bool
) -> None:
    stale = tmp_path / "caller.coverage"
    stale.write_bytes(b"caller stale coverage")
    monkeypatch.setenv("COVERAGE_FILE", str(stale))
    monkeypatch.setenv("PRIVATE_TEST_CANARY", "not-in-metadata")
    paths: list[Path] = []

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        coverage = Path(environment["COVERAGE_FILE"])
        assert not coverage.is_relative_to(source)
        if gate.name == "catalog-validate":
            assert not coverage.exists()
            paths.append(coverage)
        if gate.name == "catalog-tests":
            assert "--cov-append" not in gate.argv
            coverage.write_bytes(b"fresh catalogue coverage")
        if gate.name == "backend-tests":
            assert "--cov-append" in gate.argv
            assert "--ignore=tests/catalog" in gate.argv
            assert coverage.read_bytes() == b"fresh catalogue coverage"
            report = next(
                value.removeprefix("--cov-report=json:")
                for value in gate.argv
                if value.startswith("--cov-report=json:")
            )
            Path(report).write_text('{"source_line_counts_only":true}')
        return _pass(gate, root, environment)

    for _ in range(2):
        result = runner.verify_local(source, backend=backend, executor=execute)
        artifact_directory = Path(result["artifact_directory"])
        assert (artifact_directory / "coverage.json").is_file()
        metadata = (artifact_directory / "verification.json").read_text()
        assert "not-in-metadata" not in metadata
        assert json.loads(metadata)["summary"]["valid"] is True
    assert paths[0] != paths[1]
    assert stale.read_bytes() == b"caller stale coverage"


def test_del_07_nested_uv_commands_are_forced_offline_and_frozen(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_OFFLINE", "0")
    monkeypatch.setenv("UV_FROZEN", "false")

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        assert environment["UV_OFFLINE"] == environment["UV_FROZEN"] == "1"
        return _pass(gate, root, environment)

    assert runner.verify_local(source, executor=execute)["valid"] is True
    assert os.environ["UV_OFFLINE"] == "0"
    assert os.environ["UV_FROZEN"] == "false"


def test_del_07_backend_mode_reuses_exact_default_gates_and_commands(source: Path) -> None:
    report = source / "not-created.json"
    complete = runner.gates(report)
    backend = runner.gates(report, backend=True)
    assert tuple(gate.name for gate in backend) == runner.BACKEND_GATE_NAMES
    assert len(backend) == 8
    assert backend == tuple(gate for gate in complete if gate.name in runner.BACKEND_GATE_NAMES)
    seen: list[runner.Gate] = []

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        seen.append(gate)
        return _pass(gate, root, environment)

    result = runner.verify_local(source, backend=True, executor=execute)
    assert result["valid"] is True
    assert result["mode"] == "backend"
    assert result["completed_gates"] == result["passed_gates"] == 8
    assert tuple(gate.name for gate in seen) == runner.BACKEND_GATE_NAMES
    assert all(gate.timeout_seconds <= 1800 for gate in backend)
    assert not report.exists()


@pytest.mark.parametrize("failed_gate", runner.BACKEND_GATE_NAMES)
def test_del_07_backend_mode_remains_fail_fast(source: Path, failed_gate: str) -> None:
    seen: list[str] = []

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        seen.append(gate.name)
        if gate.name == failed_gate:
            return runner.GateResult(gate.name, "failed", 5, 1)
        return _pass(gate, root, environment)

    result = runner.verify_local(source, backend=True, executor=execute)
    assert result["valid"] is False
    assert result["error"] == "gate_failed"
    assert seen[-1] == failed_gate


@pytest.mark.parametrize("backend", [False, True])
def test_del_07_empty_gate_inventory_fails_closed(
    source: Path, monkeypatch: pytest.MonkeyPatch, backend: bool
) -> None:
    monkeypatch.setattr(runner, "gates", lambda *_args, **_kwargs: ())
    result = runner.verify_local(source, backend=backend, executor=_pass)
    assert result["valid"] is False
    assert result["error"] == "verification_gate_inventory_empty"
    assert result["completed_gates"] == 0


@pytest.mark.parametrize("empty", [False, True])
def test_del_07_backend_inventory_cannot_silently_drop_a_required_gate(
    monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    monkeypatch.setattr(
        runner, "BACKEND_GATE_NAMES", () if empty else (*runner.BACKEND_GATE_NAMES, "missing")
    )
    with pytest.raises(runner.VerificationError, match="backend_gate_inventory_invalid"):
        runner.gates(Path("unused"), backend=True)


@pytest.mark.parametrize("mutation", ["change", "add", "delete", "mode", "symlink"])
@pytest.mark.parametrize("failed", [False, True])
def test_del_07_source_drift_fails_even_after_a_gate_failure(
    source: Path, mutation: str, failed: bool
) -> None:
    path = source / "source.txt"
    link = source / "reference"
    link.symlink_to("source.txt")

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        if gate.name == "catalog-validate":
            if mutation == "change":
                path.write_text("generated drift\n")
            elif mutation == "add":
                (source / "new-source.txt").write_text("generated source\n")
            elif mutation == "delete":
                path.unlink()
            elif mutation == "mode":
                path.chmod(path.stat().st_mode ^ 0o100)
            else:
                link.unlink()
                link.symlink_to("different-target")
            if failed:
                return runner.GateResult(gate.name, "failed", 1, 1)
        return _pass(gate, root, environment)

    result = runner.verify_local(source, executor=execute)
    assert result["valid"] is False
    assert result["error"] == "source_drift_detected"
    assert result["changed_source_files"] == 1
    if mutation == "change":
        assert path.read_text() == "generated drift\n"  # No rollback of caller files.
    if mutation == "delete":
        assert not path.exists()
    if failed:
        assert result["completed_gates"] == 1
    assert Path(result["artifact_directory"], "verification.json").is_file()


def test_del_07_initial_dirty_and_deleted_files_are_preserved(source: Path) -> None:
    (source / "source.txt").unlink()
    (source / "untracked.txt").write_text("caller uncommitted work\n")
    (source / ".gitignore").write_text("*.ignored\n.coverage\n# caller change\n")
    before = runner.source_snapshot(source)
    result = runner.verify_local(source, executor=_pass)
    assert result["valid"] is True
    assert runner.source_snapshot(source) == before
    assert before["source.txt"].kind == "missing"


def test_del_07_only_existing_gitignore_rules_exclude_generated_files(source: Path) -> None:
    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        (root / "cache.ignored").write_text("ignored output")
        return _pass(gate, root, environment)

    assert runner.verify_local(source, executor=execute)["valid"] is True


def test_del_07_snapshot_does_not_follow_links_outside_the_source(
    source: Path, tmp_path: Path
) -> None:
    external = tmp_path / "external-secret"
    external.write_text("outside content")
    (source / "external-link").symlink_to(external)

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        external.write_text("changed outside content")
        return _pass(gate, root, environment)

    assert runner.verify_local(source, executor=execute)["valid"] is True


def test_del_07_snapshot_rejects_symlink_parent_escape(source: Path, tmp_path: Path) -> None:
    directory = source / "tracked-directory"
    directory.mkdir()
    leaf = directory / "tracked.txt"
    leaf.write_text("tracked content\n")
    _git(source, "add", "tracked-directory/tracked.txt")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "tracked.txt").write_text("private outside content\n")
    leaf.unlink()
    directory.rmdir()
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(runner.VerificationError, match="source_parent_escapes_repository") as error:
        runner.source_snapshot(source)
    assert "private outside content" not in str(error.value)


def test_del_07_inventory_errors_are_not_empty_success(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_git", lambda *_args: b"")
    with pytest.raises(runner.VerificationError, match="inventory_empty"):
        runner.source_snapshot(source)


def test_del_07_source_read_errors_fail_closed(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = runner.os.open

    def denied(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path).is_relative_to(source):
            raise PermissionError("private error canary")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(runner.os, "open", denied)
    with pytest.raises(runner.VerificationError, match="source_content_unreadable"):
        runner.source_snapshot(source)


def test_del_07_after_snapshot_failure_prevents_a_green_result(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = runner.source_snapshot
    calls = 0

    def snapshot(root: Path) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise runner.VerificationError("source_git_failed")
        return original(root)

    monkeypatch.setattr(runner, "source_snapshot", snapshot)
    result = runner.verify_local(source, executor=_pass)
    assert result["valid"] is False
    assert result["error"] == "source_snapshot_failed_after_gates"


@pytest.mark.parametrize("returncode", [0, 1, 5])
def test_del_07_real_command_exit_codes_are_not_coerced(source: Path, returncode: int) -> None:
    gate = runner.Gate("tiny-command", (sys.executable, "-c", f"raise SystemExit({returncode})"), 5)
    result = runner.execute_gate(gate, source, os.environ)
    assert result.returncode == returncode
    assert result.status == ("passed" if returncode == 0 else "failed")
    assert 0 <= result.elapsed_ms < 6000


def test_del_07_missing_command_is_a_failure(source: Path) -> None:
    gate = runner.Gate("absent-command", (str(source / "missing-command"),), 5)
    result = runner.execute_gate(gate, source, os.environ)
    assert result.status == "command_unavailable"
    assert result.returncode is None


def test_del_07_timed_out_command_is_terminated_and_not_passed(source: Path) -> None:
    gate = runner.Gate("timeout", (sys.executable, "-c", "import time; time.sleep(30)"), 1)
    result = runner.execute_gate(gate, source, os.environ)
    assert result.status == "timed_out"
    assert result.returncode is not None and result.returncode != 0
    assert 900 <= result.elapsed_ms < 7000


def test_del_07_signal_exit_is_a_failure(source: Path) -> None:
    gate = runner.Gate(
        "signal",
        (sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"),
        5,
    )
    result = runner.execute_gate(gate, source, os.environ)
    assert result.status == "failed"
    assert result.returncode == -signal.SIGTERM


def test_del_07_orchestrator_signal_restores_handlers_and_still_checks_drift(source: Path) -> None:
    original_handler = signal.getsignal(signal.SIGTERM)

    def interrupted(_gate: runner.Gate, root: Path, _environment: Any) -> runner.GateResult:
        (root / "source.txt").write_text("drift before interruption")
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("signal handler must interrupt execution")

    result = runner.verify_local(source, executor=interrupted)
    assert result["valid"] is False
    assert result["error"] == "source_drift_detected"
    assert result["changed_source_files"] == 1
    assert signal.getsignal(signal.SIGTERM) == original_handler


def test_del_07_acceptance_uses_resolved_commit_after_all_local_gates(source: Path) -> None:
    _git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    commit = _git(source, "rev-parse", "HEAD")
    seen: list[runner.Gate] = []

    def execute(gate: runner.Gate, root: Path, environment: Any) -> runner.GateResult:
        seen.append(gate)
        return _pass(gate, root, environment)

    result = runner.verify_local(source, acceptance=True, ref="HEAD", executor=execute)
    assert result["valid"] is True
    assert [gate.name for gate in seen[-3:]] == [
        "web-browser",
        "acceptance-clean",
        "acceptance-backup",
    ]
    assert f"REF={commit}" in seen[-2].argv
    assert all(gate.timeout_seconds <= 1800 for gate in seen)


def test_del_07_invalid_acceptance_ref_does_not_reach_make(source: Path) -> None:
    result = runner.verify_local(
        source, acceptance=True, ref="$(shell touch injected)", executor=_pass
    )
    assert result["valid"] is False
    assert result["completed_gates"] == 0
    assert not (source / "injected").exists()


@pytest.mark.parametrize("change", ["edited", "staged", "untracked", "deleted"])
def test_del_07_acceptance_rejects_source_not_matching_commit(source: Path, change: str) -> None:
    _git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    if change in {"edited", "staged"}:
        (source / "source.txt").write_text("different source\n")
        if change == "staged":
            _git(source, "add", "source.txt")
    elif change == "untracked":
        (source / "new.txt").write_text("not in committed clean export\n")
    else:
        (source / "source.txt").unlink()
    before = runner.source_snapshot(source)
    result = runner.verify_local(source, acceptance=True, ref="HEAD", executor=_pass)
    assert result["valid"] is False
    assert result["error"] == "acceptance_requires_matching_clean_source"
    assert result["completed_gates"] == 0
    assert runner.source_snapshot(source) == before


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("changed", [False, True])
def test_del_07_acceptance_compares_bytes_independently_of_index_hints(
    source: Path, flag: str, changed: bool
) -> None:
    _commit(source)
    _git(source, "update-index", flag, "source.txt")
    if changed:
        (source / "source.txt").write_text("hidden local change\n")
    assert _git(source, "diff", "--name-only", "HEAD", "--") == ""
    before = runner.source_snapshot(source)
    result = runner.verify_local(source, acceptance=True, executor=_pass)
    assert result["valid"] is not changed
    if changed:
        assert result["error"] == "acceptance_requires_matching_clean_source"
        assert result["completed_gates"] == 0
    assert runner.source_snapshot(source) == before


@pytest.mark.parametrize("initial_mode", [0o644, 0o755])
@pytest.mark.parametrize("changed", [False, True])
def test_del_07_acceptance_checks_git_executable_mode_even_when_git_ignores_it(
    source: Path, initial_mode: int, changed: bool
) -> None:
    path = source / "source.txt"
    path.chmod(initial_mode)
    _commit(source)
    _git(source, "config", "core.filemode", "false")
    if changed:
        path.chmod(initial_mode ^ 0o100)
    else:
        # Git deliberately does not record group/other read permission bits.
        path.chmod(initial_mode & ~0o044)
    assert _git(source, "diff", "--name-only", "HEAD", "--") == ""
    result = runner.verify_local(source, acceptance=True, executor=_pass)
    assert result["valid"] is not changed
    if changed:
        assert result["error"] == "acceptance_requires_matching_clean_source"


@pytest.mark.parametrize("change", ["none", "target", "regular-file"])
def test_del_07_acceptance_checks_symlink_type_and_target_bytes(source: Path, change: str) -> None:
    link = source / "reference"
    link.symlink_to("source.txt")
    _commit(source)
    if change != "none":
        link.unlink()
        if change == "target":
            link.symlink_to("missing-target")
        else:
            link.write_text("source.txt")  # Same bytes, different Git mode/type.
    result = runner.verify_local(source, acceptance=True, executor=_pass)
    assert result["valid"] is (change == "none")
    if change != "none":
        assert result["error"] == "acceptance_requires_matching_clean_source"


def test_del_07_committed_snapshot_batches_unique_blobs_and_handles_delimited_paths(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (source / "same\tcontent\n.txt").write_bytes((source / "source.txt").read_bytes())
    _commit(source)
    original = runner._git
    calls: list[tuple[tuple[str, ...], bytes | None]] = []

    def observed(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
        calls.append((args, input_bytes))
        return original(root, *args, input_bytes=input_bytes)

    monkeypatch.setattr(runner, "_git", observed)
    committed = runner.committed_snapshot(source, "HEAD")
    assert len(committed) == 3
    assert len(calls) == 2
    assert calls[0][0] == ("ls-tree", "-r", "-z", "-l", "HEAD")
    assert calls[1][0] == ("cat-file", "--batch")
    assert len(calls[1][1].splitlines()) == 2
    runner.require_matching_commit(source, "HEAD", runner.source_snapshot(source))


@pytest.mark.parametrize(
    "inventory",
    [
        b"",
        b"unterminated",
        b"missing-metadata\0",
        b"160000 commit " + b"a" * 40 + b" -\tsubmodule\0",
        b"100644 blob " + b"a" * 40 + b" 1\t../escape\0",
        b"100644 blob invalid 1\tsource\0",
        b"100644 blob " + b"a" * 40 + b" -1\tsource\0",
    ],
)
def test_del_07_invalid_committed_inventory_fails_without_reading_blobs(
    source: Path, monkeypatch: pytest.MonkeyPatch, inventory: bytes
) -> None:
    def invalid(_root: Path, *args: str, **_kwargs: Any) -> bytes:
        assert args[0] == "ls-tree"
        return inventory

    monkeypatch.setattr(runner, "_git", invalid)
    with pytest.raises(runner.VerificationError, match="committed_source_inventory_invalid"):
        runner.committed_snapshot(source, "HEAD")


@pytest.mark.parametrize("limit", ["bytes", "files"])
def test_del_07_committed_inventory_is_bounded_before_blob_contents(
    source: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    _commit(source)
    monkeypatch.setattr(
        runner,
        "MAX_COMMITTED_SOURCE_BYTES" if limit == "bytes" else "MAX_COMMITTED_SOURCE_FILES",
        1,
    )
    original = runner._git

    def checked(root: Path, *args: str, **kwargs: Any) -> bytes:
        assert args[0] == "ls-tree"
        return original(root, *args, **kwargs)

    monkeypatch.setattr(runner, "_git", checked)
    with pytest.raises(runner.VerificationError, match="too_large"):
        runner.committed_snapshot(source, "HEAD")


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"a" * 40 + b" missing\n",
        b"a" * 40 + b" blob 2\nx\n",
        b"a" * 40 + b" blob 2\nxy\nextra",
    ],
)
def test_del_07_missing_truncated_or_extra_committed_blob_output_fails(
    source: Path, monkeypatch: pytest.MonkeyPatch, contents: bytes
) -> None:
    inventory = b"100644 blob " + b"a" * 40 + b" 2\tsource\0"
    monkeypatch.setattr(
        runner,
        "_git",
        lambda _root, *args, **_kwargs: inventory if args[0] == "ls-tree" else contents,
    )
    with pytest.raises(runner.VerificationError, match="committed_source_blob_invalid"):
        runner.committed_snapshot(source, "HEAD")


@pytest.mark.parametrize(
    "key,value",
    [
        ("LLM_PROVIDER", "real-provider"),
        ("connector_mode", "real"),
        ("ALLOW_EXTERNAL_NETWORK", "true"),
        ("REAL_LLM_OPT_IN", "1"),
        ("REAL_CONNECTOR_OPT_IN", "yes"),
        ("APP_ENV", "production"),
        ("AUTH_MODE", "external"),
    ],
)
def test_del_07_unsafe_mode_fails_before_any_gate(
    source: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    result = runner.verify_local(source, executor=_pass)
    assert result["valid"] is False
    assert result["error"] == "unsafe_verification_environment"
    assert result["completed_gates"] == 0
    assert key not in json.dumps(result)


def test_del_07_explicit_mock_environment_remains_allowed(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in {
        "APP_ENV": "test",
        "AUTH_MODE": "local",
        "LLM_PROVIDER": "mock",
        "CONNECTOR_MODE": "mock",
        "ALLOW_EXTERNAL_NETWORK": "false",
    }.items():
        monkeypatch.setenv(key, value)
    assert runner.verify_local(source, executor=_pass)["valid"] is True


def test_del_07_list_is_read_only_and_ref_requires_acceptance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("listing must not execute Git or gates")

    monkeypatch.setattr(runner, "verify_local", forbidden)
    assert runner.main(["--list"]) == 0
    assert len(json.loads(capsys.readouterr().out)["gates"]) == 20
    assert runner.main(["--list", "--acceptance"]) == 0
    assert len(json.loads(capsys.readouterr().out)["gates"]) == 22
    assert runner.main(["--list", "--backend"]) == 0
    assert len(json.loads(capsys.readouterr().out)["gates"]) == 8
    with pytest.raises(SystemExit) as invalid:
        runner.main(["--ref", "HEAD"])
    assert invalid.value.code == 2
    with pytest.raises(SystemExit) as incompatible:
        runner.main(["--backend", "--acceptance"])
    assert incompatible.value.code == 2
    with pytest.raises(SystemExit) as invalid_backend_ref:
        runner.main(["--backend", "--ref", "HEAD"])
    assert invalid_backend_ref.value.code == 2


def test_del_07_direct_api_also_rejects_conflicting_modes(source: Path) -> None:
    result = runner.verify_local(source, backend=True, acceptance=True, executor=_pass)
    assert result["valid"] is False
    assert result["error"] == "verification_modes_mutually_exclusive"
    with pytest.raises(runner.VerificationError, match="mutually_exclusive"):
        runner.gates(Path("unused"), backend=True, acceptance=True)
