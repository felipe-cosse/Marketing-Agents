"""OBJ-03 populated SQLite batch migration preserves linked history atomically."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionService,
)
from marketing_agents.application.services.manual_work_intake import ManualDryRunService
from marketing_agents.demos import DemoRunCommand, DemoRunService, build_demo_read_adapter
from marketing_agents.demos.email_signup_service import EmailSignupRunCommand, EmailSignupRunService
from marketing_agents.domain.enums import ApprovalDecisionKind, RunState
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import (
    DatabaseRuntime,
    SQLAlchemyApprovalRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditRepository,
    SQLAlchemyConnectorReceiptRepository,
    SQLAlchemyExecutionControlRepository,
    SQLAlchemyExternalActionRepository,
    SQLAlchemyInstanceConfigurationRepository,
    SQLAlchemyManualAdmissionUnitOfWorkFactory,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyScheduleRepository,
    SQLAlchemyWebhookReceiptRepository,
    SQLAlchemyWorkRepository,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.migrations import DatabaseMigrationError, upgrade_database
from marketing_agents.infrastructure.db.schema import schema_matches_metadata
from marketing_agents.infrastructure.manual_work import CompiledCatalogManualAdmissionResolver
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.workers.runtime.composition import RandomIds
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection

from tests.integration.runtime.test_del_05_process_composition import CATALOG, Clock
from tests.integration.runtime.test_obj_03_local_artifacts import _rows
from tests.support.identity import human_principal


async def _populated_previous(
    database: DatabaseRuntime,
) -> tuple[OrchestrationDependencies, tuple[str, str]]:
    """Use the frozen predecessor, not create_all or a renamed head database."""
    assert await upgrade_database(database, "0007") == "0007"
    catalog = compile_catalog(CATALOG)
    await seed_catalog(catalog, database, CroniterRecurrenceCalculator())
    key = DigestKey(bytes(range(32)))
    factories = SQLAlchemyRepositoryFactories(
        works=SQLAlchemyWorkRepository,
        runs=SQLAlchemyRunRepository,
        audits=SQLAlchemyAuditRepository,
        configurations=SQLAlchemyInstanceConfigurationRepository,
        approvals=lambda session: SQLAlchemyApprovalRepository(session, key),
        run_steps=SQLAlchemyRunStepRepository,
        external_actions=SQLAlchemyExternalActionRepository,
        connector_receipts=SQLAlchemyConnectorReceiptRepository,
        execution_control=lambda session: SQLAlchemyExecutionControlRepository(session, key),
        artifacts=SQLAlchemyArtifactRepository,
        schedules=SQLAlchemyScheduleRepository,
        webhook_receipts=SQLAlchemyWebhookReceiptRepository,
    )
    dependencies = OrchestrationDependencies(
        Clock(),
        RandomIds(),
        SQLAlchemyManualAdmissionUnitOfWorkFactory(database.session_factory, factories),
    )
    manual = ManualDryRunService(
        dependencies,
        key,
        CompiledCatalogManualAdmissionResolver(catalog, mock_connectors_active=True),
        current_catalog_hash=catalog.content_hash,
    )
    adapter = build_demo_read_adapter(catalog)
    operator = human_principal(
        actor_id="principal.obj03.migration.operator",
        roles=frozenset({"operator"}),
        scopes=frozenset({"manual-work:create"}),
    )
    social = await DemoRunService(dependencies, manual, catalog, adapter).run(
        DemoRunCommand(
            scenario_id="demo.social-media.content-draft.v1",
            correlation_id="corr.obj03.migration.social",
        ),
        operator,
    )
    email_service = EmailSignupRunService(dependencies, manual, catalog, adapter)
    email = await email_service.prepare(
        EmailSignupRunCommand(input_payload={}, correlation_id="corr.obj03.migration.email"),
        operator,
    )
    assert email.run.state is RunState.AWAITING_APPROVAL
    async with dependencies.unit_of_work() as uow:
        selection = await uow.approvals.get_current_authorization_set(email.run.id)
        assert selection is not None
        requests = await uow.approvals.list_current_set(
            email.run.id,
            selection.authorization_set.plan_hash,
            selection.authorization_set.proposal_revision,
        )
    assert len(requests) == 2
    for stored in requests:
        request = stored.request
        await ApprovalDecisionService(dependencies).decide(
            ApprovalDecisionCommand(
                request.id,
                request.generation,
                request.action_hash,
                ApprovalDecisionKind.APPROVE,
                "corr.obj03.migration.approve",
            ),
            principal=human_principal(
                actor_id="principal.obj03.migration.approver",
                scopes=frozenset({"approvals:decide", "scope.external-write"}),
            ),
        )
    completed = await email_service.resume(
        email.run.id, correlation_id="corr.obj03.migration.resume"
    )
    assert social.run.state is completed.run.state is RunState.COMPLETED
    assert completed.connector_calls == 2 and completed.approval_count == 2
    return dependencies, (social.run.id, email.run.id)


def _all_state(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        "rows": _rows(connection),
        "schema": tuple(
            tuple(row)
            for row in connection.execute(
                text("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
            )
        ),
        "indexes": {name: inspector.get_indexes(name) for name in inspector.get_table_names()},
        "foreign_keys": {
            # SQLite may enumerate the same constraints in a different order
            # after a rebuild. Retain every constraint field, not its ordinal.
            name: sorted(inspector.get_foreign_keys(name), key=repr)
            for name in inspector.get_table_names()
        },
    }


async def _state(database: DatabaseRuntime) -> dict[str, Any]:
    async with database.engine.connect() as connection:
        assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []
        return await connection.run_sync(_all_state)


def _assert_populated(state: dict[str, Any]) -> None:
    rows = state["rows"]
    assert rows["alembic_version"] == (("0007",),)
    assert len(rows["runs"]) == 2
    assert len(rows["run_steps"]) == 4
    assert len(rows["artifacts"]) == len(rows["execution_attempts"]) == 2
    assert len(rows["external_actions"]) == len(rows["connector_action_receipts"]) == 2
    assert len(rows["approval_requests"]) == len(rows["approval_decisions"]) == 2
    assert rows["run_step_dependencies"]
    assert rows["run_step_state_transitions"] and rows["run_state_transitions"]
    assert rows["audit_events"] and rows["approval_uses"]


async def _assert_upgrade_preserved(
    database: DatabaseRuntime,
    before: dict[str, Any],
    dependencies: OrchestrationDependencies,
    run_ids: tuple[str, str],
) -> None:
    assert await upgrade_database(database, "0008") == "0008"
    after = await _state(database)
    assert after["rows"] == {**before["rows"], "alembic_version": (("0008",),)}
    assert after["indexes"] == before["indexes"]
    assert after["foreign_keys"] == before["foreign_keys"]
    async with database.engine.connect() as connection:
        assert await connection.run_sync(schema_matches_metadata)
    # Hash- and lineage-verifying hydration proves preserved rows remain usable.
    async with dependencies.unit_of_work() as uow:
        for run_id in run_ids:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            assert await uow.run_steps.validate_plan_for_execution(run_id)
            artifacts = await uow.artifacts.list_for_run(run_id)
            assert len(artifacts) == 1 and artifacts[0].verify_payload()
    assert await upgrade_database(database, "0008") == "0008"
    assert await _state(database) == after


@pytest.mark.asyncio
async def test_obj_03_populated_artifact_migration_preserves_model_and_approved_write_history(
    tmp_path: Path,
) -> None:
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'populated.db'}")
    try:
        dependencies, run_ids = await _populated_previous(database)
        before = await _state(database)
        _assert_populated(before)
        await _assert_upgrade_preserved(database, before, dependencies, run_ids)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_obj_03_final_foreign_key_validation_rolls_back_completed_table_rebuild(
    tmp_path: Path,
) -> None:
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'invalid-fk.db'}")
    hit_connections: list[int] = []

    def corrupt_after_revision_advance(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del cursor, parameters, context, executemany
        if " ".join(statement.split()).startswith("UPDATE alembic_version SET version_num='0008'"):
            hit_connections.append(id(connection.connection.dbapi_connection))
            # All table rebuilds and the revision write really completed; the
            # migration owner's final FK check must still prevent this commit.
            connection.exec_driver_sql(
                "UPDATE runs SET work_item_id='work.obj03.missing' "
                "WHERE id=(SELECT min(id) FROM runs)"
            )

    try:
        dependencies, run_ids = await _populated_previous(database)
        before = await _state(database)
        _assert_populated(before)
        event.listen(
            database.engine.sync_engine, "after_cursor_execute", corrupt_after_revision_advance
        )
        try:
            with pytest.raises(DatabaseMigrationError, match="migration_foreign_key_violation"):
                await upgrade_database(database, "0008")
        finally:
            event.remove(
                database.engine.sync_engine, "after_cursor_execute", corrupt_after_revision_advance
            )
        assert len(hit_connections) == 1
        async with database.engine.connect() as connection:
            identity = await connection.run_sync(lambda sync: id(sync.connection.dbapi_connection))
            assert identity == hit_connections[0]
            assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert await _state(database) == before
        await _assert_upgrade_preserved(database, before, dependencies, run_ids)
    finally:
        await database.dispose()


@pytest.mark.parametrize("table", ["run_steps", "audit_events"])
@pytest.mark.parametrize("stage", ["copy", "drop"])
@pytest.mark.asyncio
async def test_obj_03_artifact_rebuild_failure_restores_data_schema_indexes_version_and_fk(
    tmp_path: Path,
    table: str,
    stage: str,
) -> None:
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}")
    hit_connections: list[int] = []

    def fail_after_sql(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del cursor, parameters, context, executemany
        normalized = " ".join(statement.split())
        target = f"INSERT INTO _alembic_tmp_{table} " if stage == "copy" else f"DROP TABLE {table}"
        if normalized.startswith(target):
            # after_cursor_execute injects after the copy/drop actually happened.
            hit_connections.append(id(connection.connection.dbapi_connection))
            raise RuntimeError("injected_populated_batch_failure")

    try:
        dependencies, run_ids = await _populated_previous(database)
        before = await _state(database)
        _assert_populated(before)
        event.listen(database.engine.sync_engine, "after_cursor_execute", fail_after_sql)
        try:
            with pytest.raises(RuntimeError, match="injected_populated_batch_failure"):
                await upgrade_database(database, "0008")
        finally:
            event.remove(database.engine.sync_engine, "after_cursor_execute", fail_after_sql)
        assert len(hit_connections) == 1
        async with database.engine.connect() as connection:
            identity = await connection.run_sync(lambda sync: id(sync.connection.dbapi_connection))
            assert identity == hit_connections[0], (
                "verify FK restoration on the failed owner's connection"
            )
            assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert await _state(database) == before
        await _assert_upgrade_preserved(database, before, dependencies, run_ids)
    finally:
        await database.dispose()
