"""Owned, persistent native PostgreSQL with private Unix transport and no TCP listener.

Only the application supervisor starts/stops the server. Existing installations
are never adopted, upgraded, migrated from SQLite, or stopped implicitly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

if TYPE_CHECKING:
    from scripts.dev import Child, Supervisor

ADMIN = "marketing_agents_admin"
APPLICATION = "marketing_agents"
MARKER = "native-postgresql.json"


class NativePostgresError(RuntimeError):
    """Content-free native PostgreSQL prerequisite or ownership failure."""


@dataclass(frozen=True)
class PostgresTools:
    initdb: Path
    postgres: Path
    pg_ctl: Path
    psql: Path
    major: int


def installed_tools(environment: dict[str, str]) -> PostgresTools:
    """Discover installed executables only; never install, fetch, or start a service."""
    paths = {}
    majors = set()
    for name in ("initdb", "postgres", "pg_ctl", "psql"):
        selected = shutil.which(name, path=environment.get("PATH", os.defpath))
        if selected is None:
            raise NativePostgresError(
                "native_postgres_tools_missing: install PostgreSQL 14+ locally, "
                "or explicitly select --database sqlite"
            )
        try:
            result = subprocess.run(
                [selected, "--version"],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise NativePostgresError("native_postgres_tool_unavailable") from None
        match = re.fullmatch(r"\S+ \(PostgreSQL\) (\d+)\.\d+[^\n]*\n?", result.stdout)
        if match is None or int(match[1]) < 14:
            raise NativePostgresError("native_postgres_version: PostgreSQL 14+ is required")
        paths[name] = Path(selected)
        majors.add(int(match[1]))
    if len(majors) != 1:
        raise NativePostgresError("native_postgres_versions_mismatch")
    return PostgresTools(**paths, major=majors.pop())


def _private(path: Path, *, directory: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise NativePostgresError("native_postgres_state_invalid") from None
    if (
        (not stat.S_ISDIR(metadata.st_mode) if directory else not stat.S_ISREG(metadata.st_mode))
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != (0o700 if directory else 0o600)
    ):
        raise NativePostgresError("native_postgres_state_permissions")


def _read_private(path: Path, limit: int) -> bytes:
    """Bound startup reads and reject links, devices, and permissive metadata."""
    _private(path, directory=False)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > limit
        ):
            raise NativePostgresError("native_postgres_state_invalid")
        return stream.read(limit + 1)


class ManagedPostgres:
    def __init__(self, state: Path, environment: dict[str, str]) -> None:
        self.state = state
        self.environment = environment
        # A digest key is paired with its database; a backend switch never
        # repurposes an existing SQLite installation or its key silently.
        if any(
            os.path.lexists(state / name)
            for name in (
                "marketing_agents.db",
                "marketing_agents.db-wal",
                "marketing_agents.db-shm",
            )
        ):
            raise NativePostgresError(
                "native_existing_sqlite: use --database sqlite for that installation, "
                "or choose a fresh --state-dir for PostgreSQL"
            )
        self.tools = installed_tools(environment)
        self.data = state / "postgres"
        self.marker = state / MARKER
        self.identity = {
            "version": 1,
            "major": self.tools.major,
            "database": APPLICATION,
            "role": APPLICATION,
        }
        self.fresh = not os.path.lexists(self.data)
        if self.fresh:
            if os.path.lexists(self.marker) or os.path.lexists(state / "secrets" / "digest.key"):
                raise NativePostgresError(
                    "native_postgres_pair_incomplete: choose a fresh state directory"
                )
        else:
            _private(self.data, directory=True)
            try:
                marker = _read_private(self.marker, 1024)
                version = _read_private(self.data / "PG_VERSION", 16).decode().strip()
                valid = len(marker) <= 1024 and json.loads(marker) == self.identity
            except (OSError, ValueError):
                valid = False
                version = ""
            if not valid or version != str(self.tools.major):
                raise NativePostgresError(
                    "native_postgres_identity_mismatch: no automatic adoption or upgrade"
                )
            if os.path.lexists(self.data / "postmaster.pid"):
                raise NativePostgresError(
                    "native_postgres_already_running: inspect the owned installation"
                )
        # Short, private socket paths also work on macOS's 104-byte sockaddr_un.
        # TemporaryDirectory removes only this freshly created directory.
        self._socket = tempfile.TemporaryDirectory(prefix="ma-pg-", dir="/tmp")
        self.socket = Path(self._socket.name)
        self.url = f"postgresql+asyncpg://{APPLICATION}@localhost:5432/{APPLICATION}?" + urlencode(
            {"host": str(self.socket)}
        )

    def command(self, sql: str, *, application: bool = False) -> list[str]:
        return [
            str(self.tools.psql),
            "-X",
            "--no-password",
            "--host",
            str(self.socket),
            "--port",
            "5432",
            "--username",
            APPLICATION if application else ADMIN,
            "--dbname",
            APPLICATION if application else "postgres",
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--command",
            sql,
        ]

    def query(self, sql: str, *, application: bool = False) -> str | None:
        try:
            result = subprocess.run(
                self.command(sql, application=application),
                env={**self.environment, "PGCONNECT_TIMEOUT": "3"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def start(self, supervisor: Supervisor) -> Child:
        if self.fresh:
            supervisor.initialize(
                "postgres-init",
                [
                    str(self.tools.initdb),
                    "--pgdata",
                    str(self.data),
                    "--username",
                    ADMIN,
                    "--auth-local=trust",
                    "--auth-host=reject",
                    "--encoding=UTF8",
                    "--no-locale",
                ],
                cwd=self.state,
            )
        _private(self.data, directory=True)
        child = supervisor.start(
            "postgres",
            [
                str(self.tools.postgres),
                "-D",
                str(self.data),
                "-c",
                "listen_addresses=",
                "-c",
                f"unix_socket_directories={self.socket}",
                "-c",
                "unix_socket_permissions=0700",
                "-c",
                "port=5432",
            ],
            cwd=self.state,
        )
        deadline = time.monotonic() + 30
        while True:
            supervisor.check_children([child])
            if supervisor.stopping or time.monotonic() >= deadline:
                raise NativePostgresError("native_postgres_readiness_timeout")
            if self.query("SELECT 1") == "1":
                break
            time.sleep(0.1)
        if self.fresh:
            for name, sql in (
                (
                    "role",
                    f"CREATE ROLE {APPLICATION} LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS",
                ),
                ("database", f"CREATE DATABASE {APPLICATION} OWNER {APPLICATION}"),
            ):
                supervisor.initialize(f"postgres-{name}", self.command(sql), cwd=self.state)
        expected = f"{APPLICATION}|f|f|f|f|f|t|{APPLICATION}"
        actual = self.query(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls, rolcanlogin, pg_get_userbyid(datdba) "
            "FROM pg_roles CROSS JOIN pg_database "
            f"WHERE rolname = '{APPLICATION}' AND datname = '{APPLICATION}'"
        )
        if actual != expected or self.query("SELECT current_user", application=True) != APPLICATION:
            raise NativePostgresError("native_postgres_application_role_invalid")
        if self.fresh:
            descriptor = os.open(
                self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self.identity, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        return child

    def close(self) -> None:
        """Call only after the supervisor has stopped its owned server group."""
        self._socket.cleanup()
