"""AC-12: additive, integrity-bound recurrence evidence and legacy preservation."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Schedule, ScheduleOccurrence
from marketing_agents.domain.enums import MisfirePolicy, OccurrenceState
from marketing_agents.domain.recurrence_resolution import (
    RecurrenceResolution,
    RecurrenceResult,
    recurrence_result_to_dict,
)
from marketing_agents.domain.schedule_occurrence_identity import (
    SCHEDULE_OCCURRENCE_ID_SCHEME,
    SCHEDULE_RECURRENCE_VERSION,
    schedule_local_snapshot,
    schedule_occurrence_id,
)
from marketing_agents.infrastructure.db import (
    SchedulePersistenceConflict,
    SQLAlchemyScheduleRepository,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.migrations import HEAD_REVISION, upgrade_database
from marketing_agents.infrastructure.db.schema import schema_matches_metadata
from sqlalchemy import event, text

DUE = datetime(2026, 3, 8, 7, tzinfo=UTC)
NEXT = datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
ZONE = "America/New_York"
NEW_COLUMNS = {
    "schedules": {"next_recurrence_json"},
    "schedule_occurrences": {
        "scheduled_recurrence_json",
        "next_recurrence_json",
        "recurrence_resolutions_json",
    },
}


def gap() -> RecurrenceResolution:
    return RecurrenceResolution(
        reason="nonexistent_local_time",
        nominal_local="2026-03-08T02:30:00.000000",
        timezone=ZONE,
        resolved_at_utc=DUE,
    )


def schedule(*, recorded: bool = True) -> Schedule:
    return Schedule(
        id="schedule.ac12.persistence",
        trigger_id="trigger.ac12.persistence",
        instance_id="instance.ac12.persistence",
        workflow_id="workflow.ac12.persistence",
        cron="30 2 * * *",
        timezone=ZONE,
        next_run_at_utc=DUE,
        misfire_policy=MisfirePolicy.SKIP,
        misfire_grace_seconds=0,
        enabled=True,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
        next_recurrence=RecurrenceResult(scheduled_for_utc=DUE, resolution=gap())
        if recorded
        else None,
    )


def occurrence(*, recorded: bool = True) -> ScheduleOccurrence:
    pending = schedule(recorded=recorded)
    local, fold = schedule_local_snapshot(DUE, ZONE)
    return ScheduleOccurrence(
        id=schedule_occurrence_id(pending.id, DUE, recurrence_version=SCHEDULE_RECURRENCE_VERSION),
        schedule_id=pending.id,
        scheduled_for_utc=DUE,
        scheduled_local=local,
        timezone=ZONE,
        timezone_fold=fold,
        recurrence_version=SCHEDULE_RECURRENCE_VERSION,
        state=OccurrenceState.SKIPPED,
        misfire_policy_applied=MisfirePolicy.SKIP,
        misfire_grace_seconds=0,
        misfire_evaluated_at_utc=DUE + timedelta(hours=1),
        first_missed_at_utc=DUE,
        last_missed_at_utc=DUE,
        missed_count=1,
        scheduled_recurrence=pending.next_recurrence,
        next_recurrence=RecurrenceResult(scheduled_for_utc=NEXT) if recorded else None,
        recurrence_resolutions=(gap(),) if recorded else None,
    )


async def rows(database, table: str) -> tuple[dict[str, Any], ...]:
    assert table in NEW_COLUMNS or table == "alembic_version"
    async with database.engine.connect() as connection:
        result = await connection.execute(text(f"SELECT * FROM {table}"))
        return tuple(sorted((dict(row) for row in result.mappings()), key=repr))


async def legacy_state(database) -> dict[str, Any]:
    result = {name: await rows(database, name) for name in (*NEW_COLUMNS, "alembic_version")}
    async with database.engine.connect() as connection:
        result["schema"] = tuple(
            tuple(row)
            for row in await connection.execute(
                text("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
            )
        )
        assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []
    return result


async def seed_recorded(database) -> None:
    assert await upgrade_database(database) == HEAD_REVISION
    async with database.session_factory() as session:
        repository = SQLAlchemyScheduleRepository(session)
        assert (await repository.add_or_get(schedule())).inserted
        assert (await repository.add_occurrence_or_get(occurrence())).inserted
        await session.commit()


def legacy_records() -> dict[str, dict[str, Any]]:
    """Frozen pre-0009 material, deliberately independent of current serializers."""
    pending, observed = schedule(recorded=False), occurrence(recorded=False)
    schedule_material = {
        "id": pending.id,
        "trigger_id": pending.trigger_id,
        "instance_id": pending.instance_id,
        "workflow_id": pending.workflow_id,
        "cron_expression": pending.cron,
        "timezone_name": pending.timezone,
        "recurrence_version": pending.recurrence_version,
        "next_run_at_utc": DUE.isoformat(timespec="microseconds"),
        "last_scheduled_at_utc": None,
        "misfire_policy": "skip",
        "misfire_grace_seconds": 0,
        "enabled": True,
        "version": 1,
        "lease_owner": None,
        "lease_claimed_at_utc": None,
        "lease_expires_at_utc": None,
    }
    occurrence_material = {
        "id": observed.id,
        "identity_scheme": SCHEDULE_OCCURRENCE_ID_SCHEME,
        "schedule_id": pending.id,
        "scheduled_for_utc": DUE.isoformat(timespec="microseconds"),
        "scheduled_local": observed.scheduled_local,
        "timezone_name": ZONE,
        "timezone_fold": 0,
        "recurrence_version": SCHEDULE_RECURRENCE_VERSION,
        "state": "skipped",
        "work_item_id": None,
        "run_id": None,
        "misfire_policy_applied": "skip",
        "misfire_grace_seconds": 0,
        "misfire_evaluated_at_utc": (DUE + timedelta(hours=1)).isoformat(timespec="microseconds"),
        "first_missed_at_utc": DUE.isoformat(timespec="microseconds"),
        "last_missed_at_utc": DUE.isoformat(timespec="microseconds"),
        "missed_count": 1,
    }
    result = {}
    for table, material, domain in (
        ("schedules", schedule_material, b"marketing-agents:schedule:persistence:v1\x00"),
        (
            "schedule_occurrences",
            occurrence_material,
            b"marketing-agents:schedule-occurrence:persistence:v1\x00",
        ),
    ):
        row = {
            **material,
            "integrity_digest": hashlib.sha256(domain + canonical_json_bytes(material)).hexdigest(),
        }
        for name, value in row.items():
            if name.endswith("_utc") and value is not None:
                row[name] = datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S.%f")
        result[table] = row
    return result


async def seed_legacy(database) -> None:
    assert await upgrade_database(database, "0008") == "0008"
    async with database.engine.begin() as connection:
        for table, values in legacy_records().items():
            columns = ", ".join(values)
            parameters = ", ".join(f":{name}" for name in values)
            await connection.execute(
                text(f"INSERT INTO {table} ({columns}) VALUES ({parameters})"), values
            )


@pytest.mark.asyncio
async def test_ac_12_snapshot_roundtrip_replay_claim_advance_and_configuration_cas(tmp_path: Path):
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'recorded.db'}")
    try:
        await seed_recorded(database)
        initial_occurrence = await rows(database, "schedule_occurrences")
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            assert await repository.get(schedule().id) == schedule()
            assert await repository.get_occurrence(occurrence().id) == occurrence()
            assert not (await repository.add_or_get(schedule())).inserted
            assert not (await repository.add_occurrence_or_get(occurrence())).inserted
            await session.commit()
        # Same due identity is not permission to substitute observed recurrence facts.
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            with pytest.raises(
                SchedulePersistenceConflict, match="different initial configuration"
            ):
                await repository.add_or_get(
                    replace(schedule(), next_recurrence=RecurrenceResult(scheduled_for_utc=DUE))
                )
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            with pytest.raises(SchedulePersistenceConflict, match="different immutable facts"):
                await repository.add_occurrence_or_get(
                    replace(
                        occurrence(),
                        next_recurrence=RecurrenceResult(
                            scheduled_for_utc=NEXT + timedelta(hours=1)
                        ),
                    )
                )
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            claim = await repository.try_claim(
                schedule_id=schedule().id,
                expected_version=1,
                expected_due_at_utc=DUE,
                lease_owner="worker.ac12.persistence",
                claimed_at_utc=DUE,
                lease_expires_at_utc=DUE + timedelta(minutes=1),
            )
            assert claim is not None
            assert await repository.get(schedule().id) == replace(schedule(), version=2)
            assert await repository.fence_claim(claim, now=DUE)
            await session.commit()
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            advanced = await repository.advance_and_release_claim(
                claim,
                next_run_at_utc=NEXT,
                next_recurrence=occurrence().next_recurrence,
                completed_at_utc=DUE,
            )
            assert advanced == replace(
                schedule(),
                version=3,
                last_scheduled_at_utc=DUE,
                next_run_at_utc=NEXT,
                next_recurrence=occurrence().next_recurrence,
            )
            assert await repository.get_claim(schedule().id) is None
            assert advanced is not None
            replacement = replace(
                advanced,
                version=4,
                configuration_revision=2,
                next_run_at_utc=NEXT + timedelta(days=1),
                next_recurrence=RecurrenceResult(scheduled_for_utc=NEXT + timedelta(days=1)),
            )
            assert await repository.compare_and_swap_configuration(advanced, replacement)
            await session.commit()
        await database.dispose()
        database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'recorded.db'}")
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            assert await repository.get(schedule().id) == replacement
            assert await repository.get_occurrence(occurrence().id) == occurrence()
        assert await rows(database, "schedule_occurrences") == initial_occurrence
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ac_12_explicit_no_adjustment_is_not_legacy_unknown(tmp_path: Path):
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'ordinary.db'}")
    pending = replace(schedule(), next_recurrence=RecurrenceResult(scheduled_for_utc=DUE))
    observed = replace(
        occurrence(), scheduled_recurrence=pending.next_recurrence, recurrence_resolutions=()
    )
    try:
        await upgrade_database(database)
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            await repository.add_or_get(pending)
            await repository.add_occurrence_or_get(observed)
            await session.commit()
        stored = (await rows(database, "schedule_occurrences"))[0]
        assert stored["recurrence_resolutions_json"] == "[]"
        assert (
            stored["scheduled_recurrence_json"]
            == canonical_json_bytes(recurrence_result_to_dict(pending.next_recurrence)).decode()
        )
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            assert await repository.get_occurrence(observed.id) == observed
            with pytest.raises(SchedulePersistenceConflict, match="different immutable facts"):
                await repository.add_occurrence_or_get(occurrence(recorded=False))
    finally:
        await database.dispose()


@pytest.mark.parametrize("operation", ("configuration", "advancement"))
@pytest.mark.parametrize("recorded", (False, True), ids=("legacy-null", "recorded"))
@pytest.mark.asyncio
async def test_ac_12_repository_rejects_recorded_to_null_but_preserves_legacy_compatibility(
    tmp_path: Path, operation: str, recorded: bool
):
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'no-downgrade.db'}")
    original = schedule(recorded=recorded)
    try:
        await upgrade_database(database)
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            await repository.add_or_get(original)
            await repository.add_occurrence_or_get(occurrence(recorded=recorded))
            await session.commit()
        if operation == "advancement":
            async with database.session_factory() as session:
                repository = SQLAlchemyScheduleRepository(session)
                claim = await repository.try_claim(
                    schedule_id=original.id,
                    expected_version=original.version,
                    expected_due_at_utc=DUE,
                    lease_owner="worker.ac12.no-downgrade",
                    claimed_at_utc=DUE,
                    lease_expires_at_utc=DUE + timedelta(minutes=1),
                )
                assert claim is not None
                await session.commit()
            original = replace(original, version=2)
        before = {table: await rows(database, table) for table in NEW_COLUMNS}
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)

            async def update_without_evidence():
                if operation == "configuration":
                    return await repository.compare_and_swap_configuration(
                        original,
                        replace(
                            original,
                            version=original.version + 1,
                            configuration_revision=2,
                            enabled=False,
                            next_recurrence=None,
                        ),
                    )
                # Exercise omission of the optional argument, not only explicit None.
                return await repository.advance_and_release_claim(
                    claim,
                    next_run_at_utc=NEXT,
                    completed_at_utc=DUE,
                )

            if recorded:
                with pytest.raises(SchedulePersistenceConflict) as failure:
                    await update_without_evidence()
                assert failure.value.code == "schedule_recurrence_missing"
                # Commit after the caught rejection proves the guard ran before
                # any write, instead of relying on context-manager rollback.
                await session.commit()
                assert await repository.get(original.id) == original
                if operation == "advancement":
                    assert await repository.get_claim(original.id) == claim
            else:
                assert await update_without_evidence()
                await session.commit()
                restored = await repository.get(original.id)
                assert restored is not None
                assert restored.next_recurrence is None
                assert restored.version == original.version + 1
                if operation == "configuration":
                    assert restored == replace(
                        original, version=2, configuration_revision=2, enabled=False
                    )
                else:
                    assert restored == replace(
                        original, version=3, next_run_at_utc=NEXT, last_scheduled_at_utc=DUE
                    )
                    assert await repository.get_claim(original.id) is None
        after = {table: await rows(database, table) for table in NEW_COLUMNS}
        assert after["schedule_occurrences"] == before["schedule_occurrences"]
        if recorded:
            assert after == before
        else:
            assert after["schedules"] != before["schedules"]
            assert after["schedules"][0]["next_recurrence_json"] is None
    finally:
        await database.dispose()


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("schedules", "next_recurrence_json", None),
        ("schedules", "next_recurrence_json", "{}"),
        ("schedules", "next_recurrence_json", "null"),
        ("schedules", "next_recurrence_json", " []"),
        ("schedule_occurrences", "scheduled_recurrence_json", None),
        ("schedule_occurrences", "next_recurrence_json", "{broken"),
        ("schedule_occurrences", "recurrence_resolutions_json", None),
        ("schedule_occurrences", "recurrence_resolutions_json", "[]"),
    ],
)
@pytest.mark.asyncio
async def test_ac_12_raw_snapshot_tampering_fails_closed_without_repair(
    tmp_path: Path, table: str, column: str, value: str | None
):
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'tampered.db'}")
    try:
        await seed_recorded(database)
        original = await rows(database, table)
        async with database.engine.begin() as connection:
            await connection.execute(text(f"UPDATE {table} SET {column}=:value"), {"value": value})
        corrupted = await rows(database, table)
        assert corrupted == ({**original[0], column: value},)
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            with pytest.raises(SchedulePersistenceConflict) as failure:
                if table == "schedules":
                    await repository.get(schedule().id)
                else:
                    await repository.get_occurrence(occurrence().id)
            assert failure.value.code == (
                "schedule_tampered" if table == "schedules" else "occurrence_tampered"
            )
        assert await rows(database, table) == corrupted
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ac_12_populated_0008_upgrade_adds_only_null_evidence_and_preserves_digests(
    tmp_path: Path,
):
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        await seed_legacy(database)
        before = await legacy_state(database)
        assert await upgrade_database(database) == HEAD_REVISION == "0009"
        for table, added in NEW_COLUMNS.items():
            after = await rows(database, table)
            assert after == tuple({**row, **dict.fromkeys(added)} for row in before[table])
        async with database.engine.connect() as connection:
            assert await connection.run_sync(schema_matches_metadata)
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            assert await repository.get(schedule().id) == schedule(recorded=False)
            assert await repository.get_occurrence(occurrence().id) == occurrence(recorded=False)
            assert not (await repository.add_or_get(schedule(recorded=False))).inserted
            assert not (await repository.add_occurrence_or_get(occurrence(recorded=False))).inserted
            await session.commit()
        # Hydration and exact replay must not invent snapshots or rewrite old hashes.
        after = await legacy_state(database)
        assert await upgrade_database(database) == HEAD_REVISION
        assert await legacy_state(database) == after
        for table, added in NEW_COLUMNS.items():
            assert after[table] == tuple({**row, **dict.fromkeys(added)} for row in before[table])
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_ac_12_failed_additive_migration_rolls_back_all_columns_rows_and_version(
    tmp_path: Path,
):
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}")
    interrupted = []

    def fail_after_column(connection, cursor, statement, parameters, context, executemany):
        del connection, cursor, parameters, context, executemany
        if statement.startswith("ALTER TABLE schedule_occurrences ADD COLUMN next_recurrence_json"):
            interrupted.append(statement)
            raise RuntimeError("injected_recurrence_migration_failure")

    try:
        await seed_legacy(database)
        before = await legacy_state(database)
        event.listen(database.engine.sync_engine, "after_cursor_execute", fail_after_column)
        try:
            with pytest.raises(RuntimeError, match="injected_recurrence_migration_failure"):
                await upgrade_database(database)
        finally:
            event.remove(database.engine.sync_engine, "after_cursor_execute", fail_after_column)
        assert len(interrupted) == 1
        assert await legacy_state(database) == before
        assert await upgrade_database(database) == HEAD_REVISION
        async with database.session_factory() as session:
            repository = SQLAlchemyScheduleRepository(session)
            assert await repository.get(schedule().id) == schedule(recorded=False)
            assert await repository.get_occurrence(occurrence().id) == occurrence(recorded=False)
    finally:
        await database.dispose()
