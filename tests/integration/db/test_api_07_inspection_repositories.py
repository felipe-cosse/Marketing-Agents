"""API-07 bounded repository reads and exact inspection snapshots."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from marketing_agents.domain.enums import RunState
from marketing_agents.infrastructure.db.models import (
    ArtifactRecord,
    RunRecord,
)
from marketing_agents.infrastructure.db.repositories import (
    ArtifactPersistenceConflict,
    RunPersistenceInvariantError,
)
from sqlalchemy import update

from tests.integration.db.test_run_06_artifact_persistence import (
    NOW,
    _artifact,
    _persist,
    _prepare_run,
    _runtime,
    _uow_factory,
)


@pytest.mark.asyncio
async def test_api_07_run_page_binds_work_and_validates_complete_history(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "run-inspection.db")
    try:
        first_work, first = await _prepare_run(runtime, "api-07-run-a")
        second_work, second = await _prepare_run(runtime, "api-07-run-b")
        factory = _uow_factory(runtime)

        async with factory() as unit_of_work:
            page = await unit_of_work.runs.list_inspectable(
                state=RunState.PLANNED,
                instance_id=first_work.instance_id,
                workflow_id=first_work.workflow_id,
                created_at_from=first.run.created_at,
                created_at_to=second.run.created_at,
                before_created_at=None,
                before_run_id=None,
                limit=2,
            )
        assert tuple(item.run.id for item in page) == tuple(
            sorted((first.run.id, second.run.id), reverse=True)
        )
        assert {item.work_item.id for item in page} == {
            first_work.id,
            second_work.id,
        }
        assert all(item.run.work_item_id == item.work_item.id for item in page)
        assert all(len(item.transitions) == item.run.version for item in page)

        boundary = page[0]
        async with factory() as unit_of_work:
            remaining = await unit_of_work.runs.list_inspectable(
                state=RunState.PLANNED,
                instance_id=first_work.instance_id,
                workflow_id=first_work.workflow_id,
                created_at_from=None,
                created_at_to=None,
                before_created_at=boundary.run.created_at,
                before_run_id=boundary.run.id,
                limit=2,
            )
            exact = await unit_of_work.runs.get_inspectable(first.run.id)
        assert tuple(item.run.id for item in remaining) == (page[1].run.id,)
        assert exact is not None
        assert exact.work_item == first_work

        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(RunRecord)
                .where(RunRecord.id == first.run.id)
                .values(version=RunRecord.version + 1)
            )
        async with factory() as unit_of_work:
            with pytest.raises(RunPersistenceInvariantError, match="history"):
                await unit_of_work.runs.get_inspectable(first.run.id)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_07_sealed_plan_and_artifact_keyset_page_fail_closed(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "artifact-inspection.db")
    try:
        work_item, persisted = await _prepare_run(runtime, "api-07-artifacts")
        factory = _uow_factory(runtime)
        created_at = NOW + timedelta(hours=2)
        artifacts = tuple(
            _artifact(
                artifact_id=f"artifact.api-07.{index}",
                work_item=work_item,
                persisted=persisted,
                payload={"ordinal": index},
                created_at=created_at,
            )
            for index in range(1, 4)
        )
        for artifact in artifacts:
            await _persist(factory, artifact)

        async with factory() as unit_of_work:
            plan = await unit_of_work.run_steps.get_inspectable_plan(persisted.run.id)
            first_page = await unit_of_work.artifacts.list_for_run_page(
                persisted.run.id,
                after_created_at=None,
                after_artifact_id=None,
                limit=2,
            )
        assert plan is not None
        assert plan.plan == persisted.plan
        assert plan.steps == persisted.steps
        assert tuple(item.artifact.provenance.artifact_id for item in first_page) == (
            "artifact.api-07.1",
            "artifact.api-07.2",
        )
        assert all(item.step.id == item.artifact.provenance.step_id for item in first_page)

        boundary = first_page[-1].artifact.provenance
        async with factory() as unit_of_work:
            second_page = await unit_of_work.artifacts.list_for_run_page(
                persisted.run.id,
                after_created_at=boundary.created_at,
                after_artifact_id=boundary.artifact_id,
                limit=2,
            )
        assert tuple(item.artifact.provenance.artifact_id for item in second_page) == (
            "artifact.api-07.3",
        )

        async with runtime.session_factory() as session, session.begin():
            await session.execute(
                update(ArtifactRecord)
                .where(ArtifactRecord.id == "artifact.api-07.3")
                .values(payload={"ordinal": "tampered"})
            )
        async with factory() as unit_of_work:
            with pytest.raises(ArtifactPersistenceConflict, match="fingerprint"):
                await unit_of_work.artifacts.get_inspectable("artifact.api-07.3")
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_07_repository_page_limits_and_cursor_pairs_are_bounded(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "bounds.db")
    try:
        work_item, persisted = await _prepare_run(runtime, "api-07-bounds")
        factory = _uow_factory(runtime)
        async with factory() as unit_of_work:
            with pytest.raises(ValueError, match="1 through 101"):
                await unit_of_work.runs.list_inspectable(
                    state=None,
                    instance_id=None,
                    workflow_id=None,
                    created_at_from=None,
                    created_at_to=None,
                    before_created_at=None,
                    before_run_id=None,
                    limit=102,
                )
            with pytest.raises(ValueError, match="boundary"):
                await unit_of_work.artifacts.list_for_run_page(
                    persisted.run.id,
                    after_created_at=persisted.run.created_at,
                    after_artifact_id=None,
                    limit=1,
                )
            missing = await unit_of_work.runs.list_inspectable(
                state=None,
                instance_id="instance.api-07.missing",
                workflow_id=work_item.workflow_id,
                created_at_from=None,
                created_at_to=None,
                before_created_at=None,
                before_run_id=None,
                limit=1,
            )
        assert missing == ()
    finally:
        await runtime.dispose()
