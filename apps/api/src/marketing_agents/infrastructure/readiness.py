"""Read-only local readiness probes with no provider or connector invocation."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from stat import S_IWGRP, S_IWOTH, S_IWUSR
from typing import Any, Protocol

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from marketing_agents.adapters.registry import build_local_adapter_registry
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
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.db import Base
from marketing_agents.infrastructure.db.url import parse_database_url


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


def _normalized_sql(value: object | None) -> str | None:
    if value is None:
        return None
    rendered = re.sub(r"\s+", "", str(value).casefold())
    return rendered.translate(str.maketrans("", "", '"`[]'))


def _normalized_default(value: object | None) -> str | None:
    rendered = _normalized_sql(value)
    if rendered is None:
        return None
    while len(rendered) >= 2 and (
        (rendered.startswith("(") and rendered.endswith(")"))
        or (rendered.startswith("'") and rendered.endswith("'"))
    ):
        rendered = rendered[1:-1]
    return rendered


def _mapped_constraints_are_compatible(
    connection: Connection,
    table_name: str,
) -> bool:
    inspector = inspect(connection)
    table = Base.metadata.tables[table_name]

    expected_unique = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    actual_unique = {
        tuple(str(column) for column in constraint.get("column_names", ()))
        for constraint in inspector.get_unique_constraints(table_name)
    }
    if expected_unique != actual_unique:
        return False

    expected_foreign_keys = {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
            constraint.onupdate,
        )
        for constraint in table.foreign_key_constraints
    }
    actual_foreign_keys = {
        (
            tuple(str(column) for column in constraint.get("constrained_columns", ())),
            tuple(
                f"{constraint.get('referred_table')}.{column}"
                for column in constraint.get("referred_columns", ())
            ),
            constraint.get("options", {}).get("ondelete"),
            constraint.get("options", {}).get("onupdate"),
        )
        for constraint in inspector.get_foreign_keys(table_name)
    }
    if expected_foreign_keys != actual_foreign_keys:
        return False

    expected_checks = {
        str(constraint.name): (
            None
            if getattr(constraint, "_type_bound", False)
            else _normalized_sql(constraint.sqltext)
        )
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    actual_checks = {
        str(constraint.get("name")): _normalized_sql(constraint.get("sqltext"))
        for constraint in inspector.get_check_constraints(table_name)
    }
    if set(expected_checks) != set(actual_checks) or any(
        expected_sql is not None and actual_checks[name] != expected_sql
        for name, expected_sql in expected_checks.items()
    ):
        return False

    expected_indexes = {
        (tuple(index.columns.keys()), bool(index.unique)) for index in table.indexes
    }
    actual_indexes = {
        (
            tuple(str(column) for column in index.get("column_names", ())),
            bool(index.get("unique", False)),
        )
        for index in inspector.get_indexes(table_name)
        if not index.get("duplicates_constraint")
    }
    return expected_indexes == actual_indexes


def _mapped_worker_schema_is_compatible(connection: Connection) -> bool:
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)
    if not expected_tables or not expected_tables.issubset(actual_tables):
        return False
    for table_name, table in Base.metadata.tables.items():
        inspected_columns = inspector.get_columns(table_name)
        actual_columns: dict[str, Mapping[str, Any]] = {
            str(column["name"]): column for column in inspected_columns
        }
        if set(actual_columns) != set(table.columns.keys()):
            return False
        for expected in table.columns:
            actual = actual_columns[expected.name]
            actual_type = actual.get("type")
            expected_type = expected.type
            if getattr(actual_type, "_type_affinity", None) is not getattr(
                expected_type, "_type_affinity", None
            ):
                return False
            for attribute in ("length", "precision", "scale"):
                expected_value = getattr(expected_type, attribute, None)
                if (
                    expected_value is not None
                    and getattr(actual_type, attribute, None) != expected_value
                ):
                    return False
            if bool(actual.get("primary_key", False)) != bool(expected.primary_key):
                return False
            if bool(actual.get("nullable", True)) != bool(expected.nullable):
                return False
            expected_default = (
                None
                if expected.server_default is None
                else _normalized_default(
                    getattr(expected.server_default, "arg", expected.server_default)
                )
            )
            if _normalized_default(actual.get("default")) != expected_default:
                return False
        if not _mapped_constraints_are_compatible(connection, table_name):
            return False
    return True


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


class LocalReadinessProbe:
    """Recompute local facts without migrating, seeding, repairing, or calling adapters."""

    def __init__(self, settings: ReadinessSettings) -> None:
        self._settings = settings

    async def _database_checks(self) -> tuple[ReadinessCheck, ReadinessCheck]:
        url = parse_database_url(self._settings.database_url)
        preflight = _sqlite_preflight(url)
        if preflight is not None:
            return (
                _not_ready(ReadinessCheckName.DATABASE, preflight),
                _not_ready(
                    ReadinessCheckName.WORKER_SCHEMA,
                    ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE,
                ),
            )

        engine: AsyncEngine | None = None
        try:
            engine = create_async_engine(url, pool_pre_ping=True)
            async with engine.connect() as connection:
                if (await connection.execute(text("SELECT 1"))).scalar_one() != 1:
                    raise RuntimeError("database probe returned an invalid sentinel")
                compatible = await connection.run_sync(_mapped_worker_schema_is_compatible)
        except Exception:
            return (
                _not_ready(
                    ReadinessCheckName.DATABASE,
                    ReadinessCode.DATABASE_UNAVAILABLE,
                ),
                _not_ready(
                    ReadinessCheckName.WORKER_SCHEMA,
                    ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE,
                ),
            )
        finally:
            if engine is not None:
                await engine.dispose()
        return (
            _ready(ReadinessCheckName.DATABASE),
            _ready(ReadinessCheckName.WORKER_SCHEMA)
            if compatible
            else _not_ready(
                ReadinessCheckName.WORKER_SCHEMA,
                ReadinessCode.WORKER_SCHEMA_INCOMPATIBLE,
            ),
        )

    async def check(self) -> ReadinessReport:
        database, worker_schema = await self._database_checks()
        catalog, catalog_metadata, catalog_check = await asyncio.to_thread(
            _compile_catalog,
            self._settings.catalog_root,
        )
        provider_registry, connector_registry = await asyncio.to_thread(
            _adapter_checks,
            self._settings,
            catalog,
        )
        return ReadinessReport(
            checks=(
                database,
                _not_ready(
                    ReadinessCheckName.MIGRATION,
                    ReadinessCode.MIGRATION_VERIFICATION_UNAVAILABLE,
                ),
                catalog_check,
                provider_registry,
                connector_registry,
                worker_schema,
            ),
            catalog=catalog_metadata,
        )
