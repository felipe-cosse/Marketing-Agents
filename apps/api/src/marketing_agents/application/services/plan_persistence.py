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
from marketing_agents.domain.graph import DependencyGraph
from marketing_agents.domain.run_lifecycle import (
    RunLifecycleCommand,
    RunStateTransition,
    transition_run,
)
from marketing_agents.domain.step_lifecycle import (
    StepStateTransition,
    initial_pending_transition,
)

from .audit_events import AuditEventFactory


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
        if effect_plan.lifecycle_context.contains_write_actions:
            raise PlanPersistenceError(
                "write_plan_persistence_not_composed",
                "write-bearing plans require atomic action and approval persistence",
                run_id=effect_plan.run_id,
            )
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
                await _require_plan_replay(
                    unit_of_work,
                    current,
                    stored.plan,
                    stored.steps,
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
            await unit_of_work.commit()
            return PersistedRunPlan(
                transition_result.run,
                stored.plan,
                stored.steps,
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
                request_redaction_fields=planned.request_redaction_fields,
                idempotency_support=planned.idempotency_support,
                timeout_seconds=planned.connector_timeout_seconds,
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
