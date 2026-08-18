"""Atomic registration and authoritative replay of one planned write set."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

from marketing_agents.application.orchestration.dependencies import (
    OrchestrationDependencies,
)
from marketing_agents.application.orchestration.effect_planner import (
    EffectPlan,
    EffectPlanRelease,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.domain.approval import assert_request_binds_action
from marketing_agents.domain.entities import (
    MAX_DELIVERY_ATTEMPTS,
    DeliveryContractSnapshot,
    ExternalAction,
)
from marketing_agents.domain.enums import Effect


class ExternalActionRegistrationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExternalActionRegistrationDisposition(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class RegisteredExternalAction:
    """Authoritative stored identity and immutable approval projection."""

    action: ExternalAction


@dataclass(frozen=True, slots=True)
class RegisteredExternalActionSet:
    actions: tuple[RegisteredExternalAction, ...]
    disposition: ExternalActionRegistrationDisposition


class ExternalActionRegistrationService:
    """Persist a complete proposal set or replay its original exact identities."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        *,
        delivery_attempt_limit: int = 2,
    ) -> None:
        if (
            not isinstance(delivery_attempt_limit, int)
            or isinstance(delivery_attempt_limit, bool)
            or not 1 <= delivery_attempt_limit <= MAX_DELIVERY_ATTEMPTS
        ):
            raise ValueError(
                f"delivery attempt limit must be from 1 through {MAX_DELIVERY_ATTEMPTS}"
            )
        self._dependencies = dependencies
        self._delivery_attempt_limit = delivery_attempt_limit

    async def register_plan_actions(self, plan: EffectPlan) -> RegisteredExternalActionSet:
        async with self._dependencies.unit_of_work() as unit_of_work:
            result = await self.register_plan_actions_in_uow(unit_of_work, plan)
            await unit_of_work.commit()
            return result

    async def register_plan_actions_in_uow(
        self,
        unit_of_work: UnitOfWork,
        plan: EffectPlan,
    ) -> RegisteredExternalActionSet:
        candidates = self._candidates(plan)
        stored = await unit_of_work.external_actions.add_proposed_set_or_get(candidates)
        disposition = (
            ExternalActionRegistrationDisposition.CREATED
            if stored.inserted
            else ExternalActionRegistrationDisposition.REPLAYED
        )
        return RegisteredExternalActionSet(
            actions=tuple(RegisteredExternalAction(action) for action in stored.actions),
            disposition=disposition,
        )

    def _candidates(self, plan: EffectPlan) -> tuple[ExternalAction, ...]:
        if plan.release is not EffectPlanRelease.APPROVAL_REQUIRED:
            raise ExternalActionRegistrationError(
                "write_plan_required", "only write-bearing effect plans register actions"
            )
        write_steps = tuple(step for step in plan.steps if step.effect is Effect.WRITE)
        if (
            not write_steps
            or len(write_steps) != len(plan.proposed_actions)
            or len(write_steps) != len(plan.approval_requests)
        ):
            raise ExternalActionRegistrationError(
                "incomplete_action_set", "write steps, proposals, and approvals must align"
            )
        actions: list[ExternalAction] = []
        for step, proposal, approval in zip(
            write_steps,
            plan.proposed_actions,
            plan.approval_requests,
            strict=True,
        ):
            envelope = proposal.envelope
            if (
                envelope.run_id != plan.run_id
                or envelope.plan_hash != plan.plan_hash
                or envelope.step_id != step.runtime_step_id
                or envelope.step_key != step.step_key
                or envelope.capability_id != step.capability_id
                or envelope.connector_family != step.connector_family
                or envelope.binding_id != step.binding_id
                or envelope.payload_schema_id != step.request_schema_id
            ):
                raise ExternalActionRegistrationError(
                    "action_plan_mismatch", "proposal does not bind its planned write step"
                )
            try:
                assert_request_binds_action(approval, envelope)
            except ValueError as exc:
                raise ExternalActionRegistrationError(
                    "approval_plan_mismatch", "approval does not bind its proposal"
                ) from exc
            if (
                step.binding_id is None
                or step.binding_configuration_revision is None
                or step.request_schema_id is None
                or step.connector_timeout_seconds is None
            ):
                raise ExternalActionRegistrationError(
                    "delivery_contract_incomplete",
                    "planned write lacks a complete connector contract",
                )
            if step.idempotency_support not in {
                "required",
                "supported",
                "unavailable",
            }:
                raise ExternalActionRegistrationError(
                    "idempotency_support_invalid",
                    "planned write has an unsupported idempotency classification",
                )
            contract = DeliveryContractSnapshot(
                capability_id=step.capability_id,
                connector_family=step.connector_family,
                binding_id=step.binding_id,
                binding_configuration_revision=step.binding_configuration_revision,
                request_schema_id=step.request_schema_id,
                idempotency_support=cast(
                    Literal["required", "supported", "unavailable"],
                    step.idempotency_support,
                ),
                timeout_seconds=step.connector_timeout_seconds,
            )
            actions.append(
                ExternalAction.proposed(
                    proposal,
                    approval.policy,
                    contract,
                    approval.requested_at,
                    delivery_attempt_limit=self._delivery_attempt_limit,
                )
            )
        return tuple(actions)
