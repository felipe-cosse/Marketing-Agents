"""Shared native installation/key pairing, separate from schema-only revisions."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from stat import S_IMODE
from urllib.parse import quote

from sqlalchemy import insert, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from marketing_agents.infrastructure.db.migrations import (
    DatabaseMigrationError,
    _upgrade,
    expected_tables,
    inspect_migration,
    migration_transaction,
)
from marketing_agents.infrastructure.db.models.deployment import LocalRuntimeIdentityRecord
from marketing_agents.infrastructure.db.session import create_database_runtime
from marketing_agents.infrastructure.db.url import parse_database_url
from marketing_agents.security.digest_key import (
    DigestKey,
    DigestKeyError,
    digest_key_fingerprint,
    load_or_create_digest_key,
)


def _local_paths(database_url: str, key_path: Path) -> tuple[Path | None, Path]:
    """Validate private local key storage and, for SQLite, a distinct database file."""
    url = parse_database_url(database_url)
    database = None
    if url.drivername == "sqlite+aiosqlite":
        if (
            not url.database
            or url.database == ":memory:"
            or url.database.startswith("file:")
            or url.query
        ):
            raise DatabaseMigrationError("local_installation_requires_sqlite_file")
        database = Path(url.database).expanduser().absolute()
    key = key_path.expanduser().absolute()
    if database == key or (database is not None and database.is_symlink()) or key.is_symlink():
        raise DigestKeyError("local installation paths must be distinct regular files")
    if key.parent in {Path("/"), Path.home(), Path.cwd()} or key.parent.is_symlink():
        raise DigestKeyError("digest key requires a dedicated local directory")
    if key.parent.exists():
        metadata = key.parent.stat()
        if metadata.st_uid != os.getuid() or S_IMODE(metadata.st_mode) & 0o077:
            raise DigestKeyError("digest key directory requires private owner-only permissions")
    if key.exists() and key.stat().st_uid != os.getuid():
        raise DigestKeyError("digest key must belong to the current service user")
    return database, key


def _installation_read_only_engine(database_url: str, database: Path | None) -> AsyncEngine:
    url = parse_database_url(database_url)
    if database is None:
        return create_async_engine(
            url,
            connect_args={"server_settings": {"default_transaction_read_only": "on"}},
        )
    # mode=ro is deliberate here: this preflight cannot modify unknown data.
    return create_async_engine(
        url.set(
            database="file:" + quote(str(database), safe="/"),
            query={"mode": "ro", "uri": "true"},
        )
    )


def _stored_identity(connection: Connection) -> str | None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    status = inspect_migration(connection)
    if status.revision is None:
        objects = tables - {"alembic_version"}
        if connection.dialect.name == "postgresql":
            objects.update(inspector.get_view_names())
            objects.update(inspector.get_materialized_view_names())
            objects.update(inspector.get_sequence_names())
        if objects:
            raise DatabaseMigrationError("migration_unversioned_schema")
        return None
    if connection.dialect.name == "postgresql" and tables - {"alembic_version"} != expected_tables(
        status.revision
    ):
        raise DatabaseMigrationError("migration_schema_drift")
    if "local_runtime_identity" not in tables:
        raise DatabaseMigrationError("migration_schema_drift")
    rows = connection.execute(select(LocalRuntimeIdentityRecord.__table__)).mappings().all()
    if len(rows) == 1 and rows[0]["singleton_id"] == 1 and rows[0]["format_version"] == 1:
        return str(rows[0]["key_fingerprint"])
    # An explicitly versioned but empty schema can be initialized. Existing
    # application data must never be silently rebound to a replacement key.
    if rows or any(
        connection.exec_driver_sql(
            f"SELECT 1 FROM {connection.dialect.identifier_preparer.quote(table)} LIMIT 1"
        ).first()
        is not None
        for table in sorted(tables - {"alembic_version", "local_runtime_identity"})
    ):
        raise DigestKeyError("persistent state has no valid local key identity")
    return None


async def initialize_local_secret(
    database_url: str, key_path: Path, *, defer_database_check: bool = False
) -> DigestKey:
    """Verify existing DB/key pairing before creating anything; never replace a key."""
    database, key_path = _local_paths(database_url, key_path)
    if type(defer_database_check) is not bool:
        raise ValueError("deferred database check flag must be an exact boolean")
    expected = None
    exists = database is not None and database.exists()
    if defer_database_check:
        # The read-only Compose initializer owns only key creation/presence.
        # WAL databases may require sidecars to inspect, which cannot be created
        # on that mount. The following migration owner MUST verify the complete
        # pair on its writable mount before any schema/data mutation.
        if database is None:
            raise DatabaseMigrationError("local_secret_defer_requires_sqlite")
        if exists and (not database.is_file() or database.stat().st_uid != os.getuid()):
            raise DigestKeyError("local database must be a regular file owned by the service user")
        return load_or_create_digest_key(key_path, persistent_state_exists=exists)
    if database is None or exists:
        if database is not None and not database.is_file():
            raise DigestKeyError("local database must be a regular file")
        engine = _installation_read_only_engine(database_url, database)
        try:
            async with engine.connect() as connection:
                expected = await connection.run_sync(_stored_identity)
                if database is None:
                    # A provisioned PostgreSQL database may start empty. Any
                    # existing schema requires its original, already-present key.
                    exists = await connection.run_sync(
                        lambda synchronous: bool(inspect(synchronous).get_table_names())
                    )
        finally:
            await engine.dispose()
    return load_or_create_digest_key(
        key_path, persistent_state_exists=exists, expected_fingerprint=expected
    )


async def migrate_local_database(database_url: str, key_path: Path) -> str:
    """Use the shared initializer, then atomically upgrade and bind schema/key identity."""
    key = await initialize_local_secret(database_url, key_path)
    database, _ = _local_paths(database_url, key_path)
    if database is not None:
        database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime = create_database_runtime(database_url)
    try:
        async with migration_transaction(runtime) as connection:
            expected = await connection.run_sync(_stored_identity)
            fingerprint = digest_key_fingerprint(key)
            if expected is not None and expected != fingerprint:
                raise DigestKeyError("digest key does not match the persistent-state fingerprint")
            revision = await connection.run_sync(_upgrade, "head")
            if expected is None:
                await connection.execute(
                    insert(LocalRuntimeIdentityRecord).values(
                        singleton_id=1,
                        format_version=1,
                        key_fingerprint=fingerprint,
                        created_at=datetime.now(UTC),
                    )
                )
            return revision
    finally:
        await runtime.dispose()


async def verify_local_installation(database_url: str, key_path: Path) -> None:
    """Read-only command precondition: an existing current database and paired key."""
    database, key_path = _local_paths(database_url, key_path)
    if (database is not None and not database.is_file()) or not key_path.is_file():
        raise DigestKeyError("local installation is missing its database or digest key")
    await initialize_local_secret(database_url, key_path)
    engine = _installation_read_only_engine(database_url, database)
    try:
        async with engine.connect() as connection:
            status = await connection.run_sync(inspect_migration)
            fingerprint = await connection.run_sync(_stored_identity)
            if not status.current or not status.schema_matches or fingerprint is None:
                raise DatabaseMigrationError("local_installation_not_migrated")
    finally:
        await engine.dispose()
