"""DEL-05: paired, protected online SQLite backup and atomic new-storage restore."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db import local_backup as backup_module
from marketing_agents.infrastructure.db.local_backup import (
    CHECKSUM_NAME,
    DATABASE_NAME,
    KEY_NAME,
    MANIFEST_NAME,
    STAGING_PREFIX,
    LocalBackupError,
    backup_local_installation,
    restore_local_installation,
)
from marketing_agents.infrastructure.db.local_installation import (
    initialize_local_secret,
    migrate_local_database,
    verify_local_installation,
)
from marketing_agents.infrastructure.db.migrations import HEAD_REVISION
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.digest_key import DigestKeyError
from marketing_agents.security.webhook_digest import derive_webhook_body_digest

from tests.integration.db.test_api_05_webhook_receipts import (
    _factory,
    _persist_work_runs,
    _receipt,
)

ROOT = Path(__file__).resolve().parents[3]
RAW_BODY = b'{"event_id":"event.del-05.backup-replay","content":"private-canary"}'


def _url(database: Path) -> str:
    return f"sqlite+aiosqlite:///{database}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def _base_installation(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("del-05-closed-fixture")
    database, key = directory / DATABASE_NAME, directory / KEY_NAME
    await migrate_local_database(_url(database), key)
    runtime = create_database_runtime(_url(database))
    try:
        await seed_catalog(
            compile_catalog(ROOT / "catalog" / "v1"), runtime, CroniterRecurrenceCalculator()
        )
    finally:
        await runtime.dispose()
    return directory


@pytest.fixture
def installation(_base_installation: Path, tmp_path: Path) -> tuple[Path, Path]:
    # This test fixture is closed before copying. Running installations must use
    # the production online-backup API exercised below, including live WAL data.
    directory = tmp_path / "source"
    shutil.copytree(_base_installation, directory)
    return directory / DATABASE_NAME, directory / KEY_NAME


def _rows(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {
            table: sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
            for (table,) in tables
        }
    finally:
        connection.close()


def _files(directory: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(path.relative_to(directory)): (stat.S_IMODE(path.stat().st_mode), path.read_bytes())
        for path in directory.rglob("*")
        if path.is_file()
    }


def _rewrite_manifest(bundle: Path, **changes: object) -> None:
    path = bundle / MANIFEST_NAME
    document = json.loads(path.read_text())
    document.update(changes)
    payload = json.dumps(document, sort_keys=True).encode()
    path.write_bytes(payload)
    (bundle / CHECKSUM_NAME).write_text(hashlib.sha256(payload).hexdigest() + "\n")


async def test_del_05_online_wal_backup_restores_all_rows_and_exact_replay(
    installation: tuple[Path, Path], tmp_path: Path
) -> None:
    database, key_path = installation
    key = await initialize_local_secret(_url(database), key_path)
    digest = derive_webhook_body_digest(RAW_BODY, key)
    runtime = create_database_runtime(_url(database))
    try:
        work_runs = await _persist_work_runs(runtime)
        receipt = replace(
            _receipt(work_runs),
            body_digest=digest.value,
            digest_key_version=digest.digest_key_version,
        )
        async with _factory(runtime)() as unit_of_work:
            assert (await unit_of_work.webhook_receipts.add_or_get(receipt)).inserted
            await unit_of_work.commit()
    finally:
        await runtime.dispose()
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE agent_instance_configs SET variant_label='Committed in WAL' "
            "WHERE instance_id=?",
            (work_runs[0][0].instance_id,),
        )
        writer.commit()
        assert database.with_name(database.name + "-wal").stat().st_size > 0
        before = _rows(database)
        bundle, restored = tmp_path / "bundle #1", tmp_path / "restored installation"
        manifest = await backup_local_installation(_url(database), key_path, bundle)
        assert manifest.schema_revision == HEAD_REVISION
        assert manifest.secret_bearing is True
        assert await restore_local_installation(bundle, restored) == manifest
        assert _rows(database) == before
    finally:
        writer.close()
    assert _rows(restored / DATABASE_NAME) == before
    assert (restored / KEY_NAME).read_bytes() == key_path.read_bytes()
    assert len(before["webhook_receipts"]) == 1
    assert len(before["webhook_receipt_deliveries"]) == 2
    for directory in (bundle, restored):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "secrets").stat().st_mode) == 0o700
        assert {p.name for p in directory.iterdir()} == {
            DATABASE_NAME,
            "secrets",
            MANIFEST_NAME,
            CHECKSUM_NAME,
        }
        assert all(mode == 0o600 for mode, _ in _files(directory).values())
    restored_key = await initialize_local_secret(
        _url(restored / DATABASE_NAME), restored / KEY_NAME
    )
    assert derive_webhook_body_digest(RAW_BODY, restored_key).matches(digest)
    restarted = create_database_runtime(_url(restored / DATABASE_NAME))
    try:
        async with _factory(restarted)() as unit_of_work:
            replay = await unit_of_work.webhook_receipts.add_or_get(receipt)
            assert replay.inserted is False
            assert replay.receipt == receipt
            await unit_of_work.commit()
    finally:
        await restarted.dispose()
    assert _rows(restored / DATABASE_NAME) == before


@pytest.mark.parametrize(
    "problem",
    (
        "database_missing",
        "key_missing",
        "manifest_missing",
        "checksum_missing",
        "database_corrupt",
        "wal_header",
        "key_mismatch",
        "key_permissions",
        "directory_permissions",
        "manifest_checksum",
        "manifest_version",
        "boolean_version",
        "key_version",
        "schema_version",
        "metadata_mismatch",
        "manifest_extra",
        "manifest_oversized",
        "key_symlink",
    ),
)
async def test_del_05_restore_rejects_invalid_bundle_without_touching_any_destination(
    installation: tuple[Path, Path], tmp_path: Path, problem: str
) -> None:
    database, key = installation
    bundle, destination = tmp_path / "bundle", tmp_path / "restore"
    await backup_local_installation(_url(database), key, bundle)
    if problem.endswith("_missing"):
        missing = {
            "database_missing": DATABASE_NAME,
            "key_missing": KEY_NAME,
            "manifest_missing": MANIFEST_NAME,
            "checksum_missing": CHECKSUM_NAME,
        }[problem]
        (bundle / missing).unlink()
    elif problem == "database_corrupt":
        with (bundle / DATABASE_NAME).open("ab") as stream:
            stream.write(b"tampered")
    elif problem == "wal_header":
        with (bundle / DATABASE_NAME).open("r+b") as stream:
            stream.seek(18)
            stream.write(b"\x02\x02")
        _rewrite_manifest(
            bundle,
            database_sha256=hashlib.sha256((bundle / DATABASE_NAME).read_bytes()).hexdigest(),
        )
    elif problem == "key_mismatch":
        (bundle / KEY_NAME).write_bytes(base64.urlsafe_b64encode(bytes([19]) * 32) + b"\n")
        _rewrite_manifest(
            bundle, key_sha256=hashlib.sha256((bundle / KEY_NAME).read_bytes()).hexdigest()
        )
    elif problem == "key_permissions":
        (bundle / KEY_NAME).chmod(0o640)
    elif problem == "directory_permissions":
        (bundle / "secrets").chmod(0o750)
    elif problem == "manifest_checksum":
        (bundle / CHECKSUM_NAME).write_text("a" * 64 + "\n")
    elif problem == "manifest_version":
        _rewrite_manifest(bundle, format_version=2)
    elif problem == "boolean_version":
        _rewrite_manifest(bundle, format_version=True)
    elif problem == "key_version":
        _rewrite_manifest(bundle, key_format_version=2)
    elif problem == "schema_version":
        _rewrite_manifest(bundle, schema_revision="future-schema")
    elif problem == "metadata_mismatch":
        _rewrite_manifest(bundle, catalog_version="different-catalog")
    elif problem == "manifest_extra":
        _rewrite_manifest(bundle, unexpected="forbidden")
    elif problem == "manifest_oversized":
        (bundle / MANIFEST_NAME).write_bytes(b" " * 4097)
    else:
        (bundle / KEY_NAME).unlink()
        (bundle / KEY_NAME).symlink_to(key)
    before_source, before_bundle = _files(database.parent), _files(bundle)
    with pytest.raises((LocalBackupError, DigestKeyError, OSError)):
        await restore_local_installation(bundle, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(STAGING_PREFIX + "*"))
    assert _files(database.parent) == before_source
    assert _files(bundle) == before_bundle


@pytest.mark.parametrize("operation", ("backup", "restore"))
@pytest.mark.parametrize("kind", ("populated", "empty", "symlink"))
async def test_del_05_never_overwrites_existing_destination(
    installation: tuple[Path, Path], tmp_path: Path, operation: str, kind: str
) -> None:
    database, key = installation
    bundle, destination = tmp_path / "bundle", tmp_path / "existing"
    await backup_local_installation(_url(database), key, bundle)
    if kind == "symlink":
        destination.symlink_to(database.parent, target_is_directory=True)
    else:
        destination.mkdir(mode=0o700)
        if kind == "populated":
            (destination / "must-remain").write_bytes(b"existing active storage")
    before = _files(tmp_path)
    with pytest.raises(LocalBackupError, match="destination_exists"):
        if operation == "backup":
            await backup_local_installation(_url(database), key, destination)
        else:
            await restore_local_installation(bundle, destination)
    assert _files(tmp_path) == before


async def test_del_05_publication_cannot_replace_concurrently_created_empty_destination(
    installation: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, key = installation
    destination = tmp_path / "racing-target"
    publish = backup_module._publish_stage
    inode: list[int] = []

    def competing_creator(staging: Path, target: Path) -> None:
        target.mkdir(mode=0o700)
        inode.append(target.stat().st_ino)
        publish(staging, target)

    monkeypatch.setattr(backup_module, "_publish_stage", competing_creator)
    with pytest.raises(LocalBackupError, match="publish_failed"):
        await backup_local_installation(_url(database), key, destination)
    assert destination.stat().st_ino == inode[0]
    assert list(destination.iterdir()) == []
    assert not list(tmp_path.glob(STAGING_PREFIX + "*"))


async def test_del_05_interrupted_staging_is_never_a_published_or_accepted_bundle(
    installation: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, key = installation
    destination = tmp_path / "unpublished"

    def interrupted(source: Path, target: Path) -> None:
        del source
        target.write_bytes(b"incomplete snapshot")
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(backup_module, "_online_snapshot", interrupted)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await backup_local_installation(_url(database), key, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(STAGING_PREFIX + "*"))
    orphan = tmp_path / (STAGING_PREFIX + "interrupted")
    orphan.mkdir(mode=0o700)
    with pytest.raises(LocalBackupError, match="staging_incomplete"):
        await restore_local_installation(orphan, tmp_path / "restored")
    await verify_local_installation(_url(database), key)


async def test_del_05_catalog_metadata_is_from_snapshot_not_advancing_live_database(
    installation: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, key = installation
    online_snapshot = backup_module._online_snapshot

    def advance_after_snapshot(source: Path, destination: Path) -> None:
        online_snapshot(source, destination)
        connection = sqlite3.connect(source)
        try:
            connection.execute("UPDATE catalog_releases SET content_version='2.0.0'")
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(backup_module, "_online_snapshot", advance_after_snapshot)
    bundle = tmp_path / "bundle"
    manifest = await backup_local_installation(_url(database), key, bundle)
    assert manifest.catalog_version == "1.0.0"
    assert _rows(database)["catalog_releases"][0][1] == "2.0.0"
    await restore_local_installation(bundle, tmp_path / "restored")


async def test_del_05_identity_change_during_snapshot_rejects_pair_before_publication(
    installation: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, key = installation
    online_snapshot = backup_module._online_snapshot

    def changed_identity(source: Path, destination: Path) -> None:
        online_snapshot(source, destination)
        connection = sqlite3.connect(destination)
        try:
            connection.execute(
                "UPDATE local_runtime_identity SET key_fingerprint=?",
                ("digest-key-fingerprint-v1:" + "a" * 64,),
            )
            connection.commit()
        finally:
            connection.close()

    before = _rows(database)
    monkeypatch.setattr(backup_module, "_online_snapshot", changed_identity)
    destination = tmp_path / "wrong-pair"
    with pytest.raises(DigestKeyError, match="fingerprint"):
        await backup_local_installation(_url(database), key, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(STAGING_PREFIX + "*"))
    assert _rows(database) == before


async def test_del_05_online_snapshot_deadline_never_publishes_partial_backup(
    installation: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, key = installation
    destination = tmp_path / "timed-out"
    monkeypatch.setattr(backup_module, "SNAPSHOT_TIMEOUT_SECONDS", 0)
    with pytest.raises(LocalBackupError, match="snapshot_timeout"):
        await backup_local_installation(_url(database), key, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(STAGING_PREFIX + "*"))
    await verify_local_installation(_url(database), key)


async def test_del_05_cli_round_trip_and_sanitized_failure(
    installation: tuple[Path, Path], tmp_path: Path
) -> None:
    database, key = installation
    bundle, restored = tmp_path / "cli-bundle", tmp_path / "cli-restore"

    def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
        result = subprocess.run(
            [sys.executable, "-m", "marketing_agents.workers.backup_cli", *arguments],
            cwd=ROOT,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(ROOT / "apps/api/src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.stderr == ""
        assert str(tmp_path) not in result.stdout
        assert key.read_text().strip() not in result.stdout
        assert "private-canary" not in result.stdout
        return result.returncode, json.loads(result.stdout)

    code, result = invoke(
        "backup",
        "--database-url",
        _url(database),
        "--key-path",
        str(key),
        "--destination",
        str(bundle),
    )
    assert code == 0 and result["code"] == "local_backup_complete"
    code, result = invoke("restore", "--backup", str(bundle), "--destination", str(restored))
    assert code == 0 and result["code"] == "local_restore_complete"
    assert _rows(restored / DATABASE_NAME) == _rows(database)
    code, result = invoke(
        "backup",
        "--database-url",
        "postgresql+asyncpg://user:private-canary@localhost/private",
        "--key-path",
        str(key),
        "--destination",
        str(tmp_path / "unsupported"),
    )
    assert code == 1
    assert result == {"ok": False, "code": "local_backup_requires_sqlite_file"}
