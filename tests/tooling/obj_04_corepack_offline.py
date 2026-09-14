"""OBJ-04 native-only gate: real Corepack cold-cache refusal before transport admission."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import dev


def _installed_corepack() -> tuple[Path, Path]:
    node_command, pnpm_command = shutil.which("node"), shutil.which("pnpm")
    if node_command is None or pnpm_command is None:
        pytest.fail(
            "OBJ-04 requires installed pinned Node and Corepack pnpm on PATH; no auto-install"
        )
    node, shim = Path(node_command).resolve(), Path(pnpm_command).resolve()
    version = subprocess.run(
        [str(node), "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env={"PATH": str(node.parent)},
    ).stdout.strip()
    assert version == dev.NODE_VERSION, "OBJ-04 requires the repository-pinned Node 24.20.0 on PATH"
    assert version == "v" + (dev.ROOT / ".nvmrc").read_text().strip()
    manifest = shim.parent.parent / "package.json"
    assert shim.name == "pnpm.js" and manifest.is_file(), (
        "OBJ-04 requires PATH pnpm to resolve to an installed Corepack dist/pnpm.js shim"
    )
    assert json.loads(manifest.read_text())["name"] == "corepack", (
        "OBJ-04 requires real installed Corepack, not a replacement pnpm executable"
    )
    return node, shim


def test_obj_04_real_corepack_cold_cache_refuses_network_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, shim = _installed_corepack()
    project, cache, xdg = (tmp_path / name for name in ("project", "corepack", "xdg"))
    for directory in (project, cache, xdg):
        directory.mkdir()
    package = project / "package.json"
    package.write_text(json.dumps({"private": True, "packageManager": f"pnpm@{dev.PNPM_VERSION}"}))
    original_package = package.read_bytes()
    assert list(cache.iterdir()) == []
    assert list(xdg.iterdir()) == []
    monkeypatch.setenv("COREPACK_HOME", str(cache))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    for flag in (
        "COREPACK_ENABLE_NETWORK",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT",
        "COREPACK_ENABLE_AUTO_PIN",
    ):
        monkeypatch.setenv(flag, "1")
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "COREPACK_NPM_TOKEN",
    ):
        monkeypatch.setenv(name, "obj-04-inert-test-canary")
    monkeypatch.setenv("HTTP_PROXY", "http://obj-04.invalid:1234")
    monkeypatch.setenv("HTTPS_PROXY", "http://obj-04.invalid:1234")
    monkeypatch.setenv("NODE_OPTIONS", "--no-warnings")
    report = tmp_path / "probe.json"
    probe = Path(__file__).with_name("obj_04_corepack_probe.mjs")

    # Exercise the real production launcher and real installed shim. The probe
    # installs the existing deny-all guard first, but the expected refusal is
    # Corepack's own offline decision, with zero guard admissions/denials.
    with pytest.raises(dev.NativeStartupError, match="native_tool_unavailable") as failure:
        dev._version(
            [str(node), str(probe), str(shim), str(report), str(cache), str(xdg), "--version"],
            cwd=project,
        )
    assert "make bootstrap" in str(failure.value)
    assert "obj-04-inert-test-canary" not in str(failure.value)
    assert report.is_file(), "OBJ-04 guarded Corepack process did not produce its safe exit report"
    assert json.loads(report.read_text()) == {
        "exitCode": 1,
        "blockedAttempts": 0,
        "corepackLoaded": True,
        "corepackRefusedNetwork": True,
        "versionArgumentOnly": True,
        "flags": {"network": True, "downloadPrompt": True, "autoPin": True},
        "corepackHomePreserved": True,
        "xdgCachePreserved": True,
        "credentialVariableCount": 0,
    }
    assert package.read_bytes() == original_package
    assert not any(path.is_file() for path in cache.rglob("*"))
    assert not any(path.is_file() for path in xdg.rglob("*"))
