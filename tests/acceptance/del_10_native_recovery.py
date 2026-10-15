"""Real stopped-native PostgreSQL recovery, without repeating workflow suites.

All clusters, keys, bundles and process groups belong to this test invocation.
The installed server/client major must agree; no service or package is installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator

from scripts import dev, native_postgres
from scripts import postgresql_backup as backup
from scripts.del_05_runtime_smoke import database_snapshot


@contextmanager
def owned_server(state: Path):
    with dev.installation_lock(state):
        pg = native_postgres.ManagedPostgres(state, dev._tool_environment())
        with tempfile.TemporaryDirectory(
            prefix="marketing-agents-native-recovery-log-"
        ) as directory:
            supervisor = dev.Supervisor(environment=dev._tool_environment(), logs=Path(directory))
            try:
                child = pg.start(supervisor)
                yield pg
            finally:
                supervisor.close()
                pg.close()
            with pytest.raises(ProcessLookupError):
                os.killpg(child.process.pid, 0)


async def seed(database_url: str, key: Path) -> None:
    await migrate_local_database(database_url, key)
    runtime = create_database_runtime(database_url)
    try:
        await seed_catalog(
            compile_catalog(backup.ROOT / "catalog/v1"), runtime, CroniterRecurrenceCalculator()
        )
    finally:
        await runtime.dispose()


@pytest.fixture(scope="module")
def stopped_installation(tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-postgresql-recovery")
    state = directory / "source"
    key = state / "secrets/digest.key"
    with owned_server(state) as pg:
        asyncio.run(seed(pg.url, key))
        original = database_snapshot(pg.url, key)
    assert not (state / "postgres/postmaster.pid").exists()
    return state, original


@pytest.mark.enable_socket
def test_native_stopped_backup_fresh_restore_all_rows_key_and_overwrite_controls(
    stopped_installation, tmp_path
):
    source, original = stopped_installation
    bundle = tmp_path / "bundle"
    backup.native_backup(source, bundle)
    manifest = backup.validate_bundle(bundle)
    tools = native_postgres.installed_tools(dev._tool_environment())
    assert manifest.postgresql_major == tools.major
    assert not (source / "postgres/postmaster.pid").exists()
    restored = tmp_path / "restored"
    backup.native_restore(bundle, restored)
    assert not (restored / "postgres/postmaster.pid").exists()
    assert (restored / "secrets/digest.key").read_bytes() == (
        source / "secrets/digest.key"
    ).read_bytes()
    with owned_server(restored) as pg:
        assert database_snapshot(pg.url, restored / "secrets/digest.key") == original
    hashes = {name: backup._checksum(bundle / name) for name in backup.FILES}
    with pytest.raises(backup.LocalBackupError, match="destination_exists"):
        backup.native_backup(source, bundle)
    with pytest.raises(backup.LocalBackupError, match="destination_exists"):
        backup.native_restore(bundle, restored)
    assert hashes == {name: backup._checksum(bundle / name) for name in backup.FILES}
    with owned_server(source) as pg:
        assert database_snapshot(pg.url, source / "secrets/digest.key") == original
        with pytest.raises(dev.NativeStartupError, match="native_already_running"):
            backup.native_backup(source, tmp_path / "active-refused")
    assert not (tmp_path / "active-refused").exists()


@pytest.mark.enable_socket
def test_native_restore_major_mismatch_refuses_before_schema_or_key_writes(
    stopped_installation, tmp_path
):
    source, _ = stopped_installation
    bundle = tmp_path / "bundle"
    backup.native_backup(source, bundle)
    payload = json.loads((bundle / backup.MANIFEST_NAME).read_bytes())
    payload["postgresql_major"] += 1
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    (bundle / backup.MANIFEST_NAME).write_bytes(encoded)
    import hashlib

    (bundle / backup.CHECKSUM_NAME).write_text(hashlib.sha256(encoded).hexdigest() + "\n")
    target = tmp_path / "refused-major"
    with pytest.raises(backup.LocalBackupError, match="major_mismatch"):
        backup.native_restore(bundle, target)
    assert not (target / "postgres/postmaster.pid").exists()
    assert not (target / "secrets/digest.key").exists()
    with owned_server(target) as pg:
        assert (
            pg.query(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
                application=True,
            )
            == "0"
        )


@pytest.mark.enable_socket
def test_native_backup_sigterm_reaps_owned_postgres_and_removes_private_socket(
    stopped_installation, tmp_path
):
    state, _ = stopped_installation
    marker = tmp_path / "ready"
    code = "\n".join(
        [
            "import asyncio, os, sys",
            "from pathlib import Path",
            "from scripts import postgresql_backup as backup",
            "async def paused(*args):",
            "    fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
            "    os.close(fd)",
            "    await asyncio.Event().wait()",
            "backup.backup_postgresql = paused",
            "arguments = ['backup', '--mode', 'native', '--state-dir', sys.argv[1],",
            "             '--destination', sys.argv[3]]",
            "raise SystemExit(backup.main(arguments))",
        ]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(state), str(marker), str(tmp_path / "interrupted")],
        cwd=backup.ROOT,
        env=dev._tool_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pid = None
    socket = None
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "owned native backup did not reach interruption boundary"
        lines = (state / "postgres/postmaster.pid").read_text().splitlines()
        pid, socket = int(lines[0]), Path(lines[4])
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=40)
        assert process.returncode == 1
        assert json.loads(stdout) == {"ok": False, "code": "postgresql_backup_failed"}
        assert not stderr
        assert not (state / "postgres/postmaster.pid").exists()
        assert not socket.exists()
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, 0)
        assert not (tmp_path / "interrupted").exists()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        # Failure fallback is exact to the state created by this fixture, never
        # a user service, arbitrary PID, existing database, or global pg_ctl.
        if (state / "postgres/postmaster.pid").exists():
            tools = native_postgres.installed_tools(dev._tool_environment())
            result = subprocess.run(
                [
                    str(tools.pg_ctl),
                    "-D",
                    str(state / "postgres"),
                    "-m",
                    "immediate",
                    "-w",
                    "-t",
                    "20",
                    "stop",
                ],
                env=dev._tool_environment(),
                capture_output=True,
                timeout=25,
                check=False,
            )
            assert result.returncode == 0, "owned native test cluster cleanup failed"
