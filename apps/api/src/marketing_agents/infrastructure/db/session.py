"""Async engine and session composition for SQLite and PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from marketing_agents.domain.retention import RetentionPolicy
from marketing_agents.security.digest_key import DigestKey

from .url import parse_database_url

DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5_000
MAX_SQLITE_BUSY_TIMEOUT_MS = 60_000


def _is_file_backed_sqlite(url: URL) -> bool:
    database = url.database
    return bool(
        url.drivername == "sqlite+aiosqlite"
        and database
        and database != ":memory:"
        and not database.startswith("file::memory:")
    )


def _install_sqlite_pragmas(
    engine: AsyncEngine,
    *,
    file_backed: bool,
    busy_timeout_ms: int,
) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def configure_connection(dbapi_connection: Any, connection_record: Any) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms:d}")
            if file_backed:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.fetchone()
        finally:
            cursor.close()


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    """Owned async engine and session factory; construction never opens a connection."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        await self.engine.dispose()


def create_database_runtime(
    database_url: str,
    *,
    echo: bool = False,
    sqlite_busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    retention_policy: RetentionPolicy | None = None,
    retention_key: DigestKey | None = None,
) -> DatabaseRuntime:
    """Build a lazy async runtime with portable sessions and SQLite connection policy."""

    if (
        not isinstance(sqlite_busy_timeout_ms, int)
        or isinstance(sqlite_busy_timeout_ms, bool)
        or not 1 <= sqlite_busy_timeout_ms <= MAX_SQLITE_BUSY_TIMEOUT_MS
    ):
        raise ValueError("SQLite busy timeout must be an integer from 1 through 60000 ms")
    url = parse_database_url(database_url)
    engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
    if url.drivername == "sqlite+aiosqlite":
        _install_sqlite_pragmas(
            engine,
            file_backed=_is_file_backed_sqlite(url),
            busy_timeout_ms=sqlite_busy_timeout_ms,
        )
    if retention_policy is not None:
        # Loading this module installs only an opt-in, per-session transaction hook.
        from . import retention_tracking  # noqa: F401
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
        info={
            "retention_enabled": retention_policy is not None,
            "retention_policy": retention_policy,
            "retention_key": retention_key,
        },
    )
    return DatabaseRuntime(engine=engine, session_factory=sessions)
