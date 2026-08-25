"""Atomic persistence of one deterministic plan, topology, steps, and audit witnesses."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.orchestration.effect_planner import EffectPlan
from marketing_agents.application.orchestration.router import RoutingResult
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.entities import (
    Run,
    RunPlanRoutingAssignment,
    RunPlanSelectedInstance,
    RunPlanSnapshot,
    RunStep,
)
from marketing_agents.domain.enums import RunState, StepState
from marketing_agents.domain.execution_control import (
    OperationExecutionPolicy,
    RunExecutionPolicy,
)
from marketing_agents.domain.graph import DependencyGraph
from marketing_agents.domain.run_lifecycle import (
    NoRunTransitionContext,
    RunLifecycleCommand,
    RunStateTransition,
    transition_run,
)
from marketing_agents.domain.runtime_policy import AttemptKind, effective_call_timeout_seconds
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepStateTransition,
    initial_pending_transition,
    transition_step,
)

from .approval_records import ApprovalRecordService
from .audit_events import AuditEventFactory
from .external_action_registration import ExternalActionRegistrationDisposition


class PlanPersistenceError(RuntimeError):
    def __init__(self, code: str, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class PersistedRunPlan:
    run: Run
    plan: RunPlanSnapshot
    steps: tuple[RunStep, ...]
    created: bool


class AuditedPlanPersistenceService:
    """Persist the complete immutable plan set and Run mutation in one UoW."""

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies

    async def persist(
        self,
        effect_plan: EffectPlan,
        graph: DependencyGraph,
        routing: RoutingResult,
        *,
        expected_run_version: int,
        audit_context: AuditContext,
    ) -> PersistedRunPlan:
        if (
            not isinstance(expected_run_version, int)
            or isinstance(expected_run_version, bool)
            or expected_run_version < 1
        ):
            raise ValueError("expected Run version must be positive")
        async with self._dependencies.unit_of_work() as unit_of_work:
            current = await unit_of_work.runs.get(effect_plan.run_id)
            if current is None:
                raise PlanPersistenceError(
                    "run_not_found", "plan Run does not exist", run_id=effect_plan.run_id
                )
            work_item = await unit_of_work.works.get(current.work_item_id)
            _validate_admission_scope(current, work_item, effect_plan, routing)
            existing_plan = await unit_of_work.run_steps.get_plan(current.id)
            if existing_plan is not None:
                materialized = _materialize_plan(
                    effect_plan,
                    graph,
                    routing,
                    created_at=existing_plan.created_at,
                )
                try:
                    stored = await unit_of_work.run_steps.add_plan(*materialized)
                except RuntimeError as exc:
                    raise PlanPersistenceError(
                        getattr(exc, "code", "plan_snapshot_conflict"),
                        "persisted Run plan conflicts with its authoritative snapshot",
                        run_id=current.id,
                    ) from exc
                await _initialize_execution_control(
                    unit_of_work,
                    stored.plan,
                    stored.steps,
                    expect_inserted=False,
                )
                await _require_plan_replay(
                    unit_of_work,
                    current,
                    stored.plan,
                    stored.steps,
                )
                if effect_plan.lifecycle_context.contains_write_actions:
                    registered = await ApprovalRecordService(
                        self._dependencies
                    ).register_plan_in_uow(
                        unit_of_work,
                        effect_plan,
                        audit_context=audit_context,
                    )
                    if (
                        registered.actions.disposition
                        is not ExternalActionRegistrationDisposition.REPLAYED
                    ):
                        raise PlanPersistenceError(
                            "write_plan_approval_epoch_missing",
                            "existing write plan cannot recreate a missing approval epoch",
                            run_id=current.id,
                        )
                    await _require_write_activation_replay(
                        unit_of_work,
                        current,
                        stored.plan,
                        stored.steps,
                    )
                    if current.state not in {
                        RunState.AWAITING_APPROVAL,
                        RunState.EXECUTING,
                        RunState.COMPLETED,
                        RunState.REJECTED,
                        RunState.CANCELLED,
                        RunState.FAILED,
                    }:
                        raise PlanPersistenceError(
                            "write_plan_activation_missing",
                            "persisted write plan lacks its atomic approval-boundary activation",
                            run_id=current.id,
                        )
                await unit_of_work.commit()
                return PersistedRunPlan(current, stored.plan, stored.steps, created=False)
            if current.state is not RunState.VALIDATED:
                raise PlanPersistenceError(
                    "run_not_validated",
                    "only a validated Run may persist a plan",
                    run_id=current.id,
                )
            if current.version != expected_run_version:
                raise PlanPersistenceError(
                    "stale_run_version",
                    "Run changed before its deterministic plan was persisted",
                    run_id=current.id,
                )

            occurred_at = self._dependencies.utc_now()
            transition_result = transition_run(
                current,
                RunLifecycleCommand.RECORD_PLAN,
                effect_plan.lifecycle_context,
                occurred_at,
            )
            materialized = _materialize_plan(
                effect_plan,
                graph,
                routing,
                created_at=occurred_at,
            )
            applied = await unit_of_work.runs.apply_transition(
                expected_version=current.version,
                expected_state=current.state,
                result=transition_result,
            )
            if not applied:
                raise PlanPersistenceError(
                    "stale_run_version",
                    "Run changed before the complete plan transaction committed",
                    run_id=current.id,
                )
            try:
                stored = await unit_of_work.run_steps.add_plan(*materialized)
            except RuntimeError as exc:
                raise PlanPersistenceError(
                    getattr(exc, "code", "plan_snapshot_conflict"),
                    "Run plan persistence failed its exact snapshot checks",
                    run_id=current.id,
                ) from exc
            if not stored.inserted:
                raise PlanPersistenceError(
                    "concurrent_plan_conflict",
                    "another transaction persisted the Run plan first",
                    run_id=current.id,
                )
            await _initialize_execution_control(
                unit_of_work,
                stored.plan,
                stored.steps,
                expect_inserted=True,
            )
            factory = AuditEventFactory(audit_context)
            plan_event = factory.run_plan_recorded(
                transition_result.run,
                transition_result.transition,
                stored.plan,
            )
            step_events = tuple(
                factory.step_recorded(step, transition, stored.plan)
                for step, transition in zip(
                    stored.steps,
                    materialized[4],
                    strict=True,
                )
            )
            await unit_of_work.audits.append_many((plan_event, *step_events))
            final_run = transition_result.run
            final_steps = stored.steps
            if effect_plan.lifecycle_context.contains_write_actions:
                await ApprovalRecordService(self._dependencies).register_plan_in_uow(
                    unit_of_work,
                    effect_plan,
                    audit_context=audit_context,
                )
                activated_at = self._dependencies.utc_now()
                step_results = tuple(
                    transition_step(
                        step,
                        StepLifecycleCommand.WAIT_FOR_APPROVAL,
                        NoStepTransitionContext(),
                        activated_at,
                    )
                    for step in stored.steps
                    if step.effect.value == "write"
                )
                if not step_results:
                    raise PlanPersistenceError(
                        "write_plan_step_missing",
                        "write plan activation lacks a persisted write step",
                        run_id=current.id,
                    )
                updated_by_id = {result.step.id: result.step for result in step_results}
                for result in step_results:
                    applied_step = await unit_of_work.run_steps.apply_transition(
                        expected_run_version=transition_result.run.version,
                        expected_run_state=transition_result.run.state,
                        expected_version=result.transition.expected_version,
                        expected_state=StepState.PENDING,
                        result=result,
                    )
                    if not applied_step:
                        raise PlanPersistenceError(
                            "write_plan_activation_conflict",
                            "write step changed before approval-boundary activation",
                            run_id=current.id,
                        )
                activation = transition_run(
                    transition_result.run,
                    RunLifecycleCommand.ACTIVATE_PLAN,
                    NoRunTransitionContext(),
                    activated_at,
                )
                applied_run = await unit_of_work.runs.apply_transition(
                    expected_version=transition_result.run.version,
                    expected_state=transition_result.run.state,
                    result=activation,
                )
                if not applied_run:
                    raise PlanPersistenceError(
                        "write_plan_activation_conflict",
                        "Run changed before approval-boundary activation",
                        run_id=current.id,
                    )
                await unit_of_work.audits.append_many(
                    (
                        *(
                            factory.step_transition(result.step, result.transition)
                            for result in step_results
                        ),
                        factory.run_transition(activation.run, activation.transition),
                    )
                )
                final_run = activation.run
                final_steps = tuple(updated_by_id.get(step.id, step) for step in stored.steps)
            await unit_of_work.commit()
            return PersistedRunPlan(
                final_run,
                stored.plan,
                final_steps,
                created=True,
            )


def _materialize_plan(
    effect_plan: EffectPlan,
    graph: DependencyGraph,
    routing: RoutingResult,
    *,
    created_at: datetime,
) -> tuple[
    RunPlanSnapshot,
    tuple[RunPlanSelectedInstance, ...],
    tuple[RunPlanRoutingAssignment, ...],
    tuple[RunStep, ...],
    tuple[StepStateTransition, ...],
]:
    if type(effect_plan) is not EffectPlan or type(graph) is not DependencyGraph:
        raise ValueError("plan persistence requires exact immutable planning contracts")
    if type(routing) is not RoutingResult:
        raise ValueError("plan persistence requires the exact immutable routing result")
    if (
        effect_plan.graph_hash != graph.semantic_hash
        or effect_plan.routing_hash != routing.semantic_hash
        or effect_plan.workflow_id != routing.workflow_id
        or effect_plan.workflow_version != routing.workflow_version
        or effect_plan.catalog_content_hash != routing.catalog_content_hash
        or tuple(step.step_key for step in effect_plan.steps) != graph.topological_order
        or len(effect_plan.steps) > 20
    ):
        raise ValueError("effect plan, route, and dependency graph snapshots disagree")
    selected_ids = {item.instance_id for item in routing.selected_instances}
    required_selected_ids = {
        routing.target_instance_id,
        *(item.instance_id for item in routing.assignments),
        *(item.selected_instance_id for item in effect_plan.steps),
    }
    if selected_ids != required_selected_ids:
        raise ValueError("routing snapshot contains a missing or surplus selected instance")

    plan = RunPlanSnapshot(
        run_id=effect_plan.run_id,
        plan_hash=effect_plan.plan_hash,
        workflow_id=effect_plan.workflow_id,
        workflow_version=effect_plan.workflow_version,
        workflow_definition_hash=effect_plan.workflow_definition_hash,
        catalog_content_hash=effect_plan.catalog_content_hash,
        graph_hash=effect_plan.graph_hash,
        routing_hash=effect_plan.routing_hash,
        approval_required=effect_plan.lifecycle_context.contains_write_actions,
        step_count=len(effect_plan.steps),
        runtime_policy=effect_plan.run_policy,
        created_at=created_at,
    )
    selected = tuple(
        RunPlanSelectedInstance(
            run_id=effect_plan.run_id,
            plan_hash=effect_plan.plan_hash,
            instance_id=item.instance_id,
            template_id=item.template_id,
            configuration_revision=item.configuration_revision,
            display_order=item.display_order,
            source_ordinal=item.source_ordinal,
            selection_order=index,
            target=item.instance_id == routing.target_instance_id,
        )
        for index, item in enumerate(routing.selected_instances, start=1)
    )
    assignments = tuple(
        RunPlanRoutingAssignment(
            run_id=effect_plan.run_id,
            plan_hash=effect_plan.plan_hash,
            slot_key=item.slot_key,
            instance_id=item.instance_id,
            template_id=item.template_id,
            required_capability_ids=item.required_capability_ids,
            assignment_order=index,
        )
        for index, item in enumerate(routing.assignments, start=1)
    )
    topology_by_key = {step.key: step for step in graph.steps}
    selected_by_id = {item.instance_id: item for item in selected}
    steps: list[RunStep] = []
    for ordinal, planned in enumerate(effect_plan.steps, start=1):
        topology = topology_by_key[planned.step_key]
        selected_instance = selected_by_id.get(planned.selected_instance_id)
        if (
            selected_instance is None
            or selected_instance.template_id != planned.template_id
            or selected_instance.configuration_revision != planned.configuration_revision
        ):
            raise ValueError("planned step is outside the exact selected instance snapshot")
        steps.append(
            RunStep(
                id=planned.runtime_step_id,
                run_id=effect_plan.run_id,
                key=planned.step_key,
                kind=planned.kind,
                selected_instance_id=planned.selected_instance_id,
                dependency_keys=topology.dependency_keys,
                capability_id=planned.capability_id,
                effect=planned.effect,
                state=StepState.PENDING,
                plan_hash=effect_plan.plan_hash,
                graph_hash=effect_plan.graph_hash,
                ordinal=ordinal,
                source_order=topology.source_order,
                template_id=planned.template_id,
                configuration_revision=planned.configuration_revision,
                connector_family=planned.connector_family,
                routing_slot_key=planned.routing_slot_key,
                binding_id=planned.binding_id,
                binding_configuration_revision=planned.binding_configuration_revision,
                request_schema_id=planned.request_schema_id,
                result_schema_id=planned.result_schema_id,
                result_schema_hash=planned.result_schema_hash,
                request_redaction_fields=planned.request_redaction_fields,
                result_redaction_fields=planned.result_redaction_fields,
                data_classification=planned.data_classification,
                idempotency_support=planned.idempotency_support,
                timeout_seconds=planned.connector_timeout_seconds,
                runtime_policy=planned.runtime_policy,
                approval_policy_id=planned.approval_policy_id,
                approval_required_roles=planned.approval_required_roles,
                approval_required_scopes=planned.approval_required_scopes,
                approval_expires_after_seconds=planned.approval_expires_after_seconds,
                approval_allow_self_approval=planned.approval_allow_self_approval,
                terminal_result=topology.terminal_result,
                created_at=created_at,
                updated_at=created_at,
            )
        )
    step_tuple = tuple(steps)
    transitions = tuple(initial_pending_transition(step) for step in step_tuple)
    return plan, selected, assignments, step_tuple, transitions


def _execution_policy_for_plan(
    plan: RunPlanSnapshot,
    steps: tuple[RunStep, ...],
) -> RunExecutionPolicy:
    """Derive the only executable READ-operation policy from sealed plan snapshots."""

    operations = tuple(
        OperationExecutionPolicy(
            run_id=plan.run_id,
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
            result_schema_hash=_required_result_schema_hash(step),
            request_redaction_fields=step.request_redaction_fields,
            result_redaction_fields=step.result_redaction_fields,
            data_classification=step.data_classification,
            connector_timeout_seconds=step.timeout_seconds,
            policy_hash=plan.plan_hash,
            max_attempts=step.runtime_policy.retry.max_attempts,
            retry_backoff=step.runtime_policy.retry.backoff,
            step_timeout_seconds=effective_call_timeout_seconds(
                step.runtime_policy,
                step.timeout_seconds,
            ),
            max_input_bytes=step.runtime_policy.budget.max_input_bytes,
            max_input_field_bytes=step.runtime_policy.budget.max_input_field_bytes,
            max_output_bytes=step.runtime_policy.budget.max_output_bytes,
            max_model_output_tokens=step.runtime_policy.budget.max_model_output_tokens,
            rate_limit_scope=step.runtime_policy.rate_limit.scope,
            rate_limit_key=step.runtime_policy.rate_limit.key,
            rate_window_max_calls=step.runtime_policy.rate_limit.max_calls,
            rate_window_seconds=step.runtime_policy.rate_limit.window_seconds,
        )
        for step in steps
        if step.effect.value == "read"
        and step.runtime_policy.attempt_kind in {AttemptKind.MODEL, AttemptKind.TOOL}
    )
    return RunExecutionPolicy(
        run_id=plan.run_id,
        policy_hash=plan.plan_hash,
        run_timeout_seconds=plan.runtime_policy.run_timeout_seconds,
        max_model_calls=plan.runtime_policy.max_model_calls,
        max_tool_calls=plan.runtime_policy.max_tool_calls,
        operations=operations,
        created_at=plan.created_at,
    )


def _required_result_schema_hash(step: RunStep) -> str:
    if step.result_schema_hash is None:
        raise ValueError("callable READ step requires a sealed result schema hash")
    return step.result_schema_hash


async def _initialize_execution_control(
    unit_of_work: UnitOfWork,
    plan: RunPlanSnapshot,
    steps: tuple[RunStep, ...],
    *,
    expect_inserted: bool,
) -> None:
    try:
        result = await unit_of_work.execution_control.initialize(
            _execution_policy_for_plan(plan, steps)
        )
    except RuntimeError as exc:
        raise PlanPersistenceError(
            getattr(exc, "code", "execution_policy_conflict"),
            "Run execution policy could not be installed with its sealed plan",
            run_id=plan.run_id,
        ) from exc
    if result.inserted is not expect_inserted:
        raise PlanPersistenceError(
            "execution_policy_replay_mismatch",
            "execution control creation does not match the plan persistence disposition",
            run_id=plan.run_id,
        )


def _validate_admission_scope(
    run: Run,
    work_item: object,
    effect_plan: EffectPlan,
    routing: RoutingResult,
) -> None:
    from marketing_agents.domain.entities import WorkItem

    if type(work_item) is not WorkItem:
        raise PlanPersistenceError(
            "work_item_missing",
            "plan Run lacks its authoritative admitted WorkItem",
            run_id=run.id,
        )
    normalized_catalog = (
        run.catalog_hash
        if run.catalog_hash.startswith("catalog-sha256-v1:")
        else "catalog-sha256-v1:" + run.catalog_hash
    )
    target = next(
        (
            item
            for item in routing.selected_instances
            if item.instance_id == routing.target_instance_id
        ),
        None,
    )
    if (
        work_item.id != run.work_item_id
        or effect_plan.run_id != run.id
        or effect_plan.workflow_id != work_item.workflow_id
        or routing.target_instance_id != work_item.instance_id
        or target is None
        or target.configuration_revision != work_item.configuration_revision
        or run.configuration_revision != work_item.configuration_revision
        or normalized_catalog != effect_plan.catalog_content_hash
        or normalized_catalog != routing.catalog_content_hash
    ):
        raise PlanPersistenceError(
            "plan_admission_scope_mismatch",
            "plan, route, Run, and admitted WorkItem snapshots disagree",
            run_id=run.id,
        )


async def _require_plan_replay(
    unit_of_work: UnitOfWork,
    run: Run,
    plan: RunPlanSnapshot,
    steps: tuple[RunStep, ...],
) -> None:
    normalized_catalog = (
        run.catalog_hash
        if run.catalog_hash.startswith("catalog-sha256-v1:")
        else "catalog-sha256-v1:" + run.catalog_hash
    )
    if (
        run.id != plan.run_id
        or normalized_catalog != plan.catalog_content_hash
        or run.approval_required != plan.approval_required
    ):
        raise PlanPersistenceError(
            "run_plan_snapshot_mismatch",
            "persisted plan no longer binds its authoritative Run snapshot",
            run_id=run.id,
        )
    history = await unit_of_work.runs.list_transitions(run.id)
    plan_transitions = tuple(
        transition
        for transition in history
        if transition.command is RunLifecycleCommand.RECORD_PLAN
    )
    if len(plan_transitions) != 1:
        raise PlanPersistenceError(
            "plan_transition_missing",
            "persisted plan lacks its one authoritative Run transition",
            run_id=run.id,
        )
    transition = plan_transitions[0]
    if plan.created_at != transition.occurred_at:
        raise PlanPersistenceError(
            "plan_transition_mismatch",
            "persisted plan timestamp differs from its Run transition",
            run_id=run.id,
        )
    plan_event = await unit_of_work.audits.get_mutation_event(
        "run", run.id, transition.resulting_version
    )
    _require_plan_event(plan_event, plan, transition)
    for step in steps:
        event = await unit_of_work.audits.get_mutation_event("step", step.id, 1)
        _require_step_event(event, plan, step)


def _require_plan_event(
    event: AuditEvent | None,
    plan: RunPlanSnapshot,
    transition: RunStateTransition,
) -> None:
    if event is None:
        raise PlanPersistenceError(
            "plan_audit_missing", "persisted plan lacks its Run audit witness", run_id=plan.run_id
        )
    draft = event.draft
    expected_metadata = {
        "command": "record_plan",
        "plan_hash": plan.plan_hash,
        "workflow_id": plan.workflow_id,
        "workflow_version": plan.workflow_version,
        "workflow_definition_hash": plan.workflow_definition_hash,
        "catalog_content_hash": plan.catalog_content_hash,
        "graph_hash": plan.graph_hash,
        "routing_hash": plan.routing_hash,
        "step_count": plan.step_count,
    }
    if (
        draft.event_type != "run.plan_recorded"
        or draft.run_id != plan.run_id
        or draft.aggregate_id != plan.run_id
        or draft.mutation_version != transition.resulting_version
        or draft.transition_sequence != transition.sequence
        or draft.previous_state != RunState.VALIDATED.value
        or draft.new_state != RunState.PLANNED.value
        or draft.reason_code != "plan_recorded"
        or draft.occurred_at != plan.created_at
        or dict(draft.safe_metadata.values) != expected_metadata
    ):
        raise PlanPersistenceError(
            "plan_audit_mismatch",
            "persisted plan Run audit witness is not authoritative",
            run_id=plan.run_id,
        )


def _require_step_event(
    event: AuditEvent | None,
    plan: RunPlanSnapshot,
    current_step: RunStep,
) -> None:
    if event is None:
        raise PlanPersistenceError(
            "step_audit_missing",
            "persisted plan step lacks its initial audit witness",
            run_id=plan.run_id,
        )
    initial = replace(
        current_step,
        state=StepState.PENDING,
        updated_at=current_step.created_at,
        version=1,
        terminal_reason_code=None,
    )
    draft = event.draft
    expected_metadata = {
        "plan_hash": plan.plan_hash,
        "workflow_id": plan.workflow_id,
        "workflow_version": plan.workflow_version,
        "workflow_definition_hash": plan.workflow_definition_hash,
        "catalog_content_hash": plan.catalog_content_hash,
        "graph_hash": plan.graph_hash,
        "routing_hash": plan.routing_hash,
        "step_count": plan.step_count,
        "ordinal": initial.ordinal,
        "step_kind": initial.kind,
        "template_id": initial.template_id,
        "configuration_revision": initial.configuration_revision,
        "terminal_result": initial.terminal_result,
    }
    if (
        draft.event_type != "step.recorded"
        or draft.run_id != plan.run_id
        or draft.step_id != initial.id
        or draft.aggregate_id != initial.id
        or draft.occurred_at != initial.created_at
        or dict(draft.safe_metadata.values) != expected_metadata
    ):
        raise PlanPersistenceError(
            "step_audit_mismatch",
            "persisted plan step audit witness is not authoritative",
            run_id=plan.run_id,
        )


async def _require_write_activation_replay(
    unit_of_work: UnitOfWork,
    run: Run,
    plan: RunPlanSnapshot,
    steps: tuple[RunStep, ...],
) -> None:
    """Reject partial/missing write activation; replay must never heal its witnesses."""

    history = await unit_of_work.runs.list_transitions(run.id)
    activations = tuple(
        transition
        for transition in history
        if transition.command is RunLifecycleCommand.ACTIVATE_PLAN
    )
    plan_records = tuple(
        transition
        for transition in history
        if transition.command is RunLifecycleCommand.RECORD_PLAN
    )
    if len(activations) != 1 or len(plan_records) != 1:
        raise PlanPersistenceError(
            "write_plan_activation_missing",
            "persisted write plan lacks one authoritative activation transition",
            run_id=run.id,
        )
    activation = activations[0]
    plan_record = plan_records[0]
    if (
        not plan.approval_required
        or activation.previous_state is not RunState.PLANNED
        or activation.new_state is not RunState.AWAITING_APPROVAL
        or activation.reason_code != "write_plan_requires_approval"
        or activation.expected_version != plan_record.resulting_version
        or activation.resulting_version != plan_record.resulting_version + 1
        or activation.occurred_at < plan.created_at
    ):
        raise PlanPersistenceError(
            "write_plan_activation_mismatch",
            "persisted write activation differs from its exact plan transition",
            run_id=run.id,
        )
    run_event = await unit_of_work.audits.get_mutation_event(
        "run",
        run.id,
        activation.resulting_version,
    )
    if run_event is None:
        raise PlanPersistenceError(
            "write_plan_activation_audit_missing",
            "persisted write activation lacks its Run audit witness",
            run_id=run.id,
        )
    run_draft = run_event.draft
    if (
        run_draft.event_type != "run.transitioned"
        or run_draft.run_id != run.id
        or run_draft.aggregate_id != run.id
        or run_draft.mutation_version != activation.resulting_version
        or run_draft.transition_sequence != activation.sequence
        or run_draft.previous_state != RunState.PLANNED.value
        or run_draft.new_state != RunState.AWAITING_APPROVAL.value
        or run_draft.reason_code != "write_plan_requires_approval"
        or run_draft.occurred_at != activation.occurred_at
        or dict(run_draft.safe_metadata.values) != {"command": "activate_plan"}
    ):
        raise PlanPersistenceError(
            "write_plan_activation_audit_mismatch",
            "persisted write activation Run audit is not authoritative",
            run_id=run.id,
        )
    write_steps = tuple(step for step in steps if step.effect.value == "write")
    if not write_steps:
        raise PlanPersistenceError(
            "write_plan_step_missing",
            "write plan lacks persisted write steps",
            run_id=run.id,
        )
    for step in write_steps:
        step_history = await unit_of_work.run_steps.list_transitions(step.id)
        if len(step_history) < 2:
            raise PlanPersistenceError(
                "write_step_activation_missing",
                "write step lacks its approval-wait transition",
                run_id=run.id,
            )
        waiting = step_history[1]
        if (
            waiting.sequence != 2
            or waiting.command is not StepLifecycleCommand.WAIT_FOR_APPROVAL
            or waiting.previous_state is not StepState.PENDING
            or waiting.new_state is not StepState.AWAITING_APPROVAL
            or waiting.reason_code != "step_approval_required"
            or waiting.occurred_at != activation.occurred_at
        ):
            raise PlanPersistenceError(
                "write_step_activation_mismatch",
                "write step approval-wait transition differs from atomic activation",
                run_id=run.id,
            )
        step_event = await unit_of_work.audits.get_mutation_event("step", step.id, 2)
        if step_event is None:
            raise PlanPersistenceError(
                "write_step_activation_audit_missing",
                "write step approval-wait transition lacks its audit witness",
                run_id=run.id,
            )
        step_draft = step_event.draft
        if (
            step_draft.event_type != "step.transitioned"
            or step_draft.run_id != run.id
            or step_draft.step_id != step.id
            or step_draft.aggregate_id != step.id
            or step_draft.mutation_version != 2
            or step_draft.transition_sequence != 2
            or step_draft.previous_state != StepState.PENDING.value
            or step_draft.new_state != StepState.AWAITING_APPROVAL.value
            or step_draft.reason_code != "step_approval_required"
            or step_draft.occurred_at != activation.occurred_at
            or dict(step_draft.safe_metadata.values)
            != {
                "command": "wait_for_approval",
                "ordinal": step.ordinal,
                "step_kind": step.kind,
                "template_id": step.template_id,
                "configuration_revision": step.configuration_revision,
                "terminal_result": step.terminal_result,
            }
        ):
            raise PlanPersistenceError(
                "write_step_activation_audit_mismatch",
                "write step approval-wait audit is not authoritative",
                run_id=run.id,
            )
