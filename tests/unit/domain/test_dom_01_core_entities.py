"""DOM-01: all named core entities are immutable and enforce construction invariants."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import (
    AgentInstance,
    AgentTemplate,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    Artifact,
    AuditEvent,
    CampaignBrief,
    Department,
    ExternalAction,
    FunctionTeam,
    Run,
    RunStep,
    Schedule,
    ScheduleOccurrence,
    ToolCapability,
    TriggerDefinition,
    WorkItem,
)
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    Effect,
    ExternalActionState,
    MisfirePolicy,
    OccurrenceState,
    RunState,
    StepState,
    TriggerKind,
    WorkMode,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST = "a" * 64


def test_dom_01_all_named_core_entities_construct_as_immutable_values() -> None:
    department = Department("dept.email", "Email", 1)
    function = FunctionTeam("func.email.newsletter", department.id, "Newsletter", 1)
    capability = ToolCapability(
        "cap.newsletter.subscribe",
        "newsletter-email",
        Effect.WRITE,
        "schema.newsletter.subscribe.request",
        "schema.newsletter.subscribe.result",
        "required",
    )
    policy = ApprovalPolicy(
        "policy.human-write.v1", frozenset({"role.operator"}), timedelta(minutes=30), False
    )
    template = AgentTemplate(
        "tpl.email.newsletter.newsletter-subscriber",
        department.id,
        function.id,
        "Newsletter Subscriber",
        "Maintain newsletter subscriptions.",
        "prompt.newsletter-subscriber",
        "schema.newsletter.input",
        "schema.newsletter.output",
        (capability.id,),
        (TriggerKind.MANUAL, TriggerKind.WEBHOOK),
        policy.id,
    )
    instance = AgentInstance(
        "inst.email.newsletter.newsletter-subscriber.01",
        template.id,
        True,
        1,
        {"newsletter-email": "binding.mock.newsletter"},
        1,
    )
    trigger = TriggerDefinition("trigger.manual.1", instance.id, TriggerKind.MANUAL, {}, True)
    brief = CampaignBrief(
        "brief.1", "Signup", "Add a supplied contact.", {"locale": "en"}, ("source.1",)
    )
    work = WorkItem(
        "work.1",
        "manual",
        "event.1",
        instance.id,
        trigger.id,
        "workflow.email-signup.v1",
        WorkMode.MOCK_EXECUTION,
        brief.id,
        1,
        DIGEST,
        "b" * 64,
        NOW,
    )
    run = Run("run.1", work.id, RunState.RECEIVED, "c" * 64, 1, NOW)
    step = RunStep(
        "step.1",
        run.id,
        "subscribe",
        "connector",
        instance.id,
        (),
        capability.id,
        Effect.WRITE,
        StepState.PENDING,
    )
    artifact = Artifact(
        "artifact.1",
        run.id,
        step.id,
        "schema.artifact.v1",
        {"status": "draft"},
        DIGEST,
        (),
        DataClassification.INTERNAL,
        NOW,
    )
    action = ExternalAction(
        "action.1",
        run.id,
        step.id,
        DIGEST,
        "idempotency.00000001",
        "binding.mock.newsletter",
        ExternalActionState.PROPOSED,
        NOW,
    )
    request = ApprovalRequest(
        "approval-request.1",
        action.id,
        DIGEST,
        {"destination": "[REDACTED]"},
        policy.id,
        "principal.local.operator",
        NOW,
        NOW + timedelta(minutes=30),
        ApprovalStatus.PENDING,
    )
    decision = ApprovalDecision(
        "approval-decision.1",
        request.id,
        "principal.local.operator",
        ApprovalDecisionKind.APPROVE,
        DIGEST,
        "Approved for the local mock demo.",
        NOW,
    )
    schedule = Schedule(
        "schedule.1",
        trigger.id,
        instance.id,
        "0 9 * * *",
        "America/Los_Angeles",
        NOW,
        MisfirePolicy.RUN_ONCE,
        True,
    )
    occurrence = ScheduleOccurrence("occurrence.1", schedule.id, NOW, OccurrenceState.DUE)
    audit = AuditEvent(
        "audit.1",
        1,
        "run.received",
        "run",
        run.id,
        "service.intake",
        "correlation.1",
        {"state": run.state.value},
        NOW,
    )

    entities = (
        department,
        function,
        capability,
        policy,
        template,
        instance,
        trigger,
        brief,
        work,
        run,
        step,
        artifact,
        action,
        request,
        decision,
        schedule,
        occurrence,
        audit,
    )
    assert len(entities) == 18
    assert work.source_idempotency_key == ("manual", "event.1", instance.id)
    with pytest.raises(FrozenInstanceError):
        run.state = RunState.COMPLETED  # type: ignore[misc]
    with pytest.raises(TypeError):
        instance.connector_bindings["crm"] = "binding.other"  # type: ignore[index]


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: Department("bad id", "Email", 1),
        lambda: WorkItem(
            "work.1",
            "manual",
            "event.1",
            "instance.1",
            "trigger.1",
            "workflow.1",
            WorkMode.DRY_RUN,
            "brief.1",
            1,
            DIGEST,
            DIGEST,
            datetime(2026, 1, 1),
        ),
        lambda: RunStep(
            "step.1",
            "run.1",
            "same",
            "model",
            "instance.1",
            ("same",),
            "cap.model.generate",
            Effect.READ,
            StepState.PENDING,
        ),
        lambda: Artifact(
            "artifact.1",
            "run.1",
            "step.1",
            "schema.1",
            {},
            DIGEST,
            ("artifact.1",),
            DataClassification.INTERNAL,
            NOW,
        ),
        lambda: ApprovalRequest(
            "request.1",
            "action.1",
            DIGEST,
            {},
            "policy.1",
            "actor.1",
            NOW,
            NOW,
            ApprovalStatus.PENDING,
        ),
        lambda: Schedule(
            "schedule.1",
            "trigger.1",
            "instance.1",
            "* * * * *",
            "Not/A_Zone",
            NOW,
            MisfirePolicy.SKIP,
            True,
        ),
    ],
)
def test_dom_01_invalid_identity_time_lineage_and_policy_states_fail(constructor: object) -> None:
    with pytest.raises(ValueError):
        constructor()  # type: ignore[operator]
