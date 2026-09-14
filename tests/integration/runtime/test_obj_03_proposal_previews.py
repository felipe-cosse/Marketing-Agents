"""Real write-role dry runs produce inert previews, never connector authority."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.services.plan_persistence import (
    AuditedPlanPersistenceService,
    PlanPersistenceError,
)
from marketing_agents.application.services.proposal_preview_executor import (
    ProposalPreviewExecutionError,
    ProposalPreviewExecutor,
)
from marketing_agents.application.services.run_cancellation import RunCancellationService
from marketing_agents.application.services.run_lifecycle import RunLifecycleService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import Effect, RunState, StepState, TriggerKind, WorkMode
from marketing_agents.domain.planner_output import PLANNER_OUTPUT_FAMILY, PROPOSAL_PREVIEW_KIND
from marketing_agents.domain.run_lifecycle import NoRunTransitionContext, RunLifecycleCommand
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.infrastructure.db import SQLAlchemyAuditRepository
from marketing_agents.infrastructure.runtime.catalog_proposals import CatalogProposalRenderer
from marketing_agents.infrastructure.runtime.catalog_write_workflows import (
    CATALOG_WRITE_CAPABILITIES,
)
from marketing_agents.workers.runtime.composition import LocalRuntime, build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from sqlalchemy import text

from tests.integration.runtime.test_del_05_process_composition import Clock, _installation
from tests.integration.runtime.test_obj_03_catalog_writes import _receipt_count, _source, _submit
from tests.integration.runtime.test_obj_03_local_artifacts import _snapshot

PREVIEW_CONTEXT = AuditContext.worker(
    "worker.preview.atomic", correlation_id="correlation.preview.atomic"
)


async def _ready_preview(runtime: LocalRuntime, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Use real admission and proposal planning/activation; stop before the executor."""
    async with AsyncClient(
        transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
    ) as client:
        run_id = await _submit(
            client,
            "inst.email.newsletter.newsletter-subscriber.01",
            _source("tpl.email.newsletter.newsletter-subscriber"),
            mode="dry_run",
        )

    class PreviewReady(Exception):
        pass

    async def pause_before_execute(_executor, _step_id, *, audit_context):
        raise PreviewReady

    with monkeypatch.context() as patch:
        patch.setattr(ProposalPreviewExecutor, "execute", pause_before_execute)
        with pytest.raises(PreviewReady):
            await runtime.catalog_proposals.resume_persisted(
                run_id,
                worker_id=PREVIEW_CONTEXT.actor_id,
                correlation_id=PREVIEW_CONTEXT.correlation_id,
            )
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        assert run is not None and run.state is RunState.EXECUTING
        steps = await uow.run_steps.validate_plan_for_execution(run_id)
        assert len(steps) == 1 and steps[0].state is StepState.READY
        assert steps[0].kind == PROPOSAL_PREVIEW_KIND
        assert steps[0].runtime_policy.attempt_kind is AttemptKind.NO_CALL
    return run_id, steps[0].id


class _CountingPreviewRenderer:
    def __init__(self) -> None:
        self.delegate = CatalogProposalRenderer()
        self.calls = 0

    def render_proposal(
        self, template_id: str, admitted_input: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls += 1
        return self.delegate.render_proposal(template_id, admitted_input)


@pytest.mark.asyncio
async def test_all_six_write_instances_dry_run_as_no_call_proposals_after_restart(
    tmp_path: Path,
) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    submissions = []
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            for instance in runtime.catalog.instances:
                if instance.template_id not in CATALOG_WRITE_CAPABILITIES:
                    continue
                run_id = await _submit(
                    client, instance.id, _source(instance.template_id), mode="dry_run"
                )
                submissions.append((run_id, instance))
        assert len(submissions) == 6
        await runtime.close()
        runtime = await build_runtime(settings, clock=Clock())
        worker = RunWorker(runtime, "worker.obj03.preview")
        for _ in submissions:
            assert await worker.drain_once()
        assert not await worker.drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            for run_id, instance in submissions:
                run = await uow.runs.get(run_id)
                assert run is not None and run.state is RunState.COMPLETED, instance.id
                work = await uow.works.get(run.work_item_id)
                assert work is not None and work.mode is WorkMode.DRY_RUN
                assert work.admitted_payload["source_content"] == _source(instance.template_id)
                definition = runtime.workflows.for_catalog_role(
                    instance.template_id, TriggerKind.MANUAL
                )
                plan = await uow.run_steps.get_plan(run_id)
                assert plan is not None and not plan.approval_required
                assert not await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
                assert await uow.approvals.get_current_authorization_set(run_id) is None
                steps = await uow.run_steps.validate_plan_for_execution(run_id)
                assert len(steps) == 1
                step = steps[0]
                assert step.state is StepState.SUCCEEDED and step.effect is Effect.READ
                assert step.kind == PROPOSAL_PREVIEW_KIND
                assert step.connector_family == PLANNER_OUTPUT_FAMILY
                assert step.capability_id == CATALOG_WRITE_CAPABILITIES[instance.template_id]
                assert step.runtime_policy.attempt_kind is AttemptKind.NO_CALL
                assert step.binding_id is None and step.timeout_seconds is None
                assert not await uow.execution_control.list_attempts(
                    step.id, step.runtime_policy.operation_key
                )
                control = await uow.execution_control.get(run_id)
                assert control is not None and control.model_calls == control.tool_calls == 0
                artifacts = await uow.artifacts.list_for_run(run_id)
                assert len(artifacts) == 1 and artifacts[0].verify_payload()
                artifact = artifacts[0]
                assert artifact.provenance.output_schema_id == definition.output_schema_id
                assert artifact.provenance.output_schema_hash == definition.output_schema_hash
                assert artifact.provenance.providers[0].name == "catalog-write-proposal"
                assert artifact.payload["summary"].startswith("Dry-run proposal only.")
                assert len(artifact.payload["proposed_actions"]) == 1
                for key, value in json.loads(_source(instance.template_id))["command"].items():
                    assert key in artifact.payload["artifact"]
                    if isinstance(value, str):
                        assert value in artifact.payload["artifact"]
                with pytest.raises(ValueError):
                    replace(step, effect=Effect.WRITE)
                with pytest.raises(ValueError):
                    replace(step, connector_family="newsletter")
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_preview_restart_after_output_commit_reuses_exact_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    instance_id = "inst.email.newsletter.newsletter-subscriber.01"
    original_advance = RunLifecycleService.advance

    async def interrupt_complete(self, *args, **kwargs):
        if len(args) > 2 and args[2] is RunLifecycleCommand.COMPLETE:
            raise asyncio.CancelledError("simulated process exit after output commit")
        return await original_advance(self, *args, **kwargs)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await _submit(
                client,
                instance_id,
                _source("tpl.email.newsletter.newsletter-subscriber"),
                mode="dry_run",
            )
        with monkeypatch.context() as patch:
            patch.setattr(RunLifecycleService, "advance", interrupt_complete)
            with pytest.raises(asyncio.CancelledError):
                await runtime.catalog_proposals.resume_persisted(
                    run_id,
                    worker_id="worker.preview.crash",
                    correlation_id="correlation.preview.crash",
                )
        async with runtime.dependencies.unit_of_work() as uow:
            before = await uow.artifacts.list_for_run(run_id)
            run = await uow.runs.get(run_id)
            assert len(before) == 1 and run.state is RunState.EXECUTING
        await runtime.close()
        runtime = await build_runtime(settings, clock=Clock())
        assert await RunWorker(runtime, "worker.preview.replay").drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            after = await uow.artifacts.list_for_run(run_id)
            run = await uow.runs.get(run_id)
            assert after == before and run.state is RunState.COMPLETED
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_preview_persistence_rejects_a_real_mock_execution_admission(tmp_path: Path) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await _submit(
                client,
                "inst.email.newsletter.newsletter-subscriber.01",
                _source("tpl.email.newsletter.newsletter-subscriber"),
                mode="mock_execute",
            )
        context = AuditContext.worker(
            "worker.preview.negative", correlation_id="correlation.preview.negative"
        )
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            work = await uow.works.get(run.work_item_id)
        current = (
            await RunLifecycleService(runtime.dependencies).advance(
                run.id,
                run.version,
                RunLifecycleCommand.MARK_VALIDATED,
                NoRunTransitionContext(),
                audit_context=context,
            )
        ).run
        definition = runtime.workflows.get(work.workflow_id)
        plan, graph, routing = runtime.catalog_proposals._build_plan(
            replace(work, mode=WorkMode.DRY_RUN), current, definition
        )
        with pytest.raises(PlanPersistenceError, match="admitted dry run"):
            await AuditedPlanPersistenceService(runtime.dependencies).persist(
                plan, graph, routing, expected_run_version=current.version, audit_context=context
            )
        with pytest.raises(ValueError, match="catalog_preview_handler_required"):
            await runtime.catalog_proposals.resume_persisted(
                run_id,
                worker_id="worker.preview.negative",
                correlation_id="correlation.preview.negative",
            )
        async with runtime.dependencies.unit_of_work() as uow:
            assert await uow.run_steps.get_plan(run_id) is None
            assert not await uow.artifacts.list_for_run(run_id)
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_preview_audit_failure_rolls_back_step_artifact_and_audit_then_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await build_runtime(await _installation(tmp_path), clock=Clock())
    original_append = SQLAlchemyAuditRepository.append_many
    injected = False
    try:
        run_id, step_id = await _ready_preview(runtime, monkeypatch)
        before = await _snapshot(runtime.database)
        renderer = _CountingPreviewRenderer()
        executor = ProposalPreviewExecutor(runtime.dependencies, runtime.workflows, renderer)

        async def fail_after_real_preview_append(repository, events):
            nonlocal injected
            events = tuple(events)
            assert sum(event.event_type == "artifact.previewed" for event in events) == 1
            await original_append(repository, events)
            # Witness the real writes within this transaction before failing it.
            assert await repository._session.scalar(text("SELECT count(*) FROM artifacts")) == 1
            assert (
                await repository._session.scalar(
                    text("SELECT state FROM run_steps WHERE id = :step_id"), {"step_id": step_id}
                )
                == StepState.SUCCEEDED.value
            )
            assert (
                await repository._session.scalar(
                    text("SELECT count(*) FROM audit_events WHERE event_type='artifact.previewed'")
                )
                == 1
            )
            injected = True
            raise RuntimeError("injected_after_preview_audit_append")

        with monkeypatch.context() as patch:
            patch.setattr(SQLAlchemyAuditRepository, "append_many", fail_after_real_preview_append)
            with pytest.raises(RuntimeError, match="injected_after_preview_audit_append"):
                await executor.execute(step_id, audit_context=PREVIEW_CONTEXT)
        assert injected and renderer.calls == 1
        # Includes step/control versions, both audit counters, and every physical row.
        assert await _snapshot(runtime.database) == before
        assert before["artifacts"] == before["execution_attempts"] == ()
        assert before["execution_operation_policies"] == before["rate_limit_windows"] == ()
        async with runtime.dependencies.unit_of_work() as uow:
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.model_calls == control.tool_calls == 0

        artifact = await executor.execute(step_id, audit_context=PREVIEW_CONTEXT)
        assert artifact.verify_payload() and renderer.calls == 2
        await runtime.catalog_proposals.resume_persisted(
            run_id, worker_id="worker.preview.retry", correlation_id="correlation.preview.retry"
        )
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            assert await uow.artifacts.list_for_run(run_id) == (artifact,)
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.model_calls == control.tool_calls == 0
        after = await _snapshot(runtime.database)
        assert after["execution_attempts"] == after["execution_operation_policies"] == ()
        assert after["rate_limit_windows"] == after["connector_action_receipts"] == ()
        async with runtime.database.engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM audit_events WHERE event_type='artifact.previewed'")
                )
                == 1
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cancelled_preview_never_renders_writes_an_artifact_or_charges_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await build_runtime(await _installation(tmp_path), clock=Clock())
    try:
        run_id, step_id = await _ready_preview(runtime, monkeypatch)
        cancellation = await RunCancellationService(runtime.dependencies).request(
            run_id, audit_context=PREVIEW_CONTEXT
        )
        assert cancellation.run.state is RunState.CANCELLED
        before = await _snapshot(runtime.database)
        renderer = _CountingPreviewRenderer()
        executor = ProposalPreviewExecutor(runtime.dependencies, runtime.workflows, renderer)
        with pytest.raises(ProposalPreviewExecutionError, match="preview_step_not_ready"):
            await executor.execute(step_id, audit_context=PREVIEW_CONTEXT)
        assert renderer.calls == 0
        assert await _snapshot(runtime.database) == before
        for table in (
            "artifacts",
            "execution_attempts",
            "execution_operation_policies",
            "rate_limit_windows",
            "external_actions",
            "connector_action_receipts",
        ):
            assert before[table] == (), table
        async with runtime.dependencies.unit_of_work() as uow:
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.model_calls == control.tool_calls == 0
    finally:
        await runtime.close()
