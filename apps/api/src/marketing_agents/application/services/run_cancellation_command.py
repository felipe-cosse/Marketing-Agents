"""Authorized public command over the existing authoritative cancellation coordinator."""

from __future__ import annotations

from dataclasses import dataclass, replace

from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.policies.run_cancellation_authorization import (
    RunCancellationAuthorizationError,
    authorize_run_cancellation_operator,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.entities import Run
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.validation import require_id

from .cancellation import (
    RunCancellationCoordinator,
    RunCancellationCoordinatorError,
    RunCancellationOutcome,
)


class RunCancellationCommandError(RuntimeError):
    """A fixed, payload-safe application failure; unavailability may follow commit."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RunCancellationCommand:
    run_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        require_id(self.run_id, "cancellation run ID")
        require_id(self.correlation_id, "cancellation correlation ID")


def validate_cancellation_outcome(outcome: RunCancellationOutcome, *, run_id: str) -> None:
    """Recheck an executor's typed snapshot before exposing any IDs or state."""
    if type(outcome) is not RunCancellationOutcome or type(outcome.run) is not Run:
        raise ValueError("cancellation outcome must use the exact application contracts")
    replace(outcome.run)
    replace(outcome)
    if outcome.run.id != run_id:
        raise ValueError("cancellation outcome belongs to another run")
    for identifier in (
        outcome.run.id,
        *outcome.cancelled_step_ids,
        *outcome.preserved_step_ids,
        *outcome.cancelled_action_ids,
        *outcome.preserved_action_ids,
    ):
        require_id(identifier, "cancellation resource ID")


class RunCancellationCommandService:
    """Authorize before lookup, then delegate rather than recreate lifecycle semantics."""

    def __init__(self, dependencies: OrchestrationDependencies) -> None:
        self._coordinator = RunCancellationCoordinator(dependencies)

    async def request(
        self, command: RunCancellationCommand, *, principal: AuthenticatedPrincipal
    ) -> RunCancellationOutcome:
        try:
            authorize_run_cancellation_operator(principal)
        except RunCancellationAuthorizationError:
            raise RunCancellationCommandError("cancellation_forbidden") from None
        try:
            if type(command) is not RunCancellationCommand:
                raise ValueError("cancellation command must use the exact contract")
            replace(command)
        except (TypeError, ValueError):
            raise RunCancellationCommandError("cancellation_input_invalid") from None
        context = AuditContext.authenticated_user(
            principal.actor_id,
            authentication_method=principal.authentication_method.value,
            correlation_id=command.correlation_id,
        )
        try:
            outcome = await self._coordinator.request(command.run_id, audit_context=context)
            validate_cancellation_outcome(outcome, run_id=command.run_id)
        except RunCancellationCoordinatorError as error:
            error_code = error.code if type(error.code) is str else "cancellation_unavailable"
            code = {
                "run_not_found": "run_not_found",
                "terminal_state_immutable": "cancellation_conflict",
                "cancellation_conflict": "cancellation_conflict",
            }.get(error_code, "cancellation_unavailable")
            raise RunCancellationCommandError(code) from None
        except Exception:
            # The coordinator can commit cancellation before normalization fails.
            # Never claim a rollback or retry the mutation on this boundary.
            raise RunCancellationCommandError("cancellation_unavailable") from None
        return outcome
