"""RUN-02: trusted effects release reads and pause every exact external write."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from marketing_agents.application.orchestration import (
    DeterministicInstanceRouter,
    EffectAwarePlanner,
    EffectPlanningError,
    EffectPlanRelease,
    EffectPlanRequest,
    EffectStepSpec,
    RoutingRequest,
    RoutingResult,
    RoutingSlot,
    SelectedInstanceSnapshot,
    WorkflowRoutingDefinition,
    WriteActionIntent,
)
from marketing_agents.application.ports.connector_families import (
    SendCommunityMessageCommand,
    SendEmailCommand,
    SubscribeContactCommand,
    UpsertContactCommand,
)
from marketing_agents.domain.action_hash import (
    CanonicalExternalAction,
    SemanticExternalAction,
    canonical_action_hash,
    semantic_action_hash,
)
from marketing_agents.domain.enums import Effect, TriggerKind
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.infrastructure.adapters.connectors.registry import (
    build_connector_registry,
)
from marketing_agents.infrastructure.catalog import compile_catalog

ROOT = Path(__file__).resolve().parents[3]
CATALOG = compile_catalog(ROOT / "catalog" / "v1")
REGISTRY = build_connector_registry(CATALOG)
NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
WORKFLOW_HASH = "d" * 64
TARGET_INSTANCE = "inst.community.education.material-builder.01"
WORKER_INSTANCE = "inst.community.education.course-cohort-onboarder.01"
WORKER_TEMPLATE = "tpl.community.education.course-cohort-onboarder"
COMMUNITY_BINDING = "mock.community.default"


@dataclass(frozen=True, slots=True)
class BindingSource:
    instance_id: str
    connector_family: str
    binding_id: str
    enabled: bool = True
    configuration_revision: int = 1


class RecordingClock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return NOW


class RecordingIds:
    def __init__(self, seed: int = 0) -> None:
        self.seed = seed
        self.calls: list[str] = []

    def new(self, namespace: str) -> str:
        self.calls.append(namespace)
        return f"{namespace}:{self.seed + len(self.calls)}"


def _bindings(**updates: object) -> tuple[BindingSource, ...]:
    binding = BindingSource(WORKER_INSTANCE, "community", COMMUNITY_BINDING)
    return (replace(binding, **updates),)


def _planner(
    *,
    clock: RecordingClock | None = None,
    ids: RecordingIds | None = None,
    catalog_hash: str | None = None,
    capabilities: tuple[object, ...] | None = None,
    templates: tuple[object, ...] | None = None,
    policies: tuple[object, ...] | None = None,
    operations: tuple[object, ...] | None = None,
    bindings: tuple[BindingSource, ...] | None = None,
) -> tuple[EffectAwarePlanner, RecordingClock, RecordingIds]:
    clock = clock or RecordingClock()
    ids = ids or RecordingIds()
    planner = EffectAwarePlanner(
        catalog_content_hash=catalog_hash or CATALOG.content_hash,
        clock=clock,
        ids=ids,
        capabilities=capabilities or CATALOG.tool_capabilities,  # type: ignore[arg-type]
        templates=templates or CATALOG.templates,  # type: ignore[arg-type]
        approval_policies=policies or CATALOG.approval_policies,  # type: ignore[arg-type]
        operations=operations or REGISTRY.operations,  # type: ignore[arg-type]
        bindings=bindings if bindings is not None else _bindings(),
    )
    return planner, clock, ids


def _route(*, include_write: bool) -> object:
    router = DeterministicInstanceRouter(
        catalog_content_hash=CATALOG.content_hash,
        templates=CATALOG.templates,
        instances=CATALOG.instances,
        capability_ids=tuple(item.id for item in CATALOG.tool_capabilities),
    )
    slots = [
        RoutingSlot(
            key="membership",
            source_order=10,
            template_priorities=(WORKER_TEMPLATE,),
            required_capability_ids=("cap.community.read-membership",),
        )
    ]
    if include_write:
        slots.append(
            RoutingSlot(
                key="welcome",
                source_order=20,
                template_priorities=(WORKER_TEMPLATE,),
                required_capability_ids=("cap.messaging.send-message",),
            )
        )
    return router.route(
        RoutingRequest(
            target_instance_id=TARGET_INSTANCE,
            trigger_id="trigger.manual.1",
            trigger_source="operator.local",
            trigger_kind=TriggerKind.MANUAL,
        ),
        WorkflowRoutingDefinition(
            workflow_id="workflow.community.onboarding",
            workflow_version=1,
            catalog_content_hash=CATALOG.content_hash,
            eligible_trigger_kinds=(TriggerKind.MANUAL,),
            eligible_target_template_ids=("tpl.community.education.material-builder",),
            required_slots=tuple(slots),
        ),
    )


def _read_step(*, write_intent: WriteActionIntent | None = None) -> EffectStepSpec:
    return EffectStepSpec(
        runtime_step_id="runtime-step.membership",
        step_key="membership",
        kind="connector.read",
        selected_instance_id=WORKER_INSTANCE,
        routing_slot_key="membership",
        capability_id="cap.community.read-membership",
        binding_id=COMMUNITY_BINDING,
        write_intent=write_intent,
    )


def _write_step(
    *,
    key: str = "welcome",
    runtime_step_id: str = "runtime-step.welcome",
    body: str = "private welcome body",
    command: object | None = None,
) -> EffectStepSpec:
    resolved_command = command or SendCommunityMessageCommand(
        recipient_refs=("contact:private-person",), body=body
    )
    return EffectStepSpec(
        runtime_step_id=runtime_step_id,
        step_key=key,
        kind="connector.write",
        selected_instance_id=WORKER_INSTANCE,
        routing_slot_key="welcome",
        capability_id="cap.messaging.send-message",
        binding_id=COMMUNITY_BINDING,
        write_intent=WriteActionIntent(
            command=resolved_command,  # type: ignore[arg-type]
        ),
    )


def _request(
    *,
    include_write: bool,
    write_step: EffectStepSpec | None = None,
    run_id: str = "run.effect-plan.1",
) -> EffectPlanRequest:
    steps = (_read_step(),)
    topology = (TopologyStep("membership", 1, terminal_result=True),)
    if include_write:
        steps = (_read_step(), write_step or _write_step())
        topology = (
            TopologyStep("membership", 1),
            TopologyStep("welcome", 2, ("membership",), terminal_result=True),
        )
    return EffectPlanRequest(
        run_id=run_id,
        workflow_definition_hash=WORKFLOW_HASH,
        graph=DependencyGraph.build(topology, workflow_max_steps=10, global_max_steps=20),
        routing=_route(include_write=include_write),  # type: ignore[arg-type]
        steps=steps,
        requested_by="principal.local.operator",
    )


def test_run_02_read_only_plan_releases_directly_without_time_or_ids() -> None:
    planner, clock, ids = _planner()

    plan = planner.plan(_request(include_write=False))

    assert plan.release is EffectPlanRelease.DIRECT
    assert not plan.lifecycle_context.contains_write_actions
    assert plan.proposed_actions == ()
    assert plan.approval_requests == ()
    assert clock.calls == 0
    assert ids.calls == []


def test_run_02_real_mixed_route_proposes_exact_redacted_action_and_pauses() -> None:
    planner, clock, ids = _planner()

    plan = planner.plan(_request(include_write=True))

    assert plan.release is EffectPlanRelease.APPROVAL_REQUIRED
    assert plan.lifecycle_context.contains_write_actions
    assert len(plan.proposed_actions) == len(plan.approval_requests) == 1
    proposal = plan.proposed_actions[0]
    approval = plan.approval_requests[0]
    envelope = proposal.envelope
    assert envelope.plan_hash == plan.plan_hash
    assert envelope.proposal_revision == 1
    assert envelope.step_key == "welcome"
    assert envelope.semantic_action_hash == semantic_action_hash(envelope.semantic_action())
    assert proposal.key_material == envelope.key_material()
    assert approval.plan_hash == plan.plan_hash
    assert approval.proposal_revision == 1
    assert approval.step_key == "welcome"
    assert approval.semantic_action_hash == envelope.semantic_action_hash
    assert proposal.redacted_projection["payload"] == {
        "recipient_refs": "[REDACTED]",
        "body": "[REDACTED]",
    }
    assert "private welcome body" not in str(proposal.redacted_projection)
    assert envelope.destination.startswith("destination-sha256-v1:")
    assert proposal.redacted_projection["destination"] == (
        f"configured destination via {COMMUNITY_BINDING}"
    )
    assert clock.calls == 1
    assert ids.calls == ["authorization-set", "external-action", "approval-request"]


def test_run_02_all_writes_are_proposed_before_one_authorization_set_releases() -> None:
    first = _write_step(key="welcome-a", runtime_step_id="runtime-step.welcome-a")
    second = _write_step(
        key="welcome-b",
        runtime_step_id="runtime-step.welcome-b",
        body="second private body",
    )
    graph = DependencyGraph.build(
        (
            TopologyStep("membership", 1),
            TopologyStep("welcome-a", 2, ("membership",)),
            TopologyStep("welcome-b", 3, ("welcome-a",), terminal_result=True),
        ),
        workflow_max_steps=10,
        global_max_steps=20,
    )
    request = EffectPlanRequest(
        run_id="run.effect-plan.multiple",
        workflow_definition_hash=WORKFLOW_HASH,
        graph=graph,
        routing=_route(include_write=True),  # type: ignore[arg-type]
        steps=(_read_step(), first, second),
        requested_by="principal.local.operator",
    )
    planner, clock, ids = _planner()

    plan = planner.plan(request)

    assert len(plan.proposed_actions) == len(plan.approval_requests) == 2
    assert {item.envelope.step_key for item in plan.proposed_actions} == {
        "welcome-a",
        "welcome-b",
    }
    assert len({item.envelope.authorization_set_id for item in plan.proposed_actions}) == 1
    assert clock.calls == 1
    assert ids.calls.count("external-action") == ids.calls.count("approval-request") == 2


@pytest.mark.parametrize(
    ("binding_updates", "expected_code"),
    [
        ({"binding_id": "mock.community.other"}, "binding_mismatch"),
        ({"connector_family": "crm"}, "binding_missing"),
        ({"enabled": False}, "binding_disabled"),
        ({"configuration_revision": 2}, "binding_revision_drift"),
    ],
)
def test_run_02_invalid_effective_binding_fails_before_authority(
    binding_updates: dict[str, object], expected_code: str
) -> None:
    planner, clock, ids = _planner(bindings=_bindings(**binding_updates))

    with pytest.raises(EffectPlanningError) as captured:
        planner.plan(_request(include_write=True))

    assert captured.value.code == expected_code
    assert clock.calls == 0
    assert ids.calls == []


def test_run_02_missing_effective_binding_fails_before_authority() -> None:
    planner, clock, ids = _planner(bindings=())

    with pytest.raises(EffectPlanningError) as captured:
        planner.plan(_request(include_write=True))

    assert captured.value.code == "binding_missing"
    assert clock.calls == 0
    assert ids.calls == []


def test_run_02_catalog_release_label_drift_fails_before_authority() -> None:
    planner, clock, ids = _planner(catalog_hash="catalog-sha256-v1:" + "b" * 64)

    with pytest.raises(EffectPlanningError) as captured:
        planner.plan(_request(include_write=True))

    assert captured.value.code == "catalog_drift"
    assert clock.calls == 0
    assert ids.calls == []


@pytest.mark.parametrize("drift", ["effect", "idempotency"])
def test_run_02_operation_effect_or_idempotency_drift_fails_before_authority(
    drift: str,
) -> None:
    clock = RecordingClock()
    ids = RecordingIds()
    capabilities: tuple[object, ...] = CATALOG.tool_capabilities
    operations: tuple[object, ...] = REGISTRY.operations
    if drift == "effect":
        operations = tuple(
            replace(item, metadata=replace(item.metadata, effect=Effect.READ))
            if item.metadata.capability_id == "cap.messaging.send-message"
            else item
            for item in REGISTRY.operations
        )
    else:
        capabilities = tuple(
            item.model_copy(update={"idempotency_support": "supported"})
            if item.id == "cap.messaging.send-message"
            else item
            for item in CATALOG.tool_capabilities
        )

    with pytest.raises(EffectPlanningError) as captured:
        _planner(
            clock=clock,
            ids=ids,
            capabilities=capabilities,
            operations=operations,
        )

    assert captured.value.code == "operation_metadata_drift"
    assert clock.calls == 0
    assert ids.calls == []


def test_run_02_disabled_operation_and_forged_route_fail_before_authority() -> None:
    template_id = "tpl.email.newsletter.newsletter-subscriber"
    instance_id = "inst.email.newsletter.newsletter-subscriber.01"
    templates = tuple(
        item.model_copy(
            update={
                "allowed_tool_capability_ids": (
                    *item.allowed_tool_capability_ids,
                    "cap.email.send-message",
                )
            }
        )
        if item.id == template_id
        else item
        for item in CATALOG.templates
    )
    graph = DependencyGraph.build(
        (TopologyStep("send", 1, terminal_result=True),),
        workflow_max_steps=10,
        global_max_steps=20,
    )
    routing = RoutingResult(
        workflow_id="workflow.email.send",
        workflow_version=1,
        catalog_content_hash=CATALOG.content_hash,
        target_instance_id=instance_id,
        selected_instances=(SelectedInstanceSnapshot(instance_id, template_id, 1, 1, 1),),
        assignments=(),
        semantic_hash="e" * 64,
    )
    request = EffectPlanRequest(
        run_id="run.disabled",
        workflow_definition_hash=WORKFLOW_HASH,
        graph=graph,
        routing=routing,
        steps=(
            EffectStepSpec(
                runtime_step_id="runtime-step.send",
                step_key="send",
                kind="connector.write",
                selected_instance_id=instance_id,
                routing_slot_key=None,
                capability_id="cap.email.send-message",
                binding_id="mock.newsletter.default",
                write_intent=WriteActionIntent(
                    command=SendEmailCommand(
                        contact_ref="contact:1", subject="private", body="private body"
                    )
                ),
            ),
        ),
        requested_by="principal.local.operator",
    )
    planner, clock, ids = _planner(
        templates=templates,
        bindings=(BindingSource(instance_id, "newsletter", "mock.newsletter.default"),),
    )

    with pytest.raises(EffectPlanningError) as captured:
        planner.plan(request)

    assert captured.value.code == "operation_disabled"
    assert clock.calls == 0
    assert ids.calls == []

    mixed = _request(include_write=True)
    forged_assignment = replace(mixed.routing.assignments[0], instance_id=TARGET_INSTANCE)
    forged_route = replace(
        mixed.routing,
        assignments=(forged_assignment, *mixed.routing.assignments[1:]),
    )
    forged_request = replace(mixed, routing=forged_route)
    normal, normal_clock, normal_ids = _planner()
    with pytest.raises(EffectPlanningError) as route_error:
        normal.plan(forged_request)
    assert route_error.value.code == "routing_assignment_mismatch"
    assert normal_clock.calls == 0
    assert normal_ids.calls == []


@pytest.mark.parametrize(
    ("request_factory", "expected_code"),
    [
        (
            lambda: EffectPlanRequest(
                run_id="run.effect-plan.read-forgery",
                workflow_definition_hash=WORKFLOW_HASH,
                graph=DependencyGraph.build(
                    (TopologyStep("membership", 1, terminal_result=True),),
                    workflow_max_steps=10,
                    global_max_steps=20,
                ),
                routing=_route(include_write=False),  # type: ignore[arg-type]
                steps=(
                    _read_step(
                        write_intent=WriteActionIntent(
                            command=SendCommunityMessageCommand(
                                recipient_refs=("contact:1",), body="forged"
                            ),
                        )
                    ),
                ),
                requested_by="principal.local.operator",
            ),
            "read_has_write_intent",
        ),
        (
            lambda: _request(
                include_write=True,
                write_step=_write_step(
                    command=SubscribeContactCommand(contact_ref="contact:1", list_ref="list:1")
                ),
            ),
            "command_type_mismatch",
        ),
    ],
)
def test_run_02_effect_or_command_forgery_fails_closed(
    request_factory: object, expected_code: str
) -> None:
    planner, clock, ids = _planner()

    with pytest.raises(EffectPlanningError) as captured:
        planner.plan(request_factory())  # type: ignore[operator]

    assert captured.value.code == expected_code
    assert clock.calls == 0
    assert ids.calls == []


def test_run_02_replanning_reuses_structural_and_semantic_key_material() -> None:
    request = _request(include_write=True)
    first_planner, _, _ = _planner(ids=RecordingIds(seed=10))
    second_planner, _, _ = _planner(ids=RecordingIds(seed=100))

    first = first_planner.plan(request)
    second = second_planner.plan(request)

    assert first.plan_hash == second.plan_hash
    assert first.proposed_actions[0].key_material == second.proposed_actions[0].key_material
    assert first.proposed_actions[0].action_hash != second.proposed_actions[0].action_hash
    assert first.proposed_actions[0].envelope.action_id != (
        second.proposed_actions[0].envelope.action_id
    )

    changed = first_planner.plan(
        _request(
            include_write=True,
            write_step=_write_step(body="different body"),
        )
    )
    assert changed.plan_hash == first.plan_hash
    assert changed.proposed_actions[0].envelope.semantic_action_hash != (
        first.proposed_actions[0].envelope.semantic_action_hash
    )


def test_run_02_hash_domains_have_fixed_golden_vectors() -> None:
    planner, _, _ = _planner(ids=RecordingIds(seed=0))

    plan = planner.plan(_request(include_write=True))
    proposal = plan.proposed_actions[0]

    assert plan.plan_hash == "005b27fecdb86b27f320c50ac1933dfb468afaa022a315332a81f023ff7ad6a2"
    assert proposal.envelope.semantic_action_hash == (
        "4f4bee4353522eac5819cbc2ecec847b0363793723366418bfa3d8284a19a223"
    )
    assert proposal.action_hash == (
        "f663f8087895fee6ceba856728c1a3eb35fa9efd9237be01d37e11c59dfaf54a"
    )
    assert proposal.envelope.destination == (
        "destination-sha256-v1:b074c70e6a182db3e95b92955b4e844318b52194d9ed0221ad0c13bc5300e386"
    )


@pytest.mark.parametrize(
    "policy_updates",
    [
        {"required_roles": ("role.admin",)},
        {"expiry_seconds": 1_200},
        {"allow_self_approval": False},
    ],
)
def test_run_02_policy_semantics_change_the_structural_plan_hash(
    policy_updates: dict[str, object],
) -> None:
    request = _request(include_write=True)
    baseline, _, _ = _planner()
    policy_id = "policy.human-approval.external-write.v1"
    policies = tuple(
        item.model_copy(update=policy_updates) if item.id == policy_id else item
        for item in CATALOG.approval_policies
    )
    changed, _, _ = _planner(policies=policies)

    assert baseline.plan(request).plan_hash != changed.plan(request).plan_hash


def test_run_02_command_and_action_payloads_are_deep_immutable_snapshots() -> None:
    source_fields = {"profile": {"name": "Original"}, "segments": ["one"]}
    command = UpsertContactCommand(contact_ref="contact:1", fields=source_fields)
    intent = WriteActionIntent(command=command)
    source_fields["profile"]["name"] = "Source changed"  # type: ignore[index]
    command.fields["profile"]["name"] = "Command changed"  # type: ignore[index]
    assert intent.payload_snapshot["fields"]["profile"]["name"] == "Original"  # type: ignore[index]
    with pytest.raises(TypeError):
        intent.payload_snapshot["fields"]["profile"]["name"] = "blocked"  # type: ignore[index]

    payload = {"nested": {"value": "original"}, "items": [1, 2]}
    semantic = SemanticExternalAction(
        template_id=WORKER_TEMPLATE,
        instance_id=WORKER_INSTANCE,
        action_type="messaging.send-message",
        capability_id="cap.messaging.send-message",
        connector_family="community",
        binding_id=COMMUNITY_BINDING,
        destination="channel:1",
        payload_schema_id="schema:connector:messaging.send-message:request:v1",
        minimized_payload=payload,
    )
    action = CanonicalExternalAction(
        action_id="action:immutable",
        authorization_set_id="authorization-set:immutable",
        run_id="run:immutable",
        plan_hash="a" * 64,
        proposal_revision=1,
        step_id="runtime-step:immutable",
        step_key="immutable",
        template_id=semantic.template_id,
        instance_id=semantic.instance_id,
        action_type=semantic.action_type,
        capability_id=semantic.capability_id,
        connector_family=semantic.connector_family,
        binding_id=semantic.binding_id,
        destination=semantic.destination,
        payload_schema_id=semantic.payload_schema_id,
        minimized_payload=payload,
        semantic_action_hash=semantic_action_hash(semantic),
    )
    exact_hash = canonical_action_hash(action)
    payload["nested"]["value"] = "source changed"  # type: ignore[index]
    payload["items"].append(3)  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        action.minimized_payload["nested"]["value"] = "blocked"  # type: ignore[index]
    with pytest.raises(AttributeError):
        action.minimized_payload["items"].append(4)  # type: ignore[union-attr]
    assert canonical_action_hash(action) == exact_hash


def test_run_02_public_plan_cannot_forge_release_or_redacted_projection() -> None:
    planner, _, _ = _planner()
    plan = planner.plan(_request(include_write=True))

    with pytest.raises(ValueError, match="release"):
        replace(plan, release=EffectPlanRelease.DIRECT)
    downgraded_steps = tuple(replace(step, effect=Effect.READ) for step in plan.steps)
    with pytest.raises(ValueError, match="hash"):
        replace(
            plan,
            release=EffectPlanRelease.DIRECT,
            steps=downgraded_steps,
            proposed_actions=(),
            approval_requests=(),
        )
    with pytest.raises(ValueError, match="hash"):
        replace(plan, graph_hash="0" * 64)
    with pytest.raises(ValueError, match="hash"):
        replace(plan, routing_hash="0" * 64)
    with pytest.raises(ValueError, match="hash"):
        replace(
            plan,
            steps=(
                plan.steps[0],
                replace(plan.steps[1], configuration_revision=2),
            ),
        )
    with pytest.raises(ValueError, match="hash"):
        replace(
            plan,
            steps=(
                plan.steps[0],
                replace(plan.steps[1], approval_required_roles=("role.forged",)),
            ),
        )
    mutable_steps = list(plan.steps)
    with pytest.raises(ValueError, match="immutable tuple"):
        replace(plan, steps=mutable_steps)  # type: ignore[arg-type]
    mutable_roles = list(plan.steps[1].approval_required_roles)
    with pytest.raises(ValueError, match="immutable tuple"):
        replace(plan.steps[1], approval_required_roles=mutable_roles)  # type: ignore[arg-type]
    mutable_step = SimpleNamespace(
        **{field.name: getattr(plan.steps[0], field.name) for field in fields(plan.steps[0])}
    )
    with pytest.raises(ValueError, match="exact immutable contract type"):
        replace(plan, steps=(mutable_step,))  # type: ignore[arg-type]
    raw_projection = MappingProxyType(
        {
            "action_type": plan.proposed_actions[0].envelope.action_type,
            "capability_id": plan.proposed_actions[0].envelope.capability_id,
            "connector_family": "community",
            "binding_id": COMMUNITY_BINDING,
            "destination": "channel:private-cohort",
            "payload": {"body": "private welcome body"},
        }
    )
    forged = replace(plan.proposed_actions[0], redacted_projection=raw_projection)
    with pytest.raises(ValueError, match="redaction"):
        replace(plan, proposed_actions=(forged,))
