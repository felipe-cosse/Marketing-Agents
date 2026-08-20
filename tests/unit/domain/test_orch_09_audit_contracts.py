"""ORCH-09: audited step state is explicit, contiguous, and effect-aware."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.domain.audit import (
    AuditContext,
    AuditOutcome,
    _issue_audit_event_draft,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import RunStep
from marketing_agents.domain.enums import Effect, StepState
from marketing_agents.domain.step_lifecycle import (
    NoStepTransitionContext,
    StepLifecycleCommand,
    StepStateTransition,
    StepTerminalContext,
    StepTransitionError,
    StepTransitionResult,
    initial_pending_transition,
    transition_step,
)
from marketing_agents.security.audit_metadata import seal_audit_metadata

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
TERMINAL_STATES = frozenset(
    {
        StepState.SUCCEEDED,
        StepState.FAILED,
        StepState.REJECTED,
        StepState.CANCELLED,
        StepState.SKIPPED,
    }
)


def _step(effect: Effect, state: StepState, *, version: int = 1) -> RunStep:
    write = effect is Effect.WRITE
    return RunStep(
        id=f"step.{effect.value}.{state.value}",
        run_id="run.orch-09.domain",
        key="publish" if write else "analyze",
        kind="connector.write" if write else "model.read",
        selected_instance_id="instance.orch-09.01",
        dependency_keys=(),
        capability_id="cap.external.write" if write else "cap.model.read",
        effect=effect,
        state=state,
        plan_hash="a" * 64,
        graph_hash="b" * 64,
        ordinal=1,
        source_order=10,
        template_id="tpl.orch-09.01",
        configuration_revision=1,
        connector_family="newsletter" if write else "model",
        routing_slot_key="slot.publish" if write else None,
        binding_id="binding.newsletter.01" if write else None,
        binding_configuration_revision=1 if write else None,
        request_schema_id="schema.newsletter.write.v1" if write else None,
        request_redaction_fields=("/body",) if write else (),
        idempotency_support="required" if write else "not_applicable",
        timeout_seconds=30 if write else None,
        approval_policy_id="approval.human-write" if write else "approval.none",
        approval_required_roles=("role.operator",) if write else (),
        approval_required_scopes=("scope.external-write",) if write else (),
        approval_expires_after_seconds=1_800 if write else None,
        approval_allow_self_approval=False if write else None,
        terminal_result=True,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=version - 1),
        version=version,
        terminal_reason_code=(f"step_{state.value}" if state in TERMINAL_STATES else None),
    )


def _context(command: StepLifecycleCommand):  # type: ignore[no-untyped-def]
    if command in {
        StepLifecycleCommand.FAIL,
        StepLifecycleCommand.REJECT,
        StepLifecycleCommand.CANCEL,
        StepLifecycleCommand.SKIP,
    }:
        return StepTerminalContext(f"step_{command.value}")
    return NoStepTransitionContext()


def _allowed(
    effect: Effect,
    state: StepState,
    command: StepLifecycleCommand,
) -> bool:
    if state in TERMINAL_STATES:
        return False
    if command is StepLifecycleCommand.MARK_READY:
        return effect is Effect.READ and state is StepState.PENDING
    if command is StepLifecycleCommand.WAIT_FOR_APPROVAL:
        return effect is Effect.WRITE and state is StepState.PENDING
    if command is StepLifecycleCommand.START:
        return effect is Effect.READ and state is StepState.READY
    if command in {StepLifecycleCommand.SUCCEED, StepLifecycleCommand.FAIL}:
        return state is StepState.EXECUTING
    if command is StepLifecycleCommand.REJECT:
        return effect is Effect.WRITE and state is StepState.AWAITING_APPROVAL
    if command in {StepLifecycleCommand.CANCEL, StepLifecycleCommand.SKIP}:
        return state in {
            StepState.PENDING,
            StepState.READY,
            StepState.AWAITING_APPROVAL,
        }
    return False


def test_orch_09_step_lifecycle_has_exhaustive_effect_aware_edges() -> None:
    public_commands = tuple(
        command
        for command in StepLifecycleCommand
        if command is not StepLifecycleCommand.INITIALIZE
    )
    for effect in Effect:
        for state in StepState:
            if effect is Effect.READ and state in {
                StepState.AWAITING_APPROVAL,
                StepState.REJECTED,
            }:
                with pytest.raises(ValueError, match="write approval authority"):
                    _step(effect, state)
                continue
            step = _step(effect, state)
            for command in public_commands:
                if _allowed(effect, state, command):
                    result = transition_step(
                        step,
                        command,
                        _context(command),
                        step.updated_at + timedelta(seconds=1),
                    )
                    assert result.transition.previous_state is state
                    assert result.transition.sequence == step.version + 1
                    assert result.step.version == step.version + 1
                else:
                    with pytest.raises(StepTransitionError) as rejected:
                        transition_step(
                            step,
                            command,
                            _context(command),
                            step.updated_at + timedelta(seconds=1),
                        )
                    assert rejected.value.code in {
                        "invalid_transition",
                        "terminal_state_immutable",
                    }


def test_orch_09_transition_contract_rejects_forged_edges_and_mutation() -> None:
    with pytest.raises(ValueError, match="command and states"):
        StepStateTransition(
            step_id="step.forged",
            run_id="run.forged",
            sequence=2,
            command=StepLifecycleCommand.FAIL,
            previous_state=StepState.PENDING,
            new_state=StepState.FAILED,
            reason_code="step_failed",
            occurred_at=NOW,
            expected_version=1,
            resulting_version=2,
        )

    write_ready = _step(Effect.WRITE, StepState.READY, version=2)
    transition = StepStateTransition(
        step_id=write_ready.id,
        run_id=write_ready.run_id,
        sequence=2,
        command=StepLifecycleCommand.MARK_READY,
        previous_state=StepState.PENDING,
        new_state=StepState.READY,
        reason_code="step_dependencies_satisfied",
        occurred_at=write_ready.updated_at,
        expected_version=1,
        resulting_version=2,
    )
    with pytest.raises(ValueError, match="read-only"):
        StepTransitionResult(write_ready, transition)

    object.__setattr__(transition, "command", StepLifecycleCommand.WAIT_FOR_APPROVAL)
    with pytest.raises(ValueError, match="command and states"):
        StepTransitionResult(write_ready, transition)


def test_orch_09_initial_step_history_is_exact_and_terminal_state_is_immutable() -> None:
    pending = _step(Effect.READ, StepState.PENDING)
    initial = initial_pending_transition(pending)
    assert initial.sequence == initial.resulting_version == 1
    assert initial.expected_version == 0
    assert initial.previous_state is None
    assert initial.new_state is StepState.PENDING

    terminal = _step(Effect.READ, StepState.SUCCEEDED, version=4)
    for command in StepLifecycleCommand:
        if command is StepLifecycleCommand.INITIALIZE:
            continue
        with pytest.raises(StepTransitionError) as rejected:
            transition_step(
                terminal,
                command,
                _context(command),
                terminal.updated_at + timedelta(seconds=1),
            )
        assert rejected.value.code == "terminal_state_immutable"


def test_orch_09_raw_audit_hydration_rejects_impossible_initial_mutation() -> None:
    context = AuditContext.system(
        "test.orch-09.domain",
        correlation_id="orch-09.raw-hydration",
    )
    metadata = seal_audit_metadata(
        "run.received",
        {
            "command": "receive",
            "catalog_content_hash": "catalog-sha256-v1:" + ("c" * 64),
        },
        occurred_at=NOW,
        classification=DataClassification.INTERNAL,
    )
    with pytest.raises(ValueError, match="initial transition"):
        _issue_audit_event_draft(
            id="audit.impossible.initial",
            run_id="run.impossible.initial",
            event_type="run.received",
            aggregate_type="run",
            aggregate_id="run.impossible.initial",
            outcome=AuditOutcome.ACCEPTED,
            actor_id=context.actor_id,
            actor_source=context.actor_source,
            auth_method=context.auth_method,
            correlation_id=context.correlation_id,
            safe_metadata=metadata,
            occurred_at=NOW,
            mutation_version=2,
            transition_sequence=2,
            previous_state=StepState.PENDING.value,
            new_state=StepState.CANCELLED.value,
            reason_code="work_admitted",
        )
