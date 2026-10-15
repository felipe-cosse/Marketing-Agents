"""Real native PostgreSQL primary: seed, application role, workflows and restart.

Uses installed tools and only owned state/process groups. The HTTP smoke is not
a browser-security claim; existing browser and offline gates remain separate.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

from scripts import dev, native_postgres
from scripts.del_05_runtime_smoke import database_snapshot, exercise_demos
from tests.acceptance.obj_04_native_offline import (
    WebClient,
    _await_supervised_readiness,
    _ports,
    _prewarmed_corepack_cache,
)

ROOT = Path(__file__).resolve().parents[2]


def _query(psql: Path, socket: str, environment: dict[str, str], sql: str) -> str:
    result = subprocess.run(
        [
            str(psql),
            "-X",
            "--no-password",
            "--host",
            socket,
            "--port",
            "5432",
            "--username",
            "marketing_agents",
            "--dbname",
            "marketing_agents",
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--command",
            sql,
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, "native PostgreSQL verification query failed"
    return result.stdout.strip()


def _stop(process, log, pg_pid: int | None) -> None:
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("native supervisor exceeded its existing shutdown bound")
        assert process.returncode == 0, "native supervisor exited abnormally"
        if pg_pid is not None:
            with pytest.raises(ProcessLookupError):
                os.killpg(pg_pid, 0)
    finally:
        log.close()


@pytest.mark.enable_socket
def test_del_10_default_native_postgresql_seed_workflows_and_restart(tmp_path: Path):
    environment = dev._tool_environment()
    environment["COREPACK_HOME"] = str(_prewarmed_corepack_cache())
    tools = native_postgres.installed_tools(environment)
    state = tmp_path / "postgresql-installation"
    ports = _ports()
    client = WebClient(ports)
    previous = None
    snapshots = []
    for generation in (1, 2):
        reports = tmp_path / f"generation-{generation}"
        reports.mkdir(mode=0o700)
        log = (reports / "controller.log").open("wb")
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/dev.py"),
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
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pg_pid = None
        socket = None
        try:
            _await_supervised_readiness(process, reports)
            pid_lines = (state / "postgres/postmaster.pid").read_text().splitlines()
            pg_pid = int(pid_lines[0])
            assert pg_pid > 1 and os.getpgid(pg_pid) == pg_pid
            assert Path(pid_lines[1]).resolve() == (state / "postgres").resolve()
            socket = pid_lines[4]
            assert Path(socket).name.startswith("ma-pg-")
            assert Path(socket).stat().st_mode & 0o777 == 0o700
            assert _query(tools.psql, socket, environment, "SHOW listen_addresses") == ""
            assert (
                _query(tools.psql, socket, environment, "SELECT current_user, current_database()")
                == "marketing_agents|marketing_agents"
            )
            assert (
                _query(
                    tools.psql,
                    socket,
                    environment,
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user",
                )
                == "f|f|f|f|f"
            )
            assert (
                _query(
                    tools.psql,
                    socket,
                    environment,
                    "SELECT pg_get_userbyid(datdba) FROM pg_database "
                    "WHERE datname = current_database()",
                )
                == "marketing_agents"
            )
            assert not (state / "marketing_agents.db").exists()
            result = exercise_demos(client, replay=previous)
            assert len(result["demos"]) == 5
            assert all(demo["state"] == "completed" for demo in result["demos"])
            assert result["email_preapproval_zero_calls"]
            assert result["restart_replay"] is (generation == 2)
            previous = result
            url = (
                "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents?"
                + urlencode({"host": socket})
            )
            snapshots.append(database_snapshot(url, state / "secrets/digest.key"))
        finally:
            _stop(process, log, pg_pid)
        assert not (state / "postgres/postmaster.pid").exists()
        assert socket is not None and not Path(socket).exists()
        assert (state / "postgres/PG_VERSION").is_file()
        assert (state / "secrets/digest.key").is_file()
    assert snapshots[0]["key_fingerprint"] == snapshots[1]["key_fingerprint"]
    assert snapshots[0]["counts"] == snapshots[1]["counts"]
    assert snapshots[1]["counts"]["connector_action_receipts"] == 2
