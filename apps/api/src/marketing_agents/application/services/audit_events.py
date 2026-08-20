"""Typed construction of redacted, integrity-sealed audit event drafts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from marketing_agents.domain.audit import (
    AuditContext,
    AuditEventDraft,
    AuditOutcome,
    _issue_audit_event_draft,
    normalize_audit_reason_code,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import Run, RunPlanSnapshot, RunStep
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.retention import RetentionPolicy
from marketing_agents.domain.run_lifecycle import RunLifecycleCommand, RunStateTransition
from marketing_agents.domain.step_lifecycle import (
    StepStateTransition,
    StepTransitionResult,
    initial_pending_transition,
)
from marketing_agents.security.audit_metadata import seal_audit_metadata

_AUDIT_EVENT_ID_DOMAIN = b"marketing-agents:audit-event-id:v1\x00"
_AUDIT_ATTEMPT_ID_DOMAIN = b"marketing-agents:audit-run-attempt-id:v1\x00"


class AuditEventFactory:
    """The only production constructor for appendable audit drafts."""

    def __init__(
        self,
        context: AuditContext,
        *,
        retention_policy: RetentionPolicy | None = None,
    ) -> None:
        if type(context) is not AuditContext:
            raise ValueError("audit event factory requires the exact actor context")
        context.verify_integrity()
        self._context = context
        self._retention_policy = retention_policy

    def run_transition(
        self,
        run: Run,
        transition: RunStateTransition,
    ) -> AuditEventDraft:
        if type(run) is not Run or type(transition) is not RunStateTransition:
            raise ValueError("run audit requires exact persisted lifecycle contracts")
        if (
            run.id != transition.run_id
            or run.state is not transition.new_state
            or run.version != transition.resulting_version
            or run.updated_at != transition.occurred_at
        ):
            raise ValueError("run audit transition does not match its persisted Run")
        if transition.command.value == "record_plan":
            raise ValueError("record-plan transitions require the complete plan audit factory")
        if transition.sequence == 1:
            event_type = "run.received"
            metadata: Mapping[str, Any] = {
                "command": transition.command.value,
                "catalog_content_hash": _catalog_hash(run.catalog_hash),
            }
        else:
            event_type = "run.transitioned"
            metadata = {"command": transition.command.value}
        return self._build(
            run_id=transition.run_id,
            event_type=event_type,
            aggregate_type="run",
            aggregate_id=transition.run_id,
            outcome=AuditOutcome.ACCEPTED,
            occurred_at=transition.occurred_at,
            metadata=metadata,
            mutation_version=transition.resulting_version,
            transition_sequence=transition.sequence,
            previous_state=(
                None if transition.previous_state is None else transition.previous_state.value
            ),
            new_state=transition.new_state.value,
            reason_code=normalize_audit_reason_code(transition.reason_code),
        )

    def run_plan_recorded(
        self,
        run: Run,
        transition: RunStateTransition,
        plan: RunPlanSnapshot,
    ) -> AuditEventDraft:
        if (
            type(run) is not Run
            or type(transition) is not RunStateTransition
            or type(plan) is not RunPlanSnapshot
            or transition.command.value != "record_plan"
            or transition.sequence <= 1
            or run.id != transition.run_id
            or run.id != plan.run_id
            or run.state is not RunState.PLANNED
            or run.version != transition.resulting_version
            or run.updated_at != transition.occurred_at
            or plan.created_at != transition.occurred_at
            or run.approval_required != plan.approval_required
            or _catalog_hash(run.catalog_hash) != plan.catalog_content_hash
        ):
            raise ValueError("plan audit requires the accepted record-plan transition")
        return self._build(
            run_id=transition.run_id,
            event_type="run.plan_recorded",
            aggregate_type="run",
            aggregate_id=transition.run_id,
            outcome=AuditOutcome.ACCEPTED,
            occurred_at=transition.occurred_at,
            metadata={
                "command": transition.command.value,
                "plan_hash": plan.plan_hash,
                "workflow_id": plan.workflow_id,
                "workflow_version": plan.workflow_version,
                "workflow_definition_hash": plan.workflow_definition_hash,
                "catalog_content_hash": plan.catalog_content_hash,
                "graph_hash": plan.graph_hash,
                "routing_hash": plan.routing_hash,
                "step_count": plan.step_count,
            },
            mutation_version=transition.resulting_version,
            transition_sequence=transition.sequence,
            previous_state=(
                None if transition.previous_state is None else transition.previous_state.value
            ),
            new_state=transition.new_state.value,
            reason_code=normalize_audit_reason_code(transition.reason_code),
        )

    def step_recorded(
        self,
        step: RunStep,
        transition: StepStateTransition,
        plan: RunPlanSnapshot,
    ) -> AuditEventDraft:
        if (
            type(plan) is not RunPlanSnapshot
            or transition != initial_pending_transition(step)
            or step.run_id != plan.run_id
            or step.plan_hash != plan.plan_hash
            or step.graph_hash != plan.graph_hash
            or step.created_at != plan.created_at
        ):
            raise ValueError("step-recorded audit requires its exact initial transition")
        return self._build(
            run_id=step.run_id,
            event_type="step.recorded",
            aggregate_type="step",
            aggregate_id=step.id,
            outcome=AuditOutcome.ACCEPTED,
            occurred_at=transition.occurred_at,
            metadata={
                "plan_hash": step.plan_hash,
                "workflow_id": plan.workflow_id,
                "workflow_version": plan.workflow_version,
                "workflow_definition_hash": plan.workflow_definition_hash,
                "catalog_content_hash": plan.catalog_content_hash,
                "graph_hash": step.graph_hash,
                "routing_hash": plan.routing_hash,
                "step_count": plan.step_count,
                "ordinal": step.ordinal,
                "step_kind": step.kind,
                "template_id": step.template_id,
                "configuration_revision": step.configuration_revision,
                "terminal_result": step.terminal_result,
            },
            step_id=step.id,
            mutation_version=1,
            transition_sequence=1,
            previous_state=None,
            new_state=transition.new_state.value,
            reason_code=normalize_audit_reason_code(transition.reason_code),
        )

    def step_transition(
        self,
        step: RunStep,
        transition: StepStateTransition,
    ) -> AuditEventDraft:
        try:
            StepTransitionResult(step, transition)
        except ValueError as exc:
            raise ValueError("step audit requires its exact accepted transition") from exc
        if transition.sequence <= 1 or transition.previous_state is None:
            raise ValueError("step audit requires its exact accepted transition")
        return self._build(
            run_id=step.run_id,
            event_type="step.transitioned",
            aggregate_type="step",
            aggregate_id=step.id,
            outcome=AuditOutcome.ACCEPTED,
            occurred_at=transition.occurred_at,
            metadata={
                "command": transition.command.value,
                "ordinal": step.ordinal,
                "step_kind": step.kind,
                "template_id": step.template_id,
                "configuration_revision": step.configuration_revision,
                "terminal_result": step.terminal_result,
            },
            step_id=step.id,
            mutation_version=transition.resulting_version,
            transition_sequence=transition.sequence,
            previous_state=transition.previous_state.value,
            new_state=transition.new_state.value,
            reason_code=normalize_audit_reason_code(transition.reason_code),
        )

    def run_transition_rejected(
        self,
        run: Run,
        *,
        command: RunLifecycleCommand,
        caller_expected_version: int,
        reason_code: str,
        occurred_at: datetime,
    ) -> AuditEventDraft:
        if type(run) is not Run or type(command) is not RunLifecycleCommand:
            raise ValueError("rejected run audit requires exact lifecycle contracts")
        if (
            not isinstance(caller_expected_version, int)
            or isinstance(caller_expected_version, bool)
            or caller_expected_version < 0
        ):
            raise ValueError("rejected run expected version must be nonnegative")
        safe_reason = normalize_audit_reason_code(reason_code)
        if safe_reason is None:  # pragma: no cover - nonoptional input
            raise AssertionError("rejected run reason disappeared")
        attempt_identity = {
            "actor_id": self._context.actor_id,
            "actor_source": self._context.actor_source.value,
            "command": command.value,
            "correlation_id": self._context.correlation_id,
            "run_id": run.id,
        }
        attempt_id = (
            "run-attempt-v1:"
            + hashlib.sha256(
                _AUDIT_ATTEMPT_ID_DOMAIN + canonical_json_bytes(attempt_identity)
            ).hexdigest()
        )
        return self._build(
            run_id=run.id,
            event_type="run.transition_rejected",
            aggregate_type="run_attempt",
            aggregate_id=attempt_id,
            outcome=AuditOutcome.REJECTED,
            occurred_at=occurred_at,
            metadata={"command": command.value},
            attempt_id=attempt_id,
            attempted_command=command.value,
            expected_version=caller_expected_version,
            observed_version=run.version,
            observed_state=run.state.value,
            requested_state=_requested_run_state(run, command),
            mutation_version=None,
            reason_code=safe_reason,
        )

    def run_attempt_id(self, run_id: str, command: RunLifecycleCommand) -> str:
        attempt_identity = {
            "actor_id": self._context.actor_id,
            "actor_source": self._context.actor_source.value,
            "command": command.value,
            "correlation_id": self._context.correlation_id,
            "run_id": run_id,
        }
        return (
            "run-attempt-v1:"
            + hashlib.sha256(
                _AUDIT_ATTEMPT_ID_DOMAIN + canonical_json_bytes(attempt_identity)
            ).hexdigest()
        )

    def _build(
        self,
        *,
        run_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        outcome: AuditOutcome,
        occurred_at: datetime,
        metadata: Mapping[str, Any],
        classification: DataClassification = DataClassification.INTERNAL,
        step_id: str | None = None,
        action_id: str | None = None,
        action_attempt_number: int | None = None,
        receipt_id: str | None = None,
        approval_request_id: str | None = None,
        approval_decision_id: str | None = None,
        artifact_id: str | None = None,
        attempt_id: str | None = None,
        attempted_command: str | None = None,
        expected_version: int | None = None,
        observed_version: int | None = None,
        observed_state: str | None = None,
        requested_state: str | None = None,
        mutation_version: int | None,
        transition_sequence: int | None = None,
        previous_state: str | None = None,
        new_state: str | None = None,
        reason_code: str | None = None,
    ) -> AuditEventDraft:
        self._context.verify_integrity()
        sealed_metadata = seal_audit_metadata(
            event_type,
            metadata,
            occurred_at=occurred_at,
            classification=classification,
            retention_policy=self._retention_policy,
        )
        identity = {
            "schema_version": 1,
            "action_attempt_number": action_attempt_number,
            "aggregate_id": aggregate_id,
            "aggregate_type": aggregate_type,
            "outcome": outcome.value,
            "event_type": event_type,
            "mutation_version": mutation_version,
            "receipt_id": receipt_id,
            "approval_request_id": approval_request_id,
            "approval_decision_id": approval_decision_id,
            "artifact_id": artifact_id,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "step_id": step_id,
            "transition_sequence": transition_sequence,
        }
        event_id = (
            "audit."
            + hashlib.sha256(_AUDIT_EVENT_ID_DOMAIN + canonical_json_bytes(identity)).hexdigest()
        )
        return _issue_audit_event_draft(
            id=event_id,
            run_id=run_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            outcome=outcome,
            actor_id=self._context.actor_id,
            actor_source=self._context.actor_source,
            auth_method=self._context.auth_method,
            correlation_id=self._context.correlation_id,
            safe_metadata=sealed_metadata,
            occurred_at=occurred_at,
            step_id=step_id,
            action_id=action_id,
            action_attempt_number=action_attempt_number,
            receipt_id=receipt_id,
            approval_request_id=approval_request_id,
            approval_decision_id=approval_decision_id,
            artifact_id=artifact_id,
            attempt_id=attempt_id,
            attempted_command=attempted_command,
            expected_version=expected_version,
            observed_version=observed_version,
            observed_state=observed_state,
            requested_state=requested_state,
            mutation_version=mutation_version,
            transition_sequence=transition_sequence,
            previous_state=previous_state,
            new_state=new_state,
            reason_code=reason_code,
        )


def _catalog_hash(value: str) -> str:
    if value.startswith("catalog-sha256-v1:"):
        return value
    return "catalog-sha256-v1:" + value


def _requested_run_state(run: Run, command: RunLifecycleCommand) -> str | None:
    if command is RunLifecycleCommand.ACTIVATE_PLAN:
        return (
            RunState.AWAITING_APPROVAL.value if run.approval_required else RunState.EXECUTING.value
        )
    return {
        RunLifecycleCommand.RECEIVE: RunState.RECEIVED.value,
        RunLifecycleCommand.MARK_VALIDATED: RunState.VALIDATED.value,
        RunLifecycleCommand.RECORD_PLAN: RunState.PLANNED.value,
        RunLifecycleCommand.RELEASE_APPROVED_PLAN: RunState.EXECUTING.value,
        RunLifecycleCommand.REJECT_APPROVAL: RunState.REJECTED.value,
        RunLifecycleCommand.COMPLETE: RunState.COMPLETED.value,
        RunLifecycleCommand.FAIL: RunState.FAILED.value,
        RunLifecycleCommand.CANCEL: RunState.CANCELLED.value,
    }[command]
