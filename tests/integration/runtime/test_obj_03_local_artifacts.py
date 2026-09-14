"""OBJ-03 NO_CALL output is a real, bounded, atomic SQLite operation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from marketing_agents.application.policies.json_schema import JsonSchemaPolicyError
from marketing_agents.application.services.execution_activation import ExecutionActivationService
from marketing_agents.application.services.local_artifact_executor import (
    LocalArtifactExecutionError,
    LocalArtifactExecutor,
)
from marketing_agents.application.services.manual_work_intake import ManualDryRunCommand
from marketing_agents.application.services.plan_persistence import AuditedPlanPersistenceService
from marketing_agents.application.services.run_cancellation import RunCancellationService
from marketing_agents.application.services.run_lifecycle import RunLifecycleService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.run_lifecycle import NoRunTransitionContext, RunLifecycleCommand
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.infrastructure.adapters.catalog_roles import CatalogRoleRenderer
from marketing_agents.infrastructure.db import DatabaseRuntime, SQLAlchemyAuditRepository
from marketing_agents.infrastructure.db.repositories.execution_control import (
    SQLAlchemyExecutionControlRepository,
)
from marketing_agents.infrastructure.db.repositories.run import SQLAlchemyRunRepository
from marketing_agents.infrastructure.db.repositories.step import SQLAlchemyRunStepRepository
from marketing_agents.workers.runtime.composition import LocalRuntime, build_runtime
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Connection

from tests.integration.runtime.test_del_05_process_composition import Clock, _installation
from tests.support.identity import human_principal

INSTANCE_ID = "inst.partnerships.implementation-partners.partner-tracker.01"
CONTEXT = AuditContext.worker("worker.obj03.local", correlation_id="corr.obj03.local")


def _rows(connection: Connection) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Compare every stored value, including digests, counters and audit ordering."""
    quote = connection.dialect.identifier_preparer.quote
    return {
        name: tuple(
            sorted(
                (tuple(row) for row in connection.execute(text(f"SELECT * FROM {quote(name)}"))),
                key=repr,
            )
        )
        for name in sorted(inspect(connection).get_table_names())
    }


async def _snapshot(database: DatabaseRuntime) -> dict[str, tuple[tuple[Any, ...], ...]]:
    async with database.engine.connect() as connection:
        return await connection.run_sync(_rows)


async def _admit(runtime: LocalRuntime) -> str:
    receipt = await runtime.manual.submit(
        ManualDryRunCommand(
            instance_id=INSTANCE_ID,
            input_payload={
                "request_id": "request.obj03.local",
                "source_content": "A supplied local partner status note; no external metrics.",
            },
            correlation_id="corr.obj03.admit",
        ),
        principal=human_principal(
            actor_id="principal.obj03.operator",
            roles=frozenset({"operator"}),
            scopes=frozenset({"manual-work:create"}),
        ),
    )
    return receipt.run.id


async def _ready(runtime: LocalRuntime) -> tuple[str, str]:
    """Use production admission/planning/activation, stopping before the transform."""
    run_id = await _admit(runtime)
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        assert run is not None
        work = await uow.works.get(run.work_item_id)
        assert work is not None
    run = (
        await RunLifecycleService(runtime.dependencies).advance(
            run.id,
            run.version,
            RunLifecycleCommand.MARK_VALIDATED,
            NoRunTransitionContext(),
            audit_context=CONTEXT,
        )
    ).run
    definition = runtime.workflows.get(work.workflow_id)
    # Explicitly share the production planner; this helper does not fabricate plans.
    plan, graph, routing = runtime.catalog_reads._build_plan(work, run, definition)
    await AuditedPlanPersistenceService(runtime.dependencies).persist(
        plan,
        graph,
        routing,
        expected_run_version=run.version,
        audit_context=CONTEXT,
    )
    await ExecutionActivationService(runtime.dependencies).activate(run.id, audit_context=CONTEXT)
    async with runtime.dependencies.unit_of_work() as uow:
        steps = await uow.run_steps.validate_plan_for_execution(run_id)
        assert len(steps) == 1 and steps[0].state is StepState.READY
        assert steps[0].runtime_policy.attempt_kind is AttemptKind.NO_CALL
    return run_id, steps[0].id


class _CountingRenderer:
    def __init__(self, runtime: LocalRuntime) -> None:
        self.delegate = CatalogRoleRenderer(runtime.catalog)
        self.calls = 0

    def render_local(
        self, template_id: str, admitted_input: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls += 1
        return self.delegate.render_local(template_id, admitted_input)


@pytest.mark.asyncio
async def test_obj_03_local_output_has_exact_lineage_no_attempt_and_replays_after_restart(
    tmp_path: Path,
) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    try:
        run_id, step_id = await _ready(runtime)
        renderer = _CountingRenderer(runtime)
        artifact = await LocalArtifactExecutor(
            runtime.dependencies, runtime.workflows, renderer
        ).execute(step_id, audit_context=CONTEXT)
        assert renderer.calls == 1
        await runtime.catalog_reads.resume_persisted(
            run_id, worker_id="worker.obj03.complete", correlation_id="corr.obj03.complete"
        )
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            work = await uow.works.get(run.work_item_id)
            assert work is not None
            definition = runtime.workflows.get(work.workflow_id)
            assert await uow.artifacts.list_for_run(run_id) == (artifact,)
            provenance = artifact.provenance
            assert artifact.verify_payload()
            assert (provenance.work_item_id, provenance.run_id, provenance.step_id) == (
                work.id,
                run_id,
                step_id,
            )
            assert (provenance.template_id, provenance.instance_id) == (
                definition.template_id,
                work.instance_id,
            )
            assert (provenance.workflow_id, provenance.workflow_version) == (definition.id, "1")
            assert provenance.admitted_input_digest == work.input_digest
            assert provenance.catalog_hash == runtime.catalog.content_hash
            assert provenance.instance_config_revision == work.configuration_revision
            assert provenance.output_schema_id == definition.output_schema_id
            assert provenance.output_schema_hash == definition.output_schema_hash
            assert provenance.classification == work.input_classification
            assert len(provenance.sources) == 1
            assert provenance.sources[0].source_id == work.id
            assert provenance.sources[0].integrity_digest == work.input_digest
            assert provenance.sources[0].classification == work.input_classification
            assert provenance.parent_artifact_ids == ()
            assert len(provenance.providers) == 1
            assert provenance.providers[0].provider_kind == "planner"
            assert provenance.providers[0].mode == "local"
            control = await uow.execution_control.get(run_id)
            assert control is not None and (control.model_calls, control.tool_calls) == (0, 0)
        before = await _snapshot(runtime.database)
        for table in (
            "execution_attempts",
            "execution_operation_policies",
            "rate_limit_windows",
            "external_actions",
            "approval_requests",
            "connector_action_receipts",
        ):
            assert before[table] == (), table
        async with runtime.database.engine.connect() as connection:
            events = (
                await connection.execute(
                    text(
                        "SELECT event_type, attempt_id, artifact_id FROM audit_events "
                        "WHERE artifact_id IS NOT NULL"
                    )
                )
            ).all()
            assert [tuple(row) for row in events] == [
                ("artifact.transformed", None, artifact.provenance.artifact_id)
            ]
        await runtime.close()
        runtime = await build_runtime(settings, clock=Clock())
        replay_renderer = _CountingRenderer(runtime)
        replay = await LocalArtifactExecutor(
            runtime.dependencies, runtime.workflows, replay_renderer
        ).execute(step_id, audit_context=CONTEXT)
        await runtime.catalog_reads.resume_persisted(
            run_id, worker_id="worker.obj03.replay", correlation_id="corr.obj03.replay"
        )
        assert replay == artifact and replay_renderer.calls == 0
        assert await _snapshot(runtime.database) == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_cancelled_local_step_never_renders_or_reserves_a_call(tmp_path: Path) -> None:
    runtime = await build_runtime(await _installation(tmp_path), clock=Clock())
    try:
        run_id, step_id = await _ready(runtime)
        cancelled = await RunCancellationService(runtime.dependencies).request(
            run_id, audit_context=CONTEXT
        )
        assert cancelled.run.state is RunState.CANCELLED
        before = await _snapshot(runtime.database)
        renderer = _CountingRenderer(runtime)
        with pytest.raises(LocalArtifactExecutionError, match="local_step_not_ready"):
            await LocalArtifactExecutor(runtime.dependencies, runtime.workflows, renderer).execute(
                step_id, audit_context=CONTEXT
            )
        assert renderer.calls == 0
        assert (
            before["artifacts"]
            == before["execution_attempts"]
            == before["rate_limit_windows"]
            == ()
        )
        assert await _snapshot(runtime.database) == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_concurrent_local_executors_commit_one_output_and_one_audit(
    tmp_path: Path,
) -> None:
    runtime = await build_runtime(await _installation(tmp_path), clock=Clock())
    try:
        _, step_id = await _ready(runtime)
        renderer = _CountingRenderer(runtime)
        executor = LocalArtifactExecutor(runtime.dependencies, runtime.workflows, renderer)
        first, second = await asyncio.gather(
            executor.execute(step_id, audit_context=CONTEXT),
            executor.execute(step_id, audit_context=CONTEXT),
        )
        assert first == second and renderer.calls == 1
        rows = await _snapshot(runtime.database)
        assert len(rows["artifacts"]) == 1
        assert rows["execution_attempts"] == rows["rate_limit_windows"] == ()
        async with runtime.database.engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM audit_events WHERE event_type='artifact.transformed'"
                    )
                )
                == 1
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_cancellation_waits_for_fenced_local_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await build_runtime(await _installation(tmp_path), clock=Clock())
    acquired, release, cancelling = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellation_entered = asyncio.Event()
    original = SQLAlchemyExecutionControlRepository.fence_active
    original_cancel = SQLAlchemyExecutionControlRepository.request_cancel

    def observe_waiting_transaction(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if acquired.is_set() and not release.is_set() and statement == "BEGIN IMMEDIATE":
            cancelling.set()

    async def hold_fence(self: Any, **kwargs: Any) -> bool:
        result = await original(self, **kwargs)
        assert result
        acquired.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return result

    async def observe_cancel(self: Any, **kwargs: Any) -> Any:
        cancellation_entered.set()
        return await original_cancel(self, **kwargs)

    tasks: list[asyncio.Task[Any]] = []
    try:
        run_id, step_id = await _ready(runtime)
        monkeypatch.setattr(SQLAlchemyExecutionControlRepository, "fence_active", hold_fence)
        monkeypatch.setattr(SQLAlchemyExecutionControlRepository, "request_cancel", observe_cancel)
        event.listen(
            runtime.database.engine.sync_engine,
            "before_cursor_execute",
            observe_waiting_transaction,
        )
        executor = LocalArtifactExecutor(
            runtime.dependencies, runtime.workflows, _CountingRenderer(runtime)
        )
        output_task = asyncio.create_task(executor.execute(step_id, audit_context=CONTEXT))
        tasks.append(output_task)
        await asyncio.wait_for(acquired.wait(), timeout=5)
        cancel_task = asyncio.create_task(
            RunCancellationService(runtime.dependencies).request(run_id, audit_context=CONTEXT)
        )
        tasks.append(cancel_task)
        await asyncio.wait_for(cancelling.wait(), timeout=5)
        # SQLite has actually attempted its competing write transaction while the
        # local executor holds its fence; cancellation cannot read stale state.
        assert not cancellation_entered.is_set()
        assert not cancel_task.done() and not output_task.done()
        release.set()
        artifact, cancelled = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert cancellation_entered.is_set()
        assert cancelled.run.state is RunState.CANCELLED
        async with runtime.dependencies.unit_of_work() as uow:
            step = await uow.run_steps.get(step_id)
            assert step is not None and step.state is StepState.SUCCEEDED
            assert await uow.artifacts.list_for_run(run_id) == (artifact,)
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.cancel_requested_at is not None
            assert (control.model_calls, control.tool_calls) == (0, 0)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if event.contains(
            runtime.database.engine.sync_engine,
            "before_cursor_execute",
            observe_waiting_transaction,
        ):
            event.remove(
                runtime.database.engine.sync_engine,
                "before_cursor_execute",
                observe_waiting_transaction,
            )
        await runtime.close()


@pytest.mark.parametrize("fault", ["run_fence", "start_fence", "success_fence"])
@pytest.mark.asyncio
async def test_obj_03_local_late_fence_loss_rolls_back_real_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    runtime = await build_runtime(await _installation(tmp_path), clock=Clock())
    try:
        _, step_id = await _ready(runtime)
        before = await _snapshot(runtime.database)
        renderer = _CountingRenderer(runtime)
        executor = LocalArtifactExecutor(runtime.dependencies, runtime.workflows, renderer)
        original_run = SQLAlchemyRunRepository.fence
        original_step = SQLAlchemyRunStepRepository.apply_transition
        transitions: list[StepState] = []

        async def reject_run(self: Any, **kwargs: Any) -> bool:
            assert await original_run(self, **kwargs)
            return False

        async def reject_step(self: Any, **kwargs: Any) -> bool:
            assert await original_step(self, **kwargs)
            observed = kwargs["result"].step.state
            transitions.append(observed)
            rejected = StepState.EXECUTING if fault == "start_fence" else StepState.SUCCEEDED
            return observed is not rejected

        with monkeypatch.context() as patch:
            if fault == "run_fence":
                patch.setattr(SQLAlchemyRunRepository, "fence", reject_run)
                code = "local_execution_fence_lost"
            else:
                patch.setattr(SQLAlchemyRunStepRepository, "apply_transition", reject_step)
                code = "local_step_fence_lost"
            with pytest.raises(LocalArtifactExecutionError, match=code):
                await executor.execute(step_id, audit_context=CONTEXT)
        assert (
            transitions
            == {
                "run_fence": [],
                "start_fence": [StepState.EXECUTING],
                "success_fence": [StepState.EXECUTING, StepState.SUCCEEDED],
            }[fault]
        )
        assert renderer.calls == (0 if fault == "run_fence" else 1)
        assert await _snapshot(runtime.database) == before
        assert (await executor.execute(step_id, audit_context=CONTEXT)).verify_payload()
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "fault", ["renderer", "schema", "output_bytes", "run_deadline", "step_deadline"]
)
@pytest.mark.asyncio
async def test_obj_03_local_transform_failure_rolls_back_every_runtime_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    clock = Clock()
    runtime = await build_runtime(await _installation(tmp_path), clock=clock)
    try:
        run_id, step_id = await _ready(runtime)
        async with runtime.dependencies.unit_of_work() as uow:
            step = await uow.run_steps.get(step_id)
            control = await uow.execution_control.get(run_id)
            assert step is not None and control is not None and control.deadline_at is not None
        before = await _snapshot(runtime.database)
        delegate = CatalogRoleRenderer(runtime.catalog)

        class FaultRenderer:
            def render_local(
                self, template_id: str, admitted_input: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                payload = delegate.render_local(template_id, admitted_input)
                if fault == "renderer":
                    raise RuntimeError("injected_renderer_failure")
                if fault == "schema":
                    return {}
                if fault == "output_bytes":
                    return {"oversized": "x" * (step.runtime_policy.budget.max_output_bytes + 1)}
                if fault == "run_deadline":
                    clock.current = control.deadline_at
                return payload

        if fault == "step_deadline":
            ticks = iter((1.0, 1.0 + step.runtime_policy.timeout.step_seconds))
            monkeypatch.setattr(
                import_module("marketing_agents.application.services.local_artifact_executor"),
                "time",
                SimpleNamespace(monotonic=lambda: next(ticks)),
            )
        expected = {
            "renderer": (RuntimeError, "injected_renderer_failure"),
            "schema": (JsonSchemaPolicyError, None),
            "output_bytes": (LocalArtifactExecutionError, "local_output_budget_exceeded"),
            "run_deadline": (LocalArtifactExecutionError, "local_execution_deadline_exceeded"),
            "step_deadline": (LocalArtifactExecutionError, "local_execution_deadline_exceeded"),
        }[fault]
        with pytest.raises(expected[0], match=expected[1]):
            await LocalArtifactExecutor(
                runtime.dependencies, runtime.workflows, FaultRenderer()
            ).execute(step_id, audit_context=CONTEXT)
        assert await _snapshot(runtime.database) == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_audit_failure_rolls_back_output_and_step_success_then_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await build_runtime(await _installation(tmp_path), clock=Clock())
    original = SQLAlchemyAuditRepository.append_many
    try:
        _, step_id = await _ready(runtime)
        before = await _snapshot(runtime.database)

        async def fail_after_append(self: Any, events: Any) -> None:
            await original(self, events)
            raise RuntimeError("injected_after_real_audit_append")

        renderer = _CountingRenderer(runtime)
        executor = LocalArtifactExecutor(runtime.dependencies, runtime.workflows, renderer)
        with monkeypatch.context() as patch:
            patch.setattr(SQLAlchemyAuditRepository, "append_many", fail_after_append)
            with pytest.raises(RuntimeError, match="injected_after_real_audit_append"):
                await executor.execute(step_id, audit_context=CONTEXT)
        assert await _snapshot(runtime.database) == before
        artifact = await executor.execute(step_id, audit_context=CONTEXT)
        assert artifact.verify_payload() and renderer.calls == 2
        assert len((await _snapshot(runtime.database))["artifacts"]) == 1
    finally:
        await runtime.close()
