"""DEL-10: PostgreSQL runs real API/worker journeys in an owned Unix-socket database."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.config import Settings
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import (
    SQLAlchemyRunRepository,
    SQLAlchemyUnitOfWorkError,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.digest_key import load_or_create_digest_key
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from marketing_agents.workers.runtime.scheduler import SchedulerWorker
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from scripts import del_05_replay_fixture as replay
from scripts import del_05_runtime_smoke as smoke
from scripts.del_05_runtime_smoke import (
    SmokeFailure,
    async_database_snapshot,
    database_snapshot,
    readonly_database_engine,
)
from tests.integration.runtime import test_del_05_process_composition as journeys
from tests.support.postgresql_runtime import pg_database_url as pg_database_url

CATALOG = Path(__file__).resolve().parents[3] / "catalog" / "v1"


@pytest.fixture
async def application_url(pg_database_url: str):
    """Use a nonsuperuser owning only this fixture's freshly created database."""
    administrator = create_database_runtime(pg_database_url)
    parsed = make_url(pg_database_url)
    role = "primary_" + uuid4().hex
    quote = administrator.engine.dialect.identifier_preparer.quote
    created = False
    try:
        async with administrator.engine.begin() as connection:
            await connection.execute(
                text(f"CREATE ROLE {quote(role)} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
            )
            await connection.execute(
                text(f"ALTER DATABASE {quote(parsed.database)} OWNER TO {quote(role)}")
            )
            await connection.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {quote(role)}"))
        created = True
        yield parsed.set(username=role).render_as_string(hide_password=False)
    finally:
        try:
            if created:
                # This generated role and database belong solely to this fixture.
                # Return ownership before the outer fixture drops its database.
                async with administrator.engine.begin() as connection:
                    await connection.execute(
                        text(f"REASSIGN OWNED BY {quote(role)} TO {quote(parsed.username)}")
                    )
                    await connection.execute(text(f"DROP OWNED BY {quote(role)}"))
                    await connection.execute(text(f"DROP ROLE {quote(role)}"))
        finally:
            await administrator.dispose()


@pytest.fixture
async def primary_settings(application_url: str, tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=application_url,
        catalog_root=CATALOG,
        marketing_agents_digest_key_path=tmp_path / "keys" / "digest.key",
        webhook_hmac_secret=replay.PUBLIC_FIXTURE_HMAC,
    )
    await migrate_local_database(settings.database_url, settings.marketing_agents_digest_key_path)
    database = create_database_runtime(application_url)
    try:
        async with database.engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one() == (False, False, False)
        await seed_catalog(compile_catalog(CATALOG), database, CroniterRecurrenceCalculator())
    finally:
        await database.dispose()
    return settings


def _use_primary(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    async def installation(_tmp_path: Path):
        return settings

    monkeypatch.setattr(journeys, "_installation", installation)


async def test_postgresql_primary_all_read_only_workflows_and_restart(
    primary_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_primary(monkeypatch, primary_settings)
    # Run the same complete artifact/transition assertions as SQLite, not a
    # PostgreSQL-specific substitute with weaker expected behavior.
    await journeys.test_del_05_api_to_worker_all_read_only_artifacts_and_restart(tmp_path)


async def test_postgresql_primary_email_approval_resume_and_durable_receipts(
    primary_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_primary(monkeypatch, primary_settings)
    await journeys.test_del_05_email_api_approval_resume_records_two_durable_receipts(tmp_path)


async def test_postgresql_primary_claim_races_expiry_and_stale_owner_fences(
    primary_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_primary(monkeypatch, primary_settings)
    await journeys.test_del_05_claim_race_expiry_and_stale_release_are_fenced(tmp_path, monkeypatch)


async def test_postgresql_primary_reseed_preserves_config_and_bound_schedule_replay(
    primary_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = primary_settings
    monkeypatch.setattr(replay, "fixture_settings", lambda: settings)
    assert (await replay.prepare())["configurations_changed"] == 2
    before = await async_database_snapshot(
        settings.database_url, settings.marketing_agents_digest_key_path
    )
    database = create_database_runtime(settings.database_url)
    try:
        reseed = await seed_catalog(
            compile_catalog(CATALOG), database, CroniterRecurrenceCalculator()
        )
        assert (reseed.inserted, reseed.updated, reseed.deleted, reseed.configuration_inserted) == (
            0,
            0,
            0,
            0,
        )
    finally:
        await database.dispose()
    assert (
        await async_database_snapshot(
            settings.database_url, settings.marketing_agents_digest_key_path
        )
        == before
    )
    occurrences = []
    for attempt in range(2):
        runtime = await build_runtime(settings)
        try:
            assert await SchedulerWorker(runtime, f"worker.primary.{attempt}").drain_once() is (
                attempt == 0
            )
            occurrence = await asyncio.wait_for(
                replay.wait_for_schedule_occurrence(settings.database_url), timeout=2
            )
            occurrences.append(occurrence)
            async with runtime.dependencies.unit_of_work() as unit:
                work = await unit.works.get(occurrence[1])
                assert work is not None
                assert work.instance_id == replay.SCHEDULE_INSTANCE
                assert work.configuration_revision == 2
                assert work.admitted_payload == replay.scheduled_fixture_payload()
                assert work.mode.value == "dry_run"
        finally:
            await runtime.close()
    assert occurrences[0] == occurrences[1]
    after = await async_database_snapshot(
        settings.database_url, settings.marketing_agents_digest_key_path
    )
    assert after["counts"]["schedule_occurrences"] == 1
    assert after["key_fingerprint"] == before["key_fingerprint"]


async def test_postgresql_primary_snapshot_is_read_only_repeatable_and_key_bound(
    primary_settings: Settings, tmp_path: Path
) -> None:
    settings = primary_settings
    before = await async_database_snapshot(
        settings.database_url, settings.marketing_agents_digest_key_path
    )
    assert (
        database_snapshot(settings.database_url, settings.marketing_agents_digest_key_path)
        == before
    )
    for hash_seed in ("1", "2"):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CATALOG.parents[1] / "scripts" / "del_05_runtime_smoke.py"),
            "snapshot",
            "--database-url",
            settings.database_url,
            "--key",
            str(settings.marketing_agents_digest_key_path),
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        assert process.returncode == 0 and stderr == b""
        assert json.loads(stdout) == {"ok": True, **before}
    engine = readonly_database_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            assert await connection.scalar(text("SHOW transaction_read_only")) == "on"
            assert await connection.scalar(text("SHOW transaction_isolation")) == "repeatable read"
            with pytest.raises(DBAPIError):
                await connection.execute(text("UPDATE local_runtime_identity SET format_version=1"))
    finally:
        await engine.dispose()
    mismatched = tmp_path / "different" / "digest.key"
    load_or_create_digest_key(mismatched, persistent_state_exists=False)
    with pytest.raises(SmokeFailure, match="database_key_pair_mismatch"):
        await async_database_snapshot(settings.database_url, mismatched)
    assert (
        await async_database_snapshot(
            settings.database_url, settings.marketing_agents_digest_key_path
        )
        == before
    )


async def test_postgresql_primary_snapshot_transaction_holds_one_database_version(
    primary_settings: Settings,
) -> None:
    reader = readonly_database_engine(primary_settings.database_url)
    writer = create_database_runtime(primary_settings.database_url)
    query = text("SELECT display_name FROM departments WHERE id = :id")
    try:
        async with reader.begin() as connection:
            department = (
                await connection.execute(text("SELECT id FROM departments ORDER BY id"))
            ).first()[0]
            parameters = {"id": department}
            original = await connection.scalar(query, parameters)
            async with writer.engine.begin() as mutation:
                await mutation.execute(
                    text(
                        "UPDATE departments SET display_name='Concurrent owned-fixture change' "
                        "WHERE id=:id"
                    ),
                    parameters,
                )
            assert await connection.scalar(query, parameters) == original
        async with reader.begin() as connection:
            assert await connection.scalar(query, parameters) == "Concurrent owned-fixture change"
    finally:
        await reader.dispose()
        await writer.dispose()


async def test_postgresql_primary_snapshot_includes_migration_state(
    primary_settings: Settings,
) -> None:
    settings = primary_settings
    before = await async_database_snapshot(
        settings.database_url, settings.marketing_agents_digest_key_path
    )
    database = create_database_runtime(settings.database_url)
    try:
        async with database.engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num='0001'"))
        after = await async_database_snapshot(
            settings.database_url, settings.marketing_agents_digest_key_path
        )
        assert after["counts"] == before["counts"]
        assert after["key_fingerprint"] == before["key_fingerprint"]
        assert after["logical_database_sha256"] != before["logical_database_sha256"]
    finally:
        await database.dispose()


@pytest.mark.parametrize("snapshot_reads", [False, True])
async def test_postgresql_primary_projection_cannot_mix_concurrent_worker_transitions(
    primary_settings: Settings, monkeypatch: pytest.MonkeyPatch, snapshot_reads: bool
) -> None:
    runtime = await build_runtime(primary_settings)
    if not snapshot_reads:
        # Reproduce the previous default READ COMMITTED projection deterministically.
        runtime.read_unit_of_work = runtime.dependencies.unit_of_work_factory
    loaded_run = asyncio.Event()
    writer_committed = asyncio.Event()
    read_task = None
    original = SQLAlchemyRunRepository._inspect_record
    intercepted = False

    async def inspect_after_writer(self, record):
        nonlocal intercepted
        if asyncio.current_task() is read_task and not intercepted:
            intercepted = True
            loaded_run.set()
            await writer_committed.wait()
        return await original(self, record)

    monkeypatch.setattr(SQLAlchemyRunRepository, "_inspect_record", inspect_after_writer)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await journeys._submit(client, "demo.social-media.content-draft.v1")
            read_task = asyncio.create_task(client.get(f"/api/v1/runs/{run_id}"))
            await asyncio.wait_for(loaded_run.wait(), timeout=5)
            assert await RunWorker(runtime, "worker.primary.concurrent").drain_once()
            writer_committed.set()
            response = await asyncio.wait_for(read_task, timeout=5)
            assert response.status_code == (200 if snapshot_reads else 503)
            if snapshot_reads:
                observed = response.json()
                assert observed["state"] == "received" and observed["version"] == 1
                assert len(observed["transitions"]) == 1
                assert observed["plan"] is observed["execution_control"] is None
                assert observed["artifact_summaries"] == []
            fresh = await client.get(f"/api/v1/runs/{run_id}")
            assert fresh.status_code == 200
            assert fresh.json()["state"] == "completed"
            assert len(fresh.json()["transitions"]) == 5
            # Real corruption still fails closed; snapshot isolation does not
            # relax any version/history/child validation to hide the race.
            async with runtime.database.engine.begin() as connection:
                await connection.execute(
                    text("UPDATE runs SET version=version+1 WHERE id=:id"), {"id": run_id}
                )
            corrupt = await client.get(f"/api/v1/runs/{run_id}")
            assert corrupt.status_code == 503
    finally:
        writer_committed.set()
        if read_task is not None and not read_task.done():
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
        await runtime.close()


async def test_postgresql_primary_projection_isolation_never_leaks_to_writers(
    primary_settings: Settings,
) -> None:
    runtime = await build_runtime(primary_settings)
    try:
        async with runtime.read_unit_of_work() as unit:
            session = unit._require_session()
            assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
            assert await session.scalar(text("SHOW transaction_read_only")) == "on"
            with pytest.raises(SQLAlchemyUnitOfWorkError, match="read-only"):
                await unit.commit()
            with pytest.raises(DBAPIError):
                await session.execute(text("UPDATE local_runtime_identity SET format_version=1"))
        async with runtime.dependencies.unit_of_work() as unit:
            session = unit._require_session()
            assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
            assert await session.scalar(text("SHOW transaction_read_only")) == "off"
            await session.execute(text("UPDATE local_runtime_identity SET format_version=1"))
            await unit.commit()
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE VIEW snapshot_hidden AS SELECT version_num FROM alembic_version",
        "CREATE MATERIALIZED VIEW snapshot_hidden AS SELECT version_num FROM alembic_version",
        "CREATE FUNCTION snapshot_hidden() RETURNS INTEGER LANGUAGE SQL AS 'SELECT 1'",
        "CREATE SCHEMA snapshot_hidden",
        "CREATE EXTENSION hstore",
        "CREATE TRIGGER snapshot_hidden BEFORE UPDATE ON departments FOR EACH ROW "
        "EXECUTE FUNCTION suppress_redundant_updates_trigger()",
        "CREATE TYPE snapshot_hidden AS ENUM ('one', 'two')",
        "CREATE DOMAIN snapshot_hidden AS INTEGER CHECK (VALUE > 0)",
        "CREATE TYPE snapshot_hidden AS (value INTEGER)",
        "CREATE RULE snapshot_hidden AS ON UPDATE TO departments DO INSTEAD NOTHING",
        "CREATE POLICY snapshot_hidden ON departments USING (true)",
        "ALTER TABLE departments ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE departments FORCE ROW LEVEL SECURITY",
    ],
)
async def test_postgresql_primary_snapshot_refuses_uncovered_schema_objects(
    primary_settings: Settings, definition: str
) -> None:
    runtime = create_database_runtime(primary_settings.database_url)
    try:
        await async_database_snapshot(
            primary_settings.database_url, primary_settings.marketing_agents_digest_key_path
        )
        async with runtime.engine.begin() as connection:
            await connection.execute(text(definition))
        with pytest.raises(SmokeFailure, match="snapshot_unsupported_schema_objects"):
            await async_database_snapshot(
                primary_settings.database_url, primary_settings.marketing_agents_digest_key_path
            )
    finally:
        await runtime.dispose()


async def test_postgresql_primary_snapshot_normalizes_only_equivalent_check_semantics(
    primary_settings: Settings,
) -> None:
    runtime = create_database_runtime(primary_settings.database_url)
    outer_cast = (
        "value::text = ANY (ARRAY['first'::character varying, "
        "'Second value'::character varying]::text[])"
    )
    element_cast = (
        "value::text = ANY (ARRAY['first'::character varying::text, "
        "'Second value'::character varying::text])"
    )
    definitions = (
        (outer_cast, ""),
        (element_cast, ""),
        (element_cast.replace("Second value", "second value"), ""),
        (element_cast.replace("ANY", "ALL"), ""),
        (element_cast, " NOT VALID"),
    )
    hashes = []
    try:
        for expression, options in definitions:
            async with runtime.engine.begin() as connection:
                await connection.execute(text("DROP TABLE IF EXISTS snapshot_check_probe"))
                await connection.execute(text("CREATE TABLE snapshot_check_probe (value VARCHAR)"))
                await connection.execute(
                    text(
                        "ALTER TABLE snapshot_check_probe ADD CONSTRAINT ck_snapshot_value "
                        f"CHECK ({expression}){options}"
                    )
                )
            if options:
                with pytest.raises(SmokeFailure, match="snapshot_unsupported_constraint_options"):
                    await async_database_snapshot(
                        primary_settings.database_url,
                        primary_settings.marketing_agents_digest_key_path,
                    )
                continue
            hashes.append(
                (
                    await async_database_snapshot(
                        primary_settings.database_url,
                        primary_settings.marketing_agents_digest_key_path,
                    )
                )["logical_database_sha256"]
            )
        assert hashes[0] == hashes[1]
        assert len(set(hashes[1:])) == 3
    finally:
        await runtime.dispose()


async def test_postgresql_primary_snapshot_covers_sequence_state_and_definitions(
    primary_settings: Settings,
) -> None:
    runtime = create_database_runtime(primary_settings.database_url)

    async def snapshot():
        return await async_database_snapshot(
            primary_settings.database_url,
            primary_settings.marketing_agents_digest_key_path,
            include_table_digests=True,
        )

    try:
        before = await snapshot()
        sequences = before["table_digests"]["__postgresql_sequences__"]
        assert sequences["row_count"] == 3
        async with runtime.engine.begin() as connection:
            # This singleton is inserted explicitly; flip only is_called first
            # to prove the digest covers it as well as the numeric sequence value.
            assert (
                await connection.execute(
                    text("SELECT last_value, is_called FROM audit_feed_sequence_singleton_id_seq")
                )
            ).one() == (1, False)
            await connection.execute(
                text("SELECT setval('audit_feed_sequence_singleton_id_seq', 1, true)")
            )
        called = await snapshot()
        assert called["logical_database_sha256"] != before["logical_database_sha256"]
        assert (
            called["table_digests"]["__postgresql_sequences__"]["schema_sha256"]
            == sequences["schema_sha256"]
        )
        async with runtime.engine.begin() as connection:
            await connection.execute(text("SELECT nextval('audit_feed_sequence_singleton_id_seq')"))
        advanced = await snapshot()
        assert advanced["logical_database_sha256"] != called["logical_database_sha256"]
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("ALTER SEQUENCE audit_feed_sequence_singleton_id_seq INCREMENT BY 2")
            )
        altered = await snapshot()
        assert altered["logical_database_sha256"] != advanced["logical_database_sha256"]
        assert (
            altered["table_digests"]["__postgresql_sequences__"]["schema_sha256"]
            != sequences["schema_sha256"]
        )
        for result in (called, advanced, altered):
            assert result["counts"] == before["counts"]
            assert {
                name: value
                for name, value in result["table_digests"].items()
                if name != "__postgresql_sequences__"
            } == {
                name: value
                for name, value in before["table_digests"].items()
                if name != "__postgresql_sequences__"
            }
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE SEQUENCE snapshot_unmanaged",
        "ALTER SEQUENCE audit_feed_sequence_singleton_id_seq OWNED BY NONE",
        "ALTER SEQUENCE audit_feed_sequence_singleton_id_seq "
        "OWNED BY audit_feed_sequence.last_sequence",
    ],
)
async def test_postgresql_primary_snapshot_refuses_unmanaged_or_reowned_sequences(
    primary_settings: Settings, definition: str
) -> None:
    runtime = create_database_runtime(primary_settings.database_url)
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text(definition))
        with pytest.raises(SmokeFailure, match="snapshot_unsupported_sequences"):
            await async_database_snapshot(
                primary_settings.database_url, primary_settings.marketing_agents_digest_key_path
            )
    finally:
        await runtime.dispose()


async def test_postgresql_primary_snapshot_rejects_concurrent_non_mvcc_sequence_movement(
    primary_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.util.concurrency import await_only

    runtime = create_database_runtime(primary_settings.database_url)
    original = smoke._postgresql_sequence_snapshot
    samples = 0

    async def advance():
        # A separate transaction advances a sequence even though the reader's
        # table snapshot is repeatable-read and read-only. Do not mutate rows.
        async with runtime.engine.begin() as connection:
            await connection.execute(text("SELECT nextval('audit_feed_sequence_singleton_id_seq')"))

    def interleave(connection):
        nonlocal samples
        result = original(connection)
        samples += 1
        if samples == 1:
            await_only(advance())
        return result

    monkeypatch.setattr(smoke, "_postgresql_sequence_snapshot", interleave)
    try:
        with pytest.raises(SmokeFailure, match="snapshot_sequences_changed"):
            await async_database_snapshot(
                primary_settings.database_url, primary_settings.marketing_agents_digest_key_path
            )
        assert samples == 2
    finally:
        await runtime.dispose()
