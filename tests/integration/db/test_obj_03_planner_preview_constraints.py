"""OBJ-03 frozen SQLite constraints independently reject preview authority drift."""

from pathlib import Path
from typing import Any

import pytest
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.migrations import upgrade_database
from marketing_agents.infrastructure.db.models.audit import AuditEventRecord
from marketing_agents.infrastructure.db.models.step import RunStepRecord
from marketing_agents.workers.runtime.composition import build_runtime
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.db.test_obj_03_artifact_migration import _populated_previous, _state
from tests.integration.runtime.test_del_05_process_composition import Clock, _installation
from tests.integration.runtime.test_obj_03_local_artifacts import _ready, _snapshot

PREVIEW = {
    "kind": "planner.proposal-preview.v1",
    "connector_family": "planner-output",
    "capability_id": "cap.newsletter.subscribe",
}
INVALID: tuple[dict[str, Any], ...] = (
    {"kind": "artifact.transform"},
    {"connector_family": "artifact"},
    {"capability_id": "cap.artifact.transform-deterministic"},
    {"effect": "write"},
    {"idempotency_support": "required"},
    {"request_schema_id": None},
    {"result_schema_id": None, "result_schema_hash": None},
    {"result_schema_hash": None},
    {"binding_id": "binding.preview.forbidden"},
    {"binding_configuration_revision": 1},
    {"timeout_seconds": 30},
    {"request_redaction_fields": ["/private"]},
    {"result_redaction_fields": ["/private"]},
    {"data_classification": "personal"},
    {"approval_required_roles": ["approver"]},
    {"approval_required_scopes": ["scope.external-write"]},
    {"approval_expires_after_seconds": 300},
    {"approval_allow_self_approval": False},
    {"state": "awaiting_approval"},
    {"state": "rejected", "terminal_reason_code": "rejected"},
)


@pytest.mark.asyncio
async def test_obj_03_database_preview_shape_accepts_only_exact_nonexecuting_metadata(
    tmp_path: Path,
) -> None:
    runtime = await build_runtime(await _installation(tmp_path), clock=Clock())
    try:
        _, step_id = await _ready(runtime)
        before = await _snapshot(runtime.database)
        statement = update(RunStepRecord).where(RunStepRecord.id == step_id)
        for capability in (
            "cap.newsletter.subscribe",
            "cap.newsletter.unsubscribe",
            "cap.events.enroll-attendee",
            "cap.messaging.send-message",
        ):
            async with runtime.database.engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    await connection.execute(
                        statement.values(**{**PREVIEW, "capability_id": capability})
                    )
                    assert (
                        await connection.scalar(
                            select(RunStepRecord.kind).where(RunStepRecord.id == step_id)
                        )
                        == PREVIEW["kind"]
                    )
                finally:
                    await transaction.rollback()
            assert await _snapshot(runtime.database) == before
        for changes in INVALID:
            async with runtime.database.engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    await connection.execute(statement.values(**PREVIEW))
                    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                        await connection.execute(statement.values(**changes))
                finally:
                    await transaction.rollback()
            assert await _snapshot(runtime.database) == before, changes
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_database_preview_audit_cannot_claim_a_provider_attempt_or_missing_output(
    tmp_path: Path,
) -> None:
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'preview-audit.db'}")
    try:
        await _populated_previous(database)
        await upgrade_database(database, "0008")
        before = await _state(database)
        async with database.engine.connect() as connection:
            event_id = await connection.scalar(
                select(AuditEventRecord.id)
                .where(AuditEventRecord.event_type == "artifact.persisted")
                .order_by(AuditEventRecord.id)
                .limit(1)
            )
        assert event_id is not None
        statement = update(AuditEventRecord).where(AuditEventRecord.id == event_id)
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    statement.values(event_type="artifact.previewed", attempt_id=None)
                )
                assert (
                    await connection.scalar(
                        select(AuditEventRecord.event_type).where(AuditEventRecord.id == event_id)
                    )
                    == "artifact.previewed"
                )
            finally:
                await transaction.rollback()
        assert await _state(database) == before
        for changes in (
            {},  # The predecessor row retains its real, FK-valid provider attempt.
            {"attempt_id": None, "artifact_id": None},
            {"attempt_id": None, "step_id": None},
            {"attempt_id": None, "mutation_version": 2},
            {"attempt_id": None, "new_state": "succeeded"},
        ):
            async with database.engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                        await connection.execute(
                            statement.values(event_type="artifact.previewed", **changes)
                        )
                finally:
                    await transaction.rollback()
            assert await _state(database) == before, changes
    finally:
        await database.dispose()
