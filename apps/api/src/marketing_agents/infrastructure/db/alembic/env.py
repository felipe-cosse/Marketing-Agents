"""Async Alembic environment with a caller-owned connection when embedded."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from alembic import context
from sqlalchemy import CheckConstraint
from sqlalchemy.engine import Connection

from marketing_agents.config import Settings
from marketing_agents.infrastructure.db import Base, create_database_runtime
from marketing_agents.infrastructure.db.migrations import expected_tables
from marketing_agents.infrastructure.db.types import UTCDateTime

config = context.config
target_metadata = Base.metadata
arguments = context.get_x_argument(as_dictionary=True)
_TYPE_BOUND_CHECKS = {
    str(constraint.name)
    for table in target_metadata.tables.values()
    for constraint in table.constraints
    if isinstance(constraint, CheckConstraint) and getattr(constraint, "_type_bound", False)
}


def render_item(kind: str, item: Any, _context: Any) -> str | Literal[False]:
    if kind == "type" and isinstance(item, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def include_object(
    obj: Any, name: str | None, kind: str, _reflected: bool, _compare_to: Any
) -> bool:
    if kind == "check_constraint" and name in _TYPE_BOUND_CHECKS:
        # Boolean creates its own dialect-dependent check. Treating its reflected
        # SQLite check as a standalone constraint generates spurious DROP DDL.
        return False
    # This option is for generating the initial dependency-ordered revisions only.
    # Upgrade execution uses their frozen literal operations, never current ORM DDL.
    stage = arguments.get("stage")
    if stage is None:
        return True
    if kind == "index" and stage != "0005":
        return False
    table_name = name if kind == "table" else getattr(getattr(obj, "table", None), "name", None)
    return table_name is None or table_name in expected_tables(stage)


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        transactional_ddl=True,
        render_item=render_item,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    runtime = create_database_runtime(arguments.get("database_url") or Settings().database_url)
    try:
        async with runtime.engine.connect() as connection, connection.begin():
            if connection.dialect.name == "sqlite":
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(run_migrations)
    finally:
        await runtime.dispose()


if context.is_offline_mode():
    context.configure(
        url=arguments.get("database_url")
        or config.get_main_option("sqlalchemy.url")
        or Settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        run_migrations(supplied)
    else:
        asyncio.run(run_async_migrations())
