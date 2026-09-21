"""OBJ-06 bounded real native services and Chromium journey; never live providers.

Reuse the OBJ-04 instrumentation of the production supervisor: all Python
children retain their actual modules, settings and workers behind no-egress
socket guards. Chromium has a separate exact-origin native-transport guard.
Vite's Node process is not a kernel-isolated network sandbox.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import dev  # noqa: E402
from tests.acceptance.obj_04_native_offline import (  # noqa: E402
    _await_supervised_readiness,
    _launch,
    _prewarmed_corepack_cache,
    _stop,
    _verify_reports,
)


def _run_bounded(
    command: Sequence[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    evidence: Path,
    name: str,
    timeout: int,
) -> None:
    """Use the production owned-group reaper on success, failure and timeout."""
    logs = evidence / name
    logs.mkdir()
    supervisor = dev.Supervisor(environment=environment, logs=logs, grace=10)
    try:
        child = supervisor.start(name, command, cwd=cwd)
        code = child.process.wait(timeout=timeout)
        if code != 0:
            raise subprocess.CalledProcessError(code, command)
    finally:
        try:
            supervisor.close()
        finally:
            output = logs / f"{name}.log"
            if output.exists():
                print(output.read_text(errors="replace"), end="", flush=True)


@contextmanager
def _native_installation(
    state: Path,
    reports: Path,
    empty_home: Path,
    corepack_cache: Path,
    environment: dict[str, str],
) -> Iterator[None]:
    previous_path = os.environ.get("PATH")
    os.environ["PATH"] = environment["PATH"]
    try:
        process, log = _launch(state, reports, (8000, 4173), empty_home, corepack_cache)
    finally:
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path
    try:
        _await_supervised_readiness(process, reports)
        yield
    finally:
        _stop(process, log, reports)
    _verify_reports(reports)


def run(node: Path, *, canaries_only: bool = False) -> Path:
    node = node.resolve(strict=True)
    if dev._version([str(node), "--version"], node=node) != dev.NODE_VERSION:
        raise RuntimeError("OBJ-06 requires the repository-pinned installed Node")
    evidence = Path(tempfile.mkdtemp(prefix="marketing-agents-obj06-")).resolve()
    empty_home = evidence / "empty-home"
    empty_home.mkdir(mode=0o700)
    state = evidence / "native-installation"
    environment = {
        name: os.environ[name]
        for name in ("PATH", "TMPDIR", "LANG", "LC_ALL")
        if name in os.environ
    }
    environment.update(
        HOME=str(empty_home),
        PATH=str(node.parent) + os.pathsep + environment.get("PATH", os.defpath),
        PYTHONDONTWRITEBYTECODE="1",
        PLAYWRIGHT_BROWSERS_PATH="0",
        OBJ06_EVIDENCE_DIRECTORY=str(evidence),
        OBJ06_STATE_DIRECTORY=str(state),
        COREPACK_ENABLE_NETWORK="0",
        COREPACK_ENABLE_DOWNLOAD_PROMPT="0",
        COREPACK_ENABLE_AUTO_PIN="0",
        PNPM_CONFIG_OFFLINE="true",
        PNPM_CONFIG_UPDATE_NOTIFIER="false",
    )
    print(f"OBJ-06 evidence directory: {evidence}", flush=True)
    playwright = ROOT / "apps/web/node_modules/.bin/playwright"
    _run_bounded(
        [str(playwright), "test", "--config", "config/playwright-obj-06-canary.config.ts"],
        cwd=ROOT / "apps/web",
        environment=environment,
        evidence=evidence,
        name="canary-runner",
        timeout=180,
    )
    if canaries_only:
        return evidence
    dev.require_free_ports(8000, 4173)
    corepack_cache = _prewarmed_corepack_cache()
    reports = evidence / "native-processes"
    with _native_installation(state, reports, empty_home, corepack_cache, environment):
        environment["OBJ06_NATIVE_READY"] = "1"
        _run_bounded(
            [str(node), str(ROOT / "apps/web/scripts/run-obj-06-e2e.mjs"), "--browser-only"],
            cwd=ROOT,
            environment=environment,
            evidence=evidence,
            name="journey-runner",
            timeout=360,
        )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--canaries-only", action="store_true")
    args = parser.parse_args()

    def interrupted(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt("OBJ-06 harness interrupted")

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        run(args.node, canaries_only=args.canaries_only)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
