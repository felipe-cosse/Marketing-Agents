"""SQLAlchemy plan/step repository with exact snapshot replay and CAS history."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from marketing_agents.application.ports.repositories import RunStepPlanInsertResult
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import (
    RunPlanRoutingAssignment,
    RunPlanSelectedInstance,
    RunPlanSnapshot,
    RunStep,
)
from marketing_agents.domain.enums import Effect, RunState, StepState
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.plan_hash import EffectPlanStepHashMaterial, effect_plan_hash
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    BudgetPolicySnapshot,
    RateLimitPolicySnapshot,
    RateLimitScope,
    RetryBackoff,
    RetryPolicySnapshot,
    RunRuntimePolicy,
    StepRuntimeDemand,
    StepRuntimePolicy,
    TimeoutPolicySnapshot,
    run_policy_projection,
    step_policy_projection,
    validate_runtime_plan_budget,
)
from marketing_agents.domain.step_lifecycle import (
    StepLifecycleCommand,
    StepStateTransition,
    StepTransitionResult,
    initial_pending_transition,
)
from marketing_agents.infrastructure.db.models.run import RunRecord
from marketing_agents.infrastructure.db.models.step import (
    RunPlanRecord,
    RunPlanRoutingAssignmentRecord,
    RunPlanSelectedInstanceRecord,
    RunStepDependencyRecord,
    RunStepRecord,
    RunStepStateTransitionRecord,
)


class StepPersistenceConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_sqlite_busy(session: AsyncSession, exc: OperationalError) -> bool:
    code = getattr(exc.orig, "sqlite_errorcode", None)
    return session.get_bind().dialect.name == "sqlite" and code in {
        sqlite3.SQLITE_BUSY,
        getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517),
    }


def _plan_to_record(plan: RunPlanSnapshot) -> RunPlanRecord:
    return RunPlanRecord(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        workflow_id=plan.workflow_id,
        workflow_version=plan.workflow_version,
        workflow_definition_hash=plan.workflow_definition_hash,
        catalog_content_hash=plan.catalog_content_hash,
        graph_hash=plan.graph_hash,
        routing_hash=plan.routing_hash,
        approval_required=plan.approval_required,
        step_count=plan.step_count,
        runtime_policy_snapshot=run_policy_projection(plan.runtime_policy),
        runtime_policy_hash=plan.runtime_policy.semantic_hash,
        created_at=plan.created_at,
    )


def _runtime_json_object(value: object, *, expected_keys: frozenset[str]) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != expected_keys
        or any(type(key) is not str for key in value)
    ):
        raise StepPersistenceConflict(
            "runtime_policy_snapshot_corrupt",
            "persisted runtime policy is not the exact versioned JSON projection",
        )
    return value


def _run_policy_from_record(record: RunPlanRecord) -> RunRuntimePolicy:
    value = _runtime_json_object(
        record.runtime_policy_snapshot,
        expected_keys=frozenset(
            {"max_steps", "max_model_calls", "max_tool_calls", "run_timeout_seconds"}
        ),
    )
    policy = RunRuntimePolicy(
        max_steps=value["max_steps"],  # type: ignore[arg-type]
        max_model_calls=value["max_model_calls"],  # type: ignore[arg-type]
        max_tool_calls=value["max_tool_calls"],  # type: ignore[arg-type]
        run_timeout_seconds=value["run_timeout_seconds"],  # type: ignore[arg-type]
    )
    if policy.semantic_hash != record.runtime_policy_hash:
        raise StepPersistenceConflict(
            "runtime_policy_snapshot_corrupt",
            "persisted Run runtime policy hash does not match its projection",
        )
    return policy


def _plan_to_domain(record: RunPlanRecord) -> RunPlanSnapshot:
    return RunPlanSnapshot(
        run_id=record.run_id,
        plan_hash=record.plan_hash,
        workflow_id=record.workflow_id,
        workflow_version=record.workflow_version,
        workflow_definition_hash=record.workflow_definition_hash,
        catalog_content_hash=record.catalog_content_hash,
        graph_hash=record.graph_hash,
        routing_hash=record.routing_hash,
        approval_required=record.approval_required,
        step_count=record.step_count,
        runtime_policy=_run_policy_from_record(record),
        created_at=record.created_at,
    )


def _step_hash_material(step: RunStep) -> EffectPlanStepHashMaterial:
    return EffectPlanStepHashMaterial(
        step_key=step.key,
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
        request_redaction_fields=step.request_redaction_fields,
        result_redaction_fields=step.result_redaction_fields,
        data_classification=step.data_classification,
        idempotency_support=step.idempotency_support,
        connector_timeout_seconds=step.timeout_seconds,
        approval_policy_id=step.approval_policy_id,
        approval_required_roles=step.approval_required_roles,
        approval_required_scopes=step.approval_required_scopes,
        approval_expires_after_seconds=step.approval_expires_after_seconds,
        approval_allow_self_approval=step.approval_allow_self_approval,
        runtime_policy=step.runtime_policy,
    )


def _step_to_record(step: RunStep) -> RunStepRecord:
    return RunStepRecord(
        id=step.id,
        run_id=step.run_id,
        key=step.key,
        kind=step.kind,
        selected_instance_id=step.selected_instance_id,
        capability_id=step.capability_id,
        effect=step.effect.value,
        state=step.state.value,
        plan_hash=step.plan_hash,
        graph_hash=step.graph_hash,
        ordinal=step.ordinal,
        source_order=step.source_order,
        template_id=step.template_id,
        configuration_revision=step.configuration_revision,
        connector_family=step.connector_family,
        routing_slot_key=step.routing_slot_key,
        binding_id=step.binding_id,
        binding_configuration_revision=step.binding_configuration_revision,
        request_schema_id=step.request_schema_id,
        result_schema_id=step.result_schema_id,
        request_redaction_fields=list(step.request_redaction_fields),
        result_redaction_fields=list(step.result_redaction_fields),
        data_classification=step.data_classification.value,
        idempotency_support=step.idempotency_support,
        timeout_seconds=step.timeout_seconds,
        runtime_policy_snapshot=step_policy_projection(step.runtime_policy),
        runtime_policy_hash=step.runtime_policy.semantic_hash,
        approval_policy_id=step.approval_policy_id,
        approval_required_roles=list(step.approval_required_roles),
        approval_required_scopes=list(step.approval_required_scopes),
        approval_expires_after_seconds=step.approval_expires_after_seconds,
        approval_allow_self_approval=step.approval_allow_self_approval,
        terminal_result=step.terminal_result,
        created_at=step.created_at,
        updated_at=step.updated_at,
        version=step.version,
        terminal_reason_code=step.terminal_reason_code,
    )


def _json_id_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise StepPersistenceConflict(
            "step_json_snapshot_corrupt",
            f"persisted {field_name} is not an exact JSON identifier array",
        )
    return tuple(value)


def _step_policy_from_record(record: RunStepRecord) -> StepRuntimePolicy:
    value = _runtime_json_object(
        record.runtime_policy_snapshot,
        expected_keys=frozenset(
            {
                "operation_key",
                "attempt_kind",
                "max_attempts",
                "backoff",
                "step_timeout_seconds",
                "template_run_timeout_seconds",
                "max_steps",
                "max_model_calls",
                "max_tool_calls",
                "max_input_bytes",
                "max_input_field_bytes",
                "max_output_bytes",
                "max_model_output_tokens",
                "rate_limit_scope",
                "rate_limit_key",
                "rate_limit_max_calls",
                "rate_limit_window_seconds",
            }
        ),
    )
    policy = StepRuntimePolicy(
        operation_key=value["operation_key"],  # type: ignore[arg-type]
        attempt_kind=AttemptKind(value["attempt_kind"]),  # type: ignore[arg-type]
        retry=RetryPolicySnapshot(
            max_attempts=value["max_attempts"],  # type: ignore[arg-type]
            backoff=RetryBackoff(value["backoff"]),  # type: ignore[arg-type]
        ),
        timeout=TimeoutPolicySnapshot(
            step_seconds=value["step_timeout_seconds"],  # type: ignore[arg-type]
            run_seconds=value["template_run_timeout_seconds"],  # type: ignore[arg-type]
        ),
        budget=BudgetPolicySnapshot(
            max_steps=value["max_steps"],  # type: ignore[arg-type]
            max_model_calls=value["max_model_calls"],  # type: ignore[arg-type]
            max_tool_calls=value["max_tool_calls"],  # type: ignore[arg-type]
            max_input_bytes=value["max_input_bytes"],  # type: ignore[arg-type]
            max_input_field_bytes=value["max_input_field_bytes"],  # type: ignore[arg-type]
            max_output_bytes=value["max_output_bytes"],  # type: ignore[arg-type]
            max_model_output_tokens=value["max_model_output_tokens"],  # type: ignore[arg-type]
        ),
        rate_limit=RateLimitPolicySnapshot(
            scope=RateLimitScope(value["rate_limit_scope"]),  # type: ignore[arg-type]
            key=value["rate_limit_key"],  # type: ignore[arg-type]
            max_calls=value["rate_limit_max_calls"],  # type: ignore[arg-type]
            window_seconds=value["rate_limit_window_seconds"],  # type: ignore[arg-type]
        ),
    )
    if policy.semantic_hash != record.runtime_policy_hash:
        raise StepPersistenceConflict(
            "runtime_policy_snapshot_corrupt",
            "persisted step runtime policy hash does not match its projection",
        )
    return policy


def _step_to_domain_unchecked(record: RunStepRecord, dependencies: tuple[str, ...]) -> RunStep:
    return RunStep(
        id=record.id,
        run_id=record.run_id,
        key=record.key,
        kind=record.kind,
        selected_instance_id=record.selected_instance_id,
        dependency_keys=dependencies,
        capability_id=record.capability_id,
        effect=Effect(record.effect),
        state=StepState(record.state),
        plan_hash=record.plan_hash,
        graph_hash=record.graph_hash,
        ordinal=record.ordinal,
        source_order=record.source_order,
        template_id=record.template_id,
        configuration_revision=record.configuration_revision,
        connector_family=record.connector_family,
        routing_slot_key=record.routing_slot_key,
        binding_id=record.binding_id,
        binding_configuration_revision=record.binding_configuration_revision,
        request_schema_id=record.request_schema_id,
        result_schema_id=record.result_schema_id,
        request_redaction_fields=_json_id_tuple(
            record.request_redaction_fields,
            "request redaction fields",
        ),
        result_redaction_fields=_json_id_tuple(
            record.result_redaction_fields,
            "result redaction fields",
        ),
        data_classification=DataClassification(record.data_classification),
        idempotency_support=record.idempotency_support,
        timeout_seconds=record.timeout_seconds,
        runtime_policy=_step_policy_from_record(record),
        approval_policy_id=record.approval_policy_id,
        approval_required_roles=_json_id_tuple(
            record.approval_required_roles,
            "approval roles",
        ),
        approval_required_scopes=_json_id_tuple(
            record.approval_required_scopes,
            "approval scopes",
        ),
        approval_expires_after_seconds=record.approval_expires_after_seconds,
        approval_allow_self_approval=record.approval_allow_self_approval,
        terminal_result=record.terminal_result,
        created_at=record.created_at,
        updated_at=record.updated_at,
        version=record.version,
        terminal_reason_code=record.terminal_reason_code,
    )


def _step_to_domain(record: RunStepRecord, dependencies: tuple[str, ...]) -> RunStep:
    try:
        return _step_to_domain_unchecked(record, dependencies)
    except StepPersistenceConflict:
        raise
    except (TypeError, ValueError) as exc:
        raise StepPersistenceConflict(
            "plan_snapshot_corrupt",
            "persisted Run step violates its immutable plan snapshot",
        ) from exc


def _transition_to_record(transition: StepStateTransition) -> RunStepStateTransitionRecord:
    return RunStepStateTransitionRecord(
        step_id=transition.step_id,
        run_id=transition.run_id,
        sequence=transition.sequence,
        command=transition.command.value,
        previous_state=(
            None if transition.previous_state is None else transition.previous_state.value
        ),
        new_state=transition.new_state.value,
        reason_code=transition.reason_code,
        occurred_at=transition.occurred_at,
        expected_version=transition.expected_version,
        resulting_version=transition.resulting_version,
    )


def _transition_to_domain(record: RunStepStateTransitionRecord) -> StepStateTransition:
    return StepStateTransition(
        step_id=record.step_id,
        run_id=record.run_id,
        sequence=record.sequence,
        command=StepLifecycleCommand(record.command),
        previous_state=(
            None if record.previous_state is None else StepState(record.previous_state)
        ),
        new_state=StepState(record.new_state),
        reason_code=record.reason_code,
        occurred_at=record.occurred_at,
        expected_version=record.expected_version,
        resulting_version=record.resulting_version,
    )


class SQLAlchemyRunStepRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _dependencies(self, step_id: str) -> tuple[str, ...]:
        statement = (
            select(RunStepDependencyRecord.dependency_key)
            .where(RunStepDependencyRecord.step_id == step_id)
            .order_by(RunStepDependencyRecord.dependency_key)
        )
        return tuple((await self._session.execute(statement)).scalars())

    async def get(self, step_id: str) -> RunStep | None:
        record = await self._session.get(RunStepRecord, step_id)
        if record is None:
            return None
        try:
            return _step_to_domain(record, await self._dependencies(step_id))
        except (TypeError, ValueError) as exc:
            raise StepPersistenceConflict(
                "plan_snapshot_corrupt",
                "persisted Run step is invalid",
            ) from exc

    async def get_plan(self, run_id: str) -> RunPlanSnapshot | None:
        record = await self._session.get(RunPlanRecord, run_id)
        if record is None:
            return None
        try:
            return _plan_to_domain(record)
        except (TypeError, ValueError) as exc:
            raise StepPersistenceConflict(
                "plan_snapshot_corrupt",
                "persisted run plan snapshot is invalid",
            ) from exc

    async def _selected_instances(self, run_id: str) -> tuple[RunPlanSelectedInstance, ...]:
        statement = (
            select(RunPlanSelectedInstanceRecord)
            .where(RunPlanSelectedInstanceRecord.run_id == run_id)
            .order_by(RunPlanSelectedInstanceRecord.selection_order)
        )
        rows = (await self._session.execute(statement)).scalars()
        try:
            return tuple(
                RunPlanSelectedInstance(
                    run_id=row.run_id,
                    plan_hash=row.plan_hash,
                    instance_id=row.instance_id,
                    template_id=row.template_id,
                    configuration_revision=row.configuration_revision,
                    display_order=row.display_order,
                    source_ordinal=row.source_ordinal,
                    selection_order=row.selection_order,
                    target=row.target,
                )
                for row in rows
            )
        except (TypeError, ValueError) as exc:
            raise StepPersistenceConflict(
                "plan_snapshot_corrupt",
                "persisted selected-instance snapshot is invalid",
            ) from exc

    async def _assignments(self, run_id: str) -> tuple[RunPlanRoutingAssignment, ...]:
        statement = (
            select(RunPlanRoutingAssignmentRecord)
            .where(RunPlanRoutingAssignmentRecord.run_id == run_id)
            .order_by(RunPlanRoutingAssignmentRecord.assignment_order)
        )
        rows = (await self._session.execute(statement)).scalars()
        try:
            return tuple(
                RunPlanRoutingAssignment(
                    run_id=row.run_id,
                    plan_hash=row.plan_hash,
                    slot_key=row.slot_key,
                    instance_id=row.instance_id,
                    template_id=row.template_id,
                    required_capability_ids=_json_id_tuple(
                        row.required_capability_ids,
                        "routing required capabilities",
                    ),
                    assignment_order=row.assignment_order,
                )
                for row in rows
            )
        except StepPersistenceConflict:
            raise
        except (TypeError, ValueError) as exc:
            raise StepPersistenceConflict(
                "plan_snapshot_corrupt",
                "persisted routing assignment snapshot is invalid",
            ) from exc

    @staticmethod
    def _validate_plan_set(
        plan: RunPlanSnapshot,
        selected_instances: tuple[RunPlanSelectedInstance, ...],
        assignments: tuple[RunPlanRoutingAssignment, ...],
        steps: tuple[RunStep, ...],
        initial_transitions: tuple[StepStateTransition, ...],
    ) -> None:
        if type(plan) is not RunPlanSnapshot:
            raise ValueError("persisted plan must use the exact immutable snapshot")
        for values, expected_type, name in (
            (selected_instances, RunPlanSelectedInstance, "selected instances"),
            (assignments, RunPlanRoutingAssignment, "routing assignments"),
            (steps, RunStep, "run steps"),
            (initial_transitions, StepStateTransition, "initial step transitions"),
        ):
            if type(values) is not tuple or any(type(item) is not expected_type for item in values):
                raise ValueError(f"{name} must use exact immutable tuple contracts")
        if not selected_instances or not steps:
            raise ValueError("persisted plan must retain selection and steps")
        if plan.step_count != len(steps) or len(steps) != len(initial_transitions):
            raise ValueError("persisted plan step counts do not align")
        if tuple(step.ordinal for step in steps) != tuple(range(1, len(steps) + 1)):
            raise ValueError("persisted steps must use contiguous deterministic ordinals")
        selected_by_id = {item.instance_id: item for item in selected_instances}
        if len(selected_by_id) != len(selected_instances):
            raise ValueError("selected plan instance IDs must be unique")
        if sum(item.target for item in selected_instances) != 1:
            raise ValueError("persisted plan must retain exactly one routing target")
        if tuple(item.selection_order for item in selected_instances) != tuple(
            range(1, len(selected_instances) + 1)
        ):
            raise ValueError("selected instance order must be contiguous")
        if tuple(item.assignment_order for item in assignments) != tuple(
            range(1, len(assignments) + 1)
        ):
            raise ValueError("routing assignment order must be contiguous")
        step_keys = {step.key for step in steps}
        if len(step_keys) != len(steps):
            raise ValueError("persisted step keys must be unique")
        if (
            any(
                item.run_id != plan.run_id or item.plan_hash != plan.plan_hash
                for item in selected_instances
            )
            or any(
                item.run_id != plan.run_id or item.plan_hash != plan.plan_hash
                for item in assignments
            )
            or any(item.run_id != plan.run_id or item.plan_hash != plan.plan_hash for item in steps)
        ):
            raise ValueError("persisted plan members must share run and plan identity")
        if any(step.graph_hash != plan.graph_hash for step in steps):
            raise ValueError("persisted steps must share the exact plan graph hash")
        if any(
            plan.runtime_policy.run_timeout_seconds > step.runtime_policy.timeout.run_seconds
            for step in steps
        ):
            raise ValueError("persisted Run timeout exceeds a selected template timeout")
        validate_runtime_plan_budget(
            plan.runtime_policy,
            tuple(
                StepRuntimeDemand(
                    template_id=step.template_id,
                    connector_family=step.connector_family,
                    policy=step.runtime_policy,
                )
                for step in steps
            ),
        )
        if (
            effect_plan_hash(
                workflow_id=plan.workflow_id,
                workflow_version=plan.workflow_version,
                workflow_definition_hash=plan.workflow_definition_hash,
                catalog_content_hash=plan.catalog_content_hash,
                graph_hash=plan.graph_hash,
                routing_hash=plan.routing_hash,
                run_policy=plan.runtime_policy,
                steps=tuple(_step_hash_material(step) for step in steps),
            )
            != plan.plan_hash
        ):
            raise ValueError("persisted steps do not reproduce the structural plan hash")
        if plan.approval_required != any(step.effect is Effect.WRITE for step in steps):
            raise ValueError("persisted plan approval disposition must derive from step effects")
        ordinal_by_key = {step.key: step.ordinal for step in steps}
        for step in steps:
            if any(key not in ordinal_by_key for key in step.dependency_keys):
                raise ValueError("persisted step references an unknown dependency")
            if any(ordinal_by_key[key] >= step.ordinal for key in step.dependency_keys):
                raise ValueError("persisted dependencies must follow the topological step order")
        rebuilt_graph = DependencyGraph.build(
            tuple(
                TopologyStep(
                    key=step.key,
                    source_order=step.source_order,
                    dependency_keys=step.dependency_keys,
                    terminal_result=step.terminal_result,
                )
                for step in steps
            ),
            workflow_max_steps=20,
            global_max_steps=20,
        )
        if rebuilt_graph.semantic_hash != plan.graph_hash:
            raise ValueError("persisted steps do not reproduce the plan graph hash")
        if tuple(step.key for step in steps) != rebuilt_graph.topological_order:
            raise ValueError("persisted step ordinals do not match deterministic topology")
        assignment_by_slot = {item.slot_key: item for item in assignments}
        if len(assignment_by_slot) != len(assignments):
            raise ValueError("routing assignment slot keys must be unique")
        consumed_slots = {
            step.routing_slot_key for step in steps if step.routing_slot_key is not None
        }
        if set(assignment_by_slot) != consumed_slots:
            raise ValueError("routing assignments must be consumed by the persisted steps")
        target = next(item for item in selected_instances if item.target)
        expected_selected_ids = {
            target.instance_id,
            *(assignment.instance_id for assignment in assignments),
            *(step.selected_instance_id for step in steps),
        }
        if set(selected_by_id) != expected_selected_ids:
            raise ValueError("persisted selection contains an unused or missing instance")
        for assignment in assignments:
            selected = selected_by_id.get(assignment.instance_id)
            if selected is None or selected.template_id != assignment.template_id:
                raise ValueError("routing assignment is outside its selected instance snapshot")
        for step, transition in zip(steps, initial_transitions, strict=True):
            selected = selected_by_id.get(step.selected_instance_id)
            step_assignment = (
                None
                if step.routing_slot_key is None
                else assignment_by_slot.get(step.routing_slot_key)
            )
            if (
                selected is None
                or selected.template_id != step.template_id
                or selected.configuration_revision != step.configuration_revision
                or transition != initial_pending_transition(step)
                or any(key not in step_keys for key in step.dependency_keys)
                or (
                    step.routing_slot_key is None
                    and step.selected_instance_id != target.instance_id
                )
                or (
                    step.routing_slot_key is not None
                    and (
                        step_assignment is None
                        or step_assignment.instance_id != step.selected_instance_id
                        or step_assignment.template_id != step.template_id
                        or (
                            step_assignment.required_capability_ids
                            and step.capability_id not in step_assignment.required_capability_ids
                        )
                    )
                )
            ):
                raise ValueError("persisted step is outside its plan, route, or initial history")

    @staticmethod
    def _plan_projection(plan: RunPlanSnapshot) -> tuple[object, ...]:
        return (
            plan.run_id,
            plan.plan_hash,
            plan.workflow_id,
            plan.workflow_version,
            plan.workflow_definition_hash,
            plan.catalog_content_hash,
            plan.graph_hash,
            plan.routing_hash,
            plan.approval_required,
            plan.step_count,
            plan.runtime_policy,
        )

    @staticmethod
    def _step_projection(step: RunStep) -> tuple[object, ...]:
        return (
            step.run_id,
            step.key,
            step.kind,
            step.selected_instance_id,
            step.dependency_keys,
            step.capability_id,
            step.effect,
            step.plan_hash,
            step.graph_hash,
            step.ordinal,
            step.source_order,
            step.template_id,
            step.configuration_revision,
            step.connector_family,
            step.routing_slot_key,
            step.binding_id,
            step.binding_configuration_revision,
            step.request_schema_id,
            step.result_schema_id,
            step.request_redaction_fields,
            step.result_redaction_fields,
            step.data_classification,
            step.idempotency_support,
            step.timeout_seconds,
            step.runtime_policy,
            step.approval_policy_id,
            step.approval_required_roles,
            step.approval_required_scopes,
            step.approval_expires_after_seconds,
            step.approval_allow_self_approval,
            step.terminal_result,
        )

    async def _validate_stored_history(self, step: RunStep) -> None:
        history = await self.list_transitions(step.id)
        initial = replace(
            step,
            state=StepState.PENDING,
            updated_at=step.created_at,
            version=1,
            terminal_reason_code=None,
        )
        if not history or history[0] != initial_pending_transition(initial):
            raise StepPersistenceConflict(
                "initial_step_history_missing",
                "stored plan step lacks its exact sequence-one transition",
            )
        previous_state = history[0].new_state
        for expected_sequence, transition in enumerate(history[1:], start=2):
            if (
                transition.sequence != expected_sequence
                or transition.previous_state is not previous_state
            ):
                raise StepPersistenceConflict(
                    "step_history_not_contiguous",
                    "stored plan step history is not contiguous",
                )
            previous_state = transition.new_state
        if (
            history[-1].new_state is not step.state
            or history[-1].resulting_version != step.version
            or history[-1].occurred_at != step.updated_at
            or (
                step.terminal_reason_code is not None
                and step.terminal_reason_code != history[-1].reason_code
            )
        ):
            raise StepPersistenceConflict(
                "step_history_state_mismatch",
                "stored plan step does not match the end of its transition history",
            )

    async def add_plan(
        self,
        plan: RunPlanSnapshot,
        selected_instances: tuple[RunPlanSelectedInstance, ...],
        assignments: tuple[RunPlanRoutingAssignment, ...],
        steps: tuple[RunStep, ...],
        initial_transitions: tuple[StepStateTransition, ...],
    ) -> RunStepPlanInsertResult:
        self._validate_plan_set(plan, selected_instances, assignments, steps, initial_transitions)
        try:
            async with self._session.begin_nested():
                self._session.add(_plan_to_record(plan))
                self._session.add_all(
                    RunPlanSelectedInstanceRecord(
                        run_id=item.run_id,
                        plan_hash=item.plan_hash,
                        instance_id=item.instance_id,
                        template_id=item.template_id,
                        configuration_revision=item.configuration_revision,
                        display_order=item.display_order,
                        source_ordinal=item.source_ordinal,
                        selection_order=item.selection_order,
                        target=item.target,
                    )
                    for item in selected_instances
                )
                self._session.add_all(_step_to_record(step) for step in steps)
                await self._session.flush()
                self._session.add_all(
                    RunPlanRoutingAssignmentRecord(
                        run_id=item.run_id,
                        plan_hash=item.plan_hash,
                        slot_key=item.slot_key,
                        instance_id=item.instance_id,
                        template_id=item.template_id,
                        required_capability_ids=list(item.required_capability_ids),
                        assignment_order=item.assignment_order,
                    )
                    for item in assignments
                )
                self._session.add_all(
                    RunStepDependencyRecord(
                        step_id=step.id,
                        run_id=step.run_id,
                        step_key=step.key,
                        dependency_key=dependency_key,
                    )
                    for step in steps
                    for dependency_key in step.dependency_keys
                )
                self._session.add_all(
                    _transition_to_record(transition) for transition in initial_transitions
                )
                await self._session.flush()
        except IntegrityError:
            existing_plan = await self.get_plan(plan.run_id)
            existing_steps = await self.list_for_run(plan.run_id)
            candidate_by_key = {step.key: step for step in steps}
            semantic_steps_match = len(existing_steps) == len(steps) and all(
                self._step_projection(stored)
                == self._step_projection(candidate_by_key.get(stored.key, stored))
                and stored.key in candidate_by_key
                for stored in existing_steps
            )
            if (
                existing_plan is not None
                and self._plan_projection(existing_plan) == self._plan_projection(plan)
                and semantic_steps_match
                and await self._selected_instances(plan.run_id) == selected_instances
                and await self._assignments(plan.run_id) == assignments
            ):
                for stored in existing_steps:
                    await self._validate_stored_history(stored)
                return RunStepPlanInsertResult(existing_plan, existing_steps, inserted=False)
            raise StepPersistenceConflict(
                "plan_snapshot_conflict",
                "run plan identity already maps to a different complete snapshot",
            ) from None
        return RunStepPlanInsertResult(plan, steps, inserted=True)

    async def list_for_run(self, run_id: str) -> tuple[RunStep, ...]:
        statement = (
            select(RunStepRecord)
            .where(RunStepRecord.run_id == run_id)
            .order_by(RunStepRecord.ordinal)
        )
        rows = tuple((await self._session.execute(statement)).scalars())
        hydrated: list[RunStep] = []
        try:
            for row in rows:
                hydrated.append(_step_to_domain(row, await self._dependencies(row.id)))
        except (TypeError, ValueError) as exc:
            raise StepPersistenceConflict(
                "plan_snapshot_corrupt",
                "persisted Run step set is invalid",
            ) from exc
        return tuple(hydrated)

    async def validate_plan_for_execution(self, run_id: str) -> tuple[RunStep, ...]:
        plan = await self.get_plan(run_id)
        if plan is None:
            raise StepPersistenceConflict(
                "plan_snapshot_missing",
                "Run has no persisted execution plan",
            )
        steps = await self.list_for_run(run_id)
        initial_steps = tuple(
            replace(
                step,
                state=StepState.PENDING,
                updated_at=step.created_at,
                version=1,
                terminal_reason_code=None,
            )
            for step in steps
        )
        initial_transitions = tuple(initial_pending_transition(step) for step in initial_steps)
        try:
            self._validate_plan_set(
                plan,
                await self._selected_instances(run_id),
                await self._assignments(run_id),
                initial_steps,
                initial_transitions,
            )
        except StepPersistenceConflict:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise StepPersistenceConflict(
                "plan_snapshot_corrupt",
                "persisted execution plan violates its sealed topology",
            ) from exc
        for step in steps:
            await self._validate_stored_history(step)
        return steps

    async def apply_transition(
        self,
        *,
        expected_run_version: int,
        expected_run_state: RunState,
        expected_version: int,
        expected_state: StepState,
        result: StepTransitionResult,
    ) -> bool:
        transition = result.transition
        if (
            transition.expected_version != expected_version
            or transition.previous_state is not expected_state
        ):
            raise ValueError("step transition result does not match its CAS predicate")
        parent_fence = (
            update(RunRecord)
            .where(
                RunRecord.id == result.step.run_id,
                RunRecord.version == expected_run_version,
                RunRecord.state == expected_run_state.value,
            )
            .values(version=RunRecord.version)
            .returning(RunRecord.id)
            .execution_options(synchronize_session=False)
        )
        statement = (
            update(RunStepRecord)
            .where(
                RunStepRecord.id == result.step.id,
                RunStepRecord.version == expected_version,
                RunStepRecord.state == expected_state.value,
            )
            .values(
                state=result.step.state.value,
                updated_at=result.step.updated_at,
                version=result.step.version,
                terminal_reason_code=result.step.terminal_reason_code,
            )
            .returning(RunStepRecord.id)
            .execution_options(synchronize_session=False)
        )
        try:
            parent = (await self._session.execute(parent_fence)).scalar_one_or_none()
            if parent is None:
                return False
            updated = (await self._session.execute(statement)).scalar_one_or_none()
        except OperationalError as exc:
            if _is_sqlite_busy(self._session, exc):
                return False
            raise
        if updated is None:
            return False
        self._session.add(_transition_to_record(transition))
        await self._session.flush()
        return True

    async def list_transitions(self, step_id: str) -> tuple[StepStateTransition, ...]:
        statement = (
            select(RunStepStateTransitionRecord)
            .where(RunStepStateTransitionRecord.step_id == step_id)
            .order_by(RunStepStateTransitionRecord.sequence)
        )
        rows = (await self._session.execute(statement)).scalars()
        try:
            return tuple(_transition_to_domain(row) for row in rows)
        except (TypeError, ValueError) as exc:
            raise StepPersistenceConflict(
                "step_history_corrupt",
                "persisted step transition history is invalid",
            ) from exc
