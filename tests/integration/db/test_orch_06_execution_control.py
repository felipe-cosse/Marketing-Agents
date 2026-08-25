"""ORCH-06: durable budgets, deadlines, retries, rate windows, and cancel fences."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import EffectPlan, OrchestrationDependencies
from marketing_agents.application.ports.repositories import (
    AttemptCompletionResult,
    AttemptReservationResult,
    ExecutionControlRepositoryConflict,
)
from marketing_agents.application.services import (
    AuditedPlanPersistenceService,
    ExecutionActivationService,
)
from marketing_agents.domain.data_classification import highest_classification
from marketing_agents.domain.execution_control import (
    AttemptCompletionCommand,
    AttemptOutcome,
    AttemptReservationCommand,
    OperationExecutionPolicy,
    RunExecutionPolicy,
)
from marketing_agents.domain.plan_hash import EffectPlanStepHashMaterial, effect_plan_hash
from marketing_agents.domain.provenance import (
    ArtifactEnvelope,
    ProvenanceSource,
    ProviderVersion,
)
from marketing_agents.domain.runtime_policy import (
    BudgetPolicySnapshot,
    RateLimitPolicySnapshot,
    RetryBackoff,
    RetryPolicySnapshot,
    RunRuntimePolicy,
    StepRuntimePolicy,
    runtime_rate_limit_key,
)
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditRepository,
    SQLAlchemyExecutionControlRepository,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyRunStepRepository,
    SQLAlchemyUnitOfWorkFactory,
)
from marketing_agents.infrastructure.db.models import (
    ExecutionAttemptRecord,
    ExecutionOperationPolicyRecord,
    RateLimitWindowRecord,
    RunExecutionControlRecord,
)
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.digest_key import DigestKey
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from tests.integration.db.test_orch_09_audited_step_state import (
    _audit_context,
    _dependencies,
    _runtime,
    _validated_run,
)
from tests.support.orch_09_planning import build_read_only_plan

INTEGRITY_KEY = DigestKey(bytes(reversed(range(32))))


def _execution_repository(session: AsyncSession) -> SQLAlchemyExecutionControlRepository:
    return SQLAlchemyExecutionControlRepository(session, INTEGRITY_KEY)


def _uow_factory(runtime: DatabaseRuntime) -> SQLAlchemyUnitOfWorkFactory:
    return SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=SQLAlchemyWorkRepository,
            runs=SQLAlchemyRunRepository,
            audits=SQLAlchemyAuditRepository,
            run_steps=SQLAlchemyRunStepRepository,
            execution_control=_execution_repository,
            artifacts=SQLAlchemyArtifactRepository,
        ),
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


def _with_runtime_bounds(
    plan: EffectPlan,
    *,
    max_attempts: int,
    max_model_calls: int,
    rate_capacity: int,
    run_timeout_seconds: int = 120,
) -> EffectPlan:
    run_policy = RunRuntimePolicy(
        max_steps=20,
        max_model_calls=max_model_calls,
        max_tool_calls=1_000,
        run_timeout_seconds=run_timeout_seconds,
    )
    steps = tuple(
        replace(
            step,
            runtime_policy=StepRuntimePolicy(
                operation_key=step.runtime_policy.operation_key,
                attempt_kind=step.runtime_policy.attempt_kind,
                retry=RetryPolicySnapshot(max_attempts, RetryBackoff.BOUNDED_EXPONENTIAL),
                timeout=step.runtime_policy.timeout,
                budget=BudgetPolicySnapshot(
                    max_steps=20,
                    max_model_calls=max_model_calls,
                    max_tool_calls=20,
                ),
                rate_limit=RateLimitPolicySnapshot(
                    scope=step.runtime_policy.rate_limit.scope,
                    key=runtime_rate_limit_key(
                        template_id=step.template_id,
                        max_calls=rate_capacity,
                        window_seconds=step.runtime_policy.rate_limit.window_seconds,
                    ),
                    max_calls=rate_capacity,
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
        run_policy=run_policy,
        steps=tuple(_step_hash_material(step) for step in steps),
    )
    return replace(plan, run_policy=run_policy, steps=steps, plan_hash=plan_hash)


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    runtime: DatabaseRuntime
    dependencies: OrchestrationDependencies
    uow_factory: SQLAlchemyUnitOfWorkFactory
    policy: RunExecutionPolicy
    step_ids: tuple[str, ...]
    step_versions: tuple[int, ...]
    started_at: datetime
    control_version: int


async def _prepare(
    path: Path,
    *,
    max_attempts: int = 1,
    max_model_calls: int = 1,
    rate_capacity: int = 100,
    parallel_steps: bool = False,
) -> PreparedExecution:
    runtime = await _runtime(path)
    seed_dependencies = _dependencies(runtime)
    dependencies = OrchestrationDependencies(
        seed_dependencies.clock,
        seed_dependencies.ids,
        _uow_factory(runtime),
    )
    run, envelope = await _validated_run(dependencies, f"event.{path.stem}")
    plan, graph, routing = build_read_only_plan(
        run_id=run.id,
        workflow_id=envelope.workflow_id,
        target_instance_id=envelope.instance_id,
        configuration_revision=envelope.configuration_revision,
        catalog_hash=run.catalog_hash,
        parallel_steps=parallel_steps,
    )
    plan = _with_runtime_bounds(
        plan,
        max_attempts=max_attempts,
        max_model_calls=max_model_calls,
        rate_capacity=rate_capacity,
    )
    persisted = await AuditedPlanPersistenceService(dependencies).persist(
        plan,
        graph,
        routing,
        expected_run_version=run.version,
        audit_context=_audit_context(f"{path.stem}.plan"),
    )
    activated = await ExecutionActivationService(dependencies).activate(
        persisted.run.id,
        audit_context=_audit_context(f"{path.stem}.activate"),
    )
    running = activated.run
    ready_steps = list(activated.steps)
    operations = tuple(
        OperationExecutionPolicy(
            run_id=step.run_id,
            step_id=step.id,
            operation_key=step.runtime_policy.operation_key,
            kind=step.runtime_policy.attempt_kind,
            capability_id=step.capability_id,
            selected_instance_id=step.selected_instance_id,
            configuration_revision=step.configuration_revision,
            connector_family=step.connector_family,
            binding_id=step.binding_id,
            binding_configuration_revision=step.binding_configuration_revision,
            request_schema_id=step.request_schema_id,
            result_schema_id=step.result_schema_id,
            result_schema_hash=step.result_schema_hash,
            request_redaction_fields=step.request_redaction_fields,
            result_redaction_fields=step.result_redaction_fields,
            data_classification=step.data_classification,
            connector_timeout_seconds=step.timeout_seconds,
            policy_hash=persisted.plan.plan_hash,
            max_attempts=step.runtime_policy.retry.max_attempts,
            retry_backoff=step.runtime_policy.retry.backoff,
            step_timeout_seconds=step.runtime_policy.timeout.step_seconds,
            max_input_bytes=step.runtime_policy.budget.max_input_bytes,
            max_input_field_bytes=step.runtime_policy.budget.max_input_field_bytes,
            max_output_bytes=step.runtime_policy.budget.max_output_bytes,
            max_model_output_tokens=step.runtime_policy.budget.max_model_output_tokens,
            rate_limit_scope=step.runtime_policy.rate_limit.scope,
            rate_limit_key=step.runtime_policy.rate_limit.key,
            rate_window_max_calls=step.runtime_policy.rate_limit.max_calls,
            rate_window_seconds=step.runtime_policy.rate_limit.window_seconds,
        )
        for step in ready_steps
    )
    policy = RunExecutionPolicy(
        run_id=running.id,
        policy_hash=persisted.plan.plan_hash,
        run_timeout_seconds=persisted.plan.runtime_policy.run_timeout_seconds,
        max_model_calls=persisted.plan.runtime_policy.max_model_calls,
        max_tool_calls=persisted.plan.runtime_policy.max_tool_calls,
        operations=operations,
        created_at=persisted.plan.created_at,
    )
    uow_factory = _uow_factory(runtime)
    async with uow_factory() as unit_of_work:
        control = await unit_of_work.execution_control.get(running.id)
    assert control is not None and control.started_at is not None
    return PreparedExecution(
        runtime=runtime,
        dependencies=dependencies,
        uow_factory=uow_factory,
        policy=policy,
        step_ids=tuple(step.id for step in ready_steps),
        step_versions=tuple(step.version for step in ready_steps),
        started_at=control.started_at,
        control_version=control.version,
    )


def _command(
    prepared: PreparedExecution,
    *,
    suffix: str,
    step_index: int = 0,
    control_version: int | None = None,
    reserved_at: datetime | None = None,
) -> AttemptReservationCommand:
    operation = prepared.policy.operations[step_index]
    return AttemptReservationCommand(
        attempt_id=f"attempt.{suffix}",
        run_id=prepared.policy.run_id,
        step_id=prepared.step_ids[step_index],
        operation_key=operation.operation_key,
        expected_control_version=(
            prepared.control_version if control_version is None else control_version
        ),
        expected_step_version=prepared.step_versions[step_index],
        reserved_at=reserved_at or prepared.started_at + timedelta(seconds=1),
    )


async def _reserve(
    prepared: PreparedExecution,
    command: AttemptReservationCommand,
) -> AttemptReservationResult:
    async with prepared.uow_factory() as unit_of_work:
        result = await unit_of_work.execution_control.reserve_attempt(command)
        await unit_of_work.commit()
        return result


async def _complete(
    prepared: PreparedExecution,
    attempt_id: str,
    outcome: AttemptOutcome,
    completed_at: datetime,
    *,
    expected_control_version: int | None = None,
) -> AttemptCompletionResult:
    async with prepared.uow_factory() as unit_of_work:
        control = await unit_of_work.execution_control.get(prepared.policy.run_id)
        assert control is not None
        output_artifact_id: str | None = None
        if outcome is AttemptOutcome.SUCCEEDED:
            attempt = await unit_of_work.execution_control.get_attempt(attempt_id)
            run = await unit_of_work.runs.get(prepared.policy.run_id)
            plan = await unit_of_work.run_steps.get_plan(prepared.policy.run_id)
            assert attempt is not None and run is not None and plan is not None
            step = await unit_of_work.run_steps.get(attempt.step_id)
            work = await unit_of_work.works.get(run.work_item_id)
            assert (
                step is not None
                and work is not None
                and step.result_schema_id is not None
                and step.result_schema_hash is not None
            )
            output_artifact_id = f"artifact.{attempt_id}"
            artifact = ArtifactEnvelope.create(
                payload={"ok": True},
                artifact_id=output_artifact_id,
                work_item_id=work.id,
                run_id=run.id,
                step_id=step.id,
                workflow_id=plan.workflow_id,
                workflow_version=str(plan.workflow_version),
                template_id=step.template_id,
                instance_id=step.selected_instance_id,
                admitted_input_digest=work.input_digest,
                catalog_hash=plan.catalog_content_hash,
                instance_config_revision=step.configuration_revision,
                sources=(
                    ProvenanceSource(
                        kind="work_input",
                        source_id=work.id,
                        integrity_digest=work.input_digest,
                        classification=work.input_classification,
                    ),
                    ProvenanceSource(
                        kind="external_observation",
                        source_id=f"observation.{attempt_id}",
                        integrity_digest=None,
                        classification=step.data_classification,
                    ),
                ),
                parent_artifact_ids=(),
                providers=(
                    ProviderVersion(
                        provider_kind=("llm" if attempt.kind.value == "model" else "connector"),
                        mode="mock",
                        name="orch-06-test-provider",
                        version="v1",
                    ),
                ),
                output_schema_id=step.result_schema_id,
                output_schema_version="v1",
                output_schema_hash=step.result_schema_hash,
                created_at=completed_at,
                classification=highest_classification(
                    work.input_classification,
                    step.data_classification,
                ),
            )
            await unit_of_work.artifacts.add_or_get(artifact)
        result = await unit_of_work.execution_control.complete_attempt(
            AttemptCompletionCommand(
                attempt_id=attempt_id,
                outcome=outcome,
                expected_control_version=(
                    control.version
                    if expected_control_version is None
                    else expected_control_version
                ),
                completed_at=completed_at,
                output_artifact_id=output_artifact_id,
            )
        )
        await unit_of_work.commit()
        return result


def _ddl(table, dialect) -> str:  # type: ignore[no-untyped-def]
    return " ".join(str(CreateTable(table).compile(dialect=dialect)).lower().split())


def test_orch_06_schema_compiles_with_portable_attempt_and_capacity_fences() -> None:
    for dialect in (
        sqlite.dialect(),
        postgresql.dialect(),  # type: ignore[no-untyped-call]
    ):
        for table in Base.metadata.sorted_tables:
            str(CreateTable(table).compile(dialect=dialect))
    control = _ddl(RunExecutionControlRecord.__table__, sqlite.dialect())
    operation = _ddl(ExecutionOperationPolicyRecord.__table__, sqlite.dialect())
    attempt = _ddl(ExecutionAttemptRecord.__table__, sqlite.dialect())
    window = _ddl(RateLimitWindowRecord.__table__, sqlite.dialect())
    assert "constraint uq_execution_controls_run_policy unique (run_id, policy_hash)" in control
    assert "constraint ck_execution_controls_cancel_fence" in control
    assert "foreign key(run_id, policy_hash) references run_execution_controls" in operation
    assert "constraint ck_execution_operations_connector_contract" in operation
    assert "constraint ck_execution_operations_payload_budgets" in operation
    assert "request_schema_id" in operation and "result_schema_id" in operation
    assert "request_redaction_fields" in operation and "result_redaction_fields" in operation
    assert "max_input_bytes" in operation and "max_input_field_bytes" in operation
    assert "max_output_bytes" in operation and "max_model_output_tokens" in operation
    assert "constraint uq_execution_attempts_operation_number" in attempt
    assert "foreign key(rate_limit_scope, rate_limit_key, rate_window_started_at)" in attempt
    assert "outcome = 'succeeded'" in attempt and "output_artifact_id is not null" in attempt
    assert "constraint ck_rate_windows_usage check (used >= 1 and used <= capacity)" in window

    with pytest.raises(ValueError, match="durable output artifact"):
        AttemptCompletionCommand(
            attempt_id="attempt.missing-artifact",
            outcome=AttemptOutcome.SUCCEEDED,
            expected_control_version=1,
            completed_at=datetime.fromisoformat("2026-08-25T12:00:00+00:00"),
        )


@pytest.mark.asyncio
async def test_orch_06_reservation_commits_before_call_and_completion_is_separate(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(tmp_path / "split-call.db")
    command = _command(prepared, suffix="split")
    try:
        reserved = await _reserve(prepared, command)
        assert reserved.attempt.attempt_number == 1
        assert reserved.control.model_calls == 1
        assert reserved.rate_window.used == 1
        assert reserved.attempt.effective_timeout == timedelta(seconds=60)

        # This adapter-shaped probe runs after the reservation UoW has committed.
        async with prepared.uow_factory() as observing_uow:
            durable = await observing_uow.execution_control.get_attempt(command.attempt_id)
            control = await observing_uow.execution_control.get(prepared.policy.run_id)
        assert durable is not None and durable.outcome is None
        assert control is not None and control.model_calls == 1

        with pytest.raises(ExecutionControlRepositoryConflict) as replay_conflict:
            await _reserve(
                prepared,
                replace(command, redacted_input={"neutral_field": "different"}),
            )
        assert replay_conflict.value.code == "attempt_id_conflict"

        completed_at = command.reserved_at + timedelta(seconds=1)
        completed = await _complete(
            prepared,
            command.attempt_id,
            AttemptOutcome.SUCCEEDED,
            completed_at,
            expected_control_version=reserved.control.version,
        )
        assert completed.completed and completed.terminal_reason_code is None
        replayed_completion = await _complete(
            prepared,
            command.attempt_id,
            AttemptOutcome.SUCCEEDED,
            completed_at,
            expected_control_version=reserved.control.version,
        )
        assert not replayed_completion.completed

        replay = await _reserve(prepared, command)
        assert replay.attempt.outcome is AttemptOutcome.SUCCEEDED
        assert replay.control.model_calls == 1
        assert replay.rate_window.used == 1
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_transient_retries_are_deterministic_and_do_not_reconsume_logical_budget(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "retry.db", max_attempts=3, max_model_calls=1, rate_capacity=3
    )
    try:
        first_command = _command(prepared, suffix="retry.1")
        first = await _reserve(prepared, first_command)
        first_completion = await _complete(
            prepared,
            first.attempt.id,
            AttemptOutcome.TRANSIENT_FAILURE,
            first_command.reserved_at + timedelta(seconds=1),
        )
        assert first_completion.retry_not_before == first_command.reserved_at + timedelta(seconds=2)

        early = _command(
            prepared,
            suffix="retry.2.early",
            control_version=first.control.version + 1,
            reserved_at=first_completion.retry_not_before - timedelta(microseconds=1),
        )
        with pytest.raises(ExecutionControlRepositoryConflict) as captured:
            await _reserve(prepared, early)
        assert captured.value.code == "retry_not_ready"
        assert captured.value.retry_after_seconds == 1

        second_command = replace(
            early,
            attempt_id="attempt.retry.2",
            reserved_at=first_completion.retry_not_before,
        )
        second = await _reserve(prepared, second_command)
        assert second.attempt.attempt_number == 2
        assert second.control.model_calls == 1
        assert second.rate_window.used == 2
        second_completion = await _complete(
            prepared,
            second.attempt.id,
            AttemptOutcome.TRANSIENT_FAILURE,
            second_command.reserved_at + timedelta(seconds=1),
        )
        assert second_completion.retry_not_before == second_command.reserved_at + timedelta(
            seconds=3
        )

        third_command = _command(
            prepared,
            suffix="retry.3",
            control_version=second.control.version + 1,
            reserved_at=second_completion.retry_not_before,
        )
        third = await _reserve(prepared, third_command)
        assert third.attempt.attempt_number == 3
        assert third.control.model_calls == 1
        terminal = await _complete(
            prepared,
            third.attempt.id,
            AttemptOutcome.TRANSIENT_FAILURE,
            third_command.reserved_at + timedelta(seconds=1),
        )
        assert terminal.retry_not_before is None
        assert terminal.terminal_reason_code == "attempts_exhausted"

        before = terminal.attempt
        with pytest.raises(ExecutionControlRepositoryConflict) as captured:
            await _reserve(
                prepared,
                _command(
                    prepared,
                    suffix="retry.4",
                    control_version=third.control.version + 1,
                    reserved_at=third_command.reserved_at + timedelta(seconds=5),
                ),
            )
        assert captured.value.code == "attempts_exhausted"
        async with prepared.uow_factory() as unit_of_work:
            attempts = await unit_of_work.execution_control.list_attempts(
                before.step_id, before.operation_key
            )
            control = await unit_of_work.execution_control.get(before.run_id)
        assert len(attempts) == 3
        assert control is not None and control.model_calls == 1
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_fixed_window_denial_is_atomic_and_exact_end_opens_new_window(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "rate.db",
        max_model_calls=2,
        rate_capacity=1,
        parallel_steps=True,
    )
    try:
        first_command = _command(prepared, suffix="rate.1")
        first = await _reserve(prepared, first_command)
        denied = _command(
            prepared,
            suffix="rate.2.denied",
            step_index=1,
            control_version=first.control.version,
            reserved_at=first_command.reserved_at,
        )
        with pytest.raises(ExecutionControlRepositoryConflict) as captured:
            await _reserve(prepared, denied)
        assert captured.value.code == "rate_limit_exhausted"
        assert 1 <= (captured.value.retry_after_seconds or 0) <= 60
        async with prepared.uow_factory() as unit_of_work:
            unchanged = await unit_of_work.execution_control.get(prepared.policy.run_id)
            denied_attempt = await unit_of_work.execution_control.get_attempt(denied.attempt_id)
        assert unchanged is not None and unchanged.version == first.control.version
        assert unchanged.model_calls == 1 and denied_attempt is None

        at_exact_end = replace(
            denied,
            attempt_id="attempt.rate.2.new-window",
            reserved_at=first.rate_window.ends_at,
        )
        second = await _reserve(prepared, at_exact_end)
        assert second.rate_window.started_at == first.rate_window.ends_at
        assert second.rate_window.used == 1
        assert second.control.model_calls == 2
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_deadline_and_cancellation_precede_all_counter_mutations(
    tmp_path: Path,
) -> None:
    deadline_case = await _prepare(tmp_path / "deadline.db")
    try:
        async with deadline_case.uow_factory() as unit_of_work:
            control = await unit_of_work.execution_control.get(deadline_case.policy.run_id)
        assert control is not None and control.deadline_at is not None
        with pytest.raises(ExecutionControlRepositoryConflict) as captured:
            await _reserve(
                deadline_case,
                _command(
                    deadline_case,
                    suffix="deadline",
                    reserved_at=control.deadline_at,
                ),
            )
        assert captured.value.code == "deadline_exceeded"
        async with deadline_case.uow_factory() as unit_of_work:
            unchanged = await unit_of_work.execution_control.get(deadline_case.policy.run_id)
            attempts = await unit_of_work.execution_control.list_attempts(
                deadline_case.step_ids[0], deadline_case.policy.operations[0].operation_key
            )
        assert unchanged == control and attempts == ()
    finally:
        await deadline_case.runtime.dispose()

    cancelled_case = await _prepare(tmp_path / "cancelled.db")
    cancel_time = cancelled_case.started_at + timedelta(seconds=1)
    actor_digest = hashlib.sha256(b"principal.local.operator").hexdigest()
    try:
        async with cancelled_case.uow_factory() as unit_of_work:
            fenced = await unit_of_work.execution_control.request_cancel(
                run_id=cancelled_case.policy.run_id,
                expected_control_version=cancelled_case.control_version,
                actor_digest=actor_digest,
                requested_at=cancel_time,
            )
            await unit_of_work.commit()
        assert fenced.fenced
        async with cancelled_case.uow_factory() as unit_of_work:
            replay = await unit_of_work.execution_control.request_cancel(
                run_id=cancelled_case.policy.run_id,
                expected_control_version=cancelled_case.control_version,
                actor_digest=actor_digest,
                requested_at=cancel_time,
            )
        assert not replay.fenced
        with pytest.raises(ExecutionControlRepositoryConflict) as captured:
            await _reserve(
                cancelled_case,
                _command(
                    cancelled_case,
                    suffix="cancelled",
                    control_version=fenced.control.version,
                    reserved_at=cancel_time,
                ),
            )
        assert captured.value.code == "run_cancelled"
        assert fenced.control.model_calls == 0 and fenced.control.tool_calls == 0
    finally:
        await cancelled_case.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_cancellation_wins_completion_control_cas_and_forces_cancelled_replay(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "cancellation-wins-completion.db",
        max_attempts=2,
        rate_capacity=2,
    )
    command = _command(prepared, suffix="cancellation-wins")
    try:
        reserved = await _reserve(prepared, command)
        completion_time = command.reserved_at + timedelta(seconds=1)
        stale_completion = AttemptCompletionCommand(
            attempt_id=reserved.attempt.id,
            outcome=AttemptOutcome.TRANSIENT_FAILURE,
            expected_control_version=reserved.control.version,
            completed_at=completion_time,
        )
        actor_digest = hashlib.sha256(b"principal.cancel-winner").hexdigest()

        # The cancellation UoW commits against the completion worker's captured version.
        async with prepared.uow_factory() as cancelling_uow:
            fenced = await cancelling_uow.execution_control.request_cancel(
                run_id=prepared.policy.run_id,
                expected_control_version=reserved.control.version,
                actor_digest=actor_digest,
                requested_at=completion_time,
            )
            await cancelling_uow.commit()
        assert fenced.fenced

        async with prepared.uow_factory() as stale_completion_uow:
            with pytest.raises(ExecutionControlRepositoryConflict) as conflict:
                await stale_completion_uow.execution_control.complete_attempt(stale_completion)
        assert conflict.value.code == "stale_execution_control"

        cancelled_completion = replace(
            stale_completion,
            expected_control_version=fenced.control.version,
        )
        async with prepared.uow_factory() as completion_uow:
            completed = await completion_uow.execution_control.complete_attempt(
                cancelled_completion
            )
            await completion_uow.commit()
        assert completed.completed
        assert completed.attempt.outcome is AttemptOutcome.CANCELLED
        assert completed.retry_not_before is None
        assert completed.terminal_reason_code == "run_cancelled"

        async with prepared.uow_factory() as replay_uow:
            replay = await replay_uow.execution_control.complete_attempt(cancelled_completion)
            control = await replay_uow.execution_control.get(prepared.policy.run_id)
            attempts = await replay_uow.execution_control.list_attempts(
                reserved.attempt.step_id,
                reserved.attempt.operation_key,
            )
        assert not replay.completed
        assert replay.attempt == completed.attempt
        assert control is not None and control.version == fenced.control.version + 1
        assert control.cancel_requested_at == completion_time
        assert control.model_calls == 1
        assert attempts == (completed.attempt,)

        with pytest.raises(ExecutionControlRepositoryConflict) as retry_conflict:
            await _reserve(
                prepared,
                _command(
                    prepared,
                    suffix="cancellation-wins.retry",
                    control_version=control.version,
                    reserved_at=completion_time + timedelta(seconds=1),
                ),
            )
        assert retry_conflict.value.code == "run_cancelled"
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_completion_wins_control_cas_then_fresh_cancellation_blocks_retry(
    tmp_path: Path,
) -> None:
    prepared = await _prepare(
        tmp_path / "completion-wins-cancellation.db",
        max_attempts=2,
        rate_capacity=2,
    )
    command = _command(prepared, suffix="completion-wins")
    try:
        reserved = await _reserve(prepared, command)
        completion_time = command.reserved_at + timedelta(seconds=1)
        completion = await _complete(
            prepared,
            reserved.attempt.id,
            AttemptOutcome.TRANSIENT_FAILURE,
            completion_time,
            expected_control_version=reserved.control.version,
        )
        assert completion.completed and completion.retry_not_before is not None

        actor_digest = hashlib.sha256(b"principal.completion-winner").hexdigest()
        cancellation_time = completion_time + timedelta(microseconds=1)
        async with prepared.uow_factory() as stale_cancellation_uow:
            with pytest.raises(ExecutionControlRepositoryConflict) as conflict:
                await stale_cancellation_uow.execution_control.request_cancel(
                    run_id=prepared.policy.run_id,
                    expected_control_version=reserved.control.version,
                    actor_digest=actor_digest,
                    requested_at=cancellation_time,
                )
        assert conflict.value.code == "stale_execution_control"

        async with prepared.uow_factory() as cancelling_uow:
            current = await cancelling_uow.execution_control.get(prepared.policy.run_id)
            assert current is not None and current.version == reserved.control.version + 1
            fenced = await cancelling_uow.execution_control.request_cancel(
                run_id=prepared.policy.run_id,
                expected_control_version=current.version,
                actor_digest=actor_digest,
                requested_at=cancellation_time,
            )
            await cancelling_uow.commit()
        assert fenced.fenced

        with pytest.raises(ExecutionControlRepositoryConflict) as retry_conflict:
            await _reserve(
                prepared,
                _command(
                    prepared,
                    suffix="completion-wins.retry",
                    control_version=fenced.control.version,
                    reserved_at=completion.retry_not_before,
                ),
            )
        assert retry_conflict.value.code == "run_cancelled"
        async with prepared.uow_factory() as observing_uow:
            persisted = await observing_uow.execution_control.get_attempt(reserved.attempt.id)
            control = await observing_uow.execution_control.get(prepared.policy.run_id)
        assert persisted == completion.attempt
        assert control == fenced.control
        assert control.model_calls == 1
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
async def test_orch_06_reservation_rollback_and_concurrent_unique_winner(tmp_path: Path) -> None:
    rollback_case = await _prepare(tmp_path / "rollback.db")
    rollback_command = _command(rollback_case, suffix="rollback")
    try:
        async with rollback_case.uow_factory() as unit_of_work:
            await unit_of_work.execution_control.reserve_attempt(rollback_command)
            # No commit: the UoW must roll back counter, window, and attempt together.
        async with rollback_case.uow_factory() as unit_of_work:
            control = await unit_of_work.execution_control.get(rollback_case.policy.run_id)
            attempt = await unit_of_work.execution_control.get_attempt(rollback_command.attempt_id)
        assert control is not None and control.model_calls == 0
        assert attempt is None
    finally:
        await rollback_case.runtime.dispose()

    race_case = await _prepare(tmp_path / "race.db")
    first = _command(race_case, suffix="race.1")
    second = replace(first, attempt_id="attempt.race.2")

    async def contender(command: AttemptReservationCommand):  # type: ignore[no-untyped-def]
        try:
            return await _reserve(race_case, command)
        except ExecutionControlRepositoryConflict as exc:
            return exc

    try:
        results = await asyncio.gather(contender(first), contender(second))
        assert sum(not isinstance(item, Exception) for item in results) == 1
        loser = next(item for item in results if isinstance(item, Exception))
        assert isinstance(loser, ExecutionControlRepositoryConflict)
        assert loser.code in {
            "attempt_reservation_conflict",
            "stale_execution_control",
        }
        async with race_case.uow_factory() as unit_of_work:
            attempts = await unit_of_work.execution_control.list_attempts(
                race_case.step_ids[0], race_case.policy.operations[0].operation_key
            )
            control = await unit_of_work.execution_control.get(race_case.policy.run_id)
        assert len(attempts) == 1
        assert control is not None and control.model_calls == 1
    finally:
        await race_case.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "complete"])
async def test_orch_06_busy_cancel_or_completion_is_sanitized_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    prepared = await _prepare(tmp_path / f"busy-{operation}.db")
    command = _command(prepared, suffix=f"busy.{operation}")
    reserved = await _reserve(prepared, command) if operation == "complete" else None
    busy_error = sqlite3.OperationalError("database is locked")
    busy_error.sqlite_errorcode = sqlite3.SQLITE_BUSY

    async def raise_busy(*args: object, **kwargs: object) -> None:
        raise OperationalError("UPDATE", {}, busy_error)

    try:
        async with prepared.uow_factory() as unit_of_work:
            repository = cast(
                SQLAlchemyExecutionControlRepository,
                unit_of_work.execution_control,
            )
            monkeypatch.setattr(repository._session, "scalar", raise_busy)
            with pytest.raises(ExecutionControlRepositoryConflict) as conflict:
                if operation == "cancel":
                    await repository.request_cancel(
                        run_id=prepared.policy.run_id,
                        expected_control_version=prepared.control_version,
                        actor_digest=hashlib.sha256(b"principal.busy-cancel").hexdigest(),
                        requested_at=prepared.started_at + timedelta(seconds=1),
                    )
                else:
                    assert reserved is not None
                    await repository.complete_attempt(
                        AttemptCompletionCommand(
                            attempt_id=reserved.attempt.id,
                            outcome=AttemptOutcome.SUCCEEDED,
                            expected_control_version=reserved.control.version,
                            completed_at=command.reserved_at + timedelta(seconds=1),
                            output_artifact_id="artifact.not-reached.busy",
                        )
                    )
        assert conflict.value.code == "stale_execution_control"

        async with prepared.uow_factory() as observing_uow:
            control = await observing_uow.execution_control.get(prepared.policy.run_id)
            attempt = await observing_uow.execution_control.get_attempt(command.attempt_id)
        assert control is not None
        if operation == "cancel":
            assert control.version == prepared.control_version
            assert control.cancel_requested_at is None
            assert attempt is None
        else:
            assert reserved is not None
            assert control == reserved.control
            assert attempt == reserved.attempt
            assert attempt.outcome is None
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("inflation", ["run_timeout", "attempts", "omit_operation"])
async def test_orch_06_initialize_rejects_policy_inflation_or_incomplete_operation_set(
    tmp_path: Path,
    inflation: str,
) -> None:
    prepared = await _prepare(
        tmp_path / f"initialize-{inflation}.db",
        max_model_calls=2,
        parallel_steps=True,
    )
    policy = prepared.policy
    if inflation == "run_timeout":
        policy = replace(policy, run_timeout_seconds=policy.run_timeout_seconds + 1)
    elif inflation == "attempts":
        policy = replace(
            policy,
            operations=(replace(policy.operations[0], max_attempts=2), *policy.operations[1:]),
        )
    else:
        policy = replace(policy, operations=policy.operations[:1])
    try:
        async with prepared.uow_factory() as unit_of_work:
            with pytest.raises(ExecutionControlRepositoryConflict) as captured:
                await unit_of_work.execution_control.initialize(policy)
        assert captured.value.code == "execution_policy_binding_invalid"
        async with prepared.runtime.session_factory() as session:
            count = int(
                (
                    await session.execute(select(func.count(RunExecutionControlRecord.run_id)))
                ).scalar_one()
            )
        assert count == 1
    finally:
        await prepared.runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["control", "operation", "operation_payload_budget", "attempt", "window"],
)
async def test_orch_06_hmac_tamper_blocks_every_authoritative_record(
    tmp_path: Path,
    target: str,
) -> None:
    prepared = await _prepare(tmp_path / f"tamper-{target}.db")
    command = _command(prepared, suffix=f"tamper.{target}")
    reserved = await _reserve(prepared, command)
    try:
        async with prepared.runtime.session_factory() as session:
            if target == "control":
                await session.execute(
                    update(RunExecutionControlRecord)
                    .where(RunExecutionControlRecord.run_id == prepared.policy.run_id)
                    .values(max_model_calls=99)
                )
            elif target in {"operation", "operation_payload_budget"}:
                await session.execute(
                    update(ExecutionOperationPolicyRecord)
                    .where(ExecutionOperationPolicyRecord.step_id == prepared.step_ids[0])
                    .values(
                        **(
                            {"result_schema_id": "schema.tampered.result"}
                            if target == "operation"
                            else {"max_output_bytes": 262_143}
                        )
                    )
                )
            elif target == "attempt":
                await session.execute(
                    update(ExecutionAttemptRecord)
                    .where(ExecutionAttemptRecord.id == command.attempt_id)
                    .values(source_step_version=99)
                )
            else:
                await session.execute(
                    update(RateLimitWindowRecord)
                    .where(
                        RateLimitWindowRecord.scope == reserved.rate_window.scope.value,
                        RateLimitWindowRecord.key == reserved.rate_window.key,
                        RateLimitWindowRecord.started_at == reserved.rate_window.started_at,
                    )
                    .values(capacity=99)
                )
            await session.commit()
        async with prepared.uow_factory() as unit_of_work:
            with pytest.raises(ExecutionControlRepositoryConflict) as captured:
                if target == "control":
                    await unit_of_work.execution_control.complete_attempt(
                        AttemptCompletionCommand(
                            attempt_id=command.attempt_id,
                            outcome=AttemptOutcome.SUCCEEDED,
                            expected_control_version=reserved.control.version,
                            completed_at=command.reserved_at + timedelta(seconds=1),
                            output_artifact_id="artifact.not-reached.tamper",
                        )
                    )
                elif target in {"operation", "operation_payload_budget"}:
                    await unit_of_work.execution_control.get_operation(
                        prepared.step_ids[0], prepared.policy.operations[0].operation_key
                    )
                elif target == "attempt":
                    await unit_of_work.execution_control.get_attempt(command.attempt_id)
                else:
                    await unit_of_work.execution_control.get_rate_window(
                        reserved.rate_window.scope,
                        reserved.rate_window.key,
                        reserved.rate_window.started_at,
                    )
        assert captured.value.code == "execution_control_integrity_corrupt"
        if target == "control":
            async with prepared.runtime.session_factory() as session:
                attempt_record = (
                    await session.execute(
                        select(ExecutionAttemptRecord).where(
                            ExecutionAttemptRecord.id == command.attempt_id
                        )
                    )
                ).scalar_one()
            assert attempt_record.outcome is None
            assert attempt_record.version == 1
    finally:
        await prepared.runtime.dispose()
