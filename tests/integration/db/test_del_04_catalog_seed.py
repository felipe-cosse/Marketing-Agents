"""DEL-04 exact, atomic, release-aware catalog import and preserved deployment overrides."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from marketing_agents.domain.enums import MisfirePolicy, TriggerKind
from marketing_agents.domain.instance_configuration import (
    InstanceConnectorBinding,
    InstanceSchedule,
    InstanceTriggerBinding,
)
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog import seed as seed_module
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.infrastructure.catalog.seed import CatalogSeedError, seed_catalog
from marketing_agents.infrastructure.db import Base, DatabaseRuntime, create_database_runtime
from marketing_agents.infrastructure.db.migrations import HEAD_REVISION, upgrade_database
from marketing_agents.infrastructure.db.models.catalog import (
    AgentInstanceRecord,
    AgentTemplateCapabilityRecord,
    AgentTemplateRecord,
    CatalogCurrentReleaseRecord,
    CatalogReleaseRecord,
    DepartmentRecord,
)
from marketing_agents.infrastructure.db.models.instance_configuration import (
    AgentInstanceConfigurationRecord,
)
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.infrastructure.db.models.step import RunPlanRecord
from marketing_agents.infrastructure.db.models.work import WorkItemRecord
from marketing_agents.infrastructure.db.repositories.instance_configuration import (
    SQLAlchemyInstanceConfigurationRepository,
)
from marketing_agents.infrastructure.instance_configuration_constraints import (
    registered_mock_bindings,
)
from marketing_agents.infrastructure.scheduling.cron_recurrence import CroniterRecurrenceCalculator
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[3]
RECURRENCE = CroniterRecurrenceCalculator()
EXPECTED_COUNTS = {
    "departments": 5,
    "function_teams": 12,
    "tool_capabilities": 22,
    "approval_policies": 2,
    "agent_templates": 36,
    "agent_template_capabilities": 69,
    "agent_template_trigger_kinds": 71,
    "agent_instances": 43,
    "catalog_releases": 1,
    "catalog_current_release": 1,
    "agent_instance_configs": 43,
}


@pytest.fixture(scope="module")
def catalog() -> CompiledCatalog:
    return compile_catalog(ROOT / "catalog" / "v1")


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[DatabaseRuntime]:
    value = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    try:
        assert await upgrade_database(value) == HEAD_REVISION
        yield value
    finally:
        await value.dispose()


async def _snapshot(runtime: DatabaseRuntime) -> dict[str, list[dict[str, Any]]]:
    async with runtime.session_factory() as session:
        result = {}
        for table in Base.metadata.sorted_tables:
            rows = await session.execute(select(table).order_by(*table.primary_key.columns))
            result[table.name] = [dict(row._mapping) for row in rows]
        return result


def _changed_catalog(
    tmp_path: Path,
    *,
    version: str = "1.0.1",
    swap_order: bool = False,
    remove_schedules: bool = False,
) -> CompiledCatalog:
    directory = tmp_path / "catalog-copy"
    shutil.copytree(ROOT / "catalog" / "v1", directory)
    manifest = directory / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace("1.0.0", version), encoding="utf-8")
    departments = directory / "departments.yaml"
    changed = departments.read_text().replace(
        "display_name: Social media", "display_name: Social content"
    )
    if swap_order:
        changed = changed.replace("display_order: 10", "display_order: TEMP")
        changed = changed.replace("display_order: 20", "display_order: 10")
        changed = changed.replace("display_order: TEMP", "display_order: 20")
    departments.write_text(changed, encoding="utf-8")
    if remove_schedules:
        for template_path in (directory / "templates").glob("*.yaml"):
            template_path.write_text(
                template_path.read_text().replace(", schedule", ""), encoding="utf-8"
            )
    return compile_catalog(directory)


async def test_del_04_seed_exact_projection_restart_and_repeat(
    runtime: DatabaseRuntime, catalog: CompiledCatalog
) -> None:
    first = await seed_catalog(catalog, runtime, RECURRENCE)
    assert first.counts == EXPECTED_COUNTS
    assert (first.inserted, first.updated, first.unchanged, first.deleted) == (262, 0, 0, 0)
    assert (first.configuration_inserted, first.configuration_preserved) == (43, 0)
    assert first.catalog_content_hash == catalog.content_hash
    before = await _snapshot(runtime)
    await runtime.engine.dispose()
    second = await seed_catalog(catalog, runtime, RECURRENCE)
    assert (second.inserted, second.updated, second.unchanged, second.deleted) == (0, 0, 262, 0)
    assert (second.configuration_inserted, second.configuration_preserved) == (0, 43)
    assert await _snapshot(runtime) == before
    async with runtime.session_factory() as session:
        instances = (await session.scalars(select(AgentInstanceRecord))).all()
        assert {item.id: item.template_id for item in instances} == {
            item.id: item.template_id for item in catalog.instances
        }
        templates = (await session.scalars(select(AgentTemplateRecord))).all()
        for template in templates:
            assert template.system_prompt_text == catalog.prompt_text_by_template[template.id]
            assert json.loads(template.input_schema_json) == dict(
                catalog.input_schema_by_template[template.id]
            )
            assert json.loads(template.output_schema_json) == dict(
                catalog.output_schema_by_template[template.id]
            )


async def test_del_04_check_has_no_write_and_missing_database_is_not_created(
    runtime: DatabaseRuntime, catalog: CompiledCatalog, tmp_path: Path
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    before = await _snapshot(runtime)
    statements: list[str] = []

    def collect(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        del connection, cursor, parameters, context, many
        statements.append(statement.strip().split()[0].upper())

    event.listen(Engine, "before_cursor_execute", collect)
    try:
        checked = await seed_catalog(catalog, runtime, RECURRENCE, check=True)
    finally:
        event.remove(Engine, "before_cursor_execute", collect)
    assert checked.configuration_preserved == 43
    assert "SELECT" in statements
    assert set(statements) <= {"SELECT", "BEGIN"}
    assert await _snapshot(runtime) == before
    path = tmp_path / "absent.db"
    absent = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    try:
        with pytest.raises(CatalogSeedError, match="does not exist") as failure:
            await seed_catalog(catalog, absent, RECURRENCE, check=True)
        assert failure.value.code == "seed_projection_drift"
        assert not path.exists()
    finally:
        await absent.dispose()


async def test_del_04_check_owns_no_create_connection_when_file_disappears_after_preflight(
    runtime: DatabaseRuntime,
    catalog: CompiledCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    await runtime.dispose()
    path = Path(str(runtime.engine.url.database))
    original = seed_module._check_runtime

    def remove_after_preflight(value: DatabaseRuntime) -> DatabaseRuntime:
        checked = original(value)
        path.unlink()
        return checked

    monkeypatch.setattr(seed_module, "_check_runtime", remove_after_preflight)
    with pytest.raises(CatalogSeedError) as failure:
        await seed_catalog(catalog, runtime, RECURRENCE, check=True)
    assert failure.value.code == "seed_persistence_failed"
    assert not path.exists()
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


async def test_del_04_check_query_only_connection_rejects_accidental_mutation(
    runtime: DatabaseRuntime,
    catalog: CompiledCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    before = await _snapshot(runtime)

    async def accidental_write(session: Any, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        await session.execute(text("UPDATE departments SET display_name = 'forbidden'"))

    monkeypatch.setattr(seed_module, "_apply_projection", accidental_write)
    with pytest.raises(CatalogSeedError) as failure:
        await seed_catalog(catalog, runtime, RECURRENCE, check=True)
    assert failure.value.code == "seed_persistence_failed"
    assert await _snapshot(runtime) == before


async def test_del_04_check_never_connects_caller_pool_or_leaves_sidecars(
    runtime: DatabaseRuntime,
    catalog: CompiledCatalog,
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    await runtime.dispose()
    path = Path(str(runtime.engine.url.database))
    before = path.read_bytes()
    files = sorted(item.name for item in path.parent.iterdir())

    def forbidden_connection(*args: Any) -> None:
        del args
        raise AssertionError("check must not connect the caller's writable pool")

    event.listen(runtime.engine.sync_engine, "connect", forbidden_connection)
    try:
        await seed_catalog(catalog, runtime, RECURRENCE, check=True)
    finally:
        event.remove(runtime.engine.sync_engine, "connect", forbidden_connection)
    assert sorted(item.name for item in path.parent.iterdir()) == files
    assert path.read_bytes() == before


@pytest.mark.parametrize("drift", ["field", "edge", "current", "configuration"])
async def test_del_04_check_detects_exact_drift_and_seed_repairs(
    runtime: DatabaseRuntime, catalog: CompiledCatalog, drift: str
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    async with runtime.session_factory() as session, session.begin():
        if drift == "field":
            await session.execute(
                update(DepartmentRecord)
                .where(DepartmentRecord.id == "dept.email")
                .values(display_name="Drift")
            )
        elif drift == "edge":
            edge = await session.scalar(select(AgentTemplateCapabilityRecord).limit(1))
            assert edge is not None
            await session.delete(edge)
        elif drift == "current":
            current = await session.get(CatalogCurrentReleaseRecord, 1)
            assert current is not None
            await session.delete(current)
        else:
            configuration = await session.get(
                AgentInstanceConfigurationRecord, catalog.instances[0].id
            )
            assert configuration is not None
            await session.delete(configuration)
    before = await _snapshot(runtime)
    with pytest.raises(CatalogSeedError) as failure:
        await seed_catalog(catalog, runtime, RECURRENCE, check=True)
    assert failure.value.code == "seed_projection_drift"
    assert await _snapshot(runtime) == before
    repaired = await seed_catalog(catalog, runtime, RECURRENCE)
    assert repaired.inserted + repaired.updated + repaired.configuration_inserted > 0
    await seed_catalog(catalog, runtime, RECURRENCE, check=True)


async def test_del_04_reseed_preserves_all_local_configuration_and_schedule(
    runtime: DatabaseRuntime, catalog: CompiledCatalog
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    templates = {item.id: item for item in catalog.templates}
    target = next(
        item
        for item in catalog.instances
        if "schedule" in templates[item.template_id].supported_trigger_types
        and registered_mock_bindings(catalog, item.id)
    )
    schedule = InstanceSchedule("0 12 * * *", "America/Los_Angeles", MisfirePolicy.RUN_ONCE, 300)
    async with runtime.session_factory() as session, session.begin():
        repository = SQLAlchemyInstanceConfigurationRepository(session)
        previous = await repository.get(target.id)
        assert previous is not None
        changed = replace(
            previous,
            enabled=False,
            variant_label="Local operator settings",
            connector_bindings={
                family: InstanceConnectorBinding(family, binding, False)
                for family, binding in registered_mock_bindings(catalog, target.id).items()
            },
            trigger_bindings=(
                InstanceTriggerBinding(
                    kind=TriggerKind.SCHEDULE,
                    cron=schedule.cron,
                    timezone=schedule.timezone,
                    misfire_policy=schedule.misfire_policy,
                    misfire_grace_seconds=schedule.misfire_grace_seconds,
                ),
            ),
            schedule=schedule,
            configuration_revision=2,
        )
        assert await repository.compare_and_swap(previous, changed)
    before = await _snapshot(runtime)
    await seed_catalog(catalog, runtime, RECURRENCE)
    await seed_catalog(catalog, runtime, RECURRENCE, check=True)
    assert await _snapshot(runtime) == before
    async with runtime.session_factory() as session:
        assert await SQLAlchemyInstanceConfigurationRepository(session).get(target.id) == changed


async def test_del_04_new_release_preserves_history_and_can_swap_display_order(
    runtime: DatabaseRuntime, catalog: CompiledCatalog, tmp_path: Path
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    now = datetime(2026, 9, 8, tzinfo=UTC)
    async with runtime.session_factory() as session, session.begin():
        session.add(
            WorkItemRecord(
                id="work.del-04.history",
                source="manual",
                event_id="event.del-04.history",
                agent_instance_id=catalog.instances[0].id,
                trigger_id="trigger.manual",
                workflow_id="workflow.social",
                mode="dry_run",
                configuration_revision=1,
                admitted_payload={"topic": "History"},
                redacted_input_projection={"topic": "History"},
                input_schema_id=catalog.templates[0].input_schema_id,
                input_schema_hash="schema-sha256-v1:" + "a" * 64,
                input_classification="internal",
                input_projection_created_at=now,
                input_projection_expires_at=now + timedelta(days=1),
                input_projection_integrity_digest="b" * 64,
                input_digest="c" * 64,
                admission_digest="d" * 64,
                digest_key_version="key-version-sha256-v1:" + "e" * 67,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            RunRecord(
                id="run.del-04.history",
                work_item_id="work.del-04.history",
                state="planned",
                catalog_hash=catalog.content_hash,
                configuration_revision=1,
                approval_required=False,
                terminal_reason_code=None,
                created_at=now,
                updated_at=now,
                version=1,
            )
        )
        await session.flush()
        session.add(
            RunPlanRecord(
                run_id="run.del-04.history",
                plan_hash="f" * 64,
                workflow_id="workflow.social",
                workflow_version=1,
                workflow_definition_hash="a" * 64,
                catalog_content_hash=catalog.content_hash,
                graph_hash="b" * 64,
                routing_hash="c" * 64,
                approval_required=False,
                step_count=1,
                runtime_policy_snapshot=catalog.templates[0].budget_policy.model_dump(mode="json"),
                runtime_policy_hash="d" * 64,
                created_at=now,
            )
        )
    before = await _snapshot(runtime)
    changed = _changed_catalog(tmp_path, swap_order=True)
    result = await seed_catalog(changed, runtime, RECURRENCE)
    assert result.counts == {**EXPECTED_COUNTS, "catalog_releases": 2}
    assert result.configuration_inserted == 0 and result.configuration_preserved == 43
    after = await _snapshot(runtime)
    assert before["catalog_releases"][0] in after["catalog_releases"]
    assert after["agent_instance_configs"] == before["agent_instance_configs"]
    assert after["work_items"] == before["work_items"]
    assert after["runs"] == before["runs"]
    assert after["run_plans"] == before["run_plans"]
    async with runtime.session_factory() as session:
        current = await session.get(CatalogCurrentReleaseRecord, 1)
        assert current is not None and current.content_hash == changed.content_hash
        department = await session.get(DepartmentRecord, "dept.social-media")
        assert department is not None
        assert (department.display_name, department.display_order) == ("Social content", 20)
    await seed_catalog(changed, runtime, RECURRENCE, check=True)


async def test_del_04_same_version_changed_source_is_rejected_without_writes(
    runtime: DatabaseRuntime, catalog: CompiledCatalog, tmp_path: Path
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    before = await _snapshot(runtime)
    changed = _changed_catalog(tmp_path, version="1.0.0")
    with pytest.raises(CatalogSeedError) as failure:
        await seed_catalog(changed, runtime, RECURRENCE)
    assert failure.value.code == "seed_release_conflict"
    assert await _snapshot(runtime) == before


@pytest.mark.parametrize("has_local_schedule", [False, True])
async def test_del_04_new_release_edge_changes_reject_incompatible_local_override_atomically(
    runtime: DatabaseRuntime,
    catalog: CompiledCatalog,
    tmp_path: Path,
    has_local_schedule: bool,
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    if has_local_schedule:
        templates = {item.id: item for item in catalog.templates}
        target = next(
            item
            for item in catalog.instances
            if "schedule" in templates[item.template_id].supported_trigger_types
        )
        schedule = InstanceSchedule("0 12 * * *", "UTC", MisfirePolicy.SKIP, 60)
        async with runtime.session_factory() as session, session.begin():
            repository = SQLAlchemyInstanceConfigurationRepository(session)
            previous = await repository.get(target.id)
            assert previous is not None
            changed_configuration = replace(
                previous,
                configuration_revision=2,
                schedule=schedule,
                trigger_bindings=(
                    InstanceTriggerBinding(
                        kind=TriggerKind.SCHEDULE,
                        cron=schedule.cron,
                        timezone=schedule.timezone,
                        misfire_policy=schedule.misfire_policy,
                        misfire_grace_seconds=schedule.misfire_grace_seconds,
                    ),
                ),
            )
            assert await repository.compare_and_swap(previous, changed_configuration)
    before = await _snapshot(runtime)
    changed = _changed_catalog(tmp_path, remove_schedules=True)
    if has_local_schedule:
        with pytest.raises(CatalogSeedError) as failure:
            await seed_catalog(changed, runtime, RECURRENCE)
        assert failure.value.code == "seed_trigger_unsupported"
        assert await _snapshot(runtime) == before
    else:
        result = await seed_catalog(changed, runtime, RECURRENCE)
        assert result.deleted > 0
        assert result.counts["agent_template_trigger_kinds"] == 71 - result.deleted
        await seed_catalog(changed, runtime, RECURRENCE, check=True)


@pytest.mark.parametrize("change", ["remap", "extra", "history"])
async def test_del_04_unsafe_identity_or_release_corruption_fails_closed(
    runtime: DatabaseRuntime, catalog: CompiledCatalog, change: str
) -> None:
    await seed_catalog(catalog, runtime, RECURRENCE)
    async with runtime.session_factory() as session, session.begin():
        if change == "remap":
            await session.execute(
                update(AgentInstanceRecord)
                .where(AgentInstanceRecord.id == catalog.instances[0].id)
                .values(source_ordinal=99)
            )
        elif change == "extra":
            session.add(
                DepartmentRecord(
                    id="dept.unexpected",
                    display_name="Unexpected",
                    display_order=9999,
                    snapshot_json="{}",
                    release_hash=catalog.content_hash,
                )
            )
        else:
            await session.execute(
                update(CatalogReleaseRecord).values(snapshot_json='{"tampered":true}')
            )
    before = await _snapshot(runtime)
    with pytest.raises(CatalogSeedError) as failure:
        await seed_catalog(catalog, runtime, RECURRENCE)
    assert failure.value.code == (
        "seed_release_conflict" if change == "history" else "seed_identity_change"
    )
    assert await _snapshot(runtime) == before


@pytest.mark.parametrize("fault_table", ["agent_templates", "agent_instance_configs"])
async def test_del_04_seed_rolls_back_all_catalog_and_config_rows_after_fault(
    runtime: DatabaseRuntime, catalog: CompiledCatalog, fault_table: str
) -> None:
    before = await _snapshot(runtime)
    inserted_configs = 0

    def fault(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        nonlocal inserted_configs
        del connection, cursor, parameters, context, many
        if statement.startswith(f"INSERT INTO {fault_table}"):
            inserted_configs += 1
            if inserted_configs == (1 if fault_table == "agent_templates" else 5):
                raise RuntimeError("injected secret database details")

    event.listen(runtime.engine.sync_engine, "before_cursor_execute", fault)
    try:
        with pytest.raises(CatalogSeedError) as failure:
            await seed_catalog(catalog, runtime, RECURRENCE)
    finally:
        event.remove(runtime.engine.sync_engine, "before_cursor_execute", fault)
    assert failure.value.code == "seed_persistence_failed"
    assert "secret" not in str(failure.value)
    assert inserted_configs == (1 if fault_table == "agent_templates" else 5)
    assert await _snapshot(runtime) == before


async def test_del_04_independent_concurrent_seeds_converge_exactly_once(
    runtime: DatabaseRuntime, catalog: CompiledCatalog
) -> None:
    independent = create_database_runtime(str(runtime.engine.url))
    try:
        first, second = await asyncio.gather(
            seed_catalog(catalog, runtime, RECURRENCE),
            seed_catalog(catalog, independent, RECURRENCE),
        )
        assert sorted((first.inserted, second.inserted)) == [0, 262]
        assert sorted((first.configuration_inserted, second.configuration_inserted)) == [0, 43]
        assert first.counts == second.counts == EXPECTED_COUNTS
        await seed_catalog(catalog, runtime, RECURRENCE, check=True)
    finally:
        await independent.dispose()


async def test_del_04_invalid_compiled_hash_is_rejected_before_connection(
    catalog: CompiledCatalog, tmp_path: Path
) -> None:
    database = tmp_path / "never-opened.db"
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{database}")
    try:
        with pytest.raises(CatalogSeedError) as failure:
            await seed_catalog(
                replace(catalog, content_hash="catalog-sha256-v1:" + "0" * 64), runtime, RECURRENCE
            )
        assert failure.value.code == "seed_catalog_invalid"
        assert not database.exists()
    finally:
        await runtime.dispose()
