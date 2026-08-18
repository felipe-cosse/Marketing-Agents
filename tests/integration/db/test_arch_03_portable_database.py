"""ARCH-03: portable async SQLite persistence with an optional PostgreSQL driver."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.ports.repositories import (
    AuditRepository,
    RunRepository,
    WorkRepository,
)
from marketing_agents.config import Settings
from marketing_agents.infrastructure.db import (
    NAMING_CONVENTION,
    Base,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyUnitOfWorkError,
    SQLAlchemyUnitOfWorkFactory,
    UTCDateTime,
    create_database_runtime,
)
from pydantic import ValidationError
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.asyncio
async def test_arch_03_sqlite_runtime_enforces_pragmas_and_aware_utc(tmp_path: Path) -> None:
    database_path = tmp_path / "portable.db"
    runtime = create_database_runtime(
        _sqlite_url(database_path),
        sqlite_busy_timeout_ms=2_345,
    )
    metadata = MetaData()
    instants = Table(
        "arch_03_instants",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("label", String(20), nullable=False),
        Column("occurred_at", UTCDateTime(), nullable=False),
    )
    parents = Table(
        "arch_03_parents",
        metadata,
        Column("id", Integer, primary_key=True),
    )
    children = Table(
        "arch_03_children",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", ForeignKey(parents.c.id), nullable=False),
    )
    try:
        async with runtime.engine.connect() as connection:
            assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
            assert (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one() == 2_345
            journal_mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
            assert str(journal_mode).lower() == "wal"

        async with runtime.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        with pytest.raises(IntegrityError):
            async with runtime.engine.begin() as connection:
                await connection.execute(children.insert().values(id=1, parent_id=999))

        source = datetime(2026, 8, 18, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        async with runtime.engine.begin() as connection:
            await connection.execute(
                instants.insert().values(id=1, label="normalized", occurred_at=source)
            )
        async with runtime.engine.connect() as connection:
            restored = (
                await connection.execute(select(instants.c.occurred_at).where(instants.c.id == 1))
            ).scalar_one()
        assert restored == source.astimezone(UTC)
        assert restored.tzinfo is UTC

        with pytest.raises(StatementError, match="timezone-aware"):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    instants.insert().values(
                        id=2,
                        label="naive",
                        occurred_at=datetime(2026, 8, 18, 9, 15),
                    )
                )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_arch_03_repository_injected_unit_of_work_commits_or_rolls_back(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unit-of-work.db"
    runtime = create_database_runtime(_sqlite_url(database_path))
    metadata = MetaData()
    probes = Table(
        "arch_03_uow_probes",
        metadata,
        Column("id", Integer, primary_key=True),
    )
    captured_sessions: list[AsyncSession] = []

    def capture_work(session: AsyncSession) -> WorkRepository:
        captured_sessions.append(session)
        return cast(WorkRepository, object())

    def capture_run(session: AsyncSession) -> RunRepository:
        captured_sessions.append(session)
        return cast(RunRepository, object())

    def capture_audit(session: AsyncSession) -> AuditRepository:
        captured_sessions.append(session)
        return cast(AuditRepository, object())

    uow_factory = SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(capture_work, capture_run, capture_audit),
    )
    try:
        async with runtime.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        committed = uow_factory()
        with pytest.raises(SQLAlchemyUnitOfWorkError, match="not been entered"):
            _ = committed.works
        async with committed:
            assert len(captured_sessions) == 3
            assert captured_sessions[-3] is captured_sessions[-2] is captured_sessions[-1]
            await captured_sessions[-1].execute(probes.insert().values(id=1))
            await committed.commit()

        rolled_back = uow_factory()
        async with rolled_back:
            await captured_sessions[-1].execute(probes.insert().values(id=2))

        with pytest.raises(RuntimeError, match="injected failure"):
            async with uow_factory():
                await captured_sessions[-1].execute(probes.insert().values(id=3))
                raise RuntimeError("injected failure")

        async with runtime.session_factory() as session:
            identifiers = tuple((await session.execute(select(probes.c.id))).scalars())
        assert identifiers == (1,)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_arch_03_postgresql_driver_is_lazy_and_safe_without_network() -> None:
    pytest.importorskip(
        "asyncpg", reason="optional PostgreSQL compatibility extra is not installed"
    )
    password_canary = "arch-03-database-password-canary"
    query_canary = "arch-03-query-canary"
    database_url = (
        "postgresql+asyncpg://portable-user:"
        f"{password_canary}@db.example.test:5432/marketing?application_name={query_canary}"
    )
    settings = Settings(_env_file=None, database_url=database_url)
    snapshot = json.dumps(settings.safe_snapshot(), sort_keys=True)

    assert password_canary not in repr(settings)
    assert password_canary not in str(settings)
    assert password_canary not in snapshot
    assert query_canary not in snapshot
    assert "postgresql+asyncpg" in snapshot

    runtime = create_database_runtime(settings.database_url)
    try:
        assert runtime.engine.dialect.name == "postgresql"
        assert runtime.engine.url.drivername == "postgresql+asyncpg"
    finally:
        await runtime.dispose()


def test_arch_03_rejects_sync_unsupported_and_network_sqlite_urls() -> None:
    for database_url in (
        "sqlite:///./data/local.db",
        "postgresql://db.example.test/marketing",
        "mysql+aiomysql://db.example.test/marketing",
        "sqlite+aiosqlite://remote.example.test/marketing.db",
        "postgresql+asyncpg:///marketing",
        " sqlite+aiosqlite:///./data/local.db",
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, database_url=database_url)

    with pytest.raises(ValueError, match="1 through 60000"):
        create_database_runtime("sqlite+aiosqlite:///:memory:", sqlite_busy_timeout_ms=0)


def test_arch_03_base_exposes_portable_naming_convention() -> None:
    assert Base.metadata.naming_convention is NAMING_CONVENTION
    assert dict(Base.metadata.naming_convention) == dict(NAMING_CONVENTION)
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}
