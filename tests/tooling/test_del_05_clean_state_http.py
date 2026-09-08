"""Drive the clean-state client through actual native API and worker processes."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator

from scripts.del_05_clean_state import clean_environment
from scripts.del_05_runtime_smoke import LocalClient, database_snapshot, exercise_demos

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.enable_socket
@pytest.mark.asyncio
async def test_del_05_real_http_all_demos_approval_barrier_and_process_restart(tmp_path):
    database_path = tmp_path / "data" / "local.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    key = tmp_path / "keys" / "digest.key"
    await migrate_local_database(database_url, key)
    database = create_database_runtime(database_url)
    try:
        await seed_catalog(
            compile_catalog(ROOT / "catalog/v1"), database, CroniterRecurrenceCalculator()
        )
    finally:
        await database.dispose()
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    environment = {
        **clean_environment(dict(os.environ)),
        "APP_ENV": "local",
        "AUTH_MODE": "local",
        "LLM_PROVIDER": "mock",
        "CONNECTOR_MODE": "mock",
        "ALLOW_EXTERNAL_NETWORK": "false",
        "DATABASE_URL": database_url,
        "MARKETING_AGENTS_DIGEST_KEY_PATH": str(key),
        "CATALOG_ROOT": str(ROOT / "catalog/v1"),
        "API_HOST": "127.0.0.1",
        "API_PORT": str(port),
        "API_TRUSTED_ORIGINS": json.dumps([origin]),
        "PYTHONPATH": os.pathsep.join((str(ROOT / "apps/api/src"), str(ROOT))),
    }
    processes = []

    def start():
        for module in ("serve_api", "run_worker"):
            arguments = [sys.executable, "-m", f"marketing_agents.workers.{module}"]
            if module == "run_worker":
                arguments.extend(("--poll-seconds", "0.05"))
            processes.append(
                subprocess.Popen(
                    arguments,
                    cwd=tmp_path,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )

    def stop():
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        processes.clear()

    try:
        start()
        client = LocalClient(origin)
        first = exercise_demos(client)
        stop()
        first_database = database_snapshot(database_path, key)
        start()
        second = exercise_demos(client, replay=first)
        stop()
        second_database = database_snapshot(database_path, key)
        assert second["restart_replay"]
        assert first_database["key_fingerprint"] == second_database["key_fingerprint"]
        assert first_database["counts"] == second_database["counts"]
        assert second_database["counts"]["connector_action_receipts"] == 2
    finally:
        stop()
