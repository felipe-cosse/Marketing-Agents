"""OBJ-04 actual native initialization, web/API/workers, mock work and restart.

The child guard covers instrumented Python socket/DNS interfaces, not a kernel
sandbox or Vite's Node process. Compose no-egress and browser guards remain
separate evidence. Synthetic poison is never copied to reports or test output.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from scripts.del_05_runtime_smoke import LocalClient, database_snapshot, exercise_demos
from tests.acceptance.obj_04_guard import POISON_NAMES

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_MODULES = {
    "marketing_agents.workers.local_secret_init",
    "marketing_agents.workers.database_cli",
    "marketing_agents.workers.serve_api",
    "marketing_agents.workers.run_worker",
    "marketing_agents.workers.scheduler_worker",
    "marketing_agents.workers.worker_health",
}


class WebClient(LocalClient):
    """Keep every product request on Vite; health is intentionally API-only."""

    def __init__(self, ports: tuple[int, int]) -> None:
        super().__init__(f"http://127.0.0.1:{ports[1]}", timeout=90)
        self.health = LocalClient(f"http://127.0.0.1:{ports[0]}", timeout=90)

    def ready(self) -> dict:
        return self.health.ready()


def _ports() -> tuple[int, int]:
    with socket.socket() as api, socket.socket() as web:
        api.bind(("127.0.0.1", 0))
        web.bind(("127.0.0.1", 0))
        return api.getsockname()[1], web.getsockname()[1]


def _prewarmed_corepack_cache() -> Path:
    # The requirement verifier gives gates an empty HOME. Locate the installed
    # package-manager dependency independently, never copy an operator profile
    # or permit Corepack to acquire a missing package manager during this gate.
    supplied = os.environ.get("COREPACK_HOME")
    candidate = (
        Path(supplied)
        if supplied is not None
        else Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache/node/corepack"
    )
    manifest = candidate / "v1/pnpm/11.24.0/package.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        version = None
    assert version == "11.24.0", (
        "OBJ-04 native acceptance requires prewarmed pnpm 11.24.0 in COREPACK_HOME; "
        "run explicit bootstrap separately, never fetch during this gate"
    )
    return candidate.resolve()


def _poisoned_environment(empty_home: Path, corepack_cache: Path) -> dict[str, str]:
    # Keep only tool/cache discovery state, never actual ambient credentials.
    environment = {
        name: os.environ[name]
        for name in ("PATH", "TMPDIR", "LANG", "LC_ALL")
        if name in os.environ
    }
    environment["HOME"] = str(empty_home)
    environment["COREPACK_HOME"] = str(corepack_cache)
    environment.update(dict.fromkeys(POISON_NAMES, "obj04-public-synthetic-poison"))
    # PYTHONPATH/NODE_OPTIONS must be syntactically harmless to the controller
    # itself; they are still required to disappear at every real child launch.
    environment["PYTHONPATH"] = "/obj04-nonexistent-synthetic-path"
    environment["NODE_OPTIONS"] = "--no-warnings"
    environment.update(
        {
            "APP_ENV": "production",
            "LLM_PROVIDER": "real",
            "CONNECTOR_MODE": "real",
            "ALLOW_EXTERNAL_NETWORK": "true",
            "REAL_LLM_OPT_IN": "true",
            "REAL_CONNECTOR_OPT_IN": "true",
            "COREPACK_ENABLE_NETWORK": "1",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "1",
            "COREPACK_ENABLE_AUTO_PIN": "1",
            "PNPM_CONFIG_OFFLINE": "false",
            "PNPM_CONFIG_UPDATE_NOTIFIER": "true",
            "DATABASE_URL": "synthetic-invalid-database",
        }
    )
    return environment


def _launch(
    state: Path,
    reports: Path,
    ports: tuple[int, int],
    empty_home: Path,
    corepack_cache: Path,
):
    reports.mkdir()
    log = (reports / "controller.log").open("wb")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/acceptance/obj_04_controller.py"),
                str(reports),
                "--state-dir",
                str(state),
                "--api-port",
                str(ports[0]),
                "--web-port",
                str(ports[1]),
                "--startup-timeout",
                "90",
            ],
            cwd=ROOT,
            env=_poisoned_environment(empty_home, corepack_cache),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        log.close()
        raise
    return process, log


def _await_supervised_readiness(process, report_root: Path) -> None:
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        assert process.poll() is None, "native supervisor exited before readiness"
        if b"Local mock platform ready:" in (report_root / "controller.log").read_bytes():
            return
        time.sleep(0.2)
    pytest.fail("native supervisor did not reach full API/web/worker readiness")


def _reap_recorded_groups(report_root: Path) -> list[str]:
    """Check every recorded owned group, even after an early controller exit."""
    launches = report_root / "launches.jsonl"
    if not launches.is_file():
        return []
    failures = []
    owned = set()
    for line in launches.read_text().splitlines():
        try:
            event = json.loads(line)
            if event.get("owned_group") is not True:
                continue
            group = event["pid"]
            if type(group) is not int or group <= 1:
                raise ValueError("invalid owned process group")
            owned.add(group)
        except (ValueError, KeyError, AttributeError):
            failures.append("native ownership report is malformed")
    pending = set(owned)
    observed_live = set()
    kill_sent = set()
    deadline = time.monotonic() + 5
    while pending:
        for group in sorted(pending):
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                pending.remove(group)
                continue
            except PermissionError:
                # A denied probe proves neither presence nor absence. Keep it
                # pending until ESRCH is observed or the fixed budget expires.
                continue
            except OSError:
                failures.append("native owned group could not be inspected")
                continue
            if group not in observed_live:
                failures.append("native supervisor left an owned process group alive")
                observed_live.add(group)
            if group not in kill_sent:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pending.remove(group)
                except PermissionError:
                    continue
                except OSError:
                    failures.append("native owned group could not be stopped")
                else:
                    kill_sent.add(group)
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    if pending:
        failures.append(
            "native owned process group exit could not be confirmed within cleanup bound"
        )
    return failures


def _stop(process, log, report_root: Path) -> None:
    failures = []
    try:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            failures.append("native supervisor exceeded its shutdown bound")
            with suppress(ProcessLookupError):
                process.kill()
            process.wait(timeout=5)
    finally:
        try:
            # Children each own independent groups. Controller exit or failure
            # therefore cannot substitute for checking the recorded groups.
            failures.extend(_reap_recorded_groups(report_root))
        finally:
            log.close()
    if process.returncode != 0:
        failures.append("native supervisor exited abnormally")
    if failures:
        pytest.fail("; ".join(dict.fromkeys(failures)))


def _verify_reports(reports: Path) -> None:
    events = [json.loads(line) for line in (reports / "launches.jsonl").read_text().splitlines()]
    assert {event["role"] for event in events} >= EXPECTED_MODULES
    assert sum(event["role"] == "version" for event in events) == 3
    assert sum(event["role"] == "web" for event in events) == 1
    assert all(event["environment_verified"] for event in events)
    assert all(not POISON_NAMES.intersection(event["environment_names"]) for event in events)
    for event in events:
        if event["owned_group"]:
            with pytest.raises(ProcessLookupError):
                os.killpg(event["pid"], 0)
    child_reports = [json.loads(path.read_text()) for path in reports.glob("python-*.json")]
    assert len(child_reports) == sum(event["role"] in EXPECTED_MODULES for event in events)
    assert {report["role"] for report in child_reports} >= EXPECTED_MODULES
    child_reports.append(json.loads((reports / "controller.json").read_text()))
    for report in child_reports:
        assert report["canaries_blocked"] == 5
        assert report["external_attempts"] == report["delegate_violations"] == 0
        assert report["application_finished"]


@pytest.mark.enable_socket
def test_obj_04_real_native_launcher_is_credential_free_offline_and_replay_safe(tmp_path: Path):
    corepack_cache = _prewarmed_corepack_cache()
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir(mode=0o700)
    state = tmp_path / "native-installation"
    assert not state.exists()
    ports = _ports()
    client = WebClient(ports)
    previous = None
    snapshots = []
    for generation in (1, 2):
        reports = tmp_path / f"generation-{generation}"
        process, log = _launch(state, reports, ports, empty_home, corepack_cache)
        try:
            _await_supervised_readiness(process, reports)
            result = exercise_demos(client, replay=previous)
            assert len(result["demos"]) == 5
            assert all(demo["state"] == "completed" for demo in result["demos"])
            assert result["email_preapproval_zero_calls"]
            assert result["restart_replay"] is (generation == 2)
            previous = result
        finally:
            _stop(process, log, reports)
        assert process.returncode == 0
        _verify_reports(reports)
        snapshots.append(
            database_snapshot(state / "marketing_agents.db", state / "secrets/digest.key")
        )
    assert snapshots[0]["key_fingerprint"] == snapshots[1]["key_fingerprint"]
    assert snapshots[0]["counts"] == snapshots[1]["counts"]
    assert snapshots[1]["counts"]["connector_action_receipts"] == 2
