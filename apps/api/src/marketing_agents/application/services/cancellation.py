"""Authoritative cancellation routing across every persisted Run phase."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import ExternalAction, Run
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState
from marketing_agents.domain.run_lifecycle import (
    CancellationContext,
    RunLifecycleCommand,
    RunTransitionError,
)
from marketing_agents.domain.step_lifecycle import StepLifecycleCommand

from .approval_boundaries import ApprovalBoundaryService, ApprovalBoundaryServiceError
from .run_cancellation import RunCancellationService, RunCancellationServiceError
from .run_lifecycle import RunLifecycleService, RunLifecycleServiceError

_TERMINAL_RUN_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.REJECTED,
        RunState.CANCELLED,
    }
)
_STALE_ROUTE_CODES = frozenset(
    {
        "action_cancellation_conflict",
        "stale_execution_control",
        "stale_run_version",
        "step_cancellation_conflict",
        "run_transition_conflict",
    }
)


class RunCancellationCoordinatorError(RuntimeError):
    """Stable public cancellation failure without lower-layer routing details."""

    def __init__(self, code: str, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class RunCancellationOutcome:
    """Normalized cancellation-time facts for one complete persisted Run scope.

    ``preserved_*`` contains every scoped member that the cancellation did not
    transition to ``cancelled``. That includes already-terminal work and work
    whose call was already in flight; neither is represented as rolled back.
    """

    run: Run
    cancelled_at: datetime
    cancelled_step_ids: tuple[str, ...]
    preserved_step_ids: tuple[str, ...]
    cancelled_action_ids: tuple[str, ...]
    preserved_action_ids: tuple[str, ...]
    succeeded_effect_count: int
    outcome_unknown_effect_count: int

    def __post_init__(self) -> None:
        if self.run.state is not RunState.CANCELLED or self.run.updated_at != self.cancelled_at:
            raise ValueError("cancellation outcome requires its exact terminal Run")
        for values, label in (
            (self.cancelled_step_ids, "cancelled step IDs"),
            (self.preserved_step_ids, "preserved step IDs"),
            (self.cancelled_action_ids, "cancelled action IDs"),
            (self.preserved_action_ids, "preserved action IDs"),
        ):
            if type(values) is not tuple or len(values) != len(set(values)):
                raise ValueError(f"{label} must be a unique immutable tuple")
        if set(self.cancelled_step_ids) & set(self.preserved_step_ids):
            raise ValueError("cancelled and preserved step IDs must be disjoint")
        if set(self.cancelled_action_ids) & set(self.preserved_action_ids):
            raise ValueError("cancelled and preserved action IDs must be disjoint")
        for value in (self.succeeded_effect_count, self.outcome_unknown_effect_count):
            if type(value) is not int or value < 0:
                raise ValueError("cancellation-time effect counts must be nonnegative integers")


class RunCancellationCoordinator:
    """Derive the authoritative route and normalize one cancellation outcome."""

    _MAX_ROUTE_ATTEMPTS = 3

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies
        self._lifecycle = RunLifecycleService(dependencies)
        self._direct = RunCancellationService(dependencies)
        self._approval = ApprovalBoundaryService(dependencies)

    async def request(
        self,
        run_id: str,
        *,
        audit_context: AuditContext,
    ) -> RunCancellationOutcome:
        for attempt_index in range(self._MAX_ROUTE_ATTEMPTS):
            observed = await self._load_run(run_id)
            if observed.state in _TERMINAL_RUN_STATES:
                await self._reject_terminal(observed, audit_context=audit_context)

            try:
                cancelled = await self._route(observed, audit_context=audit_context)
                return await self._normalize(cancelled)
            except (
                ApprovalBoundaryServiceError,
                RunCancellationServiceError,
                RunLifecycleServiceError,
            ) as exc:
                if attempt_index < self._MAX_ROUTE_ATTEMPTS - 1 and await self._route_changed(
                    observed,
                    exc,
                ):
                    continue
                raise self._public_error(exc, run_id=run_id) from None
            except RunTransitionError as exc:
                raise RunCancellationCoordinatorError(
                    "cancellation_failed",
                    "Run cancellation was rejected by its authoritative lifecycle",
                    run_id=run_id,
                ) from exc
            except RunCancellationCoordinatorError:
                raise
            except RuntimeError as exc:
                raise RunCancellationCoordinatorError(
                    "cancellation_failed",
                    "Run cancellation could not be completed atomically",
                    run_id=run_id,
                ) from exc
        raise AssertionError("bounded cancellation routing exhausted without an outcome")

    async def _route(self, run: Run, *, audit_context: AuditContext) -> Run:
        if run.state in {RunState.RECEIVED, RunState.VALIDATED}:
            result = await self._lifecycle.advance(
                run.id,
                run.version,
                RunLifecycleCommand.CANCEL,
                CancellationContext(reason_code="operator_cancelled"),
                audit_context=audit_context,
            )
            return result.run
        if run.approval_required is False and run.state in {RunState.PLANNED, RunState.EXECUTING}:
            return (await self._direct.request(run.id, audit_context=audit_context)).run
        if run.approval_required is True and run.state in {
            RunState.AWAITING_APPROVAL,
            RunState.EXECUTING,
        }:
            return (await self._approval.cancel(run.id, audit_context=audit_context)).run
        raise RunCancellationCoordinatorError(
            "cancellation_route_invalid",
            "Run state does not have a valid authoritative cancellation route",
            run_id=run.id,
        )

    async def _reject_terminal(self, run: Run, *, audit_context: AuditContext) -> None:
        try:
            await self._lifecycle.advance(
                run.id,
                run.version,
                RunLifecycleCommand.CANCEL,
                CancellationContext(reason_code="operator_cancelled"),
                audit_context=audit_context,
            )
        except (RunLifecycleServiceError, RunTransitionError):
            raise RunCancellationCoordinatorError(
                "terminal_state_immutable",
                "terminal Run cannot accept another cancellation",
                run_id=run.id,
            ) from None
        raise RunCancellationCoordinatorError(
            "cancellation_failed",
            "terminal Run unexpectedly accepted another cancellation",
            run_id=run.id,
        )

    async def _load_run(self, run_id: str) -> Run:
        async with self._dependencies.unit_of_work() as unit_of_work:
            run = await unit_of_work.runs.get(run_id)
        if run is None:
            raise RunCancellationCoordinatorError(
                "run_not_found",
                "Run does not exist",
                run_id=run_id,
            )
        return run

    async def _route_changed(self, observed: Run, error: RuntimeError) -> bool:
        if getattr(error, "code", None) in _STALE_ROUTE_CODES:
            return True
        latest = await self._load_run(observed.id)
        return (
            latest.version != observed.version
            or latest.state is not observed.state
            or latest.approval_required is not observed.approval_required
        )

    @staticmethod
    def _public_error(error: RuntimeError, *, run_id: str) -> RunCancellationCoordinatorError:
        if getattr(error, "code", None) in _STALE_ROUTE_CODES:
            return RunCancellationCoordinatorError(
                "cancellation_conflict",
                "Run cancellation could not acquire a stable routing fence",
                run_id=run_id,
            )
        return RunCancellationCoordinatorError(
            "cancellation_failed",
            "Run cancellation failed in its authoritative service",
            run_id=run_id,
        )

    async def _normalize(self, cancelled: Run) -> RunCancellationOutcome:
        if cancelled.state is not RunState.CANCELLED:
            raise RunCancellationCoordinatorError(
                "cancellation_result_invalid",
                "authoritative cancellation did not return a cancelled Run",
                run_id=cancelled.id,
            )
        async with self._dependencies.unit_of_work() as unit_of_work:
            current = await unit_of_work.runs.get(cancelled.id)
            history = await unit_of_work.runs.list_transitions(cancelled.id)
            if current != cancelled:
                raise RunCancellationCoordinatorError(
                    "cancellation_result_invalid",
                    "persisted Run differs from its cancellation result",
                    run_id=cancelled.id,
                )
            matching = tuple(
                transition
                for transition in history
                if transition.command is RunLifecycleCommand.CANCEL
                and transition.resulting_version == cancelled.version
            )
            if (
                len(matching) != 1
                or matching[0].new_state is not RunState.CANCELLED
                or matching[0].occurred_at != cancelled.updated_at
            ):
                raise RunCancellationCoordinatorError(
                    "cancellation_result_invalid",
                    "cancelled Run lacks its exact lifecycle transition",
                    run_id=cancelled.id,
                )
            transition = matching[0]
            plan = await unit_of_work.run_steps.get_plan(cancelled.id)
            steps = () if plan is None else await unit_of_work.run_steps.list_for_run(cancelled.id)
            cancelled_step_ids: list[str] = []
            preserved_step_ids: list[str] = []
            for step in steps:
                step_history = await unit_of_work.run_steps.list_transitions(step.id)
                cancelled_here = (
                    step.state is StepState.CANCELLED
                    and step.updated_at == transition.occurred_at
                    and step.terminal_reason_code in {"operator_cancelled", "run_cancelled"}
                    and any(
                        item.command is StepLifecycleCommand.CANCEL
                        and item.resulting_version == step.version
                        and item.occurred_at == transition.occurred_at
                        for item in step_history
                    )
                )
                (cancelled_step_ids if cancelled_here else preserved_step_ids).append(step.id)

            actions: tuple[ExternalAction, ...] = ()
            if plan is not None and plan.approval_required:
                selection = await unit_of_work.approvals.get_current_authorization_set(cancelled.id)
                if selection is None or selection.authorization_set.plan_hash != plan.plan_hash:
                    raise RunCancellationCoordinatorError(
                        "cancellation_result_invalid",
                        "cancelled write Run lacks its current authorization set",
                        run_id=cancelled.id,
                    )
                stored_actions = await unit_of_work.external_actions.list_run_plan(
                    cancelled.id,
                    plan.plan_hash,
                )
                action_by_id = {action.id: action for action in stored_actions}
                try:
                    actions = tuple(
                        action_by_id[member.action_id]
                        for member in selection.authorization_set.members
                    )
                except KeyError as exc:
                    raise RunCancellationCoordinatorError(
                        "cancellation_result_invalid",
                        "cancelled write Run lost an authorization-set action",
                        run_id=cancelled.id,
                    ) from exc
            cancelled_action_ids = tuple(
                action.id
                for action in actions
                if action.state is ExternalActionState.CANCELLED
                and action.updated_at == transition.occurred_at
                and action.terminal_reason_code == "operator_cancelled"
            )
            cancelled_action_id_set = set(cancelled_action_ids)
            preserved_action_ids = tuple(
                action.id for action in actions if action.id not in cancelled_action_id_set
            )

        return RunCancellationOutcome(
            run=cancelled,
            cancelled_at=transition.occurred_at,
            cancelled_step_ids=tuple(cancelled_step_ids),
            preserved_step_ids=tuple(preserved_step_ids),
            cancelled_action_ids=cancelled_action_ids,
            preserved_action_ids=preserved_action_ids,
            succeeded_effect_count=transition.completed_effect_count,
            outcome_unknown_effect_count=transition.outcome_unknown_effect_count,
        )


__all__ = [
    "RunCancellationCoordinator",
    "RunCancellationCoordinatorError",
    "RunCancellationOutcome",
]
