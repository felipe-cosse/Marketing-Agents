"""Read-only SQLite/PostgreSQL readiness with no provider or connector invocation."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from stat import S_IWGRP, S_IWOTH, S_IWUSR
from typing import Any, Protocol
from urllib.parse import quote

from sqlalchemy import event, select, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from marketing_agents.application.ports.readiness import (
    CatalogReadinessMetadata,
    ReadinessCheck,
    ReadinessCheckName,
    ReadinessCheckStatus,
    ReadinessCode,
    ReadinessReport,
)
from marketing_agents.infrastructure.adapters.connectors.registry import (
    build_connector_registry,
)
from marketing_agents.infrastructure.adapters.safe_profile import (
    build_local_adapter_registry,
)
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db.local_installation import _local_paths
from marketing_agents.infrastructure.db.migrations import inspect_migration
from marketing_agents.infrastructure.db.models.deployment import LocalRuntimeIdentityRecord
from marketing_agents.infrastructure.db.schema import schema_matches_metadata
from marketing_agents.infrastructure.db.session import DatabaseRuntime
from marketing_agents.infrastructure.db.url import parse_database_url
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.digest_key import load_or_create_digest_key


class ReadinessSettings(Protocol):
    @property
    def database_url(self) -> str: ...

    @property
    def catalog_root(self) -> Path: ...

    @property
    def llm_provider(self) -> str: ...

    @property
    def connector_mode(self) -> str: ...

    @property
    def allow_external_network(self) -> bool: ...


_WRITABLE_MODE_BITS = S_IWUSR | S_IWGRP | S_IWOTH


@dataclass(slots=True)
class _AdapterModes:
    llm_provider: str
    connector_mode: str
    allow_external_network: bool


def _ready(name: ReadinessCheckName) -> ReadinessCheck:
    return ReadinessCheck(name, ReadinessCheckStatus.READY, ReadinessCode.READY)


def _not_ready(name: ReadinessCheckName, code: ReadinessCode) -> ReadinessCheck:
    return ReadinessCheck(name, ReadinessCheckStatus.NOT_READY, code)


def _sqlite_preflight(url: URL) -> ReadinessCode | None:
    if url.drivername != "sqlite+aiosqlite":
        return None
    database = url.database
    if database in {":memory:", "file::memory:"}:
        return None
    if database is None or database.startswith("file:"):
        return ReadinessCode.DATABASE_UNAVAILABLE
    path = Path(database).resolve()
    try:
        if not path.exists():
            return ReadinessCode.DATABASE_MISSING
        if not path.is_file():
            return ReadinessCode.DATABASE_UNAVAILABLE
        parent_mode = path.parent.stat().st_mode
        file_mode = path.stat().st_mode
    except OSError:
        return ReadinessCode.DATABASE_UNAVAILABLE
    if not parent_mode & _WRITABLE_MODE_BITS or not os.access(path.parent, os.W_OK):
        return ReadinessCode.DATABASE_DIRECTORY_UNAVAILABLE
    if not file_mode & _WRITABLE_MODE_BITS or not os.access(path, os.W_OK):
        return ReadinessCode.DATABASE_UNAVAILABLE
    return None


def _mapped_worker_schema_is_compatible(connection: Connection) -> bool:
    """Compatibility name for the original worker-schema probe."""

    return schema_matches_metadata(connection)


def _compile_catalog(
    root: Path,
) -> tuple[CompiledCatalog | None, CatalogReadinessMetadata | None, ReadinessCheck]:
    try:
        catalog = compile_catalog(root)
        metadata = CatalogReadinessMetadata(
            content_version=catalog.manifest.content_version,
            content_hash=catalog.content_hash,
            departments=len(catalog.departments),
            functions=len(catalog.functions),
            templates=len(catalog.templates),
            instances=len(catalog.instances),
        )
    except (OSError, TypeError, ValueError):
        return (
            None,
            None,
            _not_ready(ReadinessCheckName.CATALOG, ReadinessCode.CATALOG_INVALID),
        )
    return (
        catalog,
        metadata,
        _not_ready(
            ReadinessCheckName.CATALOG,
            ReadinessCode.CATALOG_SEED_VERIFICATION_UNAVAILABLE,
        ),
    )


def _adapter_checks(
    settings: ReadinessSettings,
    catalog: CompiledCatalog | None,
) -> tuple[ReadinessCheck, ReadinessCheck]:
    try:
        registry = build_local_adapter_registry(
            _AdapterModes(
                llm_provider=settings.llm_provider,
                connector_mode=settings.connector_mode,
                allow_external_network=settings.allow_external_network,
            )
        )
    except (TypeError, ValueError):
        return (
            _not_ready(
                ReadinessCheckName.PROVIDER_REGISTRY,
                ReadinessCode.PROVIDER_REGISTRY_UNAVAILABLE,
            ),
            _not_ready(
                ReadinessCheckName.CONNECTOR_REGISTRY,
                ReadinessCode.CONNECTOR_REGISTRY_UNAVAILABLE,
            ),
        )

    provider = (
        _ready(ReadinessCheckName.PROVIDER_REGISTRY)
        if registry.llm_provider_id == "mock.deterministic.v1"
        else _not_ready(
            ReadinessCheckName.PROVIDER_REGISTRY,
            ReadinessCode.PROVIDER_REGISTRY_UNAVAILABLE,
        )
    )
    if catalog is None or registry.connector_bundle_id != "mock.connectors.v1":
        connector = _not_ready(
            ReadinessCheckName.CONNECTOR_REGISTRY,
            ReadinessCode.CONNECTOR_REGISTRY_UNAVAILABLE,
        )
    else:
        try:
            build_connector_registry(catalog)
        except (TypeError, ValueError):
            connector = _not_ready(
                ReadinessCheckName.CONNECTOR_REGISTRY,
                ReadinessCode.CONNECTOR_REGISTRY_UNAVAILABLE,
            )
        else:
            connector = _ready(ReadinessCheckName.CONNECTOR_REGISTRY)
    return provider, connector


def _read_only_engine(url: URL) -> AsyncEngine:
    if url.drivername == "sqlite+aiosqlite" and url.database not in {
        ":memory:",
        "file::memory:",
    }:
        # mode=rw never creates an absent database. query_only below prevents
        # data/schema writes but permits normal WAL housekeeping on close;
        # mode=ro can leave newly created WAL/SHM sidecars behind instead.
        database = quote(str(Path(str(url.database)).resolve()), safe="/")
        url = url.set(database=f"file:{database}", query={"mode": "rw", "uri": "true"})
    if url.drivername == "postgresql+asyncpg":
        return create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args={"server_settings": {"default_transaction_read_only": "on"}},
        )
    engine = create_async_engine(url, pool_pre_ping=True)

    @event.listens_for(engine.sync_engine, "connect")
    def query_only(dbapi_connection: Any, connection_record: Any) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA query_only=ON")
        finally:
            cursor.close()

    return engine


def _verify_present_key(database_url: str, key_path: Path, fingerprint: str) -> None:
    # The key remains private and local even when the paired database is PostgreSQL.
    _, normalized_key = _local_paths(database_url, key_path)
    # Existing-state mode is intentionally non-creating even when the key is absent.
    load_or_create_digest_key(
        normalized_key, persistent_state_exists=True, expected_fingerprint=fingerprint
    )


class LocalReadinessProbe:
    """Recompute local facts without migrating, seeding, repairing, or calling adapters."""

    def __init__(self, settings: ReadinessSettings) -> None:
        self._settings = settings

    async def _database_checks(
        self, catalog: CompiledCatalog | None
    ) -> tuple[ReadinessCheck, ReadinessCheck, ReadinessCheck, ReadinessCheck]:
        database = _not_ready(ReadinessCheckName.DATABASE, ReadinessCode.DATABASE_UNAVAILABLE)
        migration = _not_ready(
            ReadinessCheckName.MIGRATION, ReadinessCode.MIGRATION_VERIFICATION_UNAVAILABLE
        )
        worker_schema = _not_ready(
            ReadinessCheckName.WORKER_SCHEMA, ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE
        )
        seeded_catalog = _not_ready(
            ReadinessCheckName.CATALOG, ReadinessCode.CATALOG_SEED_VERIFICATION_UNAVAILABLE
        )
        url = parse_database_url(self._settings.database_url)
        preflight = _sqlite_preflight(url)
        if preflight is not None:
            return (
                _not_ready(ReadinessCheckName.DATABASE, preflight),
                migration,
                worker_schema,
                seeded_catalog,
            )

        engine: AsyncEngine | None = None
        try:
            engine = _read_only_engine(url)
            async with engine.connect() as connection:
                if (await connection.execute(text("SELECT 1"))).scalar_one() != 1:
                    raise RuntimeError("database probe returned an invalid sentinel")
                database = _ready(ReadinessCheckName.DATABASE)
                if await connection.run_sync(_mapped_worker_schema_is_compatible):
                    worker_schema = _ready(ReadinessCheckName.WORKER_SCHEMA)
                deployed = await connection.run_sync(inspect_migration)
                if deployed.current and deployed.schema_matches:
                    migration = _ready(ReadinessCheckName.MIGRATION)
                    rows = (
                        (await connection.execute(select(LocalRuntimeIdentityRecord).limit(2)))
                        .mappings()
                        .all()
                    )
                    key_path = getattr(self._settings, "marketing_agents_digest_key_path", None)
                    try:
                        if (
                            len(rows) != 1
                            or rows[0]["singleton_id"] != 1
                            or rows[0]["format_version"] != 1
                            or not isinstance(key_path, Path)
                        ):
                            raise ValueError("native installation identity is invalid")
                        await asyncio.to_thread(
                            _verify_present_key,
                            self._settings.database_url,
                            key_path,
                            str(rows[0]["key_fingerprint"]),
                        )
                    except Exception:
                        database = _not_ready(
                            ReadinessCheckName.DATABASE, ReadinessCode.DATABASE_UNAVAILABLE
                        )
                        return database, migration, worker_schema, seeded_catalog
            if deployed.current and deployed.schema_matches and catalog is not None:
                runtime = DatabaseRuntime(
                    engine=engine,
                    session_factory=async_sessionmaker(
                        engine, autoflush=False, expire_on_commit=False
                    ),
                )
                await seed_catalog(catalog, runtime, CroniterRecurrenceCalculator(), check=True)
                seeded_catalog = _ready(ReadinessCheckName.CATALOG)
        except Exception:
            # Preserve proven connectivity/schema checks; never expose SQL, paths,
            # configured credentials, catalog payloads, or seed exception details.
            pass
        finally:
            if engine is not None:
                await engine.dispose()
        return database, migration, worker_schema, seeded_catalog

    async def check(self) -> ReadinessReport:
        catalog, catalog_metadata, catalog_check = await asyncio.to_thread(
            _compile_catalog,
            self._settings.catalog_root,
        )
        database, migration, worker_schema, seeded_catalog = await self._database_checks(catalog)
        provider_registry, connector_registry = await asyncio.to_thread(
            _adapter_checks,
            self._settings,
            catalog,
        )
        return ReadinessReport(
            checks=(
                database,
                migration,
                seeded_catalog if catalog is not None else catalog_check,
                provider_registry,
                connector_registry,
                worker_schema,
            ),
            catalog=catalog_metadata,
        )
