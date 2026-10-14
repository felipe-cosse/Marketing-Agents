"""Content-free retained identities never re-enter live execution or output writes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from marketing_agents.application.services.controlled_read_executor import (
    ControlledReadExecutor,
    ReadExecutionClassification,
)
from marketing_agents.application.services.local_artifact_executor import LocalArtifactExecutor
from marketing_agents.application.services.proposal_preview_executor import ProposalPreviewExecutor
from marketing_agents.demos.email_signup_service import EmailSignupRunService
from marketing_agents.demos.service import DemoRunService
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import RunState, StepState, WorkMode
from marketing_agents.domain.retained_details import DetailExpired, RetainedDetail, RetainedWorkItem
from marketing_agents.domain.retention import RetentionCategory
from marketing_agents.infrastructure.runtime.catalog_proposals import CatalogProposalWorkflowService
from marketing_agents.infrastructure.runtime.catalog_workflows import CatalogReadWorkflowService
from marketing_agents.infrastructure.runtime.catalog_write_workflows import (
    CatalogWriteWorkflowService,
)
from marketing_agents.workers.runtime.run_loop import RunWorker

NOW = datetime(2026, 10, 14, tzinfo=UTC)
CONTEXT = AuditContext.worker("worker.retained", correlation_id="correlation.retained")


def _retained_work() -> RetainedWorkItem:
    created_at = NOW - timedelta(days=8)
    expires_at = NOW - timedelta(days=1)
    return RetainedWorkItem(
        id="work.retained",
        source="manual",
        event_id="event.retained",
        instance_id="instance.retained",
        trigger_id="trigger.retained",
        workflow_id="workflow.retained",
        mode=WorkMode.DRY_RUN,
        brief_id=None,
        brief_revision=None,
        configuration_revision=1,
        created_at=created_at,
        input_schema_id="schema.retained",
        input_schema_hash="schema-sha256-v1:" + "a" * 64,
        input_classification=DataClassification.INTERNAL,
        input_projection_created_at=created_at,
        input_projection_expires_at=expires_at,
        input_digest="a" * 64,
        admission_digest="b" * 64,
        digest_key_version="key.retained",
        input_projection_integrity_digest="c" * 64,
        projection_digest="d" * 64,
        retention=RetainedDetail(
            category=RetentionCategory.ADMITTED_PAYLOAD,
            record_kind="work",
            record_id="work.retained",
            created_at=created_at,
            expires_at=expires_at,
            purged_at=NOW,
            run_id="run.retained",
            metadata={},
        ),
    )


class BoundaryUnitOfWork:
    """Only the reads before the retention fence exist; no execution collaborators."""

    def __init__(self) -> None:
        self.work = _retained_work()
        self.run = SimpleNamespace(
            id="run.retained", work_item_id=self.work.id, state=RunState.EXECUTING
        )
        self.step = SimpleNamespace(
            id="step.retained",
            run_id=self.run.id,
            state=StepState.EXECUTING,
            plan_hash="plan.retained",
        )
        self.works = SimpleNamespace(get=AsyncMock(return_value=self.work))
        self.runs = SimpleNamespace(get=AsyncMock(return_value=self.run))
        self.run_steps = SimpleNamespace(
            get=AsyncMock(return_value=self.step),
            get_plan=AsyncMock(return_value=None),
            validate_plan_for_execution=AsyncMock(return_value=(self.step,)),
        )
        self.commit = AsyncMock(side_effect=AssertionError("expired detail cannot commit"))
        self.exit_exception = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exit_exception = exc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint",
    [
        ProposalPreviewExecutor.execute,
        LocalArtifactExecutor.execute,
        CatalogReadWorkflowService.resume_persisted,
        CatalogWriteWorkflowService.resume_persisted,
        CatalogProposalWorkflowService.resume_persisted,
        DemoRunService.resume_persisted,
        EmailSignupRunService.resume,
        RunWorker._advance,
    ],
    ids=lambda entrypoint: entrypoint.__qualname__,
)
async def test_exec_06_retained_work_fails_before_live_execution(entrypoint):
    uow = BoundaryUnitOfWork()
    dependencies = SimpleNamespace(unit_of_work=lambda: uow)
    # Invoke each real entrypoint against the same minimum boundary collaborators.
    # Any access to a renderer, planner, registry, adapter or claim fails the test.
    owner = SimpleNamespace(
        _dependencies=dependencies, runtime=SimpleNamespace(dependencies=dependencies)
    )
    if entrypoint in (ProposalPreviewExecutor.execute, LocalArtifactExecutor.execute):
        arguments = {"step_id": uow.step.id, "audit_context": CONTEXT}
    elif entrypoint is RunWorker._advance:
        arguments = {"run_id": uow.run.id}
    else:
        arguments = {
            "run_id": uow.run.id,
            "worker_id": "worker.retained",
            "correlation_id": "correlation.retained",
        }
    with pytest.raises(DetailExpired) as captured:
        await entrypoint(owner, **arguments)
    assert (captured.value.record_kind, captured.value.record_id) == ("work", uow.work.id)
    assert uow.exit_exception is captured.value
    uow.works.get.assert_awaited_once_with(uow.work.id)
    uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_exec_06_read_completion_preserves_detail_expired_without_writing_output():
    uow = BoundaryUnitOfWork()
    attempt = SimpleNamespace(
        id="attempt.retained", outcome=None, step_id=uow.step.id, policy_hash=uow.step.plan_hash
    )
    uow.execution_control = SimpleNamespace(
        get_attempt=AsyncMock(return_value=attempt),
        get=AsyncMock(
            return_value=SimpleNamespace(policy_hash=uow.step.plan_hash, cancel_requested_at=None)
        ),
        complete_attempt=AsyncMock(side_effect=AssertionError("expired detail cannot complete")),
    )
    owner = SimpleNamespace(
        _dependencies=SimpleNamespace(unit_of_work=lambda: uow, utc_now=lambda: NOW)
    )
    reserved = SimpleNamespace(
        reservation=SimpleNamespace(attempt=attempt),
        request=SimpleNamespace(step_id=uow.step.id, run_id=uow.run.id),
    )
    with pytest.raises(DetailExpired) as captured:
        await ControlledReadExecutor._complete_once(
            owner,
            reserved,
            ReadExecutionClassification.SUCCEEDED,
            SimpleNamespace(),
            runtime_denial_code=None,
            safe_error_code=None,
            artifact_id="artifact.retained",
            audit_context=CONTEXT,
        )
    assert (captured.value.record_kind, captured.value.record_id) == ("work", uow.work.id)
    assert uow.exit_exception is captured.value
    uow.works.get.assert_awaited_once_with(uow.work.id)
    uow.run_steps.get_plan.assert_not_awaited()
    uow.execution_control.complete_attempt.assert_not_awaited()
    uow.commit.assert_not_awaited()
