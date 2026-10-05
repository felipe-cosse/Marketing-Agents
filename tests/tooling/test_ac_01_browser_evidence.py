"""AC-01 browser evidence is retained atomically outside the source checkout."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import del_05_clean_state as clean_state

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
DESKTOP_PNG = PNG_HEADER + b"public desktop evidence"
MOBILE_PNG = PNG_HEADER + b"public mobile evidence"
BROWSER_NAME = "marketing-agents-del05-0123456789abcdef-browser"
SCREENSHOT_SOURCE = "/tmp/marketing-agents-del05-browser-safe_123"


def browser_result(payload: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        ["docker", "run"], 0, stdout=json.dumps(payload).encode(), stderr=b""
    )


def successful_payload(*, screenshot_source: str = SCREENSHOT_SOURCE) -> dict[str, object]:
    return {
        "ok": True,
        "checks": {"desktop": True, "mobile": True},
        "warnings": [],
        "screenshots": screenshot_source,
    }


def allocate_external_directory(
    verifier: clean_state.Verification,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, list[str]]:
    evidence = tmp_path / "external-browser-evidence"
    prefixes: list[str] = []

    def external_temporary(prefix: str) -> Path:
        prefixes.append(prefix)
        evidence.mkdir(mode=0o700)
        return evidence

    monkeypatch.setattr(verifier, "external_temporary", external_temporary, raising=False)
    return evidence, prefixes


def test_ac_01_browser_evidence_requires_json_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = clean_state.Verification(tmp_path, "HEAD")

    def allocation_forbidden(_prefix: str) -> Path:
        raise AssertionError("invalid browser output must fail before allocating evidence")

    monkeypatch.setattr(verifier, "external_temporary", allocation_forbidden, raising=False)

    with pytest.raises(clean_state.VerificationFailure):
        verifier.retain_browser_evidence(browser_result([{"ok": True}]), BROWSER_NAME)

    assert "browser" not in verifier.report


def test_ac_01_browser_evidence_requires_success_before_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = clean_state.Verification(tmp_path, "HEAD")

    def allocation_forbidden(_prefix: str) -> Path:
        raise AssertionError("failed browser output must not allocate evidence")

    monkeypatch.setattr(verifier, "external_temporary", allocation_forbidden, raising=False)

    with pytest.raises(clean_state.VerificationFailure):
        verifier.retain_browser_evidence(
            browser_result({**successful_payload(), "ok": False}), BROWSER_NAME
        )

    assert "browser" not in verifier.report


@pytest.mark.parametrize(
    "screenshot_source",
    [
        "/tmp/browser-output",
        "/tmp/marketing-agents-del05-browser-../escape",
        "/tmp/marketing-agents-del05-browser-safe/nested",
        "relative/marketing-agents-del05-browser-safe",
    ],
)
def test_ac_01_browser_evidence_rejects_unsafe_source_before_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, screenshot_source: str
) -> None:
    verifier = clean_state.Verification(tmp_path, "HEAD")

    def allocation_forbidden(_prefix: str) -> Path:
        raise AssertionError("unsafe source must fail before allocating evidence")

    monkeypatch.setattr(verifier, "external_temporary", allocation_forbidden, raising=False)

    with pytest.raises(clean_state.VerificationFailure):
        verifier.retain_browser_evidence(
            browser_result(successful_payload(screenshot_source=screenshot_source)), BROWSER_NAME
        )

    assert "browser" not in verifier.report


def test_ac_01_browser_evidence_second_copy_failure_removes_partial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = clean_state.Verification(tmp_path, "HEAD")
    evidence, prefixes = allocate_external_directory(verifier, monkeypatch, tmp_path)
    calls: list[tuple[str, list[str]]] = []

    def command(label: str, arguments: list[str], **_kwargs: object):
        calls.append((label, arguments))
        if label == "retain-browser-desktop":
            Path(arguments[-1]).write_bytes(DESKTOP_PNG)
            return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")
        raise clean_state.VerificationFailure("command_failed:retain-browser-mobile")

    monkeypatch.setattr(verifier, "command", command)

    with pytest.raises(clean_state.VerificationFailure, match="retain-browser-mobile"):
        verifier.retain_browser_evidence(browser_result(successful_payload()), BROWSER_NAME)

    assert prefixes == ["marketing-agents-ac01-browser-"]
    assert [label for label, _arguments in calls] == [
        "retain-browser-desktop",
        "retain-browser-mobile",
    ]
    assert not evidence.exists()
    assert "browser" not in verifier.report


def test_ac_01_browser_evidence_invalid_png_removes_allocated_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = clean_state.Verification(tmp_path, "HEAD")
    evidence, _prefixes = allocate_external_directory(verifier, monkeypatch, tmp_path)

    def command(label: str, arguments: list[str], **_kwargs: object):
        content = DESKTOP_PNG if label == "retain-browser-desktop" else b"not a png"
        Path(arguments[-1]).write_bytes(content)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(verifier, "command", command)

    with pytest.raises(clean_state.VerificationFailure):
        verifier.retain_browser_evidence(browser_result(successful_payload()), BROWSER_NAME)

    assert not evidence.exists()
    assert "browser" not in verifier.report


def test_ac_01_browser_evidence_retains_only_two_hashed_pngs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = clean_state.Verification(tmp_path, "HEAD")
    evidence, prefixes = allocate_external_directory(verifier, monkeypatch, tmp_path)
    calls: list[tuple[str, list[str]]] = []

    def command(label: str, arguments: list[str], **_kwargs: object):
        calls.append((label, arguments))
        content = DESKTOP_PNG if label == "retain-browser-desktop" else MOBILE_PNG
        Path(arguments[-1]).write_bytes(content)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(verifier, "command", command)

    verifier.retain_browser_evidence(browser_result(successful_payload()), BROWSER_NAME)

    assert prefixes == ["marketing-agents-ac01-browser-"]
    assert calls == [
        (
            "retain-browser-desktop",
            [
                "docker",
                "cp",
                f"{BROWSER_NAME}:{SCREENSHOT_SOURCE}/desktop.png",
                str(evidence / "desktop.png"),
            ],
        ),
        (
            "retain-browser-mobile",
            [
                "docker",
                "cp",
                f"{BROWSER_NAME}:{SCREENSHOT_SOURCE}/mobile.png",
                str(evidence / "mobile.png"),
            ],
        ),
    ]
    assert sorted(path.name for path in evidence.iterdir()) == ["desktop.png", "mobile.png"]
    assert verifier.report["browser"] == {
        "ok": True,
        "checks": {"desktop": True, "mobile": True},
        "warnings": [],
        "screenshots": {
            "desktop": {
                "path": str(evidence / "desktop.png"),
                "sha256": hashlib.sha256(DESKTOP_PNG).hexdigest(),
            },
            "mobile": {
                "path": str(evidence / "mobile.png"),
                "sha256": hashlib.sha256(MOBILE_PNG).hexdigest(),
            },
        },
    }
