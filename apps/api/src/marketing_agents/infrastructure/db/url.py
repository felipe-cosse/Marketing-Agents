"""Strict asynchronous database URL parsing and credential-safe diagnostics."""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

SUPPORTED_DATABASE_DRIVERS = frozenset({"sqlite+aiosqlite", "postgresql+asyncpg"})
MAX_DATABASE_URL_LENGTH = 2_048


class DatabaseURLError(ValueError):
    """Raised when a configured database URL is not a supported async URL."""


def parse_database_url(value: str) -> URL:
    """Parse one bounded URL and reject sync or unsupported database drivers."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_DATABASE_URL_LENGTH
    ):
        raise DatabaseURLError("database URL must be a bounded, trimmed string")
    try:
        url = make_url(value)
    except ArgumentError as exc:
        raise DatabaseURLError("database URL is malformed") from exc
    if url.drivername not in SUPPORTED_DATABASE_DRIVERS:
        raise DatabaseURLError("database URL must use sqlite+aiosqlite or postgresql+asyncpg")
    if url.drivername == "sqlite+aiosqlite":
        if any(item is not None for item in (url.username, url.password, url.host, url.port)):
            raise DatabaseURLError("SQLite database URL cannot contain network authority")
        if not url.database:
            raise DatabaseURLError("SQLite database URL must identify a file or :memory:")
    elif not url.host or not url.database:
        raise DatabaseURLError("PostgreSQL database URL must identify a host and database")
    return url


def safe_database_url(value: str) -> str:
    """Render a useful URL snapshot without credentials or query parameters."""

    url = parse_database_url(value)
    safe_url = url.set(
        username="[REDACTED]" if url.username is not None else None,
        password="[REDACTED]" if url.password is not None else None,
        query={},
    )
    return safe_url.render_as_string(hide_password=True)
