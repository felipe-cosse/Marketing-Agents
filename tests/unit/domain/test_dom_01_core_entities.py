"""DOM-01: all named core entities are immutable and enforce construction invariants."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    semantic_action_hash,
)
from marketing_agents.domain.approval import (
    ApprovalDecision,
    ApprovalPolicySnapshot,
    ProposedExternalAction,
    request_approval,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import (
    AgentInstance,
    AgentTemplate,
    ApprovalPolicy,
    Artifact,
    AuditEvent,
    CampaignBrief,
    DeliveryContractSnapshot,
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
    Effect,
    MisfirePolicy,
    OccurrenceState,
    RunState,
    StepState,
    TriggerKind,
    WorkMode,
)
from marketing_agents.domain.run_lifecycle import initial_received_transition
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    BudgetPolicySnapshot,
    RateLimitPolicySnapshot,
    RateLimitScope,
    RetryBackoff,
    RetryPolicySnapshot,
    StepRuntimePolicy,
    TimeoutPolicySnapshot,
    runtime_rate_limit_key,
)
from marketing_agents.domain.schedule_occurrence_identity import schedule_occurrence_id

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST = "a" * 64


def _runtime_policy(template_id: str, kind: AttemptKind) -> StepRuntimePolicy:
    return StepRuntimePolicy(
        operation_key="runtime.operation.test",
        attempt_kind=kind,
        retry=RetryPolicySnapshot(1, RetryBackoff.NONE),
        timeout=TimeoutPolicySnapshot(30, 120),
        budget=BudgetPolicySnapshot(20, 10, 20),
        rate_limit=RateLimitPolicySnapshot(
            RateLimitScope.TEMPLATE,
            runtime_rate_limit_key(
                template_id=template_id,
                max_calls=10,
                window_seconds=60,
            ),
            10,
            60,
        ),
    )


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
        brief_revision=1,
        digest_key_version="admission-hmac-sha256-v1:" + ("d" * 64),
        admitted_payload={"email": "person@example.test"},
    )
    run = Run(
        "run.1",
        work.id,
        RunState.RECEIVED,
        "c" * 64,
        1,
        NOW,
        updated_at=NOW,
    )
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
        plan_hash=DIGEST,
        graph_hash="b" * 64,
        ordinal=1,
        source_order=10,
        template_id=template.id,
        configuration_revision=1,
        connector_family=capability.connector_family,
        routing_slot_key=None,
        binding_id="mock.newsletter.default",
        binding_configuration_revision=1,
        request_schema_id=capability.request_schema_id,
        result_schema_id=capability.result_schema_id,
        result_schema_hash="schema-sha256-v1:" + "e" * 64,
        request_redaction_fields=("/email",),
        result_redaction_fields=(),
        data_classification=DataClassification.PERSONAL,
        idempotency_support=capability.idempotency_support,
        timeout_seconds=30,
        runtime_policy=_runtime_policy(template.id, AttemptKind.TOOL),
        approval_policy_id=policy.id,
        approval_required_roles=("role.operator",),
        approval_required_scopes=("scope.external-write",),
        approval_expires_after_seconds=1_800,
        approval_allow_self_approval=False,
        terminal_result=True,
        created_at=NOW,
        updated_at=NOW,
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
    semantic_action = SemanticExternalAction(
        template_id=template.id,
        instance_id=instance.id,
        action_type="newsletter.subscribe",
        capability_id=capability.id,
        connector_family="newsletter",
        binding_id="mock.newsletter.default",
        destination="newsletter:list.demo",
        payload_schema_id=capability.request_schema_id,
        minimized_payload={"email": "person@example.test"},
    )
    action_envelope = CanonicalExternalAction(
        action_id="action.1",
        authorization_set_id="authorization-set.1",
        run_id=run.id,
        plan_hash="e" * 64,
        proposal_revision=1,
        step_id=step.id,
        step_key=step.key,
        template_id=template.id,
        instance_id=instance.id,
        action_type=semantic_action.action_type,
        capability_id=capability.id,
        connector_family=semantic_action.connector_family,
        binding_id=semantic_action.binding_id,
        destination=semantic_action.destination,
        payload_schema_id=semantic_action.payload_schema_id,
        minimized_payload=semantic_action.minimized_payload,
        semantic_action_hash=semantic_action_hash(semantic_action),
    )
    proposed_action = ProposedExternalAction.create(
        action_envelope,
        redacted_destination="configured destination via mock.newsletter.default",
        payload_schema={
            "type": "object",
            "properties": {"email": {"x-sensitive": True}},
        },
    )
    action = ExternalAction.proposed(
        proposed_action,
        ApprovalPolicySnapshot(
            policy_id=policy.id,
            required_roles=frozenset({"role.operator"}),
            required_scopes=frozenset({"scope.external-write"}),
            expires_after_seconds=1_800,
            allow_self_approval=False,
        ),
        DeliveryContractSnapshot(
            capability_id=capability.id,
            connector_family="newsletter",
            binding_id="mock.newsletter.default",
            binding_configuration_revision=1,
            request_schema_id=capability.request_schema_id,
            idempotency_support="required",
            timeout_seconds=30,
        ),
        NOW,
    )
    request = request_approval(
        request_id="approval-request.1",
        proposed_action=proposed_action,
        policy=action.approval_policy,
        requested_by="principal.local.operator",
        requested_at=NOW,
    )
    decision = ApprovalDecision(
        id="approval-decision.1",
        request_id=request.id,
        action_id=request.action_id,
        action_hash=request.action_hash,
        authorization_set_id=request.authorization_set_id,
        run_id=request.run_id,
        plan_hash=request.plan_hash,
        proposal_revision=request.proposal_revision,
        step_id=request.step_id,
        step_key=request.step_key,
        actor_id="principal.local.approver",
        authentication_method="local_session",
        correlation_id="correlation.dom-01.approval",
        decision=ApprovalDecisionKind.APPROVE,
        authority_roles=frozenset({"role.operator"}),
        authority_scopes=frozenset({"scope.external-write"}),
        reason_code="approval_granted",
        decided_at=NOW,
    )
    schedule = Schedule(
        "schedule.1",
        trigger.id,
        instance.id,
        "workflow.1",
        "0 9 * * *",
        "America/Los_Angeles",
        NOW,
        MisfirePolicy.RUN_ONCE,
        300,
        True,
        "five-field-cron-adr0008-v1",
    )
    occurrence = ScheduleOccurrence(
        schedule_occurrence_id(
            schedule.id,
            NOW,
            recurrence_version=schedule.recurrence_version,
        ),
        schedule.id,
        NOW,
        "2025-12-31T16:00:00.000000",
        schedule.timezone,
        0,
        schedule.recurrence_version,
        OccurrenceState.DUE,
    )
    audit = AuditEvent(
        AuditEventFactory(
            AuditContext.system("service.intake", correlation_id="correlation.dom-01")
        ).run_transition(
            run,
            initial_received_transition(run),
        ),
        global_sequence=1,
        run_sequence=1,
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
            brief_revision=1,
            digest_key_version="admission-hmac-sha256-v1:" + ("d" * 64),
            admitted_payload={},
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
            plan_hash=DIGEST,
            graph_hash="b" * 64,
            ordinal=1,
            source_order=10,
            template_id="template.model.1",
            configuration_revision=1,
            connector_family="model",
            routing_slot_key=None,
            binding_id=None,
            binding_configuration_revision=None,
            request_schema_id="schema.model.request",
            result_schema_id="schema.model.result",
            result_schema_hash="schema-sha256-v1:" + "e" * 64,
            request_redaction_fields=(),
            result_redaction_fields=(),
            data_classification=DataClassification.INTERNAL,
            idempotency_support="not_applicable",
            timeout_seconds=None,
            runtime_policy=_runtime_policy("template.model.1", AttemptKind.MODEL),
            approval_policy_id="policy.none.v1",
            approval_required_roles=(),
            approval_required_scopes=(),
            approval_expires_after_seconds=None,
            approval_allow_self_approval=None,
            terminal_result=True,
            created_at=NOW,
            updated_at=NOW,
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
        lambda: Schedule(
            "schedule.1",
            "trigger.1",
            "instance.1",
            "workflow.1",
            "* * * * *",
            "Not/A_Zone",
            NOW,
            MisfirePolicy.SKIP,
            300,
            True,
            "five-field-cron-adr0008-v1",
        ),
    ],
)
def test_dom_01_invalid_identity_time_lineage_and_policy_states_fail(constructor: object) -> None:
    with pytest.raises(ValueError):
        constructor()  # type: ignore[operator]
