"""Transactional primary-Run receipt and command-only lifecycle advancement."""

from __future__ import annotations

from dataclasses import dataclass

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.run_lifecycle import (
    RunLifecycleCommand,
    RunStateTransition,
    RunTransitionContext,
    RunTransitionResult,
    initial_received_transition,
    transition_run,
)


class RunLifecycleServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
        current_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.run_id = run_id
        self.current_version = current_version


@dataclass(frozen=True, slots=True)
class ReceiveRunRequest:
    work_item: WorkItem
    catalog_hash: str


@dataclass(frozen=True, slots=True)
class ReceiveRunResult:
    run: Run
    created: bool
    initial_transition: RunStateTransition | None


class RunLifecycleService:
    """Persist accepted transitions while leaving general audit append to ORCH-09."""

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._dependencies = dependencies

    async def receive(self, request: ReceiveRunRequest) -> ReceiveRunResult:
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self.receive_in_uow(unit_of_work, request)
            await unit_of_work.commit()
            return result

    async def receive_in_uow(
        self,
        unit_of_work: UnitOfWork,
        request: ReceiveRunRequest,
    ) -> ReceiveRunResult:
        now = self._dependencies.utc_now()
        candidate = Run(
            id=self._dependencies.new_id("run"),
            work_item_id=request.work_item.id,
            state=RunState.RECEIVED,
            catalog_hash=request.catalog_hash,
            configuration_revision=request.work_item.configuration_revision,
            created_at=now,
            version=1,
            updated_at=now,
        )
        initial = initial_received_transition(candidate)
        stored = await unit_of_work.runs.add_received_or_get(candidate, initial)
        if stored.inserted:
            return ReceiveRunResult(stored.run, created=True, initial_transition=initial)
        return ReceiveRunResult(stored.run, created=False, initial_transition=None)

    async def advance(
        self,
        run_id: str,
        expected_version: int,
        command: RunLifecycleCommand,
        context: RunTransitionContext,
    ) -> RunTransitionResult:
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self.advance_in_uow(
                unit_of_work,
                run_id,
                expected_version,
                command,
                context,
            )
            await unit_of_work.commit()
            return result

    async def advance_in_uow(
        self,
        unit_of_work: UnitOfWork,
        run_id: str,
        expected_version: int,
        command: RunLifecycleCommand,
        context: RunTransitionContext,
    ) -> RunTransitionResult:
        current = await unit_of_work.runs.get(run_id)
        if current is None:
            raise RunLifecycleServiceError("run_not_found", "run does not exist", run_id=run_id)
        if current.version != expected_version:
            raise RunLifecycleServiceError(
                "stale_run_version",
                "run changed before the lifecycle command was applied",
                run_id=run_id,
                current_version=current.version,
            )
        result = transition_run(current, command, context, self._dependencies.utc_now())
        applied = await unit_of_work.runs.apply_transition(
            expected_version=expected_version,
            expected_state=current.state,
            result=result,
        )
        if not applied:
            raise RunLifecycleServiceError(
                "stale_run_version",
                "run changed concurrently before the lifecycle command was persisted",
                run_id=run_id,
            )
        return result

    async def history(self, run_id: str) -> tuple[RunStateTransition, ...]:
        async with self._dependencies.unit_of_work() as unit_of_work:
            return await unit_of_work.runs.list_transitions(run_id)
