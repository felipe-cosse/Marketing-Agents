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
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )
    return DatabaseRuntime(engine=engine, session_factory=sessions)
