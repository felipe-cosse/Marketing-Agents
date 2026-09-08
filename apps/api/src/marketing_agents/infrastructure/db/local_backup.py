"""Protected, paired SQLite snapshots and restore into new local storage only.

The online-backup API includes committed WAL contents. All metadata is read from
the completed snapshot, never from a live catalog that could advance separately.
These bundles contain a secret and are local recovery artifacts, not CI evidence.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from marketing_agents.infrastructure.db.local_installation import verify_local_installation
from marketing_agents.infrastructure.db.migrations import HEAD_REVISION
from marketing_agents.infrastructure.db.url import parse_database_url

DATABASE_NAME = "marketing_agents.db"
KEY_NAME = "secrets/digest.key"
MANIFEST_NAME = "manifest.json"
CHECKSUM_NAME = "manifest.sha256"
STAGING_PREFIX = ".marketing-agents-stage-"
MANIFEST_MAX_BYTES = 4096
SNAPSHOT_TIMEOUT_SECONDS = 30


class LocalBackupError(RuntimeError):
    """Stable errors intentionally omit paths, database URLs, and secret values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    format_version: Literal[1]
    snapshot_method: Literal["sqlite-online-backup-v1"]
    secret_bearing: Literal[True]
    schema_revision: str = Field(min_length=1, max_length=32)
    key_format_version: Literal[1]
    key_fingerprint: str = Field(pattern=r"^digest-key-fingerprint-v1:[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=80)
    catalog_hash: str = Field(pattern=r"^catalog-sha256-v1:[0-9a-f]{64}$")
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("format_version", "key_format_version", mode="before")
    @classmethod
    def _integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version requires an integer")
        return value

    @field_validator("secret_bearing", mode="before")
    @classmethod
    def _boolean_marker(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("secret marker requires a boolean")
        return value


def _database_url(database: Path) -> str:
    return f"sqlite+aiosqlite:///{database}"


def _sqlite_path(database_url: str) -> Path:
    url = parse_database_url(database_url)
    if (
        url.drivername != "sqlite+aiosqlite"
        or not url.database
        or url.database == ":memory:"
        or url.database.startswith("file:")
        or url.query
    ):
        raise LocalBackupError("local_backup_requires_sqlite_file")
    database = Path(url.database).expanduser().absolute()
    if database.is_symlink() or not database.is_file():
        raise LocalBackupError("local_backup_database_missing")
    return database


def _private_directory(directory: Path) -> None:
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise LocalBackupError("local_backup_permissions_invalid")


@contextmanager
def _private_file(path: Path) -> Iterator[BinaryIO]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise LocalBackupError("local_backup_permissions_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _read_bounded(path: Path, limit: int) -> bytes:
    with _private_file(path) as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise LocalBackupError("local_backup_manifest_invalid")
    return payload


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with _private_file(path) as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _copy_private(source: Path, destination: Path) -> None:
    with _private_file(source) as original:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as copied:
            shutil.copyfileobj(original, copied, length=1024 * 1024)
            copied.flush()
            os.fsync(copied.fileno())


def _new_destination(destination: Path) -> Path:
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise LocalBackupError("local_backup_destination_exists")
    # Resolve the existing parent before staging. /tmp may itself be a platform
    # symlink; the final component must still be absent and explicitly scoped.
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination in {Path("/"), Path.home().resolve(), Path.cwd().resolve()}:
        raise LocalBackupError("local_backup_destination_unsafe")
    if os.path.lexists(destination):
        raise LocalBackupError("local_backup_destination_exists")
    if not destination.parent.is_dir() or destination.name.startswith(STAGING_PREFIX):
        raise LocalBackupError("local_backup_destination_unsafe")
    return destination


def _stage(destination: Path) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=destination.parent))
    staging.chmod(0o700)
    (staging / "secrets").mkdir(mode=0o700)
    return staging


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_stage(staging: Path, destination: Path) -> None:
    """Atomic same-filesystem publication that cannot replace even an empty dir."""
    _fsync_directory(staging / "secrets")
    _fsync_directory(staging)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(staging), os.fsencode(destination), 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1)
    else:
        raise LocalBackupError("local_backup_atomic_publish_unsupported")
    if result != 0:
        raise LocalBackupError("local_backup_publish_failed")
    _fsync_directory(destination.parent)


def _online_snapshot(source: Path, destination: Path) -> None:
    _write_private(destination, b"")
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS

    def progress(status: int, remaining: int, total: int) -> None:
        del status, remaining, total
        if time.monotonic() >= deadline:
            raise LocalBackupError("local_backup_snapshot_timeout")

    original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
    copied = sqlite3.connect(destination, timeout=5)
    try:
        original.backup(copied, pages=256, progress=progress, sleep=0.05)
        # Persist all pages in the database itself; restore must never depend on
        # transient WAL/SHM companions that are absent from the manifest.
        if copied.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise LocalBackupError("local_backup_snapshot_invalid")
    finally:
        copied.close()
        original.close()
    with _private_file(destination) as stream:
        os.fsync(stream.fileno())


def _snapshot_metadata(database: Path) -> dict[str, object]:
    with _private_file(database) as stream:
        header = stream.read(20)
    if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise LocalBackupError("local_backup_snapshot_invalid")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
            raise LocalBackupError("local_backup_snapshot_invalid")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise LocalBackupError("local_backup_snapshot_invalid")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LocalBackupError("local_backup_snapshot_invalid")
        schema = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        identities = connection.execute(
            "SELECT format_version, key_fingerprint FROM local_runtime_identity "
            "WHERE singleton_id=1"
        ).fetchall()
        catalog = connection.execute(
            "SELECT r.content_version, r.content_hash, r.snapshot_json "
            "FROM catalog_current_release c JOIN catalog_releases r "
            "ON c.content_hash = r.content_hash WHERE c.singleton_id=1"
        ).fetchall()
        if schema != [(HEAD_REVISION,)] or len(identities) != 1 or identities[0][0] != 1:
            raise LocalBackupError("local_backup_version_unsupported")
        if len(catalog) != 1 or catalog[0][1] != (
            "catalog-sha256-v1:" + hashlib.sha256(catalog[0][2].encode("utf-8")).hexdigest()
        ):
            raise LocalBackupError("local_backup_catalog_invalid")
        try:
            catalog_snapshot = json.loads(catalog[0][2])
            if catalog_snapshot["manifest"]["content_version"] != catalog[0][0]:
                raise LocalBackupError("local_backup_catalog_invalid")
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalBackupError("local_backup_catalog_invalid") from exc
        return {
            "schema_revision": schema[0][0],
            "key_format_version": identities[0][0],
            "key_fingerprint": identities[0][1],
            "catalog_version": catalog[0][0],
            "catalog_hash": catalog[0][1],
        }
    finally:
        connection.close()


async def _manifest_for_pair(directory: Path) -> BackupManifest:
    database, key = directory / DATABASE_NAME, directory / KEY_NAME
    metadata = _snapshot_metadata(database)
    await verify_local_installation(_database_url(database), key)
    return BackupManifest.model_validate(
        {
            "format_version": 1,
            "snapshot_method": "sqlite-online-backup-v1",
            "secret_bearing": True,
            **metadata,
            "database_sha256": _checksum(database),
            "key_sha256": _checksum(key),
        }
    )


async def _validate_bundle(directory: Path, *, staging: bool = False) -> BackupManifest:
    if not staging and directory.name.startswith(STAGING_PREFIX):
        raise LocalBackupError("local_backup_staging_incomplete")
    _private_directory(directory)
    _private_directory(directory / "secrets")
    if {path.name for path in directory.iterdir()} != {
        DATABASE_NAME,
        "secrets",
        MANIFEST_NAME,
        CHECKSUM_NAME,
    } or {path.name for path in (directory / "secrets").iterdir()} != {"digest.key"}:
        raise LocalBackupError("local_backup_bundle_incomplete")
    payload = _read_bounded(directory / MANIFEST_NAME, MANIFEST_MAX_BYTES)
    expected_checksum = _read_bounded(directory / CHECKSUM_NAME, 65)
    if expected_checksum != (hashlib.sha256(payload).hexdigest() + "\n").encode("ascii"):
        raise LocalBackupError("local_backup_checksum_invalid")
    try:
        manifest = BackupManifest.model_validate_json(payload)
    except ValidationError as exc:
        raise LocalBackupError("local_backup_manifest_invalid") from exc
    if manifest.schema_revision != HEAD_REVISION:
        raise LocalBackupError("local_backup_version_unsupported")
    if manifest.database_sha256 != _checksum(directory / DATABASE_NAME) or (
        manifest.key_sha256 != _checksum(directory / KEY_NAME)
    ):
        raise LocalBackupError("local_backup_checksum_invalid")
    actual = await _manifest_for_pair(directory)
    if manifest != actual:
        raise LocalBackupError("local_backup_metadata_mismatch")
    return manifest


async def backup_local_installation(
    database_url: str, key_path: Path, destination: Path
) -> BackupManifest:
    """Snapshot a running SQLite installation and publish a new protected bundle."""
    destination = _new_destination(destination)
    database = _sqlite_path(database_url)
    staging = _stage(destination)
    try:
        _private_directory(key_path.expanduser().absolute().parent)
        _write_private(staging / KEY_NAME, _read_bounded(key_path.expanduser(), 256))
        await asyncio.to_thread(_online_snapshot, database, staging / DATABASE_NAME)
        manifest = await _manifest_for_pair(staging)
        payload = (json.dumps(manifest.model_dump(), sort_keys=True, indent=2) + "\n").encode()
        _write_private(staging / MANIFEST_NAME, payload)
        _write_private(
            staging / CHECKSUM_NAME, (hashlib.sha256(payload).hexdigest() + "\n").encode("ascii")
        )
        # The completed pair was verified before serializing its checksums.
        # Inspecting live metadata separately cannot strengthen that invariant
        # and would race ordinary schema/catalog updates during the snapshot.
        _publish_stage(staging, destination)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


async def restore_local_installation(backup: Path, destination: Path) -> BackupManifest:
    """Validate both halves before publishing into an absent destination only."""
    destination = _new_destination(destination)
    backup = backup.expanduser().absolute()
    manifest = await _validate_bundle(backup)
    staging = _stage(destination)
    try:
        for name in (DATABASE_NAME, KEY_NAME, MANIFEST_NAME, CHECKSUM_NAME):
            _copy_private(backup / name, staging / name)
        # Validate staged bytes again: source changes during copying cannot
        # publish a mixed database/key pair or silently change the manifest.
        if await _validate_bundle(staging, staging=True) != manifest:
            raise LocalBackupError("local_backup_metadata_mismatch")
        _publish_stage(staging, destination)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)
