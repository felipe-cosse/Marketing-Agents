"""RUN-06: immutable schema-bound artifact and provenance persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.repositories import ArtifactRepository
from marketing_agents.application.services import (
    AuditedPlanPersistenceService,
    IdempotentWorkRunReceiptService,
    RunLifecycleService,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.provenance import (
    ArtifactEnvelope,
    ProvenanceSource,
    ProviderVersion,
)
from marketing_agents.domain.run_lifecycle import NoRunTransitionContext, RunLifecycleCommand
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    SQLAlchemyAuditRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models.artifact import (
    ArtifactParentRecord,
    ArtifactRecord,
)
from marketing_agents.infrastructure.db.repositories import (
    ArtifactPersistenceConflict,
    SQLAlchemyArtifactRepository,
    SQLAlchemyWorkRepository,
)
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from tests.support.execution_control import execution_control_repository
from tests.support.incoming_work import TEST_CATALOG_HASH, validate_incoming_for_test
from tests.support.orch_09_planning import build_read_only_plan

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


class IncrementingClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class IncrementingIds:
    def __init__(self, label: str) -> None:
        self._next = 0
        self._label = label

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.run-06.{self._label}.{self._next:04d}"


def _audit_context(label: str) -> AuditContext:
    return AuditContext.system("test.run-06", correlation_id=f"request.{label}")


def _envelope(event_id: str) -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id=event_id,
        instance_id="instance.run-06.target",
        trigger_id="trigger.run-06.manual",
        workflow_id="workflow.run-06.artifacts",
        mode=WorkMode.MOCK_EXECUTION,
        brief_id=None,
        brief_revision=None,
        configuration_revision=1,
        admitted_payload={"topic": "bounded artifact persistence"},
    )


def _uow_factory(runtime: DatabaseRuntime) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            run_steps=SQLAlchemyRunStepRepository,
            execution_control=execution_control_repository,
            artifacts=SQLAlchemyArtifactRepository,
        ),
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


async def _prepare_run(
    runtime: DatabaseRuntime,
    event_id: str,
    *,
    dependent_steps: bool = False,
    parallel_steps: bool = False,
):
    dependencies = OrchestrationDependencies(
        IncrementingClock(),
        IncrementingIds(event_id),
        _uow_factory(runtime),
    )
    incoming = _envelope(event_id)
    receipt = await IdempotentWorkRunReceiptService(
        dependencies,
        DigestKey(bytes(range(32))),
        current_catalog_hash=TEST_CATALOG_HASH,
    ).receive(
        validate_incoming_for_test(incoming),
        audit_context=_audit_context(f"{event_id}.receive"),
    )
    validated = await RunLifecycleService(dependencies).advance(
        receipt.run.id,
        receipt.run.version,
        RunLifecycleCommand.MARK_VALIDATED,
        NoRunTransitionContext(),
        audit_context=_audit_context(f"{event_id}.validate"),
    )
    plan, graph, routing = build_read_only_plan(
        run_id=validated.run.id,
        workflow_id=incoming.workflow_id,
        target_instance_id=incoming.instance_id,
        configuration_revision=incoming.configuration_revision,
        catalog_hash=validated.run.catalog_hash,
        dependent_steps=dependent_steps,
        parallel_steps=parallel_steps,
    )
    persisted = await AuditedPlanPersistenceService(dependencies).persist(
        plan,
        graph,
        routing,
        expected_run_version=validated.run.version,
        audit_context=_audit_context(f"{event_id}.plan"),
    )
    return receipt.work_item, persisted


def _artifact(
    *,
    artifact_id: str,
    work_item,
    persisted,
    step_key: str = "read",
    payload: dict[str, object] | None = None,
    created_at: datetime = NOW + timedelta(hours=1),
    parents: tuple[ArtifactEnvelope, ...] = (),
    output_schema_id: str | None = None,
    output_schema_hash: str | None = None,
) -> ArtifactEnvelope:
    step = next(item for item in persisted.steps if item.key == step_key)
    sources = [
        ProvenanceSource(
            kind="work_input",
            source_id=work_item.id,
            integrity_digest=work_item.input_digest,
            classification=DataClassification.INTERNAL,
        )
    ]
    sources.extend(
        ProvenanceSource(
            kind="parent_artifact",
            source_id=parent.provenance.artifact_id,
            integrity_digest=parent.provenance.payload_hash,
            classification=parent.provenance.classification,
        )
        for parent in parents
    )
    assert step.result_schema_id is not None and step.result_schema_hash is not None
    return ArtifactEnvelope.create(
        payload=payload or {"draft": artifact_id},
        artifact_id=artifact_id,
        work_item_id=work_item.id,
        run_id=persisted.run.id,
        step_id=step.id,
        workflow_id=persisted.plan.workflow_id,
        workflow_version=f"v{persisted.plan.workflow_version}",
        template_id=step.template_id,
        instance_id=step.selected_instance_id,
        admitted_input_digest=work_item.input_digest,
        catalog_hash=persisted.run.catalog_hash,
        instance_config_revision=step.configuration_revision,
        sources=tuple(sources),
        parent_artifact_ids=tuple(parent.provenance.artifact_id for parent in parents),
        providers=(
            ProviderVersion(
                provider_kind="llm",
                mode="mock",
                name="deterministic-test-provider",
                version="v1",
            ),
        ),
        output_schema_id=output_schema_id or step.result_schema_id,
        output_schema_version="v1",
        output_schema_hash=output_schema_hash or step.result_schema_hash,
        created_at=created_at,
        classification=DataClassification.INTERNAL,
    )


async def _persist(
    factory: SQLAlchemyUnitOfWorkFactory,
    artifact: ArtifactEnvelope,
):
    async with factory() as unit_of_work:
        result = await unit_of_work.artifacts.add_or_get(artifact)
        await unit_of_work.commit()
    return result


@pytest.mark.asyncio
async def test_run_06_artifact_round_trip_exact_replay_conflict_and_stable_run_order(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "round-trip.db")
    work_item, persisted = await _prepare_run(runtime, "round-trip")
    factory = _uow_factory(runtime)
    same_time = NOW + timedelta(hours=1)
    artifact_z = _artifact(
        artifact_id="artifact.z",
        work_item=work_item,
        persisted=persisted,
        created_at=same_time,
    )
    artifact_a = _artifact(
        artifact_id="artifact.a",
        work_item=work_item,
        persisted=persisted,
        created_at=same_time,
    )
    try:
        assert (await _persist(factory, artifact_z)).inserted is True
        assert (await _persist(factory, artifact_a)).inserted is True

        replay = await _persist(factory, artifact_a)
        assert replay.inserted is False
        assert replay.artifact == artifact_a
        assert replay.artifact.model_dump(mode="json") == artifact_a.model_dump(mode="json")

        async with factory() as unit_of_work:
            restored = await unit_of_work.artifacts.get("artifact.a")
            listed = await unit_of_work.artifacts.list_for_run(persisted.run.id)
        assert restored == artifact_a
        assert restored.provenance.output_schema_hash == artifact_a.provenance.output_schema_hash
        assert [item.provenance.artifact_id for item in listed] == [
            "artifact.a",
            "artifact.z",
        ]

        changed = _artifact(
            artifact_id="artifact.a",
            work_item=work_item,
            persisted=persisted,
            payload={"draft": "changed but internally valid"},
            created_at=same_time,
        )
        with pytest.raises(ArtifactPersistenceConflict) as conflict:
            await _persist(factory, changed)
        assert conflict.value.code == "artifact_id_conflict"

        wrong_schema = _artifact(
            artifact_id="artifact.wrong-schema",
            work_item=work_item,
            persisted=persisted,
            output_schema_id="schema:unbound:output:v1",
        )
        with pytest.raises(ArtifactPersistenceConflict) as schema_conflict:
            await _persist(factory, wrong_schema)
        assert schema_conflict.value.code == "artifact_scope_mismatch"

        downgraded_work_source = artifact_a.model_copy(
            update={
                "provenance": artifact_a.provenance.model_copy(
                    update={
                        "sources": (
                            ProvenanceSource(
                                kind="work_input",
                                source_id=work_item.id,
                                integrity_digest=work_item.input_digest,
                                classification=DataClassification.PUBLIC,
                            ),
                        ),
                        "classification": DataClassification.PUBLIC,
                    }
                )
            }
        )
        with pytest.raises(ArtifactPersistenceConflict) as source_conflict:
            await _persist(factory, downgraded_work_source)
        assert source_conflict.value.code == "artifact_scope_mismatch"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_06_parent_edges_require_exact_same_run_ancestor_lineage(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "lineage.db")
    work_item, persisted = await _prepare_run(runtime, "dependent", dependent_steps=True)
    factory = _uow_factory(runtime)
    parent = _artifact(
        artifact_id="artifact.parent",
        work_item=work_item,
        persisted=persisted,
        step_key="read",
    )
    child = _artifact(
        artifact_id="artifact.child",
        work_item=work_item,
        persisted=persisted,
        step_key="summarize",
        parents=(parent,),
        created_at=NOW + timedelta(hours=2),
    )
    try:
        await _persist(factory, parent)
        await _persist(factory, child)
        async with factory() as unit_of_work:
            assert await unit_of_work.artifacts.get("artifact.child") == child

        other_work, other_persisted = await _prepare_run(runtime, "other-run", dependent_steps=True)
        cross_run = _artifact(
            artifact_id="artifact.cross-run",
            work_item=other_work,
            persisted=other_persisted,
            step_key="summarize",
            parents=(parent,),
            created_at=NOW + timedelta(hours=3),
        )
        with pytest.raises(ArtifactPersistenceConflict) as cross_run_conflict:
            await _persist(factory, cross_run)
        assert cross_run_conflict.value.code == "artifact_parent_mismatch"

        parallel_work, parallel_persisted = await _prepare_run(
            runtime, "parallel", parallel_steps=True
        )
        sibling = _artifact(
            artifact_id="artifact.sibling",
            work_item=parallel_work,
            persisted=parallel_persisted,
            step_key="read",
            created_at=NOW + timedelta(hours=4),
        )
        await _persist(factory, sibling)
        invalid_child = _artifact(
            artifact_id="artifact.invalid-child",
            work_item=parallel_work,
            persisted=parallel_persisted,
            step_key="summarize",
            parents=(sibling,),
            created_at=NOW + timedelta(hours=5),
        )
        with pytest.raises(ArtifactPersistenceConflict) as ancestry_conflict:
            await _persist(factory, invalid_child)
        assert ancestry_conflict.value.code == "artifact_parent_not_ancestor"

        async with runtime.engine.connect() as connection:
            foreign_key_violations = tuple(
                (await connection.execute(text("PRAGMA foreign_key_check"))).all()
            )
        assert foreign_key_violations == ()
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("tamper_target", ["payload", "provenance", "schema_hash"])
@pytest.mark.asyncio
async def test_run_06_payload_or_provenance_tamper_fails_closed(
    tmp_path: Path,
    tamper_target: str,
) -> None:
    runtime = await _runtime(tmp_path / f"tamper-{tamper_target}.db")
    work_item, persisted = await _prepare_run(runtime, f"tamper-{tamper_target}")
    artifact = _artifact(
        artifact_id=f"artifact.tamper-{tamper_target}",
        work_item=work_item,
        persisted=persisted,
    )
    factory = _uow_factory(runtime)
    try:
        await _persist(factory, artifact)
        async with runtime.session_factory() as session:
            if tamper_target == "payload":
                values = {"payload": {"draft": "tampered"}}
            elif tamper_target == "schema_hash":
                values = {"output_schema_hash": "schema-sha256-v1:" + ("f" * 64)}
            else:
                record = await session.get(ArtifactRecord, artifact.provenance.artifact_id)
                assert record is not None
                provenance = dict(record.provenance_snapshot)
                provenance["workflow_id"] = "workflow.tampered"
                values = {"provenance_snapshot": provenance}
            await session.execute(
                update(ArtifactRecord)
                .where(ArtifactRecord.id == artifact.provenance.artifact_id)
                .values(**values)
            )
            await session.commit()

        async with factory() as unit_of_work:
            with pytest.raises(ArtifactPersistenceConflict) as conflict:
                await unit_of_work.artifacts.get(artifact.provenance.artifact_id)
        assert conflict.value.code == "artifact_tampered"
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_06_uow_rollback_fault_and_restart_preserve_atomic_artifact(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart.db"
    runtime = await _runtime(database_path)
    work_item, persisted = await _prepare_run(runtime, "rollback-restart")
    artifact = _artifact(
        artifact_id="artifact.restart",
        work_item=work_item,
        persisted=persisted,
    )
    factory = _uow_factory(runtime)
    try:
        async with factory() as unit_of_work:
            await unit_of_work.artifacts.add_or_get(artifact)
        async with factory() as unit_of_work:
            assert await unit_of_work.artifacts.get(artifact.provenance.artifact_id) is None

        with pytest.raises(RuntimeError, match="injected artifact fault"):
            async with factory() as unit_of_work:
                await unit_of_work.artifacts.add_or_get(artifact)
                raise RuntimeError("injected artifact fault")
        async with factory() as unit_of_work:
            assert await unit_of_work.artifacts.get(artifact.provenance.artifact_id) is None

        await _persist(factory, artifact)
    finally:
        await runtime.dispose()

    restarted = await _runtime(database_path)
    try:
        async with _uow_factory(restarted)() as unit_of_work:
            restored = await unit_of_work.artifacts.get(artifact.provenance.artifact_id)
        assert restored == artifact
    finally:
        await restarted.dispose()


def test_run_06_artifact_schema_is_portable_and_repository_is_append_only() -> None:
    dialect = postgresql.dialect()
    artifact_ddl = str(CreateTable(ArtifactRecord.__table__).compile(dialect=dialect))
    parent_ddl = str(CreateTable(ArtifactParentRecord.__table__).compile(dialect=dialect))
    assert "UNIQUE (id, run_id, step_id)" in artifact_ddl
    assert "ck_artifacts_output_schema_hash" in artifact_ddl
    assert "FOREIGN KEY(artifact_id, run_id, artifact_step_id)" in parent_ddl
    assert "FOREIGN KEY(parent_artifact_id, run_id, parent_step_id)" in parent_ddl

    public_methods = {
        name for name in dir(SQLAlchemyArtifactRepository) if not name.startswith("_")
    }
    assert public_methods == {
        "add_or_get",
        "get",
        "get_inspectable",
        "list_for_run",
        "list_for_run_page",
    }

    def accepts_port(factory: Callable[[AsyncSession], ArtifactRepository]) -> None:
        assert factory is SQLAlchemyArtifactRepository

    accepts_port(SQLAlchemyArtifactRepository)
