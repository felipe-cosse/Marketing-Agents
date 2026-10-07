"""Bounded terminal reconciliation; this service has no executable provider dependency."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.write_authorization import (
    AuthorizedExternalWrite,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.application.ports.external_writes import ConnectorDeliveryContract
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterContract,
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.runtime_inputs import RuntimeInputContract
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import ExternalAction
from marketing_agents.domain.enums import Effect, ExternalActionState, RunState, StepState
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.runtime_policy import AttemptKind
from marketing_agents.domain.validation import require_id

from .controlled_read_executor import ControlledReadExecutor
from .external_action_dispatcher import ExternalActionDispatcher


class _NoWriteGateway:
    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract:
        raise RuntimeError("cancelled recovery cannot authorize a provider call")

    async def execute(self, authorization: AuthorizedExternalWrite) -> ConnectorWriteResult:
        raise RuntimeError("cancelled recovery cannot execute a provider call")


class _NoReadAdapter:
    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        raise RuntimeError("cancelled recovery cannot authorize a READ call")

    def input_contract_for(self, operation: OperationExecutionPolicy) -> RuntimeInputContract:
        raise RuntimeError("cancelled recovery cannot prepare a READ call")

    def output_contract_for(self, operation: OperationExecutionPolicy) -> RuntimeOutputContract:
        raise RuntimeError("cancelled recovery cannot prepare a READ result")

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        raise RuntimeError("cancelled recovery cannot execute a READ call")


class CancelledRunRecoveryService:
    """Close at most 32 existing calls, preserving the terminal parent and all authority."""

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies

    async def recover(
        self,
        run_id: str,
        *,
        audit_context: AuditContext,
        recovery_fence: Callable[[UnitOfWork], Awaitable[None]] | None = None,
    ) -> None:
        require_id(run_id, "cancelled recovery Run ID")
        audit_context.verify_integrity()
        async with self._dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.state is not RunState.CANCELLED:
                raise ValueError("cancelled_recovery_parent_invalid")
            plan = await uow.run_steps.get_plan(run_id)
            if plan is None:
                return  # Early cancellation never created call authority.
            control = await uow.execution_control.get(run_id)
            if (
                control is None
                or control.cancel_requested_at is None
                or control.policy_hash != plan.plan_hash
            ):
                raise ValueError("cancelled_recovery_control_invalid")
            steps = await uow.run_steps.validate_plan_for_execution(run_id)
            actions = (
                await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
                if any(step.effect is Effect.WRITE for step in steps)
                else ()
            )
            due_reads: list[str] = []
            now = self._dependencies.utc_now()
            for step in steps:
                if (
                    step.state is StepState.EXECUTING
                    and step.effect is Effect.READ
                    and step.runtime_policy.attempt_kind is not AttemptKind.NO_CALL
                ):
                    attempts = await uow.execution_control.list_attempts(
                        step.id, step.runtime_policy.operation_key
                    )
                    if (
                        attempts
                        and attempts[-1].outcome is None
                        and attempts[-1].call_deadline_at <= now
                    ):
                        due_reads.append(step.id)
            due_writes = tuple(
                action.id
                for action in actions
                if action.state is ExternalActionState.DISPATCHING
                and action.call_started_at is not None
                and action.call_deadline_at is not None
                and action.lease is not None
                and max(action.call_deadline_at, action.lease.expires_at) <= now
            )
        # Every closure revalidates its exact parent/plan and uses original
        # aggregate versions, with outcome + step + audit in one transaction.
        # No ordinary executor, registry, workflow, or generic retry path runs.
        reads = ControlledReadExecutor(self._dependencies, _NoReadAdapter())
        writes = ExternalActionDispatcher(
            self._dependencies, _NoWriteGateway(), WriteAuthorizationGuard()
        )
        for step_id in due_reads[:32]:
            await reads.recover_cancelled_attempt(
                step_id, audit_context=audit_context, recovery_fence=recovery_fence
            )
        for action_id in due_writes[: max(0, 32 - len(due_reads))]:
            await writes.recover_cancelled_action(
                action_id, audit_context=audit_context, recovery_fence=recovery_fence
            )
