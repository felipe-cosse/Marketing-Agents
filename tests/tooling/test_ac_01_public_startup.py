"""AC-01: clean verification must traverse the public one-command startup path."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from scripts import del_05_clean_state as clean_state
from scripts.del_05_clean_state import Verification, VerificationFailure


class FixtureActivationReached(RuntimeError):
    """Stop the fake journey after public startup proof, before fixture activation."""


def _git(repository: Path, home: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ["PATH"],
    }
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env=environment,
    )


def _blob_identity(value: bytes) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(value)}\0".encode())
    digest.update(value)
    return digest.hexdigest()


def _tree_listing(files: dict[str, tuple[str, bytes]]) -> bytes:
    return b"".join(
        f"{mode} blob {_blob_identity(value)}\t{path}\0".encode()
        for path, (mode, value) in sorted(files.items())
    )


def _prepared_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Verification, Path, str]:
    source = tmp_path / "tracked-export"
    source.mkdir()
    commit = "a" * 40
    verifier = Verification(tmp_path, "HEAD")

    def prepare() -> None:
        verifier.source = source
        verifier.source_hash = "b" * 64
        verifier.origin = "http://127.0.0.1:18080"
        verifier.environment.update(
            {
                "COMPOSE_PROJECT_NAME": verifier.project,
                "MARKETING_AGENTS_WEB_PORT": "18080",
                "MARKETING_AGENTS_SOURCE_REVISION": commit,
            }
        )
        verifier.report["source_commit"] = commit
        verifier.compose = [
            "docker",
            "compose",
            "--project-name",
            verifier.project,
            "--project-directory",
            str(source),
            "--env-file",
            "/dev/null",
            "-f",
            str(source / "compose.yaml"),
            "-f",
            str(tmp_path / "verification.compose.json"),
        ]

    monkeypatch.setattr(verifier, "prepare", prepare)
    return verifier, source, commit


def _assert_public_startup_contract(
    verifier: Verification,
    calls: list[dict[str, Any]],
    *,
    source: Path,
    commit: str,
) -> None:
    public = [call for call in calls if call["argv"] == ["make", "-j1", "up"]]
    assert len(public) == 1, "AC-01 requires exactly one public make startup"
    call = public[0]
    assert call["cwd"] == source
    assert call["environment"]["COMPOSE_PROJECT_NAME"] == verifier.project
    assert call["environment"]["MARKETING_AGENTS_WEB_PORT"] == "18080"
    assert call["environment"]["MARKETING_AGENTS_SOURCE_REVISION"] == commit

    public_index = calls.index(call)
    assert all("up" not in earlier["argv"] for earlier in calls[:public_index]), (
        "a direct Compose startup must not bypass the public Make command"
    )
    command_entries = [
        entry for entry in verifier.report["commands"] if entry.get("label") == "public-startup"
    ]
    assert len(command_entries) == 1
    assert command_entries[0]["argv"] == ["make", "-j1", "up"]
    assert command_entries[0]["returncode"] == 0
    assert verifier.report["source_commit"] == commit
    assert verifier.report["public_startup"] == {
        "argv": ["make", "-j1", "up"],
        "cwd": "tracked-export",
        "source_commit": commit,
        "without_fixture_overrides": True,
    }


def test_ac_01_execute_uses_public_make_startup_from_selected_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier, source, commit = _prepared_verifier(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []
    inspected: list[dict[str, Any]] = []

    def execute(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(
            {
                "argv": list(arguments),
                "cwd": Path(kwargs["cwd"]),
                "environment": dict(kwargs["env"]),
            }
        )
        stdout = b"{}" if arguments[:1] == ["python3"] else b""
        return subprocess.CompletedProcess(arguments, 0, stdout, b"")

    monkeypatch.setattr(clean_state.subprocess, "run", execute)
    monkeypatch.setattr(
        verifier,
        "inspect_runtime_network",
        lambda *args, **kwargs: inspected.append(dict(kwargs)),
    )
    original_compose_command = verifier.compose_command

    def compose_command(label: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        if label == "activate-replay-fixtures":
            raise FixtureActivationReached
        return original_compose_command(label, *args, **kwargs)

    monkeypatch.setattr(verifier, "compose_command", compose_command)

    with pytest.raises(FixtureActivationReached):
        verifier.execute()

    _assert_public_startup_contract(verifier, calls, source=source, commit=commit)
    assert inspected == [{}]
    assert verifier.created is True


def test_ac_01_public_startup_failure_stops_before_runtime_inspection_or_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier, source, _ = _prepared_verifier(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    inspected = False

    def execute(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        calls.append(list(arguments))
        returncode = 23 if arguments == ["make", "-j1", "up"] else 0
        return subprocess.CompletedProcess(arguments, returncode, b"", b"")

    def inspect(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        nonlocal inspected
        inspected = True

    monkeypatch.setattr(clean_state.subprocess, "run", execute)
    monkeypatch.setattr(verifier, "inspect_runtime_network", inspect)

    with pytest.raises(VerificationFailure, match="command_failed:public-startup"):
        verifier.execute()

    assert calls[-1] == ["make", "-j1", "up"]
    assert inspected is False
    assert "public_startup" not in verifier.report
    failure = verifier.report["commands"][-1]
    assert failure["label"] == "public-startup"
    assert failure["argv"] == ["make", "-j1", "up"]
    assert failure["returncode"] == 23
    assert failure["seconds"] >= 0
    assert Path(source).is_dir()


def test_ac_01_negative_control_rejects_direct_compose_startup_bypass(tmp_path: Path) -> None:
    verifier = Verification(tmp_path, "HEAD")
    source = tmp_path / "tracked-export"
    source.mkdir()
    commit = "c" * 40
    verifier.report["source_commit"] = commit
    verifier.report["commands"].append(
        {
            "label": "start-fresh-runtime",
            "argv": ["docker", "compose", "up", "--detach", "--wait"],
            "returncode": 0,
        }
    )
    calls = [
        {
            "argv": ["docker", "compose", "up", "--detach", "--wait"],
            "cwd": source,
            "environment": {
                "COMPOSE_PROJECT_NAME": verifier.project,
                "MARKETING_AGENTS_WEB_PORT": "18080",
                "MARKETING_AGENTS_SOURCE_REVISION": commit,
            },
        }
    ]

    with pytest.raises(AssertionError, match="public make startup"):
        _assert_public_startup_contract(verifier, calls, source=source, commit=commit)


@pytest.mark.parametrize(
    ("tamper", "failure"),
    [
        ("inventory", "exported_tree_inventory_mismatch"),
        ("content", "exported_tree_content_mismatch"),
        ("executable-mode", "exported_tree_mode_mismatch"),
    ],
)
def test_ac_01_export_reconciliation_rejects_archive_tamper(
    tmp_path: Path, tamper: str, failure: str
) -> None:
    root = tmp_path / "tracked-export"
    root.mkdir()
    files = {
        "README.md": ("100644", b"selected commit\n"),
        "scripts/start": ("100755", b"#!/bin/sh\nexit 0\n"),
    }
    for relative, (mode, value) in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(0o755 if mode == "100755" else 0o644)
    listing = _tree_listing(files)
    clean_state.validate_exported_tree(root, listing)

    if tamper == "inventory":
        (root / "untracked-output").write_text("not selected by Git")
    elif tamper == "content":
        (root / "README.md").write_text("archive content was rewritten\n")
    else:
        (root / "scripts/start").chmod(0o644)

    with pytest.raises(VerificationFailure, match=failure):
        clean_state.validate_exported_tree(root, listing)


def test_ac_01_tree_fingerprint_frames_files_and_includes_executable_mode(tmp_path: Path) -> None:
    root = tmp_path / "tracked-export"
    root.mkdir()
    first = root / "a"
    first.write_bytes(b"b\0x")
    first.chmod(0o644)
    original = clean_state.tree_fingerprint(root)

    # This collided under the former unframed path + NUL + content encoding.
    first.write_bytes(b"")
    second = root / "b"
    second.write_bytes(b"x")
    second.chmod(0o644)
    split_inventory = clean_state.tree_fingerprint(root)
    assert split_inventory != original

    second.chmod(0o755)
    assert clean_state.tree_fingerprint(root) != split_inventory


def test_ac_01_export_reconciliation_rejects_symlink_even_when_target_blob_matches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tracked-export"
    root.mkdir()
    value = b"selected commit bytes\n"
    target = tmp_path / "outside"
    target.write_bytes(value)
    (root / "tracked.txt").symlink_to(target)
    listing = _tree_listing({"tracked.txt": ("100644", value)})

    with pytest.raises(VerificationFailure, match="exported_tree_nonregular_entry"):
        clean_state.validate_exported_tree(root, listing)


def test_ac_01_external_temporary_rejects_repository_temp_root_before_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = Verification(tmp_path, "HEAD")
    attempted = False

    def allocate(**_kwargs: Any) -> str:
        nonlocal attempted
        attempted = True
        raise AssertionError("must reject the root before allocating")

    monkeypatch.setattr(clean_state.tempfile, "gettempdir", lambda: str(tmp_path / "data"))
    monkeypatch.setattr(clean_state.tempfile, "mkdtemp", allocate)

    with pytest.raises(VerificationFailure, match="temporary_root_inside_repository"):
        verifier.external_temporary("marketing-agents-ac01-browser-")
    assert attempted is False


def test_ac_01_tar_archive_error_becomes_sanitized_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = Verification(tmp_path, "HEAD")
    private_detail = "private archive parser detail"

    def execute() -> None:
        raise tarfile.ReadError(private_detail)

    def cleanup() -> None:
        verifier.report["cleanup"] = {"ok": True, "failures": []}

    monkeypatch.setattr(clean_state, "Verification", lambda *_args, **_kwargs: verifier)
    monkeypatch.setattr(verifier, "execute", execute)
    monkeypatch.setattr(verifier, "cleanup", cleanup)

    assert clean_state.main([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failure"] == "verification_contract_failure"
    assert private_detail not in json.dumps(report)


@pytest.mark.parametrize(
    ("attribute", "value", "failure"),
    [
        (
            "export-ignore",
            b"must remain in the selected tree\n",
            "exported_tree_inventory_mismatch",
        ),
        ("export-subst", b"$Format:%H$\n", "exported_tree_content_mismatch"),
    ],
)
def test_ac_01_rejects_real_git_archive_changed_by_global_attributes(
    tmp_path: Path, attribute: str, value: bytes, failure: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    attributes = home / "global-attributes"
    attributes.write_text(f"payload.txt {attribute}\n")

    _git(repository, home, "init", "--quiet")
    _git(repository, home, "config", "--global", "core.attributesFile", str(attributes))
    (repository / "payload.txt").write_bytes(value)
    (repository / "sentinel.txt").write_text("keeps the archive structurally non-empty\n")
    _git(repository, home, "add", "payload.txt", "sentinel.txt")
    _git(
        repository,
        home,
        "-c",
        "user.name=AC-01 test",
        "-c",
        "user.email=ac01@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "selected fixture",
    )
    listing = _git(repository, home, "ls-tree", "-rz", "--full-tree", "HEAD").stdout
    archive = tmp_path / "selected.tar"
    _git(repository, home, "archive", "--format=tar", f"--output={archive}", "HEAD")
    exported = tmp_path / "tracked-export"
    exported.mkdir()
    clean_state.export_archive(archive, exported)

    if attribute == "export-ignore":
        assert not (exported / "payload.txt").exists()
    else:
        assert (exported / "payload.txt").read_bytes() != value
    with pytest.raises(VerificationFailure, match=failure):
        clean_state.validate_exported_tree(exported, listing)


def test_ac_01_prepare_reconciles_export_with_selected_git_tree_before_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_PROFILES",
        "DATABASE_URL",
        "MARKETING_AGENTS_DIGEST_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    verifier = Verification(tmp_path, "HEAD")
    commit = "d" * 40
    listing = _tree_listing({"tracked.txt": ("100644", b"selected\n")})
    temporary = tmp_path / "verification"
    temporary.mkdir()
    calls: list[tuple[str, list[str], dict[str, Any]]] = []
    reconciled: list[tuple[Path, bytes]] = []
    exported: list[tuple[Path, Path]] = []

    outputs = {
        "inspect-local-docker-context": b'[{"Endpoints":{"docker":{"Host":"unix:///tmp/docker.sock"}}}]',
        "verify-docker-engine-version": b'"28.0.0"',
        "resolve-selected-commit": f"{commit}\n".encode(),
        "caller-worktree-status": b"",
        "archive-selected-commit": b"",
        "selected-commit-tree": listing,
    }

    def command(
        label: str, arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((label, list(arguments), kwargs))
        return subprocess.CompletedProcess(arguments, 0, outputs[label], b"")

    monkeypatch.setattr(clean_state.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(verifier, "external_temporary", lambda _prefix: temporary)
    monkeypatch.setattr(verifier, "command", command)
    monkeypatch.setattr(
        clean_state,
        "export_archive",
        lambda archive, destination: exported.append((archive, destination)),
    )
    monkeypatch.setattr(
        clean_state,
        "validate_exported_tree",
        lambda root, selected: reconciled.append((root, selected)),
    )

    with pytest.raises(VerificationFailure, match="selected_commit_missing_inputs"):
        verifier.prepare()

    labels = [label for label, _arguments, _kwargs in calls]
    assert labels[-2:] == ["archive-selected-commit", "selected-commit-tree"]
    selected = calls[-1]
    assert selected[1] == ["git", "ls-tree", "-rz", "--full-tree", commit]
    assert selected[2]["cwd"] == verifier.repository
    assert exported == [(temporary / "source.tar", temporary / "source")]
    assert reconciled == [(temporary / "source", listing)]
    assert verifier.selected_tree_listing == listing
    assert verifier.report["export_matches_selected_git_objects"] is True
