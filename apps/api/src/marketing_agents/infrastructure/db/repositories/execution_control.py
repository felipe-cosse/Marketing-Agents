"""SQLAlchemy persistence for durable generic READ-operation execution control."""

from __future__ import annotations

import hmac
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import (
    AttemptCompletionResult,
    AttemptReservationResult,
    DeliveryCallReservationResult,
    ExecutionCancellationFenceResult,
    ExecutionControlInsertResult,
    ExecutionControlRepositoryConflict,
    ExecutionControlStartResult,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import Effect, RunState, StepState
from marketing_agents.domain.execution_control import (
    AttemptCompletionCommand,
    AttemptOutcome,
    AttemptReservationCommand,
    DeliveryCallPermit,
    DeliveryCallReservationCommand,
    ExecutionAttempt,
    ExpiredAttemptRecoveryCommand,
    OperationExecutionPolicy,
    RateLimitWindow,
    RunExecutionControl,
    RunExecutionPolicy,
    bounded_retry_delay_seconds,
    fixed_window_start,
)
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    RateLimitScope,
    RetryBackoff,
    effective_call_timeout_seconds,
)
from marketing_agents.domain.validation import require_digest, require_id, require_utc
from marketing_agents.infrastructure.db.models.action import (
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
)
from marketing_agents.infrastructure.db.models.execution_control import (
    ExecutionAttemptRecord,
    ExecutionOperationPolicyRecord,
    RateLimitWindowRecord,
    RunExecutionControlRecord,
)
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.infrastructure.db.models.step import RunStepRecord
from marketing_agents.infrastructure.db.repositories.step import (
    SQLAlchemyRunStepRepository,
    StepPersistenceConflict,
)
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.execution_control_digest import (
    execution_attempt_record_digest,
    execution_control_record_digest,
    execution_operation_record_digest,
    rate_limit_window_record_digest,
)


class ExecutionControlPersistenceConflict(ExecutionControlRepositoryConflict):
    """Sanitized runtime-control corruption, contention, or policy denial."""


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds")


def _redaction_tuple(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ValueError(f"persisted {name} must be an exact JSON string array")
    return tuple(value)


def _control_material(record: RunExecutionControlRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "policy_hash": record.policy_hash,
        "run_timeout_seconds": record.run_timeout_seconds,
        "max_model_calls": record.max_model_calls,
        "max_tool_calls": record.max_tool_calls,
        "model_calls": record.model_calls,
        "tool_calls": record.tool_calls,
        "started_at": _time(record.started_at),
        "deadline_at": _time(record.deadline_at),
        "cancel_requested_at": _time(record.cancel_requested_at),
        "cancel_actor_digest": record.cancel_actor_digest,
        "created_at": _time(record.created_at),
        "updated_at": _time(record.updated_at),
        "version": record.version,
    }


def _operation_material(record: ExecutionOperationPolicyRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "step_id": record.step_id,
        "operation_key": record.operation_key,
        "kind": record.kind,
        "capability_id": record.capability_id,
        "selected_instance_id": record.selected_instance_id,
        "configuration_revision": record.configuration_revision,
        "connector_family": record.connector_family,
        "binding_id": record.binding_id,
        "binding_configuration_revision": record.binding_configuration_revision,
        "request_schema_id": record.request_schema_id,
        "result_schema_id": record.result_schema_id,
        "request_redaction_fields": record.request_redaction_fields,
        "result_redaction_fields": record.result_redaction_fields,
        "data_classification": record.data_classification,
        "connector_timeout_seconds": record.connector_timeout_seconds,
        "policy_hash": record.policy_hash,
        "max_attempts": record.max_attempts,
        "retry_backoff": record.retry_backoff,
        "step_timeout_seconds": record.step_timeout_seconds,
        "max_input_bytes": record.max_input_bytes,
        "max_input_field_bytes": record.max_input_field_bytes,
        "max_output_bytes": record.max_output_bytes,
        "max_model_output_tokens": record.max_model_output_tokens,
        "rate_limit_scope": record.rate_limit_scope,
        "rate_limit_key": record.rate_limit_key,
        "rate_window_max_calls": record.rate_window_max_calls,
        "rate_window_seconds": record.rate_window_seconds,
        "created_at": _time(record.created_at),
    }


def _attempt_material(record: ExecutionAttemptRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "run_id": record.run_id,
        "step_id": record.step_id,
        "operation_key": record.operation_key,
        "policy_hash": record.policy_hash,
        "kind": record.kind,
        "attempt_number": record.attempt_number,
        "source_control_version": record.source_control_version,
        "source_step_version": record.source_step_version,
        "eligible_at": _time(record.eligible_at),
        "reserved_at": _time(record.reserved_at),
        "call_deadline_at": _time(record.call_deadline_at),
        "rate_limit_scope": record.rate_limit_scope,
        "rate_limit_key": record.rate_limit_key,
        "rate_window_started_at": _time(record.rate_window_started_at),
        "outcome": record.outcome,
        "completed_at": _time(record.completed_at),
        "retry_not_before": _time(record.retry_not_before),
        "terminal_reason_code": record.terminal_reason_code,
        "version": record.version,
    }


def _window_material(record: RateLimitWindowRecord) -> dict[str, Any]:
    return {
        "scope": record.scope,
        "key": record.key,
        "started_at": _time(record.started_at),
        "ends_at": _time(record.ends_at),
        "capacity": record.capacity,
        "used": record.used,
        "version": record.version,
        "updated_at": _time(record.updated_at),
    }


def _raise_corrupt(message: str) -> NoReturn:
    raise ExecutionControlPersistenceConflict("execution_control_integrity_corrupt", message)


def _check_digest(actual: object, expected: str) -> None:
    if type(actual) is not str or not hmac.compare_digest(actual, expected):
        _raise_corrupt("persisted execution-control integrity verification failed")


def _is_sqlite_busy(session: AsyncSession, exc: OperationalError) -> bool:
    return bool(
        session.get_bind().dialect.name == "sqlite"
        and getattr(exc.orig, "sqlite_errorcode", None)
        in {sqlite3.SQLITE_BUSY, getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517)}
    )


def _retry_after_seconds(ends_at: datetime, now: datetime) -> int:
    remaining = max(0.0, (ends_at - now).total_seconds())
    return max(1, min(3_600, math.ceil(remaining)))


class SQLAlchemyExecutionControlRepository:
    """One-UoW atomic attempt permit with no adapter invocation capability."""

    def __init__(self, session: AsyncSession, integrity_key: DigestKey) -> None:
        self._session = session
        self._integrity_key = integrity_key

    def _control_from_record(self, record: RunExecutionControlRecord) -> RunExecutionControl:
        try:
            expected = execution_control_record_digest(
                _control_material(record), self._integrity_key
            )
            _check_digest(record.integrity_digest, expected)
            return RunExecutionControl(
                run_id=record.run_id,
                policy_hash=record.policy_hash,
                run_timeout_seconds=record.run_timeout_seconds,
                max_model_calls=record.max_model_calls,
                max_tool_calls=record.max_tool_calls,
                model_calls=record.model_calls,
                tool_calls=record.tool_calls,
                started_at=record.started_at,
                deadline_at=record.deadline_at,
                cancel_requested_at=record.cancel_requested_at,
                cancel_actor_digest=record.cancel_actor_digest,
                created_at=record.created_at,
                updated_at=record.updated_at,
                version=record.version,
            )
        except ExecutionControlPersistenceConflict:
            raise
        except (TypeError, ValueError) as exc:
            raise ExecutionControlPersistenceConflict(
                "execution_control_integrity_corrupt",
                "persisted execution control violates its immutable contract",
            ) from exc

    def _operation_from_record(
        self, record: ExecutionOperationPolicyRecord
    ) -> OperationExecutionPolicy:
        try:
            expected = execution_operation_record_digest(
                _operation_material(record), self._integrity_key
            )
            _check_digest(record.integrity_digest, expected)
            return OperationExecutionPolicy(
                run_id=record.run_id,
                step_id=record.step_id,
                operation_key=record.operation_key,
                kind=AttemptKind(record.kind),
                capability_id=record.capability_id,
                selected_instance_id=record.selected_instance_id,
                configuration_revision=record.configuration_revision,
                connector_family=record.connector_family,
                binding_id=record.binding_id,
                binding_configuration_revision=record.binding_configuration_revision,
                request_schema_id=record.request_schema_id,
                result_schema_id=record.result_schema_id,
                request_redaction_fields=_redaction_tuple(
                    record.request_redaction_fields,
                    "operation request redaction fields",
                ),
                result_redaction_fields=_redaction_tuple(
                    record.result_redaction_fields,
                    "operation result redaction fields",
                ),
                data_classification=DataClassification(record.data_classification),
                connector_timeout_seconds=record.connector_timeout_seconds,
                policy_hash=record.policy_hash,
                max_attempts=record.max_attempts,
                retry_backoff=RetryBackoff(record.retry_backoff),
                step_timeout_seconds=record.step_timeout_seconds,
                max_input_bytes=record.max_input_bytes,
                max_input_field_bytes=record.max_input_field_bytes,
                max_output_bytes=record.max_output_bytes,
                max_model_output_tokens=record.max_model_output_tokens,
                rate_limit_scope=RateLimitScope(record.rate_limit_scope),
                rate_limit_key=record.rate_limit_key,
                rate_window_max_calls=record.rate_window_max_calls,
                rate_window_seconds=record.rate_window_seconds,
            )
        except ExecutionControlPersistenceConflict:
            raise
        except (TypeError, ValueError) as exc:
            raise ExecutionControlPersistenceConflict(
                "execution_control_integrity_corrupt",
                "persisted operation policy violates its immutable contract",
            ) from exc

    def _attempt_from_record(self, record: ExecutionAttemptRecord) -> ExecutionAttempt:
        try:
            expected = execution_attempt_record_digest(
                _attempt_material(record), self._integrity_key
            )
            _check_digest(record.integrity_digest, expected)
            return ExecutionAttempt(
                id=record.id,
                run_id=record.run_id,
                step_id=record.step_id,
                operation_key=record.operation_key,
                policy_hash=record.policy_hash,
                kind=AttemptKind(record.kind),
                attempt_number=record.attempt_number,
                source_control_version=record.source_control_version,
                source_step_version=record.source_step_version,
                eligible_at=record.eligible_at,
                reserved_at=record.reserved_at,
                call_deadline_at=record.call_deadline_at,
                outcome=(None if record.outcome is None else AttemptOutcome(record.outcome)),
                completed_at=record.completed_at,
                retry_not_before=record.retry_not_before,
                terminal_reason_code=record.terminal_reason_code,
                version=record.version,
            )
        except ExecutionControlPersistenceConflict:
            raise
        except (TypeError, ValueError) as exc:
            raise ExecutionControlPersistenceConflict(
                "execution_control_integrity_corrupt",
                "persisted execution attempt violates its immutable contract",
            ) from exc

    def _window_from_record(self, record: RateLimitWindowRecord) -> RateLimitWindow:
        try:
            expected = rate_limit_window_record_digest(
                _window_material(record), self._integrity_key
            )
            _check_digest(record.integrity_digest, expected)
            return RateLimitWindow(
                scope=RateLimitScope(record.scope),
                key=record.key,
                started_at=record.started_at,
                ends_at=record.ends_at,
                capacity=record.capacity,
                used=record.used,
                version=record.version,
                updated_at=record.updated_at,
            )
        except ExecutionControlPersistenceConflict:
            raise
        except (TypeError, ValueError) as exc:
            raise ExecutionControlPersistenceConflict(
                "execution_control_integrity_corrupt",
                "persisted rate window violates its immutable contract",
            ) from exc

    async def get(self, run_id: str) -> RunExecutionControl | None:
        record = (
            await self._session.execute(
                select(RunExecutionControlRecord)
                .where(RunExecutionControlRecord.run_id == run_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return None if record is None else self._control_from_record(record)

    async def get_operation(
        self, step_id: str, operation_key: str
    ) -> OperationExecutionPolicy | None:
        record = (
            await self._session.execute(
                select(ExecutionOperationPolicyRecord)
                .where(
                    ExecutionOperationPolicyRecord.step_id == step_id,
                    ExecutionOperationPolicyRecord.operation_key == operation_key,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return None if record is None else self._operation_from_record(record)

    async def _operation_records(self, run_id: str) -> tuple[ExecutionOperationPolicyRecord, ...]:
        return tuple(
            (
                await self._session.scalars(
                    select(ExecutionOperationPolicyRecord)
                    .where(ExecutionOperationPolicyRecord.run_id == run_id)
                    .order_by(
                        ExecutionOperationPolicyRecord.step_id,
                        ExecutionOperationPolicyRecord.operation_key,
                    )
                    .execution_options(populate_existing=True)
                )
            ).all()
        )

    async def _exact_initialization_replay(
        self,
        policy: RunExecutionPolicy,
        control: RunExecutionControl,
    ) -> ExecutionControlInsertResult:
        if (
            control.policy_hash != policy.policy_hash
            or control.run_timeout_seconds != policy.run_timeout_seconds
            or control.max_model_calls != policy.max_model_calls
            or control.max_tool_calls != policy.max_tool_calls
            or control.created_at != policy.created_at
        ):
            raise ExecutionControlPersistenceConflict(
                "execution_policy_conflict",
                "Run execution control already exists with a different policy",
            )
        stored_records = await self._operation_records(policy.run_id)
        stored = tuple(self._operation_from_record(record) for record in stored_records)
        expected = tuple(
            sorted(policy.operations, key=lambda item: (item.step_id, item.operation_key))
        )
        actual = tuple(sorted(stored, key=lambda item: (item.step_id, item.operation_key)))
        if actual != expected or any(
            record.created_at != policy.created_at for record in stored_records
        ):
            raise ExecutionControlPersistenceConflict(
                "execution_policy_conflict",
                "persisted operation policies differ from the trusted Run policy",
            )
        return ExecutionControlInsertResult(control=control, operations=actual, inserted=False)

    async def _validate_policy_bindings(self, policy: RunExecutionPolicy) -> None:
        run = await self._session.get(RunRecord, policy.run_id)
        if run is None:
            raise ExecutionControlPersistenceConflict(
                "execution_run_missing", "execution policy requires an existing Run"
            )
        if policy.created_at < run.created_at:
            raise ExecutionControlPersistenceConflict(
                "execution_policy_time_invalid", "execution policy predates its Run"
            )
        step_repository = SQLAlchemyRunStepRepository(self._session)
        try:
            plan = await step_repository.get_plan(policy.run_id)
            steps = await step_repository.list_for_run(policy.run_id)
        except StepPersistenceConflict as exc:
            raise ExecutionControlPersistenceConflict(
                "execution_policy_source_corrupt",
                "persisted runtime policy source cannot be trusted",
            ) from exc
        if plan is None:
            raise ExecutionControlPersistenceConflict(
                "execution_plan_missing", "execution policy requires an immutable Run plan"
            )
        if (
            policy.policy_hash != plan.plan_hash
            or policy.run_timeout_seconds != plan.runtime_policy.run_timeout_seconds
            or policy.max_model_calls != plan.runtime_policy.max_model_calls
            or policy.max_tool_calls != plan.runtime_policy.max_tool_calls
            or policy.created_at != plan.created_at
        ):
            raise ExecutionControlPersistenceConflict(
                "execution_policy_binding_invalid",
                "Run execution policy differs from its immutable plan snapshot",
            )
        expected_steps = {
            (step.id, step.runtime_policy.operation_key): step
            for step in steps
            if step.effect is Effect.READ
            and step.connector_family != "artifact"
            and step.runtime_policy.attempt_kind in {AttemptKind.MODEL, AttemptKind.TOOL}
        }
        provided = {
            (operation.step_id, operation.operation_key): operation
            for operation in policy.operations
        }
        if set(provided) != set(expected_steps):
            raise ExecutionControlPersistenceConflict(
                "execution_policy_binding_invalid",
                "execution policy must contain every exact generic READ operation",
            )
        for operation in policy.operations:
            step = expected_steps[(operation.step_id, operation.operation_key)]
            runtime_policy = step.runtime_policy
            if (
                step.run_id != policy.run_id
                or operation.policy_hash != plan.plan_hash
                or operation.kind is not runtime_policy.attempt_kind
                or operation.capability_id != step.capability_id
                or operation.selected_instance_id != step.selected_instance_id
                or operation.configuration_revision != step.configuration_revision
                or operation.connector_family != step.connector_family
                or operation.binding_id != step.binding_id
                or operation.binding_configuration_revision != step.binding_configuration_revision
                or operation.request_schema_id != step.request_schema_id
                or operation.result_schema_id != step.result_schema_id
                or operation.request_redaction_fields != step.request_redaction_fields
                or operation.result_redaction_fields != step.result_redaction_fields
                or operation.data_classification is not step.data_classification
                or operation.connector_timeout_seconds != step.timeout_seconds
                or operation.max_attempts != runtime_policy.retry.max_attempts
                or operation.retry_backoff is not runtime_policy.retry.backoff
                or operation.step_timeout_seconds
                != effective_call_timeout_seconds(runtime_policy, step.timeout_seconds)
                or operation.max_input_bytes != runtime_policy.budget.max_input_bytes
                or operation.max_input_field_bytes != runtime_policy.budget.max_input_field_bytes
                or operation.max_output_bytes != runtime_policy.budget.max_output_bytes
                or operation.max_model_output_tokens
                != runtime_policy.budget.max_model_output_tokens
                or operation.rate_limit_scope is not runtime_policy.rate_limit.scope
                or operation.rate_limit_key != runtime_policy.rate_limit.key
                or operation.rate_window_max_calls != runtime_policy.rate_limit.max_calls
                or operation.rate_window_seconds != runtime_policy.rate_limit.window_seconds
            ):
                raise ExecutionControlPersistenceConflict(
                    "execution_policy_binding_invalid",
                    "operation policy does not bind one exact generic READ step",
                )

    async def initialize(self, policy: RunExecutionPolicy) -> ExecutionControlInsertResult:
        if type(policy) is not RunExecutionPolicy:
            raise TypeError("execution initialization requires an exact RunExecutionPolicy")
        await self._validate_policy_bindings(policy)
        existing = await self.get(policy.run_id)
        if existing is not None:
            return await self._exact_initialization_replay(policy, existing)
        control_record = RunExecutionControlRecord(
            run_id=policy.run_id,
            policy_hash=policy.policy_hash,
            run_timeout_seconds=policy.run_timeout_seconds,
            max_model_calls=policy.max_model_calls,
            max_tool_calls=policy.max_tool_calls,
            model_calls=0,
            tool_calls=0,
            started_at=None,
            deadline_at=None,
            cancel_requested_at=None,
            cancel_actor_digest=None,
            created_at=policy.created_at,
            updated_at=policy.created_at,
            version=1,
            integrity_digest="",
        )
        control_record.integrity_digest = execution_control_record_digest(
            _control_material(control_record), self._integrity_key
        )
        operation_records: list[ExecutionOperationPolicyRecord] = []
        for operation in policy.operations:
            record = ExecutionOperationPolicyRecord(
                run_id=operation.run_id,
                step_id=operation.step_id,
                operation_key=operation.operation_key,
                kind=operation.kind.value,
                capability_id=operation.capability_id,
                selected_instance_id=operation.selected_instance_id,
                configuration_revision=operation.configuration_revision,
                connector_family=operation.connector_family,
                binding_id=operation.binding_id,
                binding_configuration_revision=operation.binding_configuration_revision,
                request_schema_id=operation.request_schema_id,
                result_schema_id=operation.result_schema_id,
                request_redaction_fields=list(operation.request_redaction_fields),
                result_redaction_fields=list(operation.result_redaction_fields),
                data_classification=operation.data_classification.value,
                connector_timeout_seconds=operation.connector_timeout_seconds,
                policy_hash=operation.policy_hash,
                max_attempts=operation.max_attempts,
                retry_backoff=operation.retry_backoff.value,
                step_timeout_seconds=operation.step_timeout_seconds,
                max_input_bytes=operation.max_input_bytes,
                max_input_field_bytes=operation.max_input_field_bytes,
                max_output_bytes=operation.max_output_bytes,
                max_model_output_tokens=operation.max_model_output_tokens,
                rate_limit_scope=operation.rate_limit_scope.value,
                rate_limit_key=operation.rate_limit_key,
                rate_window_max_calls=operation.rate_window_max_calls,
                rate_window_seconds=operation.rate_window_seconds,
                created_at=policy.created_at,
                integrity_digest="",
            )
            record.integrity_digest = execution_operation_record_digest(
                _operation_material(record), self._integrity_key
            )
            operation_records.append(record)
        try:
            async with self._session.begin_nested():
                self._session.add(control_record)
                await self._session.flush()
                self._session.add_all(operation_records)
                await self._session.flush()
        except IntegrityError as exc:
            current = await self.get(policy.run_id)
            if current is not None:
                return await self._exact_initialization_replay(policy, current)
            raise ExecutionControlPersistenceConflict(
                "execution_policy_conflict", "execution policy could not be installed atomically"
            ) from exc
        except OperationalError as exc:
            code = (
                "execution_policy_conflict"
                if _is_sqlite_busy(self._session, exc)
                else "execution_storage_error"
            )
            raise ExecutionControlPersistenceConflict(
                code, "execution policy could not be installed atomically"
            ) from exc
        control = self._control_from_record(control_record)
        operations = tuple(
            sorted(policy.operations, key=lambda item: (item.step_id, item.operation_key))
        )
        return ExecutionControlInsertResult(control=control, operations=operations, inserted=True)

    async def start_execution(
        self,
        *,
        run_id: str,
        expected_control_version: int,
        started_at: datetime,
    ) -> ExecutionControlStartResult:
        require_id(run_id, "execution start Run ID")
        require_utc(started_at, "execution start time")
        if type(expected_control_version) is not int or expected_control_version < 1:
            raise ValueError("expected execution-control version must be positive")
        current = await self.get(run_id)
        if current is None:
            raise ExecutionControlPersistenceConflict(
                "execution_control_missing", "Run execution control is not initialized"
            )
        if current.started_at is not None:
            if current.started_at == started_at:
                return ExecutionControlStartResult(control=current, started=False)
            raise ExecutionControlPersistenceConflict(
                "execution_start_conflict", "Run execution already started at a different instant"
            )
        if current.cancel_requested_at is not None:
            raise ExecutionControlPersistenceConflict(
                "run_cancelled", "cancelled execution cannot be started"
            )
        if current.version != expected_control_version:
            raise ExecutionControlPersistenceConflict(
                "stale_execution_control", "execution-control version is stale"
            )
        if started_at < current.updated_at:
            raise ExecutionControlPersistenceConflict(
                "execution_start_time_invalid", "execution start time is stale"
            )
        run = await self._session.get(RunRecord, run_id)
        if run is None or run.state != RunState.EXECUTING.value:
            raise ExecutionControlPersistenceConflict(
                "run_not_executing", "Run must be executing before its deadline starts"
            )
        deadline_at = started_at + timedelta(seconds=current.run_timeout_seconds)
        next_control = RunExecutionControl(
            run_id=current.run_id,
            policy_hash=current.policy_hash,
            run_timeout_seconds=current.run_timeout_seconds,
            max_model_calls=current.max_model_calls,
            max_tool_calls=current.max_tool_calls,
            model_calls=current.model_calls,
            tool_calls=current.tool_calls,
            started_at=started_at,
            deadline_at=deadline_at,
            cancel_requested_at=None,
            cancel_actor_digest=None,
            created_at=current.created_at,
            updated_at=started_at,
            version=current.version + 1,
        )
        material_record = RunExecutionControlRecord(
            run_id=next_control.run_id,
            policy_hash=next_control.policy_hash,
            run_timeout_seconds=next_control.run_timeout_seconds,
            max_model_calls=next_control.max_model_calls,
            max_tool_calls=next_control.max_tool_calls,
            model_calls=next_control.model_calls,
            tool_calls=next_control.tool_calls,
            started_at=next_control.started_at,
            deadline_at=next_control.deadline_at,
            cancel_requested_at=None,
            cancel_actor_digest=None,
            created_at=next_control.created_at,
            updated_at=next_control.updated_at,
            version=next_control.version,
            integrity_digest="",
        )
        digest = execution_control_record_digest(
            _control_material(material_record), self._integrity_key
        )
        updated_id = await self._session.scalar(
            update(RunExecutionControlRecord)
            .where(
                RunExecutionControlRecord.run_id == run_id,
                RunExecutionControlRecord.version == expected_control_version,
                RunExecutionControlRecord.integrity_digest
                == execution_control_record_digest(
                    {
                        "run_id": current.run_id,
                        "policy_hash": current.policy_hash,
                        "run_timeout_seconds": current.run_timeout_seconds,
                        "max_model_calls": current.max_model_calls,
                        "max_tool_calls": current.max_tool_calls,
                        "model_calls": current.model_calls,
                        "tool_calls": current.tool_calls,
                        "started_at": _time(current.started_at),
                        "deadline_at": _time(current.deadline_at),
                        "cancel_requested_at": _time(current.cancel_requested_at),
                        "cancel_actor_digest": current.cancel_actor_digest,
                        "created_at": _time(current.created_at),
                        "updated_at": _time(current.updated_at),
                        "version": current.version,
                    },
                    self._integrity_key,
                ),
                RunExecutionControlRecord.started_at.is_(None),
                RunExecutionControlRecord.cancel_requested_at.is_(None),
            )
            .values(
                started_at=started_at,
                deadline_at=deadline_at,
                updated_at=started_at,
                version=next_control.version,
                integrity_digest=digest,
            )
            .returning(RunExecutionControlRecord.run_id)
        )
        if updated_id is None:
            raise ExecutionControlPersistenceConflict(
                "stale_execution_control", "execution start lost its atomic version fence"
            )
        return ExecutionControlStartResult(control=next_control, started=True)

    async def request_cancel(
        self,
        *,
        run_id: str,
        expected_control_version: int,
        actor_digest: str,
        requested_at: datetime,
    ) -> ExecutionCancellationFenceResult:
        require_id(run_id, "cancellation Run ID")
        require_digest(actor_digest, "cancellation actor digest")
        require_utc(requested_at, "cancellation request time")
        if type(expected_control_version) is not int or expected_control_version < 1:
            raise ValueError("expected execution-control version must be positive")
        current = await self.get(run_id)
        if current is None:
            raise ExecutionControlPersistenceConflict(
                "execution_control_missing", "Run execution control is not initialized"
            )
        if current.cancel_requested_at is not None:
            if (
                current.cancel_requested_at == requested_at
                and current.cancel_actor_digest == actor_digest
            ):
                return ExecutionCancellationFenceResult(control=current, fenced=False)
            raise ExecutionControlPersistenceConflict(
                "cancellation_conflict", "Run cancellation fence already differs"
            )
        if current.version != expected_control_version:
            raise ExecutionControlPersistenceConflict(
                "stale_execution_control", "execution-control version is stale"
            )
        if requested_at < current.updated_at:
            raise ExecutionControlPersistenceConflict(
                "cancellation_time_invalid", "cancellation request time is stale"
            )
        next_control = RunExecutionControl(
            run_id=current.run_id,
            policy_hash=current.policy_hash,
            run_timeout_seconds=current.run_timeout_seconds,
            max_model_calls=current.max_model_calls,
            max_tool_calls=current.max_tool_calls,
            model_calls=current.model_calls,
            tool_calls=current.tool_calls,
            started_at=current.started_at,
            deadline_at=current.deadline_at,
            cancel_requested_at=requested_at,
            cancel_actor_digest=actor_digest,
            created_at=current.created_at,
            updated_at=requested_at,
            version=current.version + 1,
        )
        material_record = self._control_record(next_control)
        digest = execution_control_record_digest(
            _control_material(material_record), self._integrity_key
        )
        current_digest = execution_control_record_digest(
            _control_material(self._control_record(current)), self._integrity_key
        )
        try:
            async with self._session.begin_nested():
                updated_id = await self._session.scalar(
                    update(RunExecutionControlRecord)
                    .where(
                        RunExecutionControlRecord.run_id == run_id,
                        RunExecutionControlRecord.version == expected_control_version,
                        RunExecutionControlRecord.integrity_digest == current_digest,
                        RunExecutionControlRecord.cancel_requested_at.is_(None),
                    )
                    .values(
                        cancel_requested_at=requested_at,
                        cancel_actor_digest=actor_digest,
                        updated_at=requested_at,
                        version=next_control.version,
                        integrity_digest=digest,
                    )
                    .returning(RunExecutionControlRecord.run_id)
                )
        except IntegrityError as exc:
            raise ExecutionControlPersistenceConflict(
                "cancellation_conflict",
                "Run cancellation lost its atomic persistence fence",
            ) from exc
        except OperationalError as exc:
            code = (
                "stale_execution_control"
                if _is_sqlite_busy(self._session, exc)
                else "execution_storage_error"
            )
            raise ExecutionControlPersistenceConflict(
                code,
                "Run cancellation could not be persisted atomically",
            ) from exc
        if updated_id is None:
            raise ExecutionControlPersistenceConflict(
                "stale_execution_control", "cancellation lost its atomic version fence"
            )
        return ExecutionCancellationFenceResult(control=next_control, fenced=True)

    def _control_record(self, control: RunExecutionControl) -> RunExecutionControlRecord:
        return RunExecutionControlRecord(
            run_id=control.run_id,
            policy_hash=control.policy_hash,
            run_timeout_seconds=control.run_timeout_seconds,
            max_model_calls=control.max_model_calls,
            max_tool_calls=control.max_tool_calls,
            model_calls=control.model_calls,
            tool_calls=control.tool_calls,
            started_at=control.started_at,
            deadline_at=control.deadline_at,
            cancel_requested_at=control.cancel_requested_at,
            cancel_actor_digest=control.cancel_actor_digest,
            created_at=control.created_at,
            updated_at=control.updated_at,
            version=control.version,
            integrity_digest="",
        )

    async def get_attempt(self, attempt_id: str) -> ExecutionAttempt | None:
        record = (
            await self._session.execute(
                select(ExecutionAttemptRecord)
                .where(ExecutionAttemptRecord.id == attempt_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return None if record is None else self._attempt_from_record(record)

    async def list_attempts(self, step_id: str, operation_key: str) -> tuple[ExecutionAttempt, ...]:
        records = tuple(
            (
                await self._session.scalars(
                    select(ExecutionAttemptRecord)
                    .where(
                        ExecutionAttemptRecord.step_id == step_id,
                        ExecutionAttemptRecord.operation_key == operation_key,
                    )
                    .order_by(ExecutionAttemptRecord.attempt_number)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        attempts = tuple(self._attempt_from_record(record) for record in records)
        if tuple(item.attempt_number for item in attempts) != tuple(range(1, len(attempts) + 1)):
            _raise_corrupt("persisted attempt numbers are not contiguous")
        return attempts

    async def get_rate_window(
        self,
        scope: RateLimitScope,
        key: str,
        started_at: datetime,
    ) -> RateLimitWindow | None:
        record = await self._get_window_record(scope, key, started_at)
        return None if record is None else self._window_from_record(record)

    async def _get_window_record(
        self,
        scope: RateLimitScope,
        key: str,
        started_at: datetime,
    ) -> RateLimitWindowRecord | None:
        return (
            await self._session.execute(
                select(RateLimitWindowRecord)
                .where(
                    RateLimitWindowRecord.scope == scope.value,
                    RateLimitWindowRecord.key == key,
                    RateLimitWindowRecord.started_at == started_at,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _reservation_replay(
        self,
        command: AttemptReservationCommand,
        attempt: ExecutionAttempt,
    ) -> AttemptReservationResult:
        if (
            attempt.run_id != command.run_id
            or attempt.step_id != command.step_id
            or attempt.operation_key != command.operation_key
            or attempt.source_control_version != command.expected_control_version
            or attempt.source_step_version != command.expected_step_version
            or attempt.reserved_at != command.reserved_at
        ):
            raise ExecutionControlPersistenceConflict(
                "attempt_id_conflict", "attempt ID already binds a different reservation"
            )
        control = await self.get(command.run_id)
        if control is None:
            _raise_corrupt("reserved attempt has no parent execution control")
        operation = await self.get_operation(command.step_id, command.operation_key)
        if operation is None:
            _raise_corrupt("reserved attempt has no operation policy")
        window_start = fixed_window_start(command.reserved_at, operation.rate_window_seconds)
        window = await self.get_rate_window(
            operation.rate_limit_scope, operation.rate_limit_key, window_start
        )
        if window is None:
            _raise_corrupt("reserved attempt has no rate-window witness")
        return AttemptReservationResult(control=control, attempt=attempt, rate_window=window)

    async def reserve_attempt(self, command: AttemptReservationCommand) -> AttemptReservationResult:
        if type(command) is not AttemptReservationCommand:
            raise TypeError("attempt reservation requires an exact command")
        replay = await self.get_attempt(command.attempt_id)
        if replay is not None:
            return await self._reservation_replay(command, replay)
        control = await self.get(command.run_id)
        if control is None:
            raise ExecutionControlPersistenceConflict(
                "execution_control_missing", "Run execution control is not initialized"
            )
        if control.cancel_requested_at is not None:
            raise ExecutionControlPersistenceConflict("run_cancelled", "Run is cancellation-fenced")
        if control.started_at is None or control.deadline_at is None:
            raise ExecutionControlPersistenceConflict(
                "execution_not_started", "Run execution deadline has not started"
            )
        if command.reserved_at >= control.deadline_at:
            raise ExecutionControlPersistenceConflict(
                "deadline_exceeded", "Run execution deadline has expired"
            )
        if control.version != command.expected_control_version:
            raise ExecutionControlPersistenceConflict(
                "stale_execution_control", "execution-control version is stale"
            )
        if command.reserved_at < control.updated_at:
            raise ExecutionControlPersistenceConflict(
                "attempt_time_invalid", "attempt reservation time is stale"
            )
        run = await self._session.get(RunRecord, command.run_id)
        if run is None or run.state != RunState.EXECUTING.value:
            raise ExecutionControlPersistenceConflict(
                "run_not_executing", "attempt reservation requires an executing Run"
            )
        step = await self._session.get(RunStepRecord, command.step_id)
        if (
            step is None
            or step.run_id != command.run_id
            or step.version != command.expected_step_version
            or step.effect != Effect.READ.value
            or step.state not in {StepState.READY.value, StepState.EXECUTING.value}
        ):
            raise ExecutionControlPersistenceConflict(
                "step_fence_invalid", "attempt reservation requires the exact runnable READ step"
            )
        operation = await self.get_operation(command.step_id, command.operation_key)
        if operation is None or operation.run_id != command.run_id:
            raise ExecutionControlPersistenceConflict(
                "operation_policy_missing", "attempt operation policy is missing"
            )
        attempts = await self.list_attempts(command.step_id, command.operation_key)
        if attempts:
            previous = attempts[-1]
            if previous.outcome is None:
                raise ExecutionControlPersistenceConflict(
                    "attempt_in_progress", "previous attempt is not completed"
                )
            if previous.retry_not_before is None:
                raise ExecutionControlPersistenceConflict(
                    previous.terminal_reason_code or "attempt_not_retryable",
                    "previous attempt has no retry authority",
                )
            attempt_number = previous.attempt_number + 1
            eligible_at = previous.retry_not_before
            if command.reserved_at < eligible_at:
                raise ExecutionControlPersistenceConflict(
                    "retry_not_ready",
                    "retry backoff has not elapsed",
                    retry_after_seconds=_retry_after_seconds(eligible_at, command.reserved_at),
                )
        else:
            attempt_number = 1
            eligible_at = command.reserved_at
        if attempt_number > operation.max_attempts:
            raise ExecutionControlPersistenceConflict(
                "attempts_exhausted", "operation attempt budget is exhausted"
            )
        consumes_logical_budget = attempt_number == 1
        if consumes_logical_budget and operation.kind is AttemptKind.MODEL:
            if control.model_calls >= control.max_model_calls:
                raise ExecutionControlPersistenceConflict(
                    "model_budget_exhausted", "Run model-call budget is exhausted"
                )
        elif (
            consumes_logical_budget
            and operation.kind is AttemptKind.TOOL
            and control.tool_calls >= control.max_tool_calls
        ):
            raise ExecutionControlPersistenceConflict(
                "tool_budget_exhausted", "Run tool-call budget is exhausted"
            )
        window_start = fixed_window_start(command.reserved_at, operation.rate_window_seconds)
        window_end = window_start + timedelta(seconds=operation.rate_window_seconds)
        current_window_record = await self._get_window_record(
            operation.rate_limit_scope, operation.rate_limit_key, window_start
        )
        current_window = (
            None
            if current_window_record is None
            else self._window_from_record(current_window_record)
        )
        if current_window is not None and (
            current_window.capacity != operation.rate_window_max_calls
            or current_window.ends_at != window_end
        ):
            _raise_corrupt("rate-window policy differs from the immutable operation policy")
        if current_window is not None and current_window.used >= current_window.capacity:
            raise ExecutionControlPersistenceConflict(
                "rate_limit_exhausted",
                "fixed rate window is exhausted",
                retry_after_seconds=_retry_after_seconds(window_end, command.reserved_at),
            )
        call_deadline = min(
            command.reserved_at + timedelta(seconds=operation.step_timeout_seconds),
            control.deadline_at,
        )
        if call_deadline <= command.reserved_at:
            raise ExecutionControlPersistenceConflict(
                "deadline_exceeded", "no positive call window remains"
            )
        next_control = RunExecutionControl(
            run_id=control.run_id,
            policy_hash=control.policy_hash,
            run_timeout_seconds=control.run_timeout_seconds,
            max_model_calls=control.max_model_calls,
            max_tool_calls=control.max_tool_calls,
            model_calls=control.model_calls
            + int(consumes_logical_budget and operation.kind is AttemptKind.MODEL),
            tool_calls=control.tool_calls
            + int(consumes_logical_budget and operation.kind is AttemptKind.TOOL),
            started_at=control.started_at,
            deadline_at=control.deadline_at,
            cancel_requested_at=None,
            cancel_actor_digest=None,
            created_at=control.created_at,
            updated_at=command.reserved_at,
            version=control.version + 1,
        )
        next_control_record = self._control_record(next_control)
        next_control_digest = execution_control_record_digest(
            _control_material(next_control_record), self._integrity_key
        )
        current_control_digest = execution_control_record_digest(
            _control_material(self._control_record(control)), self._integrity_key
        )
        next_window = RateLimitWindow(
            scope=operation.rate_limit_scope,
            key=operation.rate_limit_key,
            started_at=window_start,
            ends_at=window_end,
            capacity=operation.rate_window_max_calls,
            used=1 if current_window is None else current_window.used + 1,
            version=1 if current_window is None else current_window.version + 1,
            updated_at=command.reserved_at,
        )
        window_record = RateLimitWindowRecord(
            scope=next_window.scope.value,
            key=next_window.key,
            started_at=next_window.started_at,
            ends_at=next_window.ends_at,
            capacity=next_window.capacity,
            used=next_window.used,
            version=next_window.version,
            updated_at=next_window.updated_at,
            integrity_digest="",
        )
        window_record.integrity_digest = rate_limit_window_record_digest(
            _window_material(window_record), self._integrity_key
        )
        attempt = ExecutionAttempt(
            id=command.attempt_id,
            run_id=command.run_id,
            step_id=command.step_id,
            operation_key=command.operation_key,
            policy_hash=operation.policy_hash,
            kind=operation.kind,
            attempt_number=attempt_number,
            source_control_version=command.expected_control_version,
            source_step_version=command.expected_step_version,
            eligible_at=eligible_at,
            reserved_at=command.reserved_at,
            call_deadline_at=call_deadline,
            outcome=None,
            completed_at=None,
            retry_not_before=None,
            terminal_reason_code=None,
            version=1,
        )
        attempt_record = ExecutionAttemptRecord(
            id=attempt.id,
            run_id=attempt.run_id,
            step_id=attempt.step_id,
            operation_key=attempt.operation_key,
            policy_hash=attempt.policy_hash,
            kind=attempt.kind.value,
            attempt_number=attempt.attempt_number,
            source_control_version=attempt.source_control_version,
            source_step_version=attempt.source_step_version,
            eligible_at=attempt.eligible_at,
            reserved_at=attempt.reserved_at,
            call_deadline_at=attempt.call_deadline_at,
            rate_limit_scope=operation.rate_limit_scope.value,
            rate_limit_key=operation.rate_limit_key,
            rate_window_started_at=window_start,
            outcome=None,
            completed_at=None,
            retry_not_before=None,
            terminal_reason_code=None,
            version=1,
            integrity_digest="",
        )
        attempt_record.integrity_digest = execution_attempt_record_digest(
            _attempt_material(attempt_record), self._integrity_key
        )
        try:
            async with self._session.begin_nested():
                control_fences = [
                    RunExecutionControlRecord.run_id == command.run_id,
                    RunExecutionControlRecord.version == command.expected_control_version,
                    RunExecutionControlRecord.integrity_digest == current_control_digest,
                    RunExecutionControlRecord.cancel_requested_at.is_(None),
                    RunExecutionControlRecord.deadline_at > command.reserved_at,
                ]
                if consumes_logical_budget:
                    control_fences.append(
                        RunExecutionControlRecord.model_calls
                        < RunExecutionControlRecord.max_model_calls
                        if operation.kind is AttemptKind.MODEL
                        else RunExecutionControlRecord.tool_calls
                        < RunExecutionControlRecord.max_tool_calls
                    )
                updated_control_id = await self._session.scalar(
                    update(RunExecutionControlRecord)
                    .where(*control_fences)
                    .values(
                        model_calls=next_control.model_calls,
                        tool_calls=next_control.tool_calls,
                        updated_at=next_control.updated_at,
                        version=next_control.version,
                        integrity_digest=next_control_digest,
                    )
                    .returning(RunExecutionControlRecord.run_id)
                )
                if updated_control_id is None:
                    raise ExecutionControlPersistenceConflict(
                        "stale_execution_control",
                        "attempt reservation lost its budget or version fence",
                    )
                if current_window is None:
                    self._session.add(window_record)
                    await self._session.flush()
                else:
                    assert current_window_record is not None
                    updated_window_key = await self._session.scalar(
                        update(RateLimitWindowRecord)
                        .where(
                            RateLimitWindowRecord.scope == current_window.scope.value,
                            RateLimitWindowRecord.key == current_window.key,
                            RateLimitWindowRecord.started_at == current_window.started_at,
                            RateLimitWindowRecord.version == current_window.version,
                            RateLimitWindowRecord.used == current_window.used,
                            RateLimitWindowRecord.integrity_digest
                            == current_window_record.integrity_digest,
                            RateLimitWindowRecord.used < RateLimitWindowRecord.capacity,
                        )
                        .values(
                            used=next_window.used,
                            version=next_window.version,
                            updated_at=next_window.updated_at,
                            integrity_digest=window_record.integrity_digest,
                        )
                        .returning(RateLimitWindowRecord.key)
                    )
                    if updated_window_key is None:
                        raise ExecutionControlPersistenceConflict(
                            "rate_limit_conflict", "rate-window capacity was consumed concurrently"
                        )
                self._session.add(attempt_record)
                await self._session.flush()
        except ExecutionControlPersistenceConflict:
            raise
        except IntegrityError as exc:
            persisted = await self.get_attempt(command.attempt_id)
            if persisted is not None:
                return await self._reservation_replay(command, persisted)
            raise ExecutionControlPersistenceConflict(
                "attempt_reservation_conflict",
                "attempt reservation lost its atomic uniqueness fence",
            ) from exc
        except OperationalError as exc:
            code = (
                "attempt_reservation_conflict"
                if _is_sqlite_busy(self._session, exc)
                else "execution_storage_error"
            )
            raise ExecutionControlPersistenceConflict(
                code, "attempt reservation could not be committed atomically"
            ) from exc
        return AttemptReservationResult(
            control=next_control, attempt=attempt, rate_window=next_window
        )

    async def reserve_delivery_call(
        self,
        command: DeliveryCallReservationCommand,
    ) -> DeliveryCallReservationResult:
        """Reserve one physical WRITE delivery without granting template retry authority."""

        if type(command) is not DeliveryCallReservationCommand:
            raise TypeError("delivery reservation requires an exact command")
        control = await self.get(command.run_id)
        if control is None:
            raise ExecutionControlPersistenceConflict(
                "execution_control_missing", "Run execution control is not initialized"
            )
        if control.cancel_requested_at is not None:
            raise ExecutionControlPersistenceConflict("run_cancelled", "Run is cancellation-fenced")
        if control.started_at is None or control.deadline_at is None:
            raise ExecutionControlPersistenceConflict(
                "execution_not_started", "Run execution deadline has not started"
            )
        if command.reserved_at >= control.deadline_at:
            raise ExecutionControlPersistenceConflict(
                "deadline_exceeded", "Run execution deadline has expired"
            )
        if control.version != command.expected_control_version:
            raise ExecutionControlPersistenceConflict(
                "stale_execution_control", "execution-control version is stale"
            )
        if command.reserved_at < control.updated_at:
            raise ExecutionControlPersistenceConflict(
                "delivery_time_invalid", "delivery reservation time is stale"
            )
        run = await self._session.get(RunRecord, command.run_id)
        if run is None or run.state != RunState.EXECUTING.value:
            raise ExecutionControlPersistenceConflict(
                "run_not_executing", "delivery reservation requires an executing Run"
            )
        try:
            step = await SQLAlchemyRunStepRepository(self._session).get(command.step_id)
        except StepPersistenceConflict as exc:
            raise ExecutionControlPersistenceConflict(
                "execution_policy_source_corrupt",
                "delivery step policy cannot be trusted",
            ) from exc
        action = (
            await self._session.execute(
                select(ExternalActionRecord)
                .where(ExternalActionRecord.id == command.action_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            step is None
            or step.run_id != command.run_id
            or step.version != command.expected_step_version
            or step.effect is not Effect.WRITE
            or step.state not in {StepState.READY, StepState.EXECUTING}
            or step.plan_hash != control.policy_hash
            or step.runtime_policy.attempt_kind is not AttemptKind.TOOL
            or step.runtime_policy.retry.max_attempts != 1
            or step.runtime_policy.retry.backoff is not RetryBackoff.NONE
        ):
            raise ExecutionControlPersistenceConflict(
                "delivery_step_fence_invalid",
                "delivery permit requires the exact runnable WRITE step policy",
            )
        if (
            action is None
            or action.run_id != command.run_id
            or action.step_id != command.step_id
            or action.plan_hash != control.policy_hash
            or action.version != command.expected_action_version
            or action.state != "dispatching"
            or action.delivery_attempt_count != command.delivery_attempt_number
            or action.dispatch_attempt_number != command.delivery_attempt_number
            or action.connector_call_started_at is not None
            or action.connector_call_deadline_at is not None
        ):
            raise ExecutionControlPersistenceConflict(
                "delivery_action_fence_invalid",
                "delivery permit requires the exact unstarted dispatch attempt",
            )

        prior_started = (
            await self._session.execute(
                select(ExternalActionDispatchAttemptRecord.external_action_id)
                .where(
                    ExternalActionDispatchAttemptRecord.external_action_id == command.action_id,
                    ExternalActionDispatchAttemptRecord.attempt_number
                    < command.delivery_attempt_number,
                    ExternalActionDispatchAttemptRecord.call_started_at.is_not(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        consumes_logical_budget = prior_started is None
        if consumes_logical_budget and control.tool_calls >= control.max_tool_calls:
            raise ExecutionControlPersistenceConflict(
                "tool_budget_exhausted", "Run tool-call budget is exhausted"
            )

        runtime_policy = step.runtime_policy
        rate_policy = runtime_policy.rate_limit
        window_start = fixed_window_start(command.reserved_at, rate_policy.window_seconds)
        window_end = window_start + timedelta(seconds=rate_policy.window_seconds)
        current_window_record = await self._get_window_record(
            rate_policy.scope,
            rate_policy.key,
            window_start,
        )
        current_window = (
            None
            if current_window_record is None
            else self._window_from_record(current_window_record)
        )
        if current_window is not None and (
            current_window.capacity != rate_policy.max_calls or current_window.ends_at != window_end
        ):
            _raise_corrupt("delivery rate window differs from the immutable step policy")
        if current_window is not None and current_window.used >= current_window.capacity:
            raise ExecutionControlPersistenceConflict(
                "rate_limit_exhausted",
                "fixed delivery rate window is exhausted",
                retry_after_seconds=_retry_after_seconds(window_end, command.reserved_at),
            )
        call_deadline = min(
            command.reserved_at + timedelta(seconds=runtime_policy.timeout.step_seconds),
            command.reserved_at + timedelta(seconds=action.timeout_seconds),
            control.deadline_at,
        )
        if call_deadline <= command.reserved_at:
            raise ExecutionControlPersistenceConflict(
                "deadline_exceeded", "no positive delivery call window remains"
            )

        next_control = RunExecutionControl(
            run_id=control.run_id,
            policy_hash=control.policy_hash,
            run_timeout_seconds=control.run_timeout_seconds,
            max_model_calls=control.max_model_calls,
            max_tool_calls=control.max_tool_calls,
            model_calls=control.model_calls,
            tool_calls=control.tool_calls + int(consumes_logical_budget),
            started_at=control.started_at,
            deadline_at=control.deadline_at,
            cancel_requested_at=None,
            cancel_actor_digest=None,
            created_at=control.created_at,
            updated_at=command.reserved_at,
            version=control.version + 1,
        )
        next_control_record = self._control_record(next_control)
        next_control_digest = execution_control_record_digest(
            _control_material(next_control_record), self._integrity_key
        )
        next_window = RateLimitWindow(
            scope=rate_policy.scope,
            key=rate_policy.key,
            started_at=window_start,
            ends_at=window_end,
            capacity=rate_policy.max_calls,
            used=1 if current_window is None else current_window.used + 1,
            version=1 if current_window is None else current_window.version + 1,
            updated_at=command.reserved_at,
        )
        window_record = RateLimitWindowRecord(
            scope=next_window.scope.value,
            key=next_window.key,
            started_at=next_window.started_at,
            ends_at=next_window.ends_at,
            capacity=next_window.capacity,
            used=next_window.used,
            version=next_window.version,
            updated_at=next_window.updated_at,
            integrity_digest="",
        )
        window_record.integrity_digest = rate_limit_window_record_digest(
            _window_material(window_record), self._integrity_key
        )
        current_control_digest = execution_control_record_digest(
            _control_material(self._control_record(control)), self._integrity_key
        )
        predicates: list[Any] = [
            RunExecutionControlRecord.run_id == command.run_id,
            RunExecutionControlRecord.version == command.expected_control_version,
            RunExecutionControlRecord.integrity_digest == current_control_digest,
            RunExecutionControlRecord.cancel_requested_at.is_(None),
            RunExecutionControlRecord.deadline_at > command.reserved_at,
        ]
        if consumes_logical_budget:
            predicates.append(
                RunExecutionControlRecord.tool_calls < RunExecutionControlRecord.max_tool_calls
            )
        try:
            async with self._session.begin_nested():
                updated_control_id = await self._session.scalar(
                    update(RunExecutionControlRecord)
                    .where(*predicates)
                    .values(
                        tool_calls=next_control.tool_calls,
                        updated_at=next_control.updated_at,
                        version=next_control.version,
                        integrity_digest=next_control_digest,
                    )
                    .returning(RunExecutionControlRecord.run_id)
                )
                if updated_control_id is None:
                    raise ExecutionControlPersistenceConflict(
                        "delivery_reservation_conflict",
                        "delivery reservation lost its cancellation or budget fence",
                    )
                if current_window is None:
                    self._session.add(window_record)
                    await self._session.flush()
                else:
                    assert current_window_record is not None
                    updated_window_key = await self._session.scalar(
                        update(RateLimitWindowRecord)
                        .where(
                            RateLimitWindowRecord.scope == current_window.scope.value,
                            RateLimitWindowRecord.key == current_window.key,
                            RateLimitWindowRecord.started_at == current_window.started_at,
                            RateLimitWindowRecord.version == current_window.version,
                            RateLimitWindowRecord.used == current_window.used,
                            RateLimitWindowRecord.integrity_digest
                            == current_window_record.integrity_digest,
                            RateLimitWindowRecord.used < RateLimitWindowRecord.capacity,
                        )
                        .values(
                            used=next_window.used,
                            version=next_window.version,
                            updated_at=next_window.updated_at,
                            integrity_digest=window_record.integrity_digest,
                        )
                        .returning(RateLimitWindowRecord.key)
                    )
                    if updated_window_key is None:
                        raise ExecutionControlPersistenceConflict(
                            "rate_limit_conflict",
                            "delivery rate-window capacity was consumed concurrently",
                        )
        except ExecutionControlPersistenceConflict:
            raise
        except IntegrityError as exc:
            raise ExecutionControlPersistenceConflict(
                "delivery_reservation_conflict",
                "delivery reservation lost its atomic uniqueness fence",
            ) from exc
        except OperationalError as exc:
            code = (
                "delivery_reservation_conflict"
                if _is_sqlite_busy(self._session, exc)
                else "execution_storage_error"
            )
            raise ExecutionControlPersistenceConflict(
                code, "delivery reservation could not be committed atomically"
            ) from exc

        permit = DeliveryCallPermit(
            run_id=command.run_id,
            step_id=command.step_id,
            action_id=command.action_id,
            delivery_attempt_number=command.delivery_attempt_number,
            policy_hash=control.policy_hash,
            source_control_version=command.expected_control_version,
            source_step_version=command.expected_step_version,
            source_action_version=command.expected_action_version,
            reserved_at=command.reserved_at,
            call_deadline_at=call_deadline,
            rate_limit_scope=rate_policy.scope,
            rate_limit_key=rate_policy.key,
            rate_window_started_at=window_start,
            logical_budget_consumed=consumes_logical_budget,
        )
        return DeliveryCallReservationResult(next_control, permit, next_window)

    async def complete_attempt(self, command: AttemptCompletionCommand) -> AttemptCompletionResult:
        if type(command) is not AttemptCompletionCommand:
            raise TypeError("attempt completion requires an exact command")
        attempt = await self.get_attempt(command.attempt_id)
        if attempt is None:
            raise ExecutionControlPersistenceConflict(
                "attempt_missing", "attempt completion requires a persisted reservation"
            )
        control = await self.get(attempt.run_id)
        if control is None or control.deadline_at is None:
            _raise_corrupt("attempt completion has no exact Run control")
        run_record = await self._session.get(RunRecord, attempt.run_id)
        if run_record is None:
            _raise_corrupt("attempt completion has no exact parent Run")
        if attempt.outcome is not None:
            replay_outcome_matches = attempt.outcome is command.outcome or (
                command.outcome is AttemptOutcome.TRANSIENT_FAILURE
                and attempt.outcome is AttemptOutcome.CANCELLED
                and attempt.terminal_reason_code == "run_cancelled"
            )
            if (
                replay_outcome_matches
                and attempt.completed_at == command.completed_at
                and control.version >= command.expected_control_version + 1
            ):
                return AttemptCompletionResult(attempt=attempt, completed=False)
            raise ExecutionControlPersistenceConflict(
                "attempt_completion_conflict", "attempt already has a different completion"
            )
        if run_record.state not in {
            RunState.EXECUTING.value,
            RunState.CANCELLED.value,
            RunState.FAILED.value,
            RunState.REJECTED.value,
        }:
            _raise_corrupt("open attempt belongs to a non-executable parent Run state")
        if control.version != command.expected_control_version:
            raise ExecutionControlPersistenceConflict(
                "stale_execution_control",
                "attempt completion lost its execution-control version fence",
            )
        if command.completed_at < max(
            attempt.reserved_at,
            control.updated_at,
            run_record.updated_at,
        ):
            raise ExecutionControlPersistenceConflict(
                "attempt_completion_time_invalid",
                "attempt completion predates its reservation or Run control",
            )
        cancellation_fenced = (
            control.cancel_requested_at is not None or run_record.state == RunState.CANCELLED.value
        )
        terminal_parent = run_record.state in {
            RunState.FAILED.value,
            RunState.REJECTED.value,
        }
        effective_outcome = command.outcome
        if command.outcome is AttemptOutcome.TRANSIENT_FAILURE:
            if cancellation_fenced:
                effective_outcome = AttemptOutcome.CANCELLED
            elif terminal_parent:
                effective_outcome = AttemptOutcome.PERMANENT_FAILURE
        current_record = (
            await self._session.execute(
                select(ExecutionAttemptRecord)
                .where(ExecutionAttemptRecord.id == attempt.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        current_digest = execution_attempt_record_digest(
            _attempt_material(current_record), self._integrity_key
        )
        _check_digest(current_record.integrity_digest, current_digest)
        operation = await self.get_operation(attempt.step_id, attempt.operation_key)
        if operation is None:
            _raise_corrupt("attempt completion has no exact operation")
        retry_not_before: datetime | None = None
        terminal_reason: str | None = None
        if effective_outcome is AttemptOutcome.TRANSIENT_FAILURE:
            if attempt.attempt_number >= operation.max_attempts:
                terminal_reason = "attempts_exhausted"
            else:
                retry_not_before = command.completed_at + timedelta(
                    seconds=bounded_retry_delay_seconds(
                        operation.retry_backoff, attempt.attempt_number + 1
                    )
                )
                if retry_not_before >= control.deadline_at:
                    retry_not_before = None
                    terminal_reason = "retry_deadline_exceeded"
        elif effective_outcome is AttemptOutcome.PERMANENT_FAILURE:
            terminal_reason = "permanent_failure"
        elif effective_outcome is AttemptOutcome.CANCELLED:
            terminal_reason = "run_cancelled" if cancellation_fenced else "cancelled"
        completed = ExecutionAttempt(
            id=attempt.id,
            run_id=attempt.run_id,
            step_id=attempt.step_id,
            operation_key=attempt.operation_key,
            policy_hash=attempt.policy_hash,
            kind=attempt.kind,
            attempt_number=attempt.attempt_number,
            source_control_version=attempt.source_control_version,
            source_step_version=attempt.source_step_version,
            eligible_at=attempt.eligible_at,
            reserved_at=attempt.reserved_at,
            call_deadline_at=attempt.call_deadline_at,
            outcome=effective_outcome,
            completed_at=command.completed_at,
            retry_not_before=retry_not_before,
            terminal_reason_code=terminal_reason,
            version=2,
        )
        record = ExecutionAttemptRecord(
            id=completed.id,
            run_id=completed.run_id,
            step_id=completed.step_id,
            operation_key=completed.operation_key,
            policy_hash=completed.policy_hash,
            kind=completed.kind.value,
            attempt_number=completed.attempt_number,
            source_control_version=completed.source_control_version,
            source_step_version=completed.source_step_version,
            eligible_at=completed.eligible_at,
            reserved_at=completed.reserved_at,
            call_deadline_at=completed.call_deadline_at,
            rate_limit_scope=operation.rate_limit_scope.value,
            rate_limit_key=operation.rate_limit_key,
            rate_window_started_at=fixed_window_start(
                completed.reserved_at, operation.rate_window_seconds
            ),
            outcome=effective_outcome.value,
            completed_at=completed.completed_at,
            retry_not_before=completed.retry_not_before,
            terminal_reason_code=completed.terminal_reason_code,
            version=completed.version,
            integrity_digest="",
        )
        digest = execution_attempt_record_digest(_attempt_material(record), self._integrity_key)
        next_control = RunExecutionControl(
            run_id=control.run_id,
            policy_hash=control.policy_hash,
            run_timeout_seconds=control.run_timeout_seconds,
            max_model_calls=control.max_model_calls,
            max_tool_calls=control.max_tool_calls,
            model_calls=control.model_calls,
            tool_calls=control.tool_calls,
            started_at=control.started_at,
            deadline_at=control.deadline_at,
            cancel_requested_at=control.cancel_requested_at,
            cancel_actor_digest=control.cancel_actor_digest,
            created_at=control.created_at,
            updated_at=command.completed_at,
            version=control.version + 1,
        )
        next_control_record = self._control_record(next_control)
        next_control_digest = execution_control_record_digest(
            _control_material(next_control_record), self._integrity_key
        )
        current_control_digest = execution_control_record_digest(
            _control_material(self._control_record(control)), self._integrity_key
        )
        try:
            async with self._session.begin_nested():
                parent_run_id = await self._session.scalar(
                    update(RunRecord)
                    .where(
                        RunRecord.id == run_record.id,
                        RunRecord.version == run_record.version,
                        RunRecord.state == run_record.state,
                    )
                    .values(version=RunRecord.version)
                    .returning(RunRecord.id)
                )
                if parent_run_id is None:
                    raise ExecutionControlPersistenceConflict(
                        "stale_parent_run",
                        "attempt completion lost its atomic parent Run fence",
                    )
                updated_control_id = await self._session.scalar(
                    update(RunExecutionControlRecord)
                    .where(
                        RunExecutionControlRecord.run_id == control.run_id,
                        RunExecutionControlRecord.version == command.expected_control_version,
                        RunExecutionControlRecord.integrity_digest == current_control_digest,
                    )
                    .values(
                        updated_at=next_control.updated_at,
                        version=next_control.version,
                        integrity_digest=next_control_digest,
                    )
                    .returning(RunExecutionControlRecord.run_id)
                )
                if updated_control_id is None:
                    raise ExecutionControlPersistenceConflict(
                        "stale_execution_control",
                        "attempt completion lost its atomic Run control fence",
                    )
                updated_attempt_id = await self._session.scalar(
                    update(ExecutionAttemptRecord)
                    .where(
                        ExecutionAttemptRecord.id == attempt.id,
                        ExecutionAttemptRecord.version == 1,
                        ExecutionAttemptRecord.outcome.is_(None),
                        ExecutionAttemptRecord.call_deadline_at == attempt.call_deadline_at,
                        ExecutionAttemptRecord.integrity_digest == current_digest,
                    )
                    .values(
                        outcome=record.outcome,
                        completed_at=record.completed_at,
                        retry_not_before=record.retry_not_before,
                        terminal_reason_code=record.terminal_reason_code,
                        version=2,
                        integrity_digest=digest,
                    )
                    .returning(ExecutionAttemptRecord.id)
                )
                if updated_attempt_id is None:
                    raise ExecutionControlPersistenceConflict(
                        "attempt_completion_conflict",
                        "attempt completion lost its atomic attempt fence",
                    )
        except ExecutionControlPersistenceConflict:
            raise
        except IntegrityError as exc:
            raise ExecutionControlPersistenceConflict(
                "attempt_completion_conflict",
                "attempt completion lost its atomic persistence fence",
            ) from exc
        except OperationalError as exc:
            code = (
                "stale_execution_control"
                if _is_sqlite_busy(self._session, exc)
                else "execution_storage_error"
            )
            raise ExecutionControlPersistenceConflict(
                code,
                "attempt completion could not be persisted atomically",
            ) from exc
        return AttemptCompletionResult(attempt=completed, completed=True)

    async def recover_expired_attempt(
        self,
        command: ExpiredAttemptRecoveryCommand,
    ) -> AttemptCompletionResult:
        """Close one expired crash-orphaned attempt without caller-owned outcome authority."""

        if type(command) is not ExpiredAttemptRecoveryCommand:
            raise TypeError("attempt recovery requires an exact command")
        attempt = await self.get_attempt(command.attempt_id)
        if attempt is None:
            raise ExecutionControlPersistenceConflict(
                "attempt_missing", "attempt recovery requires a persisted reservation"
            )
        if attempt.call_deadline_at != command.expected_call_deadline_at:
            raise ExecutionControlPersistenceConflict(
                "attempt_completion_conflict",
                "attempt recovery deadline differs from the persisted reservation",
            )
        operation = await self.get_operation(attempt.step_id, attempt.operation_key)
        control = await self.get(attempt.run_id)
        run_record = await self._session.get(RunRecord, attempt.run_id)
        if (
            operation is None
            or control is None
            or control.deadline_at is None
            or run_record is None
        ):
            _raise_corrupt("attempt recovery has no exact operation or Run control")
        expected_deadline = min(
            attempt.reserved_at + timedelta(seconds=operation.step_timeout_seconds),
            control.deadline_at,
        )
        if expected_deadline != attempt.call_deadline_at:
            _raise_corrupt("attempt recovery deadline differs from immutable policy")
        terminal_parent = run_record.state in {
            RunState.FAILED.value,
            RunState.REJECTED.value,
        }
        cancelled_during_attempt = (
            control.cancel_requested_at is not None or run_record.state == RunState.CANCELLED.value
        )
        recovery_outcome = (
            AttemptOutcome.CANCELLED
            if cancelled_during_attempt
            else (
                AttemptOutcome.PERMANENT_FAILURE
                if terminal_parent
                else AttemptOutcome.TRANSIENT_FAILURE
            )
        )
        if attempt.outcome is not None:
            if (
                attempt.version == 2
                and attempt.completed_at is not None
                and attempt.completed_at >= attempt.call_deadline_at
                and attempt.outcome is recovery_outcome
            ):
                return AttemptCompletionResult(attempt=attempt, completed=False)
            raise ExecutionControlPersistenceConflict(
                "attempt_completion_conflict",
                "attempt recovery lost to a different authoritative completion",
            )
        if run_record.state == RunState.COMPLETED.value:
            _raise_corrupt("completed Run cannot retain an open READ attempt")
        if attempt.version != command.expected_attempt_version:
            raise ExecutionControlPersistenceConflict(
                "attempt_completion_conflict",
                "attempt recovery lost its open-version fence",
            )
        if command.recovered_at < attempt.call_deadline_at:
            raise ExecutionControlPersistenceConflict(
                "attempt_in_progress",
                "attempt retains bounded call authority",
                retry_after_seconds=_retry_after_seconds(
                    attempt.call_deadline_at,
                    command.recovered_at,
                ),
            )
        for retry_index in range(3):
            current_control = await self.get(attempt.run_id)
            if current_control is None:
                _raise_corrupt("attempt recovery lost its exact Run control")
            current_outcome = (
                AttemptOutcome.CANCELLED
                if (
                    current_control.cancel_requested_at is not None
                    or run_record.state == RunState.CANCELLED.value
                )
                else (
                    AttemptOutcome.PERMANENT_FAILURE
                    if terminal_parent
                    else AttemptOutcome.TRANSIENT_FAILURE
                )
            )
            try:
                return await self.complete_attempt(
                    AttemptCompletionCommand(
                        attempt_id=attempt.id,
                        outcome=current_outcome,
                        expected_control_version=current_control.version,
                        completed_at=command.recovered_at,
                    )
                )
            except ExecutionControlPersistenceConflict as exc:
                if (
                    exc.code
                    not in {
                        "stale_execution_control",
                        "stale_parent_run",
                    }
                    or retry_index == 2
                ):
                    raise
        raise AssertionError("bounded attempt recovery retry exhausted without an outcome")
