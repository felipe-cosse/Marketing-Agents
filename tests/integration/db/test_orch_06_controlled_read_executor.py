"""ORCH-06: controlled READ calls compose durable attempts and step audits."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import (
    EffectPlan,
    OrchestrationDependencies,
)
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterCancelledError,
    ReadAdapterContract,
    ReadAdapterPermanentError,
    ReadAdapterRequest,
    ReadAdapterResult,
    ReadAdapterTransientError,
)
from marketing_agents.application.ports.repositories import (
    ExecutionControlRepositoryConflict,
)
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.application.ports.unit_of_work import UnitOfWorkFactory
from marketing_agents.application.services import (
    AuditedPlanPersistenceService,
    ControlledReadCommand,
    ControlledReadExecutor,
    ControlledReadExecutorError,
    ExecutionActivationService,
    IdempotentWorkRunReceiptService,
    ReadExecutionClassification,
    RunCancellationService,
    RunLifecycleService,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import RunState, StepState, WorkMode
from marketing_agents.domain.execution_control import (
    AttemptOutcome,
    ExpiredAttemptRecoveryCommand,
    OperationExecutionPolicy,
    fixed_window_start,
)
from marketing_agents.domain.plan_hash import EffectPlanStepHashMaterial, effect_plan_hash
from marketing_agents.domain.run_lifecycle import (
    FailureContext,
    NoRunTransitionContext,
    RunFailurePhase,
    RunLifecycleCommand,
)
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    RateLimitPolicySnapshot,
    RetryBackoff,
    RetryPolicySnapshot,
    TimeoutPolicySnapshot,
    canonical_payload_size_bytes,
    runtime_rate_limit_key,
)
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models import ExecutionAttemptRecord
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import select

from tests.support.execution_control import execution_control_repository
from tests.support.incoming_work import TEST_CATALOG_HASH, validate_incoming_for_test
from tests.support.orch_09_planning import build_read_only_plan
from tests.support.read_adapter import ExactReadContractAdapter, observation_for

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
RECEIPT_KEY = DigestKey(bytes(range(32)))


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class IncrementingIds:
    def __init__(self) -> None:
        self.next_value = 0

    def new(self, namespace: str) -> str:
        self.next_value += 1
        return f"{namespace}.orch-06-executor.{self.next_value:04d}"


def _audit_context(label: str) -> AuditContext:
    return AuditContext.worker(
        "worker.orch-06-executor",
        correlation_id=f"correlation.orch-06-executor.{label}",
    )


def _uow_factory(runtime: DatabaseRuntime) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            artifacts=SQLAlchemyArtifactRepository,
            run_steps=SQLAlchemyRunStepRepository,
            execution_control=execution_control_repository,
        ),
    )


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


def _envelope(suffix: str) -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id=f"event.orch-06-executor.{suffix}",
        instance_id="instance.orch-06-executor.target",
        trigger_id="trigger.orch-06-executor.manual",
        workflow_id="workflow.orch-06-executor",
        mode=WorkMode.MOCK_EXECUTION,
        brief_id=None,
        brief_revision=None,
        configuration_revision=1,
        admitted_payload={"query": "safe"},
    )


def _step_hash_material(step) -> EffectPlanStepHashMaterial:  # type: ignore[no-untyped-def]
    return EffectPlanStepHashMaterial(
        step_key=step.step_key,
        kind=step.kind,
        selected_instance_id=step.selected_instance_id,
        routing_slot_key=step.routing_slot_key,
        template_id=step.template_id,
        configuration_revision=step.configuration_revision,
        capability_id=step.capability_id,
        effect=step.effect,
        connector_family=step.connector_family,
        binding_id=step.binding_id,
        binding_configuration_revision=step.binding_configuration_revision,
        request_schema_id=step.request_schema_id,
        result_schema_id=step.result_schema_id,
        result_schema_hash=step.result_schema_hash,
        request_redaction_fields=step.request_redaction_fields,
        result_redaction_fields=step.result_redaction_fields,
        data_classification=step.data_classification,
        idempotency_support=step.idempotency_support,
        connector_timeout_seconds=step.connector_timeout_seconds,
        approval_policy_id=step.approval_policy_id,
        approval_required_roles=step.approval_required_roles,
        approval_required_scopes=step.approval_required_scopes,
        approval_expires_after_seconds=step.approval_expires_after_seconds,
        approval_allow_self_approval=step.approval_allow_self_approval,
        runtime_policy=step.runtime_policy,
    )


def _with_attempt_policy(
    plan: EffectPlan,
    *,
    max_attempts: int = 1,
    step_timeout_seconds: int = 60,
    rate_max_calls: int = 100,
    max_input_bytes: int | None = None,
    max_input_field_bytes: int | None = None,
    max_output_bytes: int | None = None,
    max_model_output_tokens: int | None = None,
    data_classification: DataClassification | None = None,
) -> EffectPlan:
    steps = tuple(
        replace(
            step,
            kind=("connector.read" if data_classification is not None else step.kind),
            connector_family=("crm" if data_classification is not None else step.connector_family),
            binding_id=("binding.test.run-06.crm" if data_classification is not None else None),
            binding_configuration_revision=(
                step.configuration_revision if data_classification is not None else None
            ),
            connector_timeout_seconds=(
                step_timeout_seconds if data_classification is not None else None
            ),
            data_classification=(
                step.data_classification if data_classification is None else data_classification
            ),
            runtime_policy=replace(
                step.runtime_policy,
                attempt_kind=(
                    AttemptKind.TOOL
                    if data_classification is not None
                    else step.runtime_policy.attempt_kind
                ),
                retry=RetryPolicySnapshot(
                    max_attempts,
                    RetryBackoff.BOUNDED_EXPONENTIAL if max_attempts > 1 else RetryBackoff.NONE,
                ),
                timeout=TimeoutPolicySnapshot(
                    step_timeout_seconds,
                    step.runtime_policy.timeout.run_seconds,
                ),
                budget=replace(
                    step.runtime_policy.budget,
                    max_input_bytes=(
                        step.runtime_policy.budget.max_input_bytes
                        if max_input_bytes is None
                        else max_input_bytes
                    ),
                    max_input_field_bytes=(
                        step.runtime_policy.budget.max_input_field_bytes
                        if max_input_field_bytes is None
                        else max_input_field_bytes
                    ),
                    max_output_bytes=(
                        step.runtime_policy.budget.max_output_bytes
                        if max_output_bytes is None
                        else max_output_bytes
                    ),
                    max_model_output_tokens=(
                        step.runtime_policy.budget.max_model_output_tokens
                        if max_model_output_tokens is None
                        else max_model_output_tokens
                    ),
                ),
                rate_limit=RateLimitPolicySnapshot(
                    scope=step.runtime_policy.rate_limit.scope,
                    key=runtime_rate_limit_key(
                        template_id=step.template_id,
                        max_calls=rate_max_calls,
                        window_seconds=step.runtime_policy.rate_limit.window_seconds,
                    ),
                    max_calls=rate_max_calls,
                    window_seconds=step.runtime_policy.rate_limit.window_seconds,
                ),
            ),
        )
        for step in plan.steps
    )
    plan_hash = effect_plan_hash(
        workflow_id=plan.workflow_id,
        workflow_version=plan.workflow_version,
        workflow_definition_hash=plan.workflow_definition_hash,
        catalog_content_hash=plan.catalog_content_hash,
        graph_hash=plan.graph_hash,
        routing_hash=plan.routing_hash,
        run_policy=plan.run_policy,
        steps=tuple(_step_hash_material(step) for step in steps),
    )
    return replace(plan, steps=steps, plan_hash=plan_hash)


@dataclass(frozen=True, slots=True)
class PreparedRead:
    runtime: DatabaseRuntime
    dependencies: OrchestrationDependencies
    clock: MutableClock
    step_id: str
    run_id: str
    operation_key: str


async def _prepare(
    path: Path,
    *,
    max_attempts: int = 1,
    step_timeout_seconds: int = 60,
    rate_max_calls: int = 100,
    max_input_bytes: int | None = None,
    max_input_field_bytes: int | None = None,
    max_output_bytes: int | None = None,
    max_model_output_tokens: int | None = None,
    data_classification: DataClassification | None = None,
    output_schema: Mapping[str, object] | None = None,
) -> PreparedRead:
    runtime = await _runtime(path)
    clock = MutableClock()
    dependencies = OrchestrationDependencies(clock, IncrementingIds(), _uow_factory(runtime))
    envelope = _envelope(path.stem)
    received = await IdempotentWorkRunReceiptService(
        dependencies,
        RECEIPT_KEY,
        current_catalog_hash=TEST_CATALOG_HASH,
    ).receive(
        validate_incoming_for_test(envelope),
        audit_context=_audit_context("receive"),
    )
    validated = await RunLifecycleService(dependencies).advance(
        received.run.id,
        received.run.version,
        RunLifecycleCommand.MARK_VALIDATED,
        NoRunTransitionContext(),
        audit_context=_audit_context("validate"),
    )
    plan, graph, routing = build_read_only_plan(
        run_id=validated.run.id,
        workflow_id=envelope.workflow_id,
        target_instance_id=envelope.instance_id,
        configuration_revision=envelope.configuration_revision,
        catalog_hash=validated.run.catalog_hash,
        output_schema=output_schema,
    )
    plan = _with_attempt_policy(
        plan,
        max_attempts=max_attempts,
        step_timeout_seconds=step_timeout_seconds,
        rate_max_calls=rate_max_calls,
        max_input_bytes=max_input_bytes,
        max_input_field_bytes=max_input_field_bytes,
        max_output_bytes=max_output_bytes,
        max_model_output_tokens=max_model_output_tokens,
        data_classification=data_classification,
    )
    persisted = await AuditedPlanPersistenceService(dependencies).persist(
        plan,
        graph,
        routing,
        expected_run_version=validated.run.version,
        audit_context=_audit_context("plan"),
    )
    activated = await ExecutionActivationService(dependencies).activate(
        persisted.run.id,
        audit_context=_audit_context("activate"),
    )
    assert activated.run.state is RunState.EXECUTING
    assert len(activated.steps) == 1 and activated.steps[0].state is StepState.READY
    return PreparedRead(
        runtime,
        dependencies,
        clock,
        activated.steps[0].id,
        activated.run.id,
        activated.steps[0].runtime_policy.operation_key,
    )


class DurableObservationAdapter(ExactReadContractAdapter):
    def __init__(self, prepared: PreparedRead) -> None:
        self.prepared = prepared
        self.calls: list[ReadAdapterRequest] = []

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        async with self.prepared.dependencies.unit_of_work() as unit_of_work:
            attempt = await unit_of_work.execution_control.get_attempt(request.attempt_id)
            step = await unit_of_work.run_steps.get(request.step_id)
            timeline = await unit_of_work.audits.list_run(request.run_id)
        assert attempt is not None and attempt.outcome is None
        assert step is not None and step.state is StepState.EXECUTING
        assert tuple(event.event_type for event in timeline[-2:]) == (
            "step.transitioned",
            "attempt.reserved",
        )
        assert timeline[-2].safe_metadata.values["command"] == "start"
        assert timeline[-1].attempt_id == request.attempt_id
        return observation_for(request, {"observation": "committed"})


class SequenceAdapter(ExactReadContractAdapter):
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[ReadAdapterRequest] = []

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, Mapping)
        return observation_for(request, outcome)


class StrictResultSchemaAdapter(SequenceAdapter):
    def output_contract_for(
        self,
        operation: OperationExecutionPolicy,
    ) -> RuntimeOutputContract:
        if operation.result_schema_id is None:
            raise ValueError("strict test operation requires a result schema")
        return RuntimeOutputContract(
            schema_id=operation.result_schema_id,
            schema_version="v1",
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["accepted"],
                "properties": {"accepted": {"type": "boolean"}},
            },
            classification=operation.data_classification,
            provider_kind="llm",
            provider_mode="mock",
            provider_name="strict-run-06-test",
            provider_version="v1",
        )


class BudgetResultAdapter(ExactReadContractAdapter):
    def __init__(
        self,
        output_payload: Mapping[str, object],
        *,
        model_output_tokens: int | None,
        forge_missing_tokens: bool = False,
    ) -> None:
        self.output_payload = output_payload
        self.model_output_tokens = model_output_tokens
        self.forge_missing_tokens = forge_missing_tokens
        self.calls: list[ReadAdapterRequest] = []

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        result = observation_for(
            request,
            self.output_payload,
            model_output_tokens=self.model_output_tokens,
        )
        if self.forge_missing_tokens:
            object.__setattr__(result, "model_output_tokens", None)
        return result


class DriftingOutputSchemaAdapter(SequenceAdapter):
    def output_contract_for(
        self,
        operation: OperationExecutionPolicy,
    ) -> RuntimeOutputContract:
        return RuntimeOutputContract(
            schema_id=operation.result_schema_id,
            schema_version="v1",
            schema={"type": "object", "additionalProperties": False},
            classification=operation.data_classification,
            provider_kind="llm",
            provider_mode="mock",
            provider_name="drifted-schema-test",
            provider_version="v1",
        )


class SlowAdapter(ExactReadContractAdapter):
    def __init__(self) -> None:
        self.started = False
        self.cancelled = False

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.started = True
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return observation_for(request, {"late": True})


class CancellationSwallowingLateAdapter(ExactReadContractAdapter):
    def __init__(self, clock: MutableClock, *, past_deadline: bool) -> None:
        self.clock = clock
        self.past_deadline = past_deadline
        self.calls: list[ReadAdapterRequest] = []
        self.swallowed_cancellation = False

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            self.swallowed_cancellation = True
            self.clock.current = request.call_deadline_at + (
                timedelta(microseconds=1) if self.past_deadline else timedelta(0)
            )
            return observation_for(request, {"must_not": "become-success"})
        raise AssertionError("bounded executor failed to cancel the late adapter")


class SimulatedWorkerCrash(BaseException):
    pass


class CrashAfterCommittedReservationAdapter(ExactReadContractAdapter):
    def __init__(self) -> None:
        self.calls: list[ReadAdapterRequest] = []

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        raise SimulatedWorkerCrash("simulated process termination")


class UnavailableContractAdapter(ExactReadContractAdapter):
    def __init__(self) -> None:
        self.contract_calls = 0
        self.calls: list[ReadAdapterRequest] = []

    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        self.contract_calls += 1
        raise ReadAdapterPermanentError(
            "adapter_contract_unavailable",
            "safe unavailable adapter contract",
        )

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        raise AssertionError("unavailable adapter contract authorized a READ call")


class RestartIds:
    def __init__(self) -> None:
        self.next_value = 0

    def new(self, namespace: str) -> str:
        self.next_value += 1
        return f"{namespace}.orch-06-restart.{self.next_value:04d}"


def _restart_dependencies(prepared: PreparedRead) -> OrchestrationDependencies:
    return OrchestrationDependencies(
        prepared.clock,
        RestartIds(),
        _uow_factory(prepared.runtime),
    )


class CancellingAdapter(ExactReadContractAdapter):
    def __init__(self, prepared: PreparedRead, *, return_success: bool) -> None:
        self.prepared = prepared
        self.return_success = return_success

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        await RunCancellationService(self.prepared.dependencies).request(
            request.run_id,
            audit_context=_audit_context("cancel-during-call"),
        )
        if not self.return_success:
            raise ReadAdapterTransientError("upstream_unavailable", "safe transient failure")
        return observation_for(request, {"returned": True})


class ParentTerminalizingAdapter(ExactReadContractAdapter):
    def __init__(self, prepared: PreparedRead, *, return_success: bool) -> None:
        self.prepared = prepared
        self.return_success = return_success
        self.calls: list[ReadAdapterRequest] = []

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        async with self.prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(request.run_id)
        assert run is not None and run.state is RunState.EXECUTING
        await RunLifecycleService(self.prepared.dependencies).advance(
            run.id,
            run.version,
            RunLifecycleCommand.FAIL,
            FailureContext(RunFailurePhase.EXECUTION, "model_budget_exhausted"),
            audit_context=_audit_context("parallel-terminal-parent"),
        )
        if self.return_success:
            return observation_for(request, {"returned": True})
        raise ReadAdapterTransientError("upstream_unavailable", "safe transient failure")


@pytest.mark.asyncio
async def test_run_06_same_schema_id_changed_body_precedes_attempt_and_budget_mutation(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(tmp_path / "result-schema-content-drift.db")
    adapter = DriftingOutputSchemaAdapter([{"must": "not-call"}])
    try:
        with pytest.raises(ControlledReadExecutorError) as captured:
            await ControlledReadExecutor(prepared.dependencies, adapter).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("result-schema-content-drift"),
            )
        assert captured.value.code == "adapter_contract_drift"
        assert adapter.calls == []
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(prepared.run_id)
            step = await unit_of_work.run_steps.get(prepared.step_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            control = await unit_of_work.execution_control.get(prepared.run_id)
        assert run is not None and run.state is RunState.FAILED
        assert run.terminal_reason_code == "adapter_contract_drift"
        assert step is not None and step.state is StepState.FAILED
        assert step.terminal_reason_code == "adapter_contract_drift"
        assert attempts == ()
        assert control is not None and control.model_calls == 0
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_first_start_commits_before_adapter_and_success_completes_later(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(tmp_path / "commit-before-call.db")
    adapter = DurableObservationAdapter(prepared)
    try:
        secret_canary = "run06-attempt-secret-canary"
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(
                prepared.step_id,
                {"query": "safe", "api_token": secret_canary},
            ),
            audit_context=_audit_context("execute-success"),
        )

        assert result.classification is ReadExecutionClassification.SUCCEEDED
        assert result.step.state is StepState.SUCCEEDED
        assert result.output is not None
        assert result.output.output_payload == {"observation": "committed"}
        assert len(adapter.calls) == 1
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            attempt = await unit_of_work.execution_control.get_attempt(result.attempt.id)
            step = await unit_of_work.run_steps.get(prepared.step_id)
            operation = await unit_of_work.execution_control.get_operation(
                prepared.step_id,
                prepared.operation_key,
            )
            artifact = await unit_of_work.artifacts.get(result.attempt.output_artifact_id or "")
            timeline = await unit_of_work.audits.list_run(prepared.run_id)
        assert attempt == result.attempt and attempt.outcome is AttemptOutcome.SUCCEEDED
        assert attempt.redacted_input == {"query": "safe", "api_token": "[REDACTED]"}
        assert attempt.output_artifact_id is not None
        assert artifact == result.artifact and artifact is not None
        assert operation is not None
        assert artifact.provenance.output_schema_id == result.step.result_schema_id
        assert (
            artifact.provenance.output_schema_hash
            == adapter.output_contract_for(operation).schema_hash
        )
        assert step == result.step
        assert secret_canary not in str([event.safe_metadata.values for event in timeline])
        step_events = [event for event in timeline if event.event_type == "step.transitioned"]
        assert [event.safe_metadata.values["command"] for event in step_events[-2:]] == [
            "start",
            "succeed",
        ]
        assert tuple(event.event_type for event in timeline[-3:]) == (
            "attempt.completed",
            "artifact.persisted",
            "step.transitioned",
        )
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_run_06_personal_attempt_input_is_fully_masked_with_incomplete_pointers(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "personal-input-mask.db",
        data_classification=DataClassification.PERSONAL,
    )
    adapter = DurableObservationAdapter(prepared)
    neutral_canary = "customer-reference-run06-canary"
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(
                prepared.step_id,
                {"query": "safe", "opaque_ref": neutral_canary},
            ),
            audit_context=_audit_context("personal-input-mask"),
        )

        assert result.attempt.redacted_input == {"$redacted": "[REDACTED]"}
        async with prepared.runtime.session_factory() as session:
            record = await session.scalar(
                select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.id == result.attempt.id)
            )
        assert record is not None
        assert neutral_canary not in str(record.redacted_input)
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            timeline = await unit_of_work.audits.list_run(prepared.run_id)
        assert neutral_canary not in str([event.safe_metadata.values for event in timeline])
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_run_06_schema_invalid_output_is_safe_and_has_no_artifact(
    tmp_path: Path,
) -> None:
    strict_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["accepted"],
        "properties": {"accepted": {"type": "boolean"}},
    }
    prepared = await _prepare(
        tmp_path / "run-06-schema-invalid.db",
        output_schema=strict_schema,
    )
    output_canary = "run06-provider-output-secret-canary"
    adapter = StrictResultSchemaAdapter(
        [{"accepted": "not-a-boolean", "provider_detail": output_canary}]
    )
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("run-06-schema-invalid"),
        )

        assert result.classification is ReadExecutionClassification.PERMANENT_FAILURE
        assert result.output is None and result.artifact is None
        assert result.attempt.safe_error_code == "output_schema_invalid"
        assert result.attempt.output_artifact_id is None
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            artifacts = await unit_of_work.artifacts.list_for_run(prepared.run_id)
            timeline = await unit_of_work.audits.list_run(prepared.run_id)
        completion = next(event for event in timeline if event.event_type == "attempt.completed")
        assert artifacts == ()
        assert completion.safe_metadata.values["safe_error_code"] == "output_schema_invalid"
        assert output_canary not in str([event.safe_metadata.values for event in timeline])
        assert any(
            event.event_type == "runtime.control_denied"
            and event.safe_metadata.values["denial_code"] == "output_schema_invalid"
            for event in timeline
        )
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_transient_retry_leaves_step_executing_then_succeeds_without_restarting(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(tmp_path / "retry.db", max_attempts=2)
    adapter = SequenceAdapter(
        [
            ReadAdapterTransientError("temporary_failure", "safe transient failure"),
            {"retry": "ok"},
        ]
    )
    executor = ControlledReadExecutor(prepared.dependencies, adapter)
    try:
        first = await executor.execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("execute-transient"),
        )
        assert first.classification is ReadExecutionClassification.TRANSIENT_FAILURE
        assert first.retryable and first.step.state is StepState.EXECUTING
        assert first.retry_not_before is not None

        prepared.clock.current = first.retry_not_before
        second = await executor.execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("execute-retry"),
        )
        assert second.classification is ReadExecutionClassification.SUCCEEDED
        assert second.step.state is StepState.SUCCEEDED
        assert [request.attempt_number for request in adapter.calls] == [1, 2]
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id, prepared.operation_key
            )
            control = await unit_of_work.execution_control.get(prepared.run_id)
            transitions = await unit_of_work.run_steps.list_transitions(prepared.step_id)
        assert len(attempts) == 2
        assert control is not None and control.model_calls == 1
        assert [transition.command.value for transition in transitions[-2:]] == [
            "start",
            "succeed",
        ]
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_factory", "classification", "outcome"),
    [
        (
            lambda: ReadAdapterPermanentError("invalid_request", "safe permanent failure"),
            ReadExecutionClassification.PERMANENT_FAILURE,
            AttemptOutcome.PERMANENT_FAILURE,
        ),
        (
            lambda: ReadAdapterCancelledError("adapter_cancelled", "safe cancellation"),
            ReadExecutionClassification.CANCELLED,
            AttemptOutcome.CANCELLED,
        ),
    ],
)
async def test_orch_06_permanent_and_adapter_cancellation_are_terminally_classified(
    tmp_path: Path,
    failure_factory: Callable[[], BaseException],
    classification: ReadExecutionClassification,
    outcome: AttemptOutcome,
) -> None:
    prepared = await _prepare(tmp_path / f"terminal-{classification.value}.db")
    adapter = SequenceAdapter([failure_factory()])
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context(f"execute-{classification.value}"),
        )
        assert result.classification is classification
        assert result.attempt.outcome is outcome
        assert result.step.state is StepState.FAILED
        assert not result.retryable
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_effective_attempt_timeout_cancels_adapter_and_fails_exhausted_step(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(tmp_path / "timeout.db", step_timeout_seconds=1)
    adapter = SlowAdapter()
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("execute-timeout"),
        )
        assert adapter.started and adapter.cancelled
        assert result.classification is ReadExecutionClassification.TIMED_OUT
        assert result.attempt.outcome is AttemptOutcome.TRANSIENT_FAILURE
        assert result.attempt.terminal_reason_code == "attempts_exhausted"
        assert result.step.state is StepState.FAILED
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("past_deadline", (False, True))
async def test_orch_06_adapter_cannot_swallow_timeout_and_return_success(
    tmp_path: Path,
    past_deadline: bool,
) -> None:
    prepared = await _prepare(
        tmp_path / f"swallowed-timeout-{past_deadline}.db",
        step_timeout_seconds=1,
    )
    adapter = CancellationSwallowingLateAdapter(
        prepared.clock,
        past_deadline=past_deadline,
    )
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context(f"swallowed-timeout-{past_deadline}"),
        )

        assert adapter.swallowed_cancellation
        assert len(adapter.calls) == 1
        assert prepared.clock.current >= adapter.calls[0].call_deadline_at
        assert result.classification is ReadExecutionClassification.TIMED_OUT
        assert result.output is None
        assert result.attempt.outcome is AttemptOutcome.TRANSIENT_FAILURE
        assert result.attempt.terminal_reason_code == "attempts_exhausted"
        assert result.step.state is StepState.FAILED
        assert result.step.terminal_reason_code == "attempts_exhausted"
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_attempts", (1, 2))
async def test_orch_06_restart_expires_open_attempt_then_retries_or_fails_terminally(
    tmp_path: Path,
    max_attempts: int,
) -> None:
    prepared = await _prepare(
        tmp_path / f"open-attempt-restart-{max_attempts}.db",
        max_attempts=max_attempts,
        step_timeout_seconds=10,
    )
    crashing = CrashAfterCommittedReservationAdapter()
    try:
        with pytest.raises(SimulatedWorkerCrash):
            await ControlledReadExecutor(prepared.dependencies, crashing).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context(f"open-attempt-crash-{max_attempts}"),
            )
        assert len(crashing.calls) == 1

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            initial_attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            initial_step = await unit_of_work.run_steps.get(prepared.step_id)
            initial_control = await unit_of_work.execution_control.get(prepared.run_id)
        assert len(initial_attempts) == 1
        open_attempt = initial_attempts[0]
        assert open_attempt.outcome is None
        assert initial_step is not None and initial_step.state is StepState.EXECUTING
        assert initial_control is not None and initial_control.model_calls == 1

        restarted_dependencies = _restart_dependencies(prepared)
        retry_adapter = SequenceAdapter([{"restart": "ok"}])
        restarted = ControlledReadExecutor(restarted_dependencies, retry_adapter)
        prepared.clock.current = open_attempt.call_deadline_at - timedelta(seconds=1)
        with pytest.raises(ControlledReadExecutorError) as waiting:
            await restarted.execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context(f"open-attempt-wait-{max_attempts}"),
            )
        assert waiting.value.code == "attempt_in_progress"
        assert waiting.value.retry_after_seconds == 1
        assert retry_adapter.calls == []
        async with restarted_dependencies.unit_of_work() as unit_of_work:
            waiting_attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            waiting_control = await unit_of_work.execution_control.get(prepared.run_id)
        assert waiting_attempts == (open_attempt,)
        assert waiting_control == initial_control

        prepared.clock.current = open_attempt.call_deadline_at
        recovered = await restarted.execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context(f"open-attempt-expire-{max_attempts}"),
        )
        assert recovered.classification is ReadExecutionClassification.TIMED_OUT
        assert recovered.output is None
        assert recovered.attempt.id == open_attempt.id
        assert recovered.attempt.completed_at == open_attempt.call_deadline_at
        assert recovered.attempt.outcome is AttemptOutcome.TRANSIENT_FAILURE
        assert retry_adapter.calls == []

        if max_attempts == 1:
            assert recovered.retry_not_before is None
            assert recovered.attempt.terminal_reason_code == "attempts_exhausted"
            assert recovered.step.state is StepState.FAILED
        else:
            assert recovered.retry_not_before is not None
            assert recovered.step.state is StepState.EXECUTING
            prepared.clock.current = recovered.retry_not_before
            succeeded = await restarted.execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("open-attempt-retry"),
            )
            assert succeeded.classification is ReadExecutionClassification.SUCCEEDED
            assert succeeded.step.state is StepState.SUCCEEDED
            assert succeeded.output is not None
            assert succeeded.output.output_payload == {"restart": "ok"}
            assert [request.attempt_number for request in retry_adapter.calls] == [2]

        async with restarted_dependencies.unit_of_work() as unit_of_work:
            final_attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            final_control = await unit_of_work.execution_control.get(prepared.run_id)
        assert len(final_attempts) == max_attempts
        assert final_control is not None and final_control.model_calls == 1
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_open_attempt_recovery_precedes_replacement_adapter_contract(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "open-attempt-contract-unavailable.db",
        max_attempts=2,
        step_timeout_seconds=10,
    )
    crashing = CrashAfterCommittedReservationAdapter()
    try:
        with pytest.raises(SimulatedWorkerCrash):
            await ControlledReadExecutor(prepared.dependencies, crashing).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("open-attempt-unavailable-crash"),
            )
        assert len(crashing.calls) == 1

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            initial_control = await unit_of_work.execution_control.get(prepared.run_id)
        assert len(attempts) == 1 and attempts[0].outcome is None
        open_attempt = attempts[0]
        assert initial_control is not None and initial_control.model_calls == 1

        unavailable = UnavailableContractAdapter()
        restarted = ControlledReadExecutor(_restart_dependencies(prepared), unavailable)
        prepared.clock.current = open_attempt.call_deadline_at - timedelta(seconds=1)
        with pytest.raises(ControlledReadExecutorError) as waiting:
            await restarted.execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("open-attempt-unavailable-wait"),
            )
        assert waiting.value.code == "attempt_in_progress"
        assert waiting.value.retry_after_seconds == 1
        assert unavailable.contract_calls == 0
        assert unavailable.calls == []

        prepared.clock.current = open_attempt.call_deadline_at
        recovered = await restarted.execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("open-attempt-unavailable-recover"),
        )
        assert recovered.classification is ReadExecutionClassification.TIMED_OUT
        assert recovered.attempt.id == open_attempt.id
        assert recovered.attempt.completed_at == open_attempt.call_deadline_at
        assert recovered.attempt.outcome is AttemptOutcome.TRANSIENT_FAILURE
        assert recovered.retry_not_before is not None
        assert recovered.step.state is StepState.EXECUTING
        assert unavailable.contract_calls == 0
        assert unavailable.calls == []

        prepared.clock.current = recovered.retry_not_before
        with pytest.raises(ControlledReadExecutorError) as unavailable_error:
            await restarted.execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("open-attempt-unavailable-retry"),
            )
        assert unavailable_error.value.code == "adapter_contract_unavailable"
        assert unavailable.contract_calls == 1
        assert unavailable.calls == []
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            final_attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            final_control = await unit_of_work.execution_control.get(prepared.run_id)
            final_step = await unit_of_work.run_steps.get(prepared.step_id)
            final_run = await unit_of_work.runs.get(prepared.run_id)
        assert final_attempts == (recovered.attempt,)
        assert final_control is not None and final_control.model_calls == 1
        assert final_step is not None and final_step.state is StepState.FAILED
        assert final_step.terminal_reason_code == "adapter_contract_unavailable"
        assert final_run is not None and final_run.state is RunState.FAILED
        assert final_run.terminal_reason_code == "adapter_contract_unavailable"
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_expired_attempt_recovery_has_one_cas_winner_and_exact_replay(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "expired-attempt-recovery-replay.db",
        max_attempts=2,
        step_timeout_seconds=10,
    )
    crashing = CrashAfterCommittedReservationAdapter()
    try:
        with pytest.raises(SimulatedWorkerCrash):
            await ControlledReadExecutor(prepared.dependencies, crashing).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("expired-attempt-replay-crash"),
            )
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
        assert len(attempts) == 1 and attempts[0].outcome is None
        open_attempt = attempts[0]
        command = ExpiredAttemptRecoveryCommand(
            attempt_id=open_attempt.id,
            expected_attempt_version=open_attempt.version,
            expected_call_deadline_at=open_attempt.call_deadline_at,
            recovered_at=open_attempt.call_deadline_at,
        )

        first_uow = prepared.dependencies.unit_of_work()
        async with first_uow:
            first = await first_uow.execution_control.recover_expired_attempt(command)
            await first_uow.commit()
        assert first.completed
        assert first.attempt.outcome is AttemptOutcome.TRANSIENT_FAILURE
        assert first.attempt.completed_at == open_attempt.call_deadline_at
        assert first.retry_not_before is not None

        second_uow = prepared.dependencies.unit_of_work()
        async with second_uow:
            replay = await second_uow.execution_control.recover_expired_attempt(command)
            await second_uow.commit()
        assert not replay.completed
        assert replay.attempt == first.attempt

        conflicting = ExpiredAttemptRecoveryCommand(
            attempt_id=open_attempt.id,
            expected_attempt_version=open_attempt.version,
            expected_call_deadline_at=open_attempt.call_deadline_at + timedelta(seconds=1),
            recovered_at=open_attempt.call_deadline_at + timedelta(seconds=1),
        )
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            with pytest.raises(ExecutionControlRepositoryConflict) as conflict:
                await unit_of_work.execution_control.recover_expired_attempt(conflicting)
        assert conflict.value.code == "attempt_completion_conflict"

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            persisted_attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            control = await unit_of_work.execution_control.get(prepared.run_id)
        assert persisted_attempts == (first.attempt,)
        assert control is not None and control.model_calls == 1
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_restart_honors_cancellation_before_open_attempt_deadline(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "open-attempt-cancelled.db",
        max_attempts=2,
        step_timeout_seconds=10,
    )
    crashing = CrashAfterCommittedReservationAdapter()
    try:
        with pytest.raises(SimulatedWorkerCrash):
            await ControlledReadExecutor(prepared.dependencies, crashing).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("open-attempt-cancel-crash"),
            )
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
        assert len(attempts) == 1 and attempts[0].outcome is None
        open_attempt = attempts[0]

        prepared.clock.current = open_attempt.call_deadline_at - timedelta(seconds=1)
        await RunCancellationService(prepared.dependencies).request(
            prepared.run_id,
            audit_context=_audit_context("open-attempt-cancel-request"),
        )
        prepared.clock.current = open_attempt.call_deadline_at
        retry_adapter = SequenceAdapter([{"must_not": "call"}])
        recovered = await ControlledReadExecutor(
            _restart_dependencies(prepared),
            retry_adapter,
        ).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("open-attempt-cancel-recover"),
        )
        assert recovered.classification is ReadExecutionClassification.CANCELLED
        assert recovered.attempt.outcome is AttemptOutcome.CANCELLED
        assert recovered.retry_not_before is None
        assert recovered.output is None
        assert recovered.step.state is StepState.FAILED
        assert recovered.step.terminal_reason_code == "run_cancelled"
        assert recovered.cancellation_observed_after_return
        assert retry_adapter.calls == []
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("return_success", [True, False])
async def test_orch_06_post_return_cancellation_preserves_success_and_stops_non_success(
    tmp_path: Path,
    return_success: bool,
) -> None:
    prepared = await _prepare(tmp_path / f"post-return-cancel-{return_success}.db")
    adapter = CancellingAdapter(prepared, return_success=return_success)
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("execute-cancel-race"),
        )
        assert result.cancellation_observed_after_return
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(prepared.run_id)
        assert run is not None and run.state is RunState.CANCELLED
        if return_success:
            assert result.classification is ReadExecutionClassification.SUCCEEDED
            assert result.attempt.outcome is AttemptOutcome.SUCCEEDED
            assert result.step.state is StepState.SUCCEEDED
        else:
            assert result.classification is ReadExecutionClassification.CANCELLED
            assert result.attempt.outcome is AttemptOutcome.CANCELLED
            assert result.step.state is StepState.FAILED
            assert result.step.terminal_reason_code == "run_cancelled"
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("return_success", [True, False])
async def test_orch_06_in_flight_read_closes_without_retry_after_parent_terminalizes(
    tmp_path: Path,
    return_success: bool,
) -> None:
    prepared = await _prepare(
        tmp_path / f"in-flight-terminal-parent-{return_success}.db",
        max_attempts=2,
    )
    adapter = ParentTerminalizingAdapter(prepared, return_success=return_success)
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("in-flight-terminal-parent"),
        )

        assert len(adapter.calls) == 1
        assert result.retry_not_before is None
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(prepared.run_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
        assert run is not None and run.state is RunState.FAILED
        assert run.terminal_reason_code == "model_budget_exhausted"
        assert attempts == (result.attempt,)
        if return_success:
            assert result.classification is ReadExecutionClassification.SUCCEEDED
            assert result.attempt.outcome is AttemptOutcome.SUCCEEDED
            assert result.step.state is StepState.SUCCEEDED
        else:
            assert result.classification is ReadExecutionClassification.PERMANENT_FAILURE
            assert result.attempt.outcome is AttemptOutcome.PERMANENT_FAILURE
            assert result.step.state is StepState.FAILED
            assert result.step.terminal_reason_code == "permanent_failure"
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_expired_open_read_under_failed_parent_recovers_terminally(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "expired-open-terminal-parent.db",
        max_attempts=2,
        step_timeout_seconds=10,
    )
    crashing = CrashAfterCommittedReservationAdapter()
    try:
        with pytest.raises(SimulatedWorkerCrash):
            await ControlledReadExecutor(prepared.dependencies, crashing).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("expired-open-terminal-parent.crash"),
            )
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(prepared.run_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
        assert run is not None and len(attempts) == 1
        open_attempt = attempts[0]
        await RunLifecycleService(prepared.dependencies).advance(
            run.id,
            run.version,
            RunLifecycleCommand.FAIL,
            FailureContext(RunFailurePhase.EXECUTION, "model_budget_exhausted"),
            audit_context=_audit_context("expired-open-terminal-parent.fail"),
        )
        prepared.clock.current = open_attempt.call_deadline_at
        retry_adapter = SequenceAdapter([{"must_not": "call"}])

        recovered = await ControlledReadExecutor(
            _restart_dependencies(prepared),
            retry_adapter,
        ).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context("expired-open-terminal-parent.recover"),
        )

        assert retry_adapter.calls == []
        assert recovered.classification is ReadExecutionClassification.PERMANENT_FAILURE
        assert recovered.attempt.outcome is AttemptOutcome.PERMANENT_FAILURE
        assert recovered.retry_not_before is None
        assert recovered.step.state is StepState.FAILED
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
        assert attempts == (recovered.attempt,)
    finally:
        await prepared.runtime.dispose()


class FailCommitUnitOfWork:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    async def __aenter__(self) -> FailCommitUnitOfWork:
        await self.delegate.__aenter__()  # type: ignore[attr-defined]
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.delegate.__aexit__(*args)  # type: ignore[attr-defined]

    async def commit(self) -> None:
        raise RuntimeError("injected reservation commit failure")


class FailFirstCommitFactory:
    def __init__(self, delegate: SQLAlchemyUnitOfWorkFactory) -> None:
        self.delegate = delegate
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        unit_of_work = self.delegate()
        return FailCommitUnitOfWork(unit_of_work) if self.calls == 1 else unit_of_work


class FailNthCommitFactory:
    def __init__(self, delegate: SQLAlchemyUnitOfWorkFactory, *, fail_on: int) -> None:
        self.delegate = delegate
        self.fail_on = fail_on
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        unit_of_work = self.delegate()
        return FailCommitUnitOfWork(unit_of_work) if self.calls == self.fail_on else unit_of_work


@pytest.mark.asyncio
async def test_orch_06_reservation_commit_failure_rolls_back_and_never_calls_adapter(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(tmp_path / "reservation-rollback.db")
    adapter = SequenceAdapter([{"must": "not-call"}])
    failing_dependencies = OrchestrationDependencies(
        prepared.clock,
        IncrementingIds(),
        cast(UnitOfWorkFactory, FailFirstCommitFactory(_uow_factory(prepared.runtime))),
    )
    try:
        with pytest.raises(ControlledReadExecutorError) as captured:
            await ControlledReadExecutor(failing_dependencies, adapter).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("reservation-rollback"),
            )
        assert captured.value.code == "reservation_conflict"
        assert adapter.calls == []
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            step = await unit_of_work.run_steps.get(prepared.step_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id, prepared.operation_key
            )
            control = await unit_of_work.execution_control.get(prepared.run_id)
        assert step is not None and step.state is StepState.READY
        assert attempts == ()
        assert control is not None and control.model_calls == 0
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_denied_read_reservation_audits_once_without_consuming_control(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "runtime-control-denied-audit.db",
        max_attempts=2,
        rate_max_calls=1,
    )
    setup_adapter = SequenceAdapter(
        [ReadAdapterTransientError("upstream_unavailable", "safe transient failure")]
    )
    adapter = SequenceAdapter([{"must": "not-call"}])
    context = _audit_context("runtime-control-denied-replay")
    try:
        setup = await ControlledReadExecutor(
            prepared.dependencies,
            setup_adapter,
        ).execute(
            ControlledReadCommand(prepared.step_id, {"query": "setup-rate-window"}),
            audit_context=_audit_context("runtime-control-denied-setup"),
        )
        assert setup.classification is ReadExecutionClassification.TRANSIENT_FAILURE
        assert setup.retry_not_before is not None
        assert len(setup_adapter.calls) == 1
        prepared.clock.current = setup.retry_not_before

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            before_control = await unit_of_work.execution_control.get(prepared.run_id)
            operation = await unit_of_work.execution_control.get_operation(
                prepared.step_id,
                prepared.operation_key,
            )
            before_step = await unit_of_work.run_steps.get(prepared.step_id)
            before_attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            before_timeline = await unit_of_work.audits.list_run(prepared.run_id)
            assert operation is not None
            window_start = fixed_window_start(prepared.clock.now(), operation.rate_window_seconds)
            before_window = await unit_of_work.execution_control.get_rate_window(
                operation.rate_limit_scope,
                operation.rate_limit_key,
                window_start,
            )

        executor = ControlledReadExecutor(prepared.dependencies, adapter)
        command = ControlledReadCommand(
            prepared.step_id,
            {"query": "provider-secret-canary"},
        )
        observed_retry_after: list[int] = []
        for attempt_index in range(2):
            with pytest.raises(ControlledReadExecutorError) as denied:
                await executor.execute(command, audit_context=context)
            assert denied.value.code == "rate_limit_exhausted"
            assert denied.value.retry_after_seconds is not None
            assert 1 <= denied.value.retry_after_seconds <= 3_600
            observed_retry_after.append(denied.value.retry_after_seconds)
            if attempt_index == 0:
                prepared.clock.current += timedelta(seconds=1)
        assert observed_retry_after[1] < observed_retry_after[0]

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            after_control = await unit_of_work.execution_control.get(prepared.run_id)
            after_step = await unit_of_work.run_steps.get(prepared.step_id)
            after_attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            after_window = await unit_of_work.execution_control.get_rate_window(
                operation.rate_limit_scope,
                operation.rate_limit_key,
                window_start,
            )
            after_timeline = await unit_of_work.audits.list_run(prepared.run_id)

        denials = tuple(
            event for event in after_timeline if event.event_type == "runtime.control_denied"
        )
        assert adapter.calls == []
        assert after_control == before_control
        assert after_step == before_step
        assert after_attempts == before_attempts
        assert len(after_attempts) == 1
        assert after_window == before_window
        assert after_window is not None and after_window.used == after_window.capacity == 1
        assert len(denials) == 1
        assert after_timeline[:-1] == before_timeline
        assert denials[0] == after_timeline[-1]
        assert denials[0].action_id is None
        assert denials[0].outcome.value == "rejected"
        assert denials[0].mutation_version is None
        assert denials[0].safe_metadata.values == {
            "denial_code": "rate_limit_exhausted",
            "operation_key": prepared.operation_key,
            "retry_after_seconds": denials[0].safe_metadata.values["retry_after_seconds"],
        }
        assert 1 <= denials[0].safe_metadata.values["retry_after_seconds"] <= 3_600
        assert "provider-secret-canary" not in str(denials[0].safe_metadata.values)
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("exceeds_limit", (False, True))
async def test_orch_06_input_byte_limit_is_inclusive_and_precedes_every_runtime_mutation(
    tmp_path: Path,
    exceeds_limit: bool,
) -> None:
    exact_payload = {"q": "12345678"}
    limit = canonical_payload_size_bytes(exact_payload)
    payload = {"q": "123456789"} if exceeds_limit else exact_payload
    prepared = await _prepare(
        tmp_path / f"input-bytes-{exceeds_limit}.db",
        max_input_bytes=limit,
        max_input_field_bytes=16,
    )
    adapter = SequenceAdapter([{"ok": True}])
    try:
        if exceeds_limit:
            with pytest.raises(ControlledReadExecutorError) as captured:
                await ControlledReadExecutor(prepared.dependencies, adapter).execute(
                    ControlledReadCommand(prepared.step_id, payload),
                    audit_context=_audit_context("input-bytes-denied"),
                )
            assert captured.value.code == "input_payload_too_large"
        else:
            result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
                ControlledReadCommand(prepared.step_id, payload),
                audit_context=_audit_context("input-bytes-exact"),
            )
            assert result.classification is ReadExecutionClassification.SUCCEEDED

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(prepared.run_id)
            step = await unit_of_work.run_steps.get(prepared.step_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            control = await unit_of_work.execution_control.get(prepared.run_id)
            timeline = await unit_of_work.audits.list_run(prepared.run_id)
        assert run is not None and step is not None and control is not None
        if exceeds_limit:
            assert adapter.calls == []
            assert attempts == () and control.model_calls == 0
            assert run.state is RunState.FAILED
            assert run.terminal_reason_code == "input_payload_too_large"
            assert step.state is StepState.FAILED
            assert step.terminal_reason_code == "input_payload_too_large"
            denials = tuple(
                event for event in timeline if event.event_type == "runtime.control_denied"
            )
            assert len(denials) == 1
            assert denials[0].safe_metadata.values == {
                "denial_code": "input_payload_too_large",
                "operation_key": prepared.operation_key,
            }
            assert "123456789" not in str(denials[0].safe_metadata.values)
        else:
            assert len(adapter.calls) == 1
            assert len(attempts) == 1 and control.model_calls == 1
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("exceeds_limit", (False, True))
async def test_orch_06_recursive_utf8_input_field_limit_is_inclusive(
    tmp_path: Path,
    exceeds_limit: bool,
) -> None:
    payload = {"q": "é" * 4 + ("x" if exceeds_limit else "")}
    prepared = await _prepare(
        tmp_path / f"input-field-{exceeds_limit}.db",
        max_input_bytes=64,
        max_input_field_bytes=8,
    )
    adapter = SequenceAdapter([{"ok": True}])
    try:
        if exceeds_limit:
            with pytest.raises(ControlledReadExecutorError) as captured:
                await ControlledReadExecutor(prepared.dependencies, adapter).execute(
                    ControlledReadCommand(prepared.step_id, payload),
                    audit_context=_audit_context("input-field-denied"),
                )
            assert captured.value.code == "input_field_too_large"
        else:
            result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
                ControlledReadCommand(prepared.step_id, payload),
                audit_context=_audit_context("input-field-exact"),
            )
            assert result.classification is ReadExecutionClassification.SUCCEEDED

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            control = await unit_of_work.execution_control.get(prepared.run_id)
            step = await unit_of_work.run_steps.get(prepared.step_id)
        assert control is not None and step is not None
        if exceeds_limit:
            assert adapter.calls == [] and attempts == () and control.model_calls == 0
            assert step.state is StepState.FAILED
            assert step.terminal_reason_code == "input_field_too_large"
        else:
            assert len(adapter.calls) == len(attempts) == control.model_calls == 1
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("exceeds_limit", (False, True))
async def test_orch_06_output_byte_limit_is_inclusive_and_denial_completes_atomically(
    tmp_path: Path,
    exceeds_limit: bool,
) -> None:
    exact_output = {"value": "12345678"}
    limit = canonical_payload_size_bytes(exact_output)
    output = {"value": "123456789"} if exceeds_limit else exact_output
    prepared = await _prepare(
        tmp_path / f"output-bytes-{exceeds_limit}.db",
        max_output_bytes=limit,
    )
    adapter = BudgetResultAdapter(output, model_output_tokens=1)
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context(f"output-bytes-{exceeds_limit}"),
        )
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(prepared.run_id)
            step = await unit_of_work.run_steps.get(prepared.step_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            control = await unit_of_work.execution_control.get(prepared.run_id)
            timeline = await unit_of_work.audits.list_run(prepared.run_id)
        assert len(adapter.calls) == len(attempts) == 1
        assert control is not None and control.model_calls == 1
        assert run is not None and step is not None
        if exceeds_limit:
            assert result.classification is ReadExecutionClassification.PERMANENT_FAILURE
            assert result.output is None
            assert attempts[0].outcome is AttemptOutcome.PERMANENT_FAILURE
            assert run.state is RunState.FAILED
            assert run.terminal_reason_code == "output_payload_too_large"
            assert step.state is StepState.FAILED
            assert step.terminal_reason_code == "output_payload_too_large"
            denials = tuple(
                event for event in timeline if event.event_type == "runtime.control_denied"
            )
            assert len(denials) == 1
            assert denials[0].safe_metadata.values == {
                "denial_code": "output_payload_too_large",
                "operation_key": prepared.operation_key,
            }
            assert "123456789" not in str(denials[0].safe_metadata.values)
        else:
            assert result.classification is ReadExecutionClassification.SUCCEEDED
            assert result.output is not None
            assert attempts[0].outcome is AttemptOutcome.SUCCEEDED
            assert step.state is StepState.SUCCEEDED
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_output_denial_completion_rollback_retains_open_attempt_only(
    tmp_path: Path,
) -> None:
    exact_output = {"value": "12345678"}
    prepared = await _prepare(
        tmp_path / "output-denial-rollback.db",
        max_output_bytes=canonical_payload_size_bytes(exact_output),
    )
    adapter = BudgetResultAdapter({"value": "123456789"}, model_output_tokens=1)
    failing_dependencies = OrchestrationDependencies(
        prepared.clock,
        IncrementingIds(),
        cast(
            UnitOfWorkFactory,
            FailNthCommitFactory(_uow_factory(prepared.runtime), fail_on=2),
        ),
    )
    try:
        with pytest.raises(ControlledReadExecutorError) as captured:
            await ControlledReadExecutor(failing_dependencies, adapter).execute(
                ControlledReadCommand(prepared.step_id, {"query": "safe"}),
                audit_context=_audit_context("output-denial-rollback"),
            )
        assert captured.value.code == "completion_conflict"
        assert len(adapter.calls) == 1

        async with prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(prepared.run_id)
            step = await unit_of_work.run_steps.get(prepared.step_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            timeline = await unit_of_work.audits.list_run(prepared.run_id)
            artifacts = await unit_of_work.artifacts.list_for_run(prepared.run_id)
        assert run is not None and run.state is RunState.EXECUTING
        assert step is not None and step.state is StepState.EXECUTING
        assert len(attempts) == 1 and attempts[0].outcome is None
        assert artifacts == ()
        assert not any(
            event.event_type in {"attempt.completed", "artifact.persisted"} for event in timeline
        )
        assert not any(
            event.event_type == "runtime.control_denied"
            and event.safe_metadata.values.get("denial_code") == "output_payload_too_large"
            for event in timeline
        )
        assert not any(
            event.event_type == "step.transitioned"
            and event.safe_metadata.values.get("reason_code") == "output_payload_too_large"
            for event in timeline
        )
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tokens", "forge_missing", "expected_code"),
    [
        (2, False, None),
        (3, False, "model_output_tokens_exceeded"),
        (1, True, "model_output_tokens_invalid"),
    ],
)
async def test_orch_06_model_output_token_usage_is_required_and_bounded(
    tmp_path: Path,
    tokens: int,
    forge_missing: bool,
    expected_code: str | None,
) -> None:
    prepared = await _prepare(
        tmp_path / f"model-tokens-{tokens}-{forge_missing}.db",
        max_model_output_tokens=2,
    )
    adapter = BudgetResultAdapter(
        {"value": "safe"},
        model_output_tokens=tokens,
        forge_missing_tokens=forge_missing,
    )
    try:
        result = await ControlledReadExecutor(prepared.dependencies, adapter).execute(
            ControlledReadCommand(prepared.step_id, {"query": "safe"}),
            audit_context=_audit_context(f"model-tokens-{tokens}-{forge_missing}"),
        )
        async with prepared.dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(prepared.run_id)
            step = await unit_of_work.run_steps.get(prepared.step_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                prepared.step_id,
                prepared.operation_key,
            )
            timeline = await unit_of_work.audits.list_run(prepared.run_id)
        assert len(adapter.calls) == len(attempts) == 1
        assert run is not None and step is not None
        if expected_code is None:
            assert result.classification is ReadExecutionClassification.SUCCEEDED
            assert result.output is not None and result.output.model_output_tokens == 2
            assert attempts[0].outcome is AttemptOutcome.SUCCEEDED
        else:
            assert result.classification is ReadExecutionClassification.PERMANENT_FAILURE
            assert result.output is None
            assert attempts[0].outcome is AttemptOutcome.PERMANENT_FAILURE
            assert run.state is RunState.FAILED and run.terminal_reason_code == expected_code
            assert step.state is StepState.FAILED and step.terminal_reason_code == expected_code
            denials = tuple(
                event for event in timeline if event.event_type == "runtime.control_denied"
            )
            assert len(denials) == 1
            assert denials[0].safe_metadata.values == {
                "denial_code": expected_code,
                "operation_key": prepared.operation_key,
            }
    finally:
        await prepared.runtime.dispose()
