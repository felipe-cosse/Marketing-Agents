"""API-09: failed Run detail exposes only bounded terminal error facts."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from marketing_agents.api.routes.runs import _resource_view
from marketing_agents.api.schemas.runs import RunResourceView
from marketing_agents.application.ports.repositories import InspectableRun
from marketing_agents.application.services.run_resources import (
    RunResource,
    RunResourceService,
    RunResourceServiceError,
)
from marketing_agents.domain.entities import RunStep
from marketing_agents.domain.enums import Effect
from marketing_agents.domain.execution_control import AttemptOutcome, ExecutionAttempt
from marketing_agents.domain.run_lifecycle import (
    FailureContext,
    NoRunTransitionContext,
    RunFailurePhase,
    RunLifecycleCommand,
    transition_run,
)
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepTerminalContext,
    transition_step,
)

from tests.unit.application.test_api_07_run_resources import (
    NOW,
    _CompositeFactory,
    _CompositeRuns,
    _CompositeUnitOfWork,
    _planned_fixture,
    _reader,
    _write_action_plan,
)


class _AttemptExecutionControl:
    def __init__(self, control: object, attempts: tuple[ExecutionAttempt, ...]) -> None:
        self.control = control
        self.attempts = attempts
        self.list_calls: list[tuple[str, str]] = []

    async def get(self, run_id: str) -> object:
        return self.control if getattr(self.control, "run_id", None) == run_id else None

    async def list_attempts(
        self,
        step_id: str,
        operation_key: str,
    ) -> tuple[ExecutionAttempt, ...]:
        self.list_calls.append((step_id, operation_key))
        return tuple(
            value
            for value in self.attempts
            if (value.step_id, value.operation_key) == (step_id, operation_key)
        )


async def _step_only_terminal_projection(
    *,
    effect: Effect,
    failure_code: str,
) -> tuple[RunResource, RunResourceView, RunStep, _AttemptExecutionControl]:
    stored, base_plan, control, _approval, _artifact = _planned_fixture()
    plan = base_plan if effect is Effect.READ else _write_action_plan(base_plan)
    activated = transition_run(
        stored.run,
        RunLifecycleCommand.ACTIVATE_PLAN,
        NoRunTransitionContext(),
        NOW + timedelta(seconds=1),
    )
    failed_run = transition_run(
        activated.run,
        RunLifecycleCommand.FAIL,
        FailureContext(RunFailurePhase.EXECUTION, failure_code),
        NOW + timedelta(seconds=3),
    )
    inspectable = InspectableRun(
        run=failed_run.run,
        work_item=stored.work_item,
        transitions=(*stored.transitions, activated.transition, failed_run.transition),
    )
    if effect is Effect.READ:
        ready = transition_step(
            plan.steps[0],
            StepLifecycleCommand.MARK_READY,
            NoStepTransitionContext(),
            NOW + timedelta(seconds=1),
        )
    else:
        awaiting = transition_step(
            plan.steps[0],
            StepLifecycleCommand.WAIT_FOR_APPROVAL,
            NoStepTransitionContext(),
            NOW + timedelta(seconds=1),
        )
        ready = transition_step(
            awaiting.step,
            StepLifecycleCommand.RELEASE_APPROVAL,
            NoStepTransitionContext(),
            NOW + timedelta(seconds=2),
        )
    failed_step = transition_step(
        ready.step,
        StepLifecycleCommand.FAIL,
        StepTerminalContext(failure_code),
        NOW + timedelta(seconds=3),
    )
    failed_plan = replace(
        plan,
        plan=replace(
            plan.plan,
            runtime_policy=replace(plan.plan.runtime_policy, run_timeout_seconds=600),
        ),
        steps=(failed_step.step,),
    )
    started_control = replace(
        control,
        run_timeout_seconds=600,
        started_at=NOW + timedelta(seconds=1),
        deadline_at=NOW + timedelta(seconds=601),
        updated_at=NOW + timedelta(seconds=1),
        version=2,
    )
    unit = _CompositeUnitOfWork(
        runs=_CompositeRuns([inspectable]),
        plan=failed_plan,
        control=started_control,
    )
    attempts = _AttemptExecutionControl(started_control, ())
    unit.execution_control = attempts  # type: ignore[assignment]
    resource = await RunResourceService(
        _CompositeFactory(unit),  # type: ignore[arg-type]
        catalog_instance_ids=(inspectable.work_item.instance_id,),
        utc_now=lambda: NOW + timedelta(seconds=4),
    ).read(inspectable.run.id, principal=_reader())
    return resource, _resource_view(resource), failed_step.step, attempts


@pytest.mark.asyncio
async def test_api_09_run_detail_exposes_terminal_retry_and_safe_adapter_cause() -> None:
    stored, plan, control, _approval, _artifact = _planned_fixture()
    activated = transition_run(
        stored.run,
        RunLifecycleCommand.ACTIVATE_PLAN,
        NoRunTransitionContext(),
        NOW + timedelta(seconds=1),
    )
    failed_run = transition_run(
        activated.run,
        RunLifecycleCommand.FAIL,
        FailureContext(RunFailurePhase.EXECUTION, "attempts_exhausted"),
        NOW + timedelta(seconds=4),
    )
    inspectable = InspectableRun(
        run=failed_run.run,
        work_item=stored.work_item,
        transitions=(*stored.transitions, activated.transition, failed_run.transition),
    )

    ready = transition_step(
        plan.steps[0],
        StepLifecycleCommand.MARK_READY,
        NoStepTransitionContext(),
        NOW + timedelta(seconds=1),
    )
    executing = transition_step(
        ready.step,
        StepLifecycleCommand.START,
        NoStepTransitionContext(),
        NOW + timedelta(seconds=2),
    )
    failed_step = transition_step(
        executing.step,
        StepLifecycleCommand.FAIL,
        StepTerminalContext("attempts_exhausted"),
        NOW + timedelta(seconds=4),
    )
    failed_plan = replace(
        plan,
        plan=replace(
            plan.plan,
            runtime_policy=replace(plan.plan.runtime_policy, run_timeout_seconds=600),
        ),
        steps=(failed_step.step,),
    )
    started_control = replace(
        control,
        run_timeout_seconds=600,
        started_at=NOW + timedelta(seconds=1),
        deadline_at=NOW + timedelta(seconds=601),
        updated_at=NOW + timedelta(seconds=1),
        version=2,
    )
    attempt = ExecutionAttempt(
        id="attempt.api09.terminal.0001",
        run_id=inspectable.run.id,
        step_id=failed_step.step.id,
        operation_key=failed_step.step.runtime_policy.operation_key,
        policy_hash=started_control.policy_hash,
        kind=failed_step.step.runtime_policy.attempt_kind,
        attempt_number=1,
        source_control_version=2,
        source_step_version=3,
        eligible_at=NOW + timedelta(seconds=2),
        reserved_at=NOW + timedelta(seconds=2),
        call_deadline_at=NOW + timedelta(seconds=3),
        input_schema_id=failed_step.step.request_schema_id or "schema.api09.input",
        redacted_input={},
        input_classification=failed_step.step.data_classification,
        outcome=AttemptOutcome.TRANSIENT_FAILURE,
        completed_at=NOW + timedelta(seconds=4),
        retry_not_before=None,
        terminal_reason_code="attempts_exhausted",
        safe_error_code="connector_timeout",
        output_artifact_id=None,
        version=2,
    )
    unit = _CompositeUnitOfWork(
        runs=_CompositeRuns([inspectable]),
        plan=failed_plan,
        control=started_control,
    )
    attempts = _AttemptExecutionControl(started_control, (attempt,))
    unit.execution_control = attempts  # type: ignore[assignment]
    service = RunResourceService(
        _CompositeFactory(unit),  # type: ignore[arg-type]
        catalog_instance_ids=(inspectable.work_item.instance_id,),
        utc_now=lambda: NOW + timedelta(seconds=5),
    )

    resource = await service.read(inspectable.run.id, principal=_reader())
    view = _resource_view(resource)

    assert resource.terminal_error is not None
    assert resource.terminal_error.code == "attempts_exhausted"
    assert resource.terminal_error.cause_code == "connector_timeout"
    assert resource.terminal_error.source == "read_attempt"
    assert resource.terminal_error.outcome == "transient_failure"
    assert resource.terminal_error.final_attempt_number == 1
    assert resource.terminal_error.retryable is False
    assert resource.terminal_error.call_deadline_at == attempt.call_deadline_at
    assert resource.terminal_error.run_deadline_at == started_control.deadline_at
    assert attempts.list_calls == [
        (failed_step.step.id, failed_step.step.runtime_policy.operation_key)
    ]
    assert view.terminal_error is not None
    assert view.terminal_error.model_dump() == {
        "code": "attempts_exhausted",
        "cause_code": "connector_timeout",
        "source": "read_attempt",
        "step_id": failed_step.step.id,
        "action_id": None,
        "outcome": "transient_failure",
        "final_attempt_number": 1,
        "retryable": False,
        "call_deadline_at": attempt.call_deadline_at,
        "run_deadline_at": started_control.deadline_at,
        "occurred_at": failed_run.run.updated_at,
        "step_url": f"/api/v1/runs/{inspectable.run.id}/steps/{failed_step.step.id}",
        "action_url": None,
    }

    unit.execution_control = _AttemptExecutionControl(
        started_control,
        (replace(attempt, policy_hash="8" * 64),),
    )  # type: ignore[assignment]
    with pytest.raises(RunResourceServiceError) as invalid_lineage:
        await service.read(inspectable.run.id, principal=_reader())
    assert invalid_lineage.value.code == "runtime_record_corrupt"

    denied_run = transition_run(
        activated.run,
        RunLifecycleCommand.FAIL,
        FailureContext(RunFailurePhase.EXECUTION, "output_payload_too_large"),
        NOW + timedelta(seconds=4),
    )
    denied_step = transition_step(
        executing.step,
        StepLifecycleCommand.FAIL,
        StepTerminalContext("output_payload_too_large"),
        NOW + timedelta(seconds=4),
    )
    denied_inspectable = InspectableRun(
        run=denied_run.run,
        work_item=stored.work_item,
        transitions=(*stored.transitions, activated.transition, denied_run.transition),
    )
    denied_plan = replace(failed_plan, steps=(denied_step.step,))
    denied_attempt = replace(
        attempt,
        outcome=AttemptOutcome.PERMANENT_FAILURE,
        terminal_reason_code="permanent_failure",
        safe_error_code="output_payload_too_large",
    )
    denied_unit = _CompositeUnitOfWork(
        runs=_CompositeRuns([denied_inspectable]),
        plan=denied_plan,
        control=started_control,
    )
    denied_unit.execution_control = _AttemptExecutionControl(
        started_control,
        (denied_attempt,),
    )  # type: ignore[assignment]
    denied_resource = await RunResourceService(
        _CompositeFactory(denied_unit),  # type: ignore[arg-type]
        catalog_instance_ids=(denied_inspectable.work_item.instance_id,),
        utc_now=lambda: NOW + timedelta(seconds=5),
    ).read(denied_inspectable.run.id, principal=_reader())
    assert denied_resource.terminal_error is not None
    assert denied_resource.terminal_error.code == "output_payload_too_large"
    assert denied_resource.terminal_error.cause_code == "output_payload_too_large"
    assert denied_resource.terminal_error.outcome == "permanent_failure"

    impossible_run = transition_run(
        activated.run,
        RunLifecycleCommand.FAIL,
        FailureContext(RunFailurePhase.EXECUTION, "input_schema_invalid"),
        NOW + timedelta(seconds=4),
    )
    impossible_step = transition_step(
        executing.step,
        StepLifecycleCommand.FAIL,
        StepTerminalContext("input_schema_invalid"),
        NOW + timedelta(seconds=4),
    )
    impossible_inspectable = InspectableRun(
        run=impossible_run.run,
        work_item=stored.work_item,
        transitions=(*stored.transitions, activated.transition, impossible_run.transition),
    )
    impossible_unit = _CompositeUnitOfWork(
        runs=_CompositeRuns([impossible_inspectable]),
        plan=replace(failed_plan, steps=(impossible_step.step,)),
        control=started_control,
    )
    impossible_unit.execution_control = _AttemptExecutionControl(
        started_control,
        (
            replace(
                attempt,
                outcome=AttemptOutcome.PERMANENT_FAILURE,
                terminal_reason_code="permanent_failure",
                safe_error_code="input_schema_invalid",
            ),
        ),
    )  # type: ignore[assignment]
    with pytest.raises(RunResourceServiceError) as impossible_lineage:
        await RunResourceService(
            _CompositeFactory(impossible_unit),  # type: ignore[arg-type]
            catalog_instance_ids=(impossible_inspectable.work_item.instance_id,),
            utc_now=lambda: NOW + timedelta(seconds=5),
        ).read(impossible_inspectable.run.id, principal=_reader())
    assert impossible_lineage.value.code == "runtime_record_corrupt"


@pytest.mark.asyncio
async def test_api_09_read_failure_without_attempt_projects_step_error() -> None:
    resource, view, step, attempts = await _step_only_terminal_projection(
        effect=Effect.READ,
        failure_code="model_budget_exhausted",
    )

    assert resource.terminal_error is not None
    assert resource.terminal_error.source == "step"
    assert resource.terminal_error.step_id == step.id
    assert resource.terminal_error.cause_code is None
    assert attempts.list_calls == [(step.id, step.runtime_policy.operation_key)]
    assert view.terminal_error is not None
    assert view.terminal_error.source == "step"
    assert view.terminal_error.step_url.endswith(f"/steps/{step.id}")


@pytest.mark.asyncio
async def test_api_09_pre_call_write_failure_without_action_projects_step_error() -> None:
    resource, view, step, attempts = await _step_only_terminal_projection(
        effect=Effect.WRITE,
        failure_code="pre_call_attempts_exhausted",
    )

    assert resource.terminal_error is not None
    assert resource.terminal_error.source == "step"
    assert resource.terminal_error.step_id == step.id
    assert resource.terminal_error.action_id is None
    assert attempts.list_calls == []
    assert view.terminal_error is not None
    assert view.terminal_error.source == "step"
    assert view.terminal_error.action_url is None


@pytest.mark.asyncio
async def test_api_09_unknown_terminal_identifier_fails_closed() -> None:
    with pytest.raises(RunResourceServiceError) as unsafe:
        await _step_only_terminal_projection(
            effect=Effect.READ,
            failure_code="provider_specific_secret_code",
        )

    assert unsafe.value.code == "runtime_record_corrupt"


@pytest.mark.asyncio
async def test_api_09_post_call_denial_without_attempt_fails_closed() -> None:
    with pytest.raises(RunResourceServiceError) as missing_attempt:
        await _step_only_terminal_projection(
            effect=Effect.READ,
            failure_code="output_payload_too_large",
        )

    assert missing_attempt.value.code == "runtime_record_corrupt"
