"""Private PostgreSQL/key pairs; restore only into a newly owned local installation.

The custom dump and metadata share one exported read-only transaction snapshot.
Checksums detect accidental corruption, not an untrusted archive author: restore
only trusted, locally created bundles. No raw PostgreSQL data directory is copied.
Compose helpers have no network, use only Unix sockets, and never pull images.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

import asyncpg
from marketing_agents.infrastructure.db.local_backup import (
    CHECKSUM_NAME,
    KEY_NAME,
    MANIFEST_MAX_BYTES,
    MANIFEST_NAME,
    STAGING_PREFIX,
    BackupManifest,
    LocalBackupError,
    _checksum,
    _copy_private,
    _new_destination,
    _private_directory,
    _private_file,
    _publish_stage,
    _read_bounded,
    _stage,
    _write_private,
)
from marketing_agents.infrastructure.db.local_installation import verify_local_installation
from marketing_agents.infrastructure.db.migrations import HEAD_REVISION
from marketing_agents.infrastructure.db.url import parse_database_url
from marketing_agents.security.digest_key import _decode_key, digest_key_fingerprint
from pydantic import Field, ValidationError

try:
    from . import local_backup as legacy
except ImportError:
    import local_backup as legacy

ROOT = Path(__file__).resolve().parents[1]
DUMP_NAME = "marketing_agents.pgdump"
FILES = (DUMP_NAME, KEY_NAME, MANIFEST_NAME, CHECKSUM_NAME)
DATA_DIRECTORY = Path("/var/lib/postgresql/data")
SOCKET_DIRECTORY = Path("/var/run/marketing-agents-postgres")
SECRET_DIRECTORY = Path("/var/lib/marketing-agents/secrets")
KEY_PATH = SECRET_DIRECTORY / "digest.key"
DATABASE_URL = (
    "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents"
    "?host=/var/run/marketing-agents-postgres"
)
VOLUMES = ("postgres-data", "postgres-socket", "postgres-secrets")
RUN_LABEL = "org.marketing-agents.postgresql-backup"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
TOOL_TIMEOUT = 90


def require(condition: object, code: str) -> None:
    if not condition:
        raise LocalBackupError(code)


class PostgresqlManifest(BackupManifest):
    snapshot_method: Literal["postgresql-exported-snapshot-custom-v1"]  # type: ignore[assignment]
    postgresql_major: int = Field(ge=14, le=99)


def connection_options(database_url: str) -> dict[str, object]:
    """Reject TCP, passwords, alternate roles/databases, and libpq overrides."""
    url = parse_database_url(database_url)
    socket = url.query.get("host")
    require(
        url.drivername == "postgresql+asyncpg"
        and url.host == "localhost"
        and url.port in {None, 5432}
        and url.username == "marketing_agents"
        and url.password is None
        and url.database == "marketing_agents"
        and set(url.query) == {"host"}
        and isinstance(socket, str)
        and socket.startswith("/")
        and "," not in socket
        and "\x00" not in socket,
        "postgresql_backup_requires_local_socket",
    )
    return {
        "host": socket,
        "port": 5432,
        "user": "marketing_agents",
        "database": "marketing_agents",
        "password": "",
        "passfile": "/nonexistent/marketing-agents-no-passfile",
        "ssl": False,
        "timeout": 5,
        "command_timeout": TOOL_TIMEOUT,
    }


def tool_environment(database_url: str) -> dict[str, str]:
    options = connection_options(database_url)
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "PGHOST": str(options["host"]),
        "PGPORT": "5432",
        "PGUSER": "marketing_agents",
        "PGDATABASE": "marketing_agents",
        "PGPASSFILE": "/nonexistent/marketing-agents-no-passfile",
        "PGSERVICEFILE": "/dev/null",
        "PGCONNECT_TIMEOUT": "5",
        "PGSSLMODE": "disable",
    }


def run_tool(arguments: list[str], database_url: str, *, output: BinaryIO | None = None) -> bytes:
    try:
        with tempfile.TemporaryDirectory(prefix="marketing-agents-pg-client-") as directory:
            passfile = Path(directory) / "empty-passfile"
            _write_private(passfile, b"")
            result = subprocess.run(
                arguments,
                env={**tool_environment(database_url), "PGPASSFILE": str(passfile)},
                stdin=subprocess.DEVNULL,
                stdout=output if output is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TOOL_TIMEOUT,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        raise LocalBackupError("postgresql_backup_tool_failed") from None
    require(result.returncode == 0, "postgresql_backup_tool_failed")
    # Warnings must not silently qualify a partial or questionable dump.
    require(not result.stderr.strip(), "postgresql_backup_tool_warning")
    return result.stdout or b""


def client_major(tool: str, database_url: str) -> int:
    result = run_tool([tool, "--version"], database_url).decode("ascii")
    match = re.fullmatch(r"\S+ \(PostgreSQL\) (\d+)\.\d+[^\n]*\n?", result)
    require(match is not None, "postgresql_backup_version_invalid")
    assert match is not None
    return int(match[1])


async def server_major(connection) -> int:
    identity = await connection.fetchrow(
        "SELECT current_user AS role, current_database() AS database, "
        "current_setting('server_version_num')::int AS version, "
        "rolsuper, rolreplication, rolbypassrls, rolcreatedb, rolcreaterole "
        "FROM pg_roles WHERE rolname=current_user"
    )
    require(
        identity is not None
        and identity["role"] == "marketing_agents"
        and identity["database"] == "marketing_agents"
        and not identity["rolsuper"]
        and not identity["rolreplication"]
        and not identity["rolbypassrls"]
        and not identity["rolcreatedb"]
        and not identity["rolcreaterole"],
        "postgresql_backup_identity_invalid",
    )
    major = identity["version"] // 10000
    require(14 <= major <= 99, "postgresql_backup_version_invalid")
    return major


async def snapshot_metadata(connection) -> dict[str, object]:
    schema = await connection.fetch("SELECT version_num FROM alembic_version")
    identities = await connection.fetch(
        "SELECT singleton_id, format_version, key_fingerprint FROM local_runtime_identity"
    )
    catalog = await connection.fetch(
        "SELECT r.content_version, r.content_hash, r.snapshot_json "
        "FROM catalog_current_release c JOIN catalog_releases r "
        "ON c.content_hash=r.content_hash WHERE c.singleton_id=1"
    )
    require(
        len(schema) == 1
        and schema[0]["version_num"] == HEAD_REVISION
        and len(identities) == 1
        and identities[0]["singleton_id"] == 1
        and identities[0]["format_version"] == 1,
        "postgresql_backup_version_unsupported",
    )
    require(len(catalog) == 1, "postgresql_backup_catalog_invalid")
    release = catalog[0]
    require(
        release["content_hash"]
        == "catalog-sha256-v1:" + hashlib.sha256(release["snapshot_json"].encode()).hexdigest(),
        "postgresql_backup_catalog_invalid",
    )
    try:
        snapshot = json.loads(release["snapshot_json"])
        require(
            snapshot["manifest"]["content_version"] == release["content_version"],
            "postgresql_backup_catalog_invalid",
        )
    except (ValueError, KeyError, TypeError):
        raise LocalBackupError("postgresql_backup_catalog_invalid") from None
    return {
        "schema_revision": HEAD_REVISION,
        "key_format_version": 1,
        "key_fingerprint": identities[0]["key_fingerprint"],
        "catalog_version": release["content_version"],
        "catalog_hash": release["content_hash"],
    }


def validate_bundle(directory: Path, *, staging: bool = False) -> PostgresqlManifest:
    require(
        staging or not directory.name.startswith(STAGING_PREFIX),
        "postgresql_backup_staging_incomplete",
    )
    _private_directory(directory)
    _private_directory(directory / "secrets")
    require(
        {p.name for p in directory.iterdir()}
        == {DUMP_NAME, "secrets", MANIFEST_NAME, CHECKSUM_NAME}
        and {p.name for p in (directory / "secrets").iterdir()} == {"digest.key"},
        "postgresql_backup_bundle_incomplete",
    )
    payload = _read_bounded(directory / MANIFEST_NAME, MANIFEST_MAX_BYTES)
    require(
        _read_bounded(directory / CHECKSUM_NAME, 65)
        == (hashlib.sha256(payload).hexdigest() + "\n").encode(),
        "postgresql_backup_checksum_invalid",
    )
    try:
        manifest = PostgresqlManifest.model_validate_json(payload)
    except ValidationError:
        raise LocalBackupError("postgresql_backup_manifest_invalid") from None
    require(manifest.schema_revision == HEAD_REVISION, "postgresql_backup_version_unsupported")
    require(
        manifest.database_sha256 == _checksum(directory / DUMP_NAME)
        and manifest.key_sha256 == _checksum(directory / KEY_NAME),
        "postgresql_backup_checksum_invalid",
    )
    with _private_file(directory / DUMP_NAME) as stream:
        require(stream.read(5) == b"PGDMP", "postgresql_backup_dump_invalid")
    require(
        digest_key_fingerprint(_decode_key(_read_bounded(directory / KEY_NAME, 256)))
        == manifest.key_fingerprint,
        "postgresql_backup_key_mismatch",
    )
    return manifest


async def backup_postgresql(
    database_url: str, key_path: Path, destination: Path
) -> PostgresqlManifest:
    options = connection_options(database_url)
    destination = _new_destination(destination)
    _private_directory(key_path.parent)
    await verify_local_installation(database_url, key_path)
    staging = _stage(destination)
    connection = None
    try:
        _copy_private(key_path, staging / KEY_NAME)
        connection = await asyncpg.connect(**options)
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            major = await server_major(connection)
            require(
                client_major("pg_dump", database_url) == major, "postgresql_backup_major_mismatch"
            )
            snapshot = await connection.fetchval("SELECT pg_export_snapshot()")
            require(
                isinstance(snapshot, str) and re.fullmatch(r"[0-9A-Fa-f-]+", snapshot),
                "postgresql_backup_snapshot_invalid",
            )
            metadata = await snapshot_metadata(connection)
            require(
                digest_key_fingerprint(_decode_key(_read_bounded(staging / KEY_NAME, 256)))
                == metadata["key_fingerprint"],
                "postgresql_backup_key_mismatch",
            )
            descriptor = os.open(
                staging / DUMP_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                await asyncio.to_thread(
                    run_tool,
                    [
                        "pg_dump",
                        "--no-password",
                        "--format=custom",
                        "--no-acl",
                        f"--snapshot={snapshot}",
                    ],
                    database_url,
                    output=output,
                )
                output.flush()
                os.fsync(output.fileno())
        manifest = PostgresqlManifest.model_validate(
            {
                "format_version": 1,
                "snapshot_method": "postgresql-exported-snapshot-custom-v1",
                "secret_bearing": True,
                "postgresql_major": major,
                **metadata,
                "database_sha256": _checksum(staging / DUMP_NAME),
                "key_sha256": _checksum(staging / KEY_NAME),
            }
        )
        payload = (json.dumps(manifest.model_dump(), sort_keys=True, indent=2) + "\n").encode()
        _write_private(staging / MANIFEST_NAME, payload)
        _write_private(
            staging / CHECKSUM_NAME, (hashlib.sha256(payload).hexdigest() + "\n").encode()
        )
        validate_bundle(staging, staging=True)
        _publish_stage(staging, destination)
        return manifest
    finally:
        if connection is not None:
            await connection.close()
        if staging.exists():
            shutil.rmtree(staging)


def copy_bundle(backup: Path, destination: Path) -> PostgresqlManifest:
    destination = _new_destination(destination)
    expected = validate_bundle(backup)
    staging = _stage(destination)
    try:
        for name in FILES:
            _copy_private(backup / name, staging / name)
        require(validate_bundle(staging, staging=True) == expected, "postgresql_backup_changed")
        _publish_stage(staging, destination)
        return expected
    finally:
        if staging.exists():
            shutil.rmtree(staging)


async def restore_into_empty_database(backup: Path, database_url: str, key_path: Path) -> None:
    """Internal primitive: callers MUST own this freshly provisioned empty database.

    No public CLI accepts an arbitrary restore URL. Compose/native wrappers establish
    fresh storage ownership before calling here. A second restore is refused.
    """
    options = connection_options(database_url)
    _private_directory(key_path.parent)
    require(not any(key_path.parent.iterdir()), "postgresql_restore_key_directory_not_empty")
    with tempfile.TemporaryDirectory(prefix="marketing-agents-pg-verified-") as directory:
        verified = Path(directory) / "bundle"
        manifest = copy_bundle(backup, verified)
        connection = await asyncpg.connect(**options)
        try:
            major = await server_major(connection)
            require(
                major == manifest.postgresql_major == client_major("pg_restore", database_url),
                "postgresql_backup_major_mismatch",
            )
            # An empty public schema is standard bootstrap state; every user
            # relation, routine, extra schema, or non-default extension refuses.
            occupied = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') "
                "OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') "
                "OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' "
                "AND nspname NOT IN ('public','information_schema')) "
                "OR EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
                "AND t.typtype IN ('e','d','r','m')) "
                "OR EXISTS (SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql')"
            )
            require(not occupied, "postgresql_restore_database_not_empty")
        finally:
            await connection.close()
        await asyncio.to_thread(
            run_tool,
            [
                "pg_restore",
                "--no-password",
                "--single-transaction",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "--dbname=marketing_agents",
                str(verified / DUMP_NAME),
            ],
            database_url,
        )
        # Validate restored metadata before committing the separate key file.
        connection = await asyncpg.connect(**options)
        try:
            actual = await snapshot_metadata(connection)
            require(
                all(getattr(manifest, name) == value for name, value in actual.items()),
                "postgresql_backup_metadata_mismatch",
            )
        finally:
            await connection.close()
        await verify_local_installation(database_url, verified / KEY_NAME)
        _copy_private(verified / KEY_NAME, key_path)
        await verify_local_installation(database_url, key_path)


def pack_bundle(bundle: Path, stream: BinaryIO) -> None:
    validate_bundle(bundle)
    with tarfile.open(fileobj=stream, mode="w|") as archive:
        directory = tarfile.TarInfo("secrets")
        directory.type, directory.mode = tarfile.DIRTYPE, 0o700
        archive.addfile(directory)
        total = 0
        for name in FILES:
            with _private_file(bundle / name) as source:
                item = tarfile.TarInfo(name)
                item.mode, item.size = 0o600, os.fstat(source.fileno()).st_size
                total += item.size
                require(total <= MAX_ARCHIVE_BYTES - 10240, "postgresql_backup_archive_too_large")
                archive.addfile(item, source)


def unpack_bundle(archive_path: Path, destination: Path) -> None:
    require(archive_path.stat().st_size <= MAX_ARCHIVE_BYTES, "postgresql_backup_archive_too_large")
    require(not os.path.lexists(destination), "postgresql_backup_destination_exists")
    with tarfile.open(archive_path, "r:") as archive:
        members = archive.getmembers()
        require(
            len(members) == 5 and {m.name for m in members} == {*FILES, "secrets"},
            "postgresql_backup_archive_invalid",
        )
        require(
            all(
                (m.name == "secrets" and m.isdir() and m.mode == 0o700)
                or (
                    m.name in FILES
                    and m.isfile()
                    and not m.issparse()
                    and m.mode == 0o600
                    and 0 <= m.size <= MAX_ARCHIVE_BYTES
                )
                for m in members
            )
            and sum(m.size for m in members) <= MAX_ARCHIVE_BYTES - 10240,
            "postgresql_backup_archive_invalid",
        )
        destination.mkdir(mode=0o700)
        (destination / "secrets").mkdir(mode=0o700)
        for member in members:
            if member.isdir():
                continue
            source = archive.extractfile(member)
            require(source is not None, "postgresql_backup_archive_invalid")
            assert source is not None
            descriptor = os.open(
                destination / member.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with source, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
    validate_bundle(destination)


class DockerTransport(legacy.DockerTransport):
    """Exact new PostgreSQL volumes; inherited local-daemon/image pinning only."""

    def __init__(self, project: str) -> None:
        super().__init__(project)
        self.server_name: str | None = None

    def command(
        self,
        arguments: list[str],
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> bytes:
        result = subprocess.run(
            ["docker", *arguments],
            cwd=ROOT,
            env=self.env,
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.subprocess_timeout(180),
            check=False,
        )
        if result.returncode:
            # Our guest emits only a bounded code, never raw SQL/server output.
            # Docker's own diagnostics are deliberately not forwarded.
            code = "postgresql_backup_docker_command_failed"
            try:
                diagnostic = json.loads(result.stderr[:4096])
                candidate = diagnostic.get("code", "")
                if re.fullmatch(r"postgresql_backup_[a-z_]{1,80}", candidate):
                    code = candidate
            except (ValueError, TypeError, AttributeError):
                pass
            raise LocalBackupError(code)
        return result.stdout or b""

    def subprocess_timeout(self, default: float) -> float:
        """Ordinary transport bounds; the isolated verifier can tighten them."""
        return default

    def owned_volume(self, name: str, *, created: bool = False) -> None:
        require(
            name in {f"{self.project}_{v}" for v in VOLUMES}, "postgresql_backup_volume_invalid"
        )
        inspected = json.loads(self.command(["volume", "inspect", name]))[0]
        labels = inspected.get("Labels") or {}
        require(
            inspected.get("Name") == name
            and labels.get("com.docker.compose.project") == self.project
            and labels.get("com.docker.compose.volume") == name.removeprefix(self.project + "_")
            and (not created or labels.get(RUN_LABEL) == self.token),
            "postgresql_backup_volume_ownership_invalid",
        )

    def require_absent_project(self) -> None:
        super().require_absent_project()
        for suffix in VOLUMES:
            result = self.command(
                [
                    "volume",
                    "ls",
                    "--filter",
                    f"name=^{self.project}_{suffix}$",
                    "--format",
                    "{{.Name}}",
                ]
            )
            require(not result.strip(), "postgresql_backup_restore_volume_exists")

    def create_volumes(self) -> None:
        self.require_absent_project()
        for suffix in VOLUMES:
            name = f"{self.project}_{suffix}"
            # Track intent before the mutating request: a timeout may happen
            # after Docker creates the volume but before it returns success.
            self.created_volumes.append(name)
            self.command(
                [
                    "volume",
                    "create",
                    "--label",
                    f"com.docker.compose.project={self.project}",
                    "--label",
                    f"com.docker.compose.volume={suffix}",
                    "--label",
                    f"{RUN_LABEL}={self.token}",
                    name,
                ]
            )
            self.owned_volume(name, created=True)

    def cleanup_created_volumes(self) -> None:
        failed = False
        for name in tuple(reversed(self.created_volumes)):
            try:
                exists = self.command(
                    ["volume", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"]
                )
                if exists.strip():
                    self.owned_volume(name, created=True)
                    require(
                        not self.command(["ps", "-aq", "--filter", f"volume={name}"]).strip(),
                        "postgresql_backup_restore_volume_in_use",
                    )
                    self.command(["volume", "rm", name])
                self.created_volumes.remove(name)
            except Exception:
                failed = True
        require(not failed, "postgresql_backup_owned_volume_cleanup_failed")

    def source_image(self) -> str:
        config = json.loads(
            self.command(
                [
                    "compose",
                    "--env-file",
                    "/dev/null",
                    "--project-directory",
                    str(ROOT),
                    "--project-name",
                    self.project,
                    "--file",
                    str(ROOT / "compose.yaml"),
                    "config",
                    "--format",
                    "json",
                ]
            )
        )
        require(
            set(config["volumes"]) == {*VOLUMES, "api-socket"}, "postgresql_backup_compose_invalid"
        )
        for suffix, value in config["volumes"].items():
            require(
                value.get("name") == f"{self.project}_{suffix}" and not value.get("external"),
                "postgresql_backup_compose_invalid",
            )
            if suffix in VOLUMES:
                self.owned_volume(f"{self.project}_{suffix}")
        api = config["services"]["api"]
        require(
            api.get("user") == "10001:10001"
            and api.get("network_mode") == "none"
            and len(api["volumes"]) == 3
            and api["environment"].get("DATABASE_URL") == DATABASE_URL
            and api["environment"].get("MARKETING_AGENTS_DIGEST_KEY_PATH") == str(KEY_PATH)
            and {
                m["target"]: (m["type"], m["source"], m.get("read_only", False))
                for m in api["volumes"]
            }
            == {
                str(SOCKET_DIRECTORY): ("volume", "postgres-socket", True),
                str(SECRET_DIRECTORY): ("volume", "postgres-secrets", True),
                "/var/run/marketing-agents": ("volume", "api-socket", False),
            },
            "postgresql_backup_compose_invalid",
        )
        return self.image_id(api["image"])

    def remove_container(self, name: str) -> None:
        found = self.command(["ps", "-aq", "--filter", f"name=^/{name}$"])
        if not found.strip():
            return
        value = json.loads(self.command(["inspect", name]))[0]
        require(
            value["Config"]["Labels"].get(RUN_LABEL) == self.token,
            "postgresql_backup_container_ownership_invalid",
        )
        self.command(["stop", "--time", "30", name])
        self.command(["rm", name])

    def start_server(self, image: str) -> None:
        require(self.server_name is None, "postgresql_backup_server_exists")
        for suffix in VOLUMES:
            self.owned_volume(f"{self.project}_{suffix}", created=True)
        self.server_name = "marketing-agents-pg-restore-" + uuid.uuid4().hex[:16]
        self.command(
            [
                "run",
                "--detach",
                "--pull",
                "never",
                "--name",
                self.server_name,
                "--label",
                f"{RUN_LABEL}={self.token}",
                "--label",
                f"com.docker.compose.project={self.project}",
                "--network",
                "none",
                "--user",
                "999:10001",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/tmp:size=64m,mode=1777",
                "--mount",
                f"type=volume,source={self.project}_postgres-data,target={DATA_DIRECTORY}",
                "--mount",
                f"type=volume,source={self.project}_postgres-socket,target={SOCKET_DIRECTORY}",
                image,
            ]
        )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    self.server_name,
                    "/bin/sh",
                    "-c",
                    'test "$(cat /proc/1/comm)" = postgres && exec psql -X -w '
                    "-h /var/run/marketing-agents-postgres -U postgres "
                    '-d marketing_agents -tAc "SELECT 1"',
                ],
                env=self.env,
                capture_output=True,
                timeout=self.subprocess_timeout(5),
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip() == b"1":
                return
            time.sleep(0.25)
        raise LocalBackupError("postgresql_backup_server_not_ready")

    def stop_server(self) -> None:
        with cleanup_signal_shield():
            if self.server_name is not None:
                self.remove_container(self.server_name)
                self.server_name = None

    def helper(self, image: str, operation: str, *, archive: BinaryIO | None = None) -> bytes:
        require(
            operation in {"_export", "_import", "_snapshot", "_seed"},
            "postgresql_backup_operation_invalid",
        )
        for suffix in ("postgres-socket", "postgres-secrets"):
            self.owned_volume(f"{self.project}_{suffix}")
        name = "marketing-agents-pg-transfer-" + uuid.uuid4().hex[:16]
        writing = operation in {"_import", "_seed"}
        arguments = [
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"{RUN_LABEL}={self.token}",
            "--network",
            "none",
            "--user",
            "10001:10001",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=1536m,mode=1777",
            "--mount",
            f"type=volume,source={self.project}_postgres-socket,target={SOCKET_DIRECTORY},readonly",
            "--mount",
            f"type=volume,source={self.project}_postgres-secrets,target={SECRET_DIRECTORY}"
            + ("" if writing else ",readonly"),
        ]
        if operation == "_import":
            arguments.append("--interactive")
        arguments.extend([image, "python", "/app/scripts/postgresql_backup.py", operation])
        try:
            return self.command(
                arguments,
                stdin=archive if operation == "_import" else None,
                stdout=archive if operation == "_export" else None,
            )
        finally:
            with cleanup_signal_shield():
                self.remove_container(name)


@contextmanager
def cleanup_signal_shield() -> Iterator[None]:
    """Cleanup is already stopping: keep SIGTERM from interrupting the stop.

    The verifier's separate SIGALRM deadline is deliberately not masked.
    """
    previous = {
        number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextmanager
def owned_signal_cleanup() -> Iterator[None]:
    """Turn termination into unwinding; a second signal cannot strand cleanup."""
    numbers = (signal.SIGINT, signal.SIGTERM)

    def interrupted(signum, frame) -> None:
        for number in numbers:
            signal.signal(number, signal.SIG_IGN)
        raise LocalBackupError("postgresql_backup_interrupted")

    previous = {number: signal.signal(number, interrupted) for number in numbers}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@owned_signal_cleanup()
def compose_backup(project: str, destination: Path) -> None:
    destination = _new_destination(destination)
    _private_directory(destination.parent)
    docker = DockerTransport(project)
    docker.local_daemon()
    image = docker.source_image()
    with tempfile.TemporaryDirectory(prefix="marketing-agents-pg-backup-") as directory:
        temporary = Path(directory)
        archive_path = temporary / "transfer.tar"
        with archive_path.open("xb") as archive:
            archive_path.chmod(0o600)
            docker.helper(image, "_export", archive=archive)
        unpack_bundle(archive_path, temporary / "incoming")
        copy_bundle(temporary / "incoming", destination)


@owned_signal_cleanup()
def compose_restore(project: str, image: str, postgres_image: str, backup: Path) -> None:
    docker = DockerTransport(project)
    docker.local_daemon()
    docker.require_absent_project()
    backend, server = docker.image_id(image), docker.image_id(postgres_image)
    with tempfile.TemporaryDirectory(prefix="marketing-agents-pg-restore-") as directory:
        temporary = Path(directory)
        copy_bundle(backup, temporary / "verified")
        archive_path = temporary / "transfer.tar"
        with archive_path.open("xb") as archive:
            archive_path.chmod(0o600)
            pack_bundle(temporary / "verified", archive)
        complete = False
        try:
            docker.create_volumes()
            docker.start_server(server)
            with archive_path.open("rb") as archive:
                docker.helper(backend, "_import", archive=archive)
            complete = True
        finally:
            failed_cleanup = False
            operations = [docker.stop_server]
            if not complete:
                operations.append(docker.cleanup_created_volumes)
            for operation in operations:
                try:
                    operation()
                except Exception:
                    failed_cleanup = True
            require(not failed_cleanup, "postgresql_backup_restore_cleanup_failed")


def native_backup(state: Path, destination: Path) -> None:
    try:
        from . import dev, native_postgres
    except ImportError:
        import dev
        import native_postgres

    destination = _new_destination(destination)
    state = state.expanduser().absolute()
    _private_directory(state)
    require(
        (state / native_postgres.MARKER).is_file()
        and (state / "postgres").is_dir()
        and (state / "secrets" / "digest.key").is_file(),
        "postgresql_backup_existing_native_state_required",
    )
    # The lock refuses an active app; never discover/adopt its temporary socket.
    with owned_signal_cleanup(), dev.installation_lock(state):
        pg = native_postgres.ManagedPostgres(state, dev._tool_environment())
        require(not pg.fresh, "postgresql_backup_existing_native_state_required")
        with tempfile.TemporaryDirectory(prefix="marketing-agents-pg-logs-") as directory:
            supervisor = dev.Supervisor(environment=dev._tool_environment(), logs=Path(directory))
            try:
                pg.start(supervisor)
                asyncio.run(
                    backup_postgresql(pg.url, state / "secrets" / "digest.key", destination)
                )
            finally:
                with cleanup_signal_shield():
                    supervisor.close()
                    pg.close()


def native_restore(backup: Path, destination: Path) -> None:
    try:
        from . import dev, native_postgres
    except ImportError:
        import dev
        import native_postgres

    destination = _new_destination(destination)
    validate_bundle(backup)
    destination.mkdir(mode=0o700)
    # A failed owned native target is retained for diagnosis; never erase a
    # cluster whose shutdown could not be confirmed or adopt existing storage.
    with owned_signal_cleanup(), dev.installation_lock(destination):
        pg = native_postgres.ManagedPostgres(destination, dev._tool_environment())
        logs = destination / "logs"
        logs.mkdir(mode=0o700)
        secrets = destination / "secrets"
        secrets.mkdir(mode=0o700)
        supervisor = dev.Supervisor(environment=dev._tool_environment(), logs=logs)
        try:
            pg.start(supervisor)
            asyncio.run(restore_into_empty_database(backup, pg.url, secrets / "digest.key"))
        finally:
            with cleanup_signal_shield():
                supervisor.close()
                pg.close()


def guest(operation: str) -> None:
    require(os.getuid() == 10001, "postgresql_backup_guest_uid_invalid")
    if operation == "_snapshot":
        try:
            from .del_05_runtime_smoke import database_snapshot
        except ImportError:
            from del_05_runtime_smoke import database_snapshot
        print(
            json.dumps(
                database_snapshot(DATABASE_URL, KEY_PATH, include_table_digests=True),
                sort_keys=True,
            )
        )
        return
    if operation == "_seed":
        result = subprocess.run(
            ["/bin/sh", "/app/scripts/migrate-and-seed.sh"],
            env={
                **os.environ,
                "DATABASE_URL": DATABASE_URL,
                "MARKETING_AGENTS_DIGEST_KEY_PATH": str(KEY_PATH),
            },
            capture_output=True,
            check=False,
            timeout=120,
        )
        require(result.returncode == 0, "postgresql_backup_seed_failed")
        return
    with tempfile.TemporaryDirectory(prefix="marketing-agents-pg-transfer-") as directory:
        temporary = Path(directory)
        if operation == "_export":
            asyncio.run(backup_postgresql(DATABASE_URL, KEY_PATH, temporary / "bundle"))
            pack_bundle(temporary / "bundle", sys.stdout.buffer)
        else:
            archive_path = temporary / "transfer.tar"
            with archive_path.open("xb") as archive:
                archive_path.chmod(0o600)
                total = 0
                while block := sys.stdin.buffer.read(1024 * 1024):
                    total += len(block)
                    require(total <= MAX_ARCHIVE_BYTES, "postgresql_backup_archive_too_large")
                    archive.write(block)
            unpack_bundle(archive_path, temporary / "incoming")
            asyncio.run(restore_into_empty_database(temporary / "incoming", DATABASE_URL, KEY_PATH))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    for operation in ("backup", "restore"):
        command = operations.add_parser(operation)
        command.add_argument("--mode", choices=("compose", "native"), default="compose")
        command.add_argument("--project")
        command.add_argument("--destination", type=Path)
        if operation == "backup":
            command.add_argument("--database-url")
            command.add_argument("--key-path", type=Path)
            command.add_argument("--state-dir", type=Path)
        else:
            command.add_argument("--backup", type=Path, required=True)
            command.add_argument("--image", default="marketing-agents-local-backend:local")
            command.add_argument(
                "--postgres-image", default="marketing-agents-local-postgres:local"
            )
    for operation in ("_export", "_import", "_snapshot", "_seed"):
        operations.add_parser(operation)
    args = parser.parse_args(argv)
    try:
        if args.operation.startswith("_"):
            guest(args.operation)
            return 0
        if args.operation == "backup":
            require(args.destination is not None, "postgresql_backup_destination_required")
            if args.mode == "native":
                if args.state_dir is not None:
                    require(
                        not args.database_url and not args.key_path,
                        "postgresql_backup_choose_state_or_explicit_paths",
                    )
                    native_backup(args.state_dir, args.destination)
                else:
                    require(
                        args.database_url and args.key_path,
                        "postgresql_backup_native_paths_required",
                    )
                    asyncio.run(
                        backup_postgresql(args.database_url, args.key_path, args.destination)
                    )
            else:
                require(
                    not args.database_url and not args.key_path and not args.state_dir,
                    "postgresql_backup_compose_paths_fixed",
                )
                compose_backup(args.project or "marketing-agents-local", args.destination)
        elif args.mode == "native":
            require(args.destination is not None, "postgresql_backup_destination_required")
            native_restore(args.backup, args.destination)
        else:
            require(args.destination is None, "postgresql_backup_compose_destination_is_project")
            compose_restore(
                legacy.validate_project(args.project), args.image, args.postgres_image, args.backup
            )
    except Exception as error:
        # Do not expose database names/URLs, paths, server output, or key bytes.
        code = error.code if isinstance(error, LocalBackupError) else "postgresql_backup_failed"
        if args.operation.startswith("_"):
            if code == "postgresql_backup_failed":
                code += "_" + type(error).__name__.lower()
            print(json.dumps({"ok": False, "code": code}), file=sys.stderr)
        else:
            print(json.dumps({"ok": False, "code": "postgresql_backup_failed"}))
        return 1
    print(json.dumps({"ok": True, "code": f"postgresql_{args.mode}_{args.operation}_complete"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
