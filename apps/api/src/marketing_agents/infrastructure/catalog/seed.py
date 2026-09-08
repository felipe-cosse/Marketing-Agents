"""Atomic catalog import, preserved deployment defaults, and exact no-write parity checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from sqlalchemy import String, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from marketing_agents.application.ports.recurrence import RecurrenceCalculator
from marketing_agents.infrastructure.catalog.instance_configuration_seed import (
    InstanceConfigurationSeedError,
    catalog_instance_configuration_defaults,
    seed_instance_configurations_in_repository,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.db.base import Base
from marketing_agents.infrastructure.db.models.catalog import (
    AgentInstanceRecord,
    AgentTemplateCapabilityRecord,
    AgentTemplateRecord,
    AgentTemplateTriggerKindRecord,
    ApprovalPolicyRecord,
    CatalogCurrentReleaseRecord,
    CatalogReleaseRecord,
    DepartmentRecord,
    FunctionTeamRecord,
    ToolCapabilityRecord,
)
from marketing_agents.infrastructure.db.models.instance_configuration import (
    AgentInstanceConfigurationRecord,
)
from marketing_agents.infrastructure.db.repositories.instance_configuration import (
    SQLAlchemyInstanceConfigurationRepository,
)
from marketing_agents.infrastructure.db.session import DatabaseRuntime

_CATALOG_SEED_LOCK = 7_243_815_041
_MODELS: tuple[type[Base], ...] = (
    DepartmentRecord,
    FunctionTeamRecord,
    ToolCapabilityRecord,
    ApprovalPolicyRecord,
    AgentTemplateRecord,
    AgentTemplateCapabilityRecord,
    AgentTemplateTriggerKindRecord,
    AgentInstanceRecord,
)
_EDGES = frozenset({AgentTemplateCapabilityRecord, AgentTemplateTriggerKindRecord})
_IDENTITY_FIELDS: dict[type[Base], tuple[str, ...]] = {
    DepartmentRecord: (),
    FunctionTeamRecord: ("department_id",),
    ToolCapabilityRecord: ("connector_family", "effect"),
    ApprovalPolicyRecord: ("kind",),
    AgentTemplateRecord: ("department_id", "function_id"),
    AgentInstanceRecord: ("template_id", "source_ordinal"),
}


class CatalogSeedError(RuntimeError):
    """Safe, stable catalog import or comparison failure without database/source payloads."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CatalogSeedResult:
    """Catalog-row mutation totals and exact catalog/config counts, without operator data.

    Counts cover the catalog release/current pointer, eight relational projections,
    and configuration aggregate. Normalized mutable triggers are verified by their
    owning configuration repository and are not counted as catalog-controlled rows.
    """

    inserted: int
    updated: int
    unchanged: int
    deleted: int
    configuration_inserted: int
    configuration_preserved: int
    catalog_content_hash: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _PreparedProjection:
    snapshot_json: str
    rows: dict[type[Base], tuple[dict[str, Any], ...]]


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _json(value: Any) -> str:
    # Match the compiler's versioned hash exactly, including its Unicode semantics.
    return json.dumps(
        _plain(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prepare_projection(catalog: CompiledCatalog) -> _PreparedProjection:
    if type(catalog) is not CompiledCatalog:
        raise CatalogSeedError("seed_catalog_invalid", "seed requires a compiled catalog")
    try:
        source = {
            "manifest": catalog.manifest.model_dump(mode="json"),
            **{
                name: [record.model_dump(mode="json") for record in getattr(catalog, name)]
                for name in (
                    "departments",
                    "functions",
                    "tool_capabilities",
                    "approval_policies",
                    "templates",
                    "instances",
                )
            },
            "prompts": catalog.prompt_text_by_template,
            "input_schemas": catalog.input_schema_by_template,
            "output_schemas": catalog.output_schema_by_template,
        }
        snapshot_json = _json(source)
        if catalog.content_hash != "catalog-sha256-v1:" + _hash(snapshot_json):
            raise ValueError("compiled content hash disagrees with compiled records")
        for name, count in {
            "departments": 5,
            "functions": 12,
            "templates": 36,
            "instances": 43,
        }.items():
            records = getattr(catalog, name)
            if len(records) != count or len({record.id for record in records}) != count:
                raise ValueError("compiled identity counts do not match the v1 contract")
        if not 1 <= len(catalog.manifest.content_version) <= 80:
            raise ValueError("catalog version is not bounded")
        rows: dict[type[Base], tuple[dict[str, Any], ...]] = {}

        def projected(record: Any, *fields: str) -> dict[str, Any]:
            return {
                **{field: getattr(record, field) for field in fields},
                "snapshot_json": _json(record.model_dump(mode="json")),
                "release_hash": catalog.content_hash,
            }

        rows[DepartmentRecord] = tuple(
            projected(record, "id", "display_name", "display_order")
            for record in catalog.departments
        )
        rows[FunctionTeamRecord] = tuple(
            projected(record, "id", "department_id", "display_name", "display_order")
            for record in catalog.functions
        )
        rows[ToolCapabilityRecord] = tuple(
            projected(
                record,
                "id",
                "connector_family",
                "effect",
                "idempotency_support",
                "default_timeout_seconds",
            )
            for record in catalog.tool_capabilities
        )
        rows[ApprovalPolicyRecord] = tuple(
            projected(record, "id", "kind", "expiry_seconds")
            for record in catalog.approval_policies
        )
        policies = {record.id: record for record in catalog.approval_policies}
        templates = []
        for record in catalog.templates:
            prompt = catalog.prompt_text_by_template[record.id]
            input_schema = _json(catalog.input_schema_by_template[record.id])
            output_schema = _json(catalog.output_schema_by_template[record.id])
            policy = _json(policies[record.approval_policy_id].model_dump(mode="json"))
            templates.append(
                {
                    **projected(
                        record,
                        "id",
                        "department_id",
                        "function_id",
                        "approval_policy_id",
                        "display_name",
                        "display_order",
                    ),
                    "system_prompt_text": prompt,
                    "input_schema_json": input_schema,
                    "output_schema_json": output_schema,
                    "instruction_hash": _hash(prompt),
                    "input_schema_hash": _hash(input_schema),
                    "output_schema_hash": _hash(output_schema),
                    "policy_hash": _hash(policy),
                }
            )
        rows[AgentTemplateRecord] = tuple(templates)
        rows[AgentTemplateCapabilityRecord] = tuple(
            {"template_id": record.id, "capability_id": capability}
            for record in catalog.templates
            for capability in record.allowed_tool_capability_ids
        )
        rows[AgentTemplateTriggerKindRecord] = tuple(
            {"template_id": record.id, "trigger_kind": kind}
            for record in catalog.templates
            for kind in record.supported_trigger_types
        )
        instances = []
        for instance in catalog.instances:
            identity = {
                "id": instance.id,
                "template_id": instance.template_id,
                "source_ordinal": 1
                if instance.variant is None
                else instance.variant.source_ordinal,
                "display_order": instance.display_order,
            }
            instances.append(
                {**identity, "snapshot_json": _json(identity), "release_hash": catalog.content_hash}
            )
        rows[AgentInstanceRecord] = tuple(instances)
        for model, values in rows.items():
            if len({_key(model, row) for row in values}) != len(values):
                raise ValueError("compiled relational identities are duplicated")
            for column in model.__table__.columns:
                if (
                    isinstance(column.type, String)
                    and column.type.length is not None
                    and any(
                        type(row[column.name]) is not str
                        or len(row[column.name]) > column.type.length
                        for row in values
                    )
                ):
                    raise ValueError("compiled projection exceeds a portable string bound")
        return _PreparedProjection(snapshot_json, rows)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CatalogSeedError(
            "seed_catalog_invalid", "compiled catalog integrity is invalid"
        ) from exc


def _key(model: type[Base], row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row[column.name] for column in model.__table__.primary_key)


def _material(record: Base) -> dict[str, Any]:
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


async def _records(session: AsyncSession, model: type[Base]) -> dict[tuple[Any, ...], Base]:
    records = (await session.scalars(select(model).execution_options(populate_existing=True))).all()
    return {_key(model, _material(record)): record for record in records}


async def _release(
    session: AsyncSession, catalog: CompiledCatalog, prepared: _PreparedProjection, *, check: bool
) -> tuple[int, int, int]:
    releases = (
        await session.scalars(
            select(CatalogReleaseRecord).execution_options(populate_existing=True)
        )
    ).all()
    matched = False
    for release in releases:
        try:
            snapshot = json.loads(release.snapshot_json)
            if (
                _json(snapshot) != release.snapshot_json
                or "catalog-sha256-v1:" + _hash(release.snapshot_json) != release.content_hash
                or snapshot["manifest"]["content_version"] != release.content_version
            ):
                raise ValueError("historic release integrity failed")
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogSeedError(
                "seed_release_conflict", "persisted catalog release integrity is invalid"
            ) from exc
        if (
            release.content_version == catalog.manifest.content_version
            or release.content_hash == catalog.content_hash
        ):
            if (
                release.content_version != catalog.manifest.content_version
                or release.content_hash != catalog.content_hash
                or release.snapshot_json != prepared.snapshot_json
            ):
                raise CatalogSeedError(
                    "seed_release_conflict",
                    "catalog version and immutable release content disagree",
                )
            matched = True
    current = await session.get(CatalogCurrentReleaseRecord, 1, populate_existing=True)
    if check and (not matched or current is None or current.content_hash != catalog.content_hash):
        raise CatalogSeedError("seed_projection_drift", "catalog release is absent or not current")
    inserted = updated = unchanged = 0
    if matched:
        unchanged += 1
    else:
        session.add(
            CatalogReleaseRecord(
                content_hash=catalog.content_hash,
                content_version=catalog.manifest.content_version,
                snapshot_json=prepared.snapshot_json,
                recorded_at=datetime.now(UTC),
            )
        )
        await session.flush()
        inserted += 1
    if current is None:
        session.add(CatalogCurrentReleaseRecord(singleton_id=1, content_hash=catalog.content_hash))
        inserted += 1
    elif current.content_hash == catalog.content_hash:
        unchanged += 1
    else:
        current.content_hash = catalog.content_hash
        updated += 1
    if not check:
        await session.flush()
    return inserted, updated, unchanged


async def _apply_projection(
    session: AsyncSession, catalog: CompiledCatalog, prepared: _PreparedProjection, *, check: bool
) -> CatalogSeedResult:
    inserted, updated, unchanged = await _release(session, catalog, prepared, check=check)
    deleted = 0
    for model in _MODELS:
        expected = {_key(model, row): row for row in prepared.rows[model]}
        existing = await _records(session, model)
        extra = set(existing) - set(expected)
        if extra and model not in _EDGES:
            raise CatalogSeedError(
                "seed_identity_change", "catalog import would remove stable identities"
            )
        for key in set(existing) & set(expected):
            if any(
                getattr(existing[key], field) != expected[key][field]
                for field in _IDENTITY_FIELDS.get(model, ())
            ):
                raise CatalogSeedError(
                    "seed_identity_change", "catalog import would remap a stable identity"
                )
        changed = {
            key
            for key in set(existing) & set(expected)
            if _material(existing[key]) != expected[key]
        }
        missing = set(expected) - set(existing)
        if check and (extra or changed or missing):
            raise CatalogSeedError(
                "seed_projection_drift", "catalog projection differs from compiled source"
            )
        for key in extra:
            await session.delete(existing[key])
        deleted += len(extra)
        # Two-phase order updates permit valid sibling-order swaps without dropping
        # any identity or weakening unique constraints outside this transaction.
        order_column = "display_order"
        reordered = [
            key
            for key in changed
            if "display_order" in expected[key]
            and getattr(existing[key], order_column) != expected[key]["display_order"]
        ]
        if reordered:
            temporary = (
                max(int(getattr(record, order_column)) for record in existing.values()) + 10_001
            )
            for ordinal, key in enumerate(sorted(reordered)):
                setattr(existing[key], order_column, temporary + ordinal)
            await session.flush()
        for key in sorted(changed):
            for field, value in expected[key].items():
                setattr(existing[key], field, value)
        for key in sorted(missing):
            session.add(model(**expected[key]))
        inserted += len(missing)
        updated += len(changed)
        unchanged += len(expected) - len(changed) - len(missing)
        if not check:
            await session.flush()
    counts = {model.__tablename__: len(prepared.rows[model]) for model in _MODELS}
    counts["catalog_releases"] = len(
        (await session.scalars(select(CatalogReleaseRecord.content_hash))).all()
    )
    counts["catalog_current_release"] = 1
    return CatalogSeedResult(
        inserted, updated, unchanged, deleted, 0, 0, catalog.content_hash, counts
    )


async def seed_catalog_projection(
    session: AsyncSession, catalog: CompiledCatalog, *, check: bool = False
) -> CatalogSeedResult:
    """Persist only catalog parents in the caller's transaction; never commit.

    This supports targeted repository fixtures. Production callers use seed_catalog
    so catalog, defaults, and all post-write checks share one serialized transaction.
    """

    prepared = _prepare_projection(catalog)
    result = await _apply_projection(session, catalog, prepared, check=check)
    if not check:
        await _apply_projection(session, catalog, prepared, check=True)
    return result


def _check_runtime(runtime: DatabaseRuntime) -> DatabaseRuntime:
    """Own a no-create read-only connection independently of caller pool configuration."""

    url = runtime.engine.url
    if url.drivername == "sqlite+aiosqlite":
        database = str(url.database or "")
        if database.startswith("file:"):
            database = unquote(database.removeprefix("file:"))
        if not database or database == ":memory:" or url.query.get("mode") == "memory":
            raise CatalogSeedError(
                "seed_projection_drift", "catalog check requires an existing persistent database"
            )
        path = Path(database).resolve()
        if not path.is_file():
            raise CatalogSeedError("seed_projection_drift", "catalog database does not exist")
        url = url.set(
            database=f"file:{quote(str(path), safe='/')}", query={"mode": "rw", "uri": "true"}
        )
        engine = create_async_engine(url, pool_pre_ping=True)

        @event.listens_for(engine.sync_engine, "connect")
        def query_only(dbapi_connection: Any, connection_record: Any) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            try:
                # mode=rw disallows creation even if the file disappears after
                # preflight. query_only rejects DML/DDL while still allowing WAL
                # housekeeping at close; plain mode=ro can leave new sidecars.
                cursor.execute("PRAGMA query_only=ON")
            finally:
                cursor.close()

    elif url.drivername == "postgresql+asyncpg":
        engine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args={"server_settings": {"default_transaction_read_only": "on"}},
        )
    else:
        raise CatalogSeedError(
            "seed_catalog_invalid", "catalog check database driver is unsupported"
        )
    return DatabaseRuntime(
        engine=engine,
        session_factory=async_sessionmaker(engine, autoflush=False, expire_on_commit=False),
    )


async def seed_catalog(
    catalog: CompiledCatalog,
    runtime: DatabaseRuntime,
    recurrence: RecurrenceCalculator,
    *,
    check: bool = False,
) -> CatalogSeedResult:
    """Import all catalog/config defaults atomically, or verify without any write.

    Schema creation/migration belongs to the caller. Check mode owns a separate
    no-create/query-only runtime; it never connects the caller's writable engine.
    Compiled source and defaults are fully prepared before the transaction is opened.
    """

    prepared = _prepare_projection(catalog)
    check_runtime: DatabaseRuntime | None = None
    try:
        defaults = catalog_instance_configuration_defaults(catalog, recurrence)
        active_runtime = runtime
        if check:
            check_runtime = _check_runtime(runtime)
            active_runtime = check_runtime
        async with active_runtime.session_factory() as session:
            async with session.begin():
                dialect = session.get_bind().dialect.name
                if dialect == "sqlite":
                    await session.execute(text("BEGIN" if check else "BEGIN IMMEDIATE"))
                elif dialect == "postgresql" and not check:
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_id)"),
                        {"lock_id": _CATALOG_SEED_LOCK},
                    )
                    # Seeders share the advisory lock; operator updates instead
                    # participate through configuration row locks. Hold these
                    # before comparing any local override against the new release.
                    await session.execute(
                        select(AgentInstanceConfigurationRecord.instance_id)
                        .order_by(AgentInstanceConfigurationRecord.instance_id)
                        .with_for_update()
                    )
                projection = await _apply_projection(session, catalog, prepared, check=check)
                configurations = await seed_instance_configurations_in_repository(
                    catalog,
                    SQLAlchemyInstanceConfigurationRepository(session),
                    recurrence,
                    defaults=defaults,
                    check=check,
                )
                if not check:
                    await _apply_projection(session, catalog, prepared, check=True)
                counts = {**projection.counts, "agent_instance_configs": configurations.total}
                result = CatalogSeedResult(
                    inserted=projection.inserted,
                    updated=projection.updated,
                    unchanged=projection.unchanged,
                    deleted=projection.deleted,
                    configuration_inserted=configurations.inserted,
                    configuration_preserved=configurations.preserved,
                    catalog_content_hash=catalog.content_hash,
                    counts=counts,
                )
                if check:
                    # Explicit rollback avoids even a COMMIT in the check path.
                    await session.rollback()
            return result
    except CatalogSeedError:
        raise
    except InstanceConfigurationSeedError as exc:
        raise CatalogSeedError(exc.code, str(exc)) from exc
    except Exception as exc:
        raise CatalogSeedError(
            "seed_persistence_failed", "catalog seed transaction could not complete"
        ) from exc
    finally:
        if check_runtime is not None:
            await check_runtime.dispose()
