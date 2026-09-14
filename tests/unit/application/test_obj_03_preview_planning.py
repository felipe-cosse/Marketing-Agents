"""OBJ-03 planner-owned previews preserve the real WRITE capability without granting it."""

from dataclasses import replace

import pytest
from marketing_agents.application.orchestration import (
    DeterministicInstanceRouter,
    EffectPlanningError,
    EffectPlanRequest,
    EffectStepSpec,
    RoutingRequest,
    WorkflowRoutingDefinition,
    WriteActionIntent,
)
from marketing_agents.application.ports.connector_families import SendCommunityMessageCommand
from marketing_agents.domain.enums import Effect, TriggerKind, WorkMode
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.planner_output import PLANNER_OUTPUT_FAMILY, PROPOSAL_PREVIEW_KIND
from marketing_agents.domain.runtime_policy import AttemptKind, attempt_kind_for_connector

from tests.unit.application.test_run_02_effect_aware_planning import (
    CATALOG,
    WORKER_INSTANCE,
    WORKER_TEMPLATE,
    WORKFLOW_HASH,
    _planner,
)


def test_planner_output_family_is_never_a_model_or_tool_call() -> None:
    assert attempt_kind_for_connector(PLANNER_OUTPUT_FAMILY) is AttemptKind.NO_CALL


def _request():
    routing = DeterministicInstanceRouter(
        catalog_content_hash=CATALOG.content_hash,
        templates=CATALOG.templates,
        instances=CATALOG.instances,
        capability_ids=tuple(item.id for item in CATALOG.tool_capabilities),
    ).route(
        RoutingRequest(
            target_instance_id=WORKER_INSTANCE,
            trigger_id="trigger.manual.preview",
            trigger_source="operator.local",
            trigger_kind=TriggerKind.MANUAL,
        ),
        WorkflowRoutingDefinition(
            workflow_id="workflow.preview.test",
            workflow_version=1,
            catalog_content_hash=CATALOG.content_hash,
            eligible_trigger_kinds=(TriggerKind.MANUAL,),
            eligible_target_template_ids=(WORKER_TEMPLATE,),
        ),
    )
    return EffectPlanRequest(
        run_id="run.preview.test",
        workflow_definition_hash=WORKFLOW_HASH,
        graph=DependencyGraph.build(
            (TopologyStep("preview", 1, (), terminal_result=True),),
            workflow_max_steps=1,
            global_max_steps=1,
        ),
        routing=routing,
        steps=(
            EffectStepSpec(
                runtime_step_id="step.preview.test",
                step_key="preview",
                kind=PROPOSAL_PREVIEW_KIND,
                selected_instance_id=WORKER_INSTANCE,
                routing_slot_key=None,
                capability_id="cap.messaging.send-message",
                binding_id=None,
            ),
        ),
        requested_by="worker.preview.test",
    )


def test_preview_is_deterministic_no_call_and_allocates_no_approval_identity() -> None:
    planner, clock, ids = _planner(bindings=())
    request = _request()
    plan = planner.plan_proposal_preview(request, mode=WorkMode.DRY_RUN)
    second = planner.plan_proposal_preview(
        replace(
            request,
            run_id="run.preview.other",
            steps=(replace(request.steps[0], runtime_step_id="step.preview.other"),),
        ),
        mode=WorkMode.DRY_RUN,
    )
    assert plan.plan_hash == second.plan_hash
    assert plan.proposed_actions == plan.approval_requests == ()
    assert plan.run_policy.max_model_calls == plan.run_policy.max_tool_calls == 0
    assert clock.calls == 0 and ids.calls == []
    step = plan.steps[0]
    assert step.effect is Effect.READ and step.connector_family == PLANNER_OUTPUT_FAMILY
    assert step.capability_id == "cap.messaging.send-message"
    assert step.runtime_policy.attempt_kind is AttemptKind.NO_CALL
    assert step.binding_id is None and step.binding_configuration_revision is None
    with pytest.raises(EffectPlanningError) as error:
        planner.plan(request)
    assert error.value.code == "preview_entrypoint_required"


@pytest.mark.parametrize("mode", [WorkMode.MOCK_EXECUTION, "dry_run", None, True])
def test_preview_requires_exact_dry_run_mode(mode) -> None:
    planner, _, _ = _planner()
    with pytest.raises(EffectPlanningError) as error:
        planner.plan_proposal_preview(_request(), mode=mode)
    assert error.value.code == "preview_mode_invalid"


@pytest.mark.parametrize(
    "change",
    [
        {"binding_id": "mock.community.default"},
        {
            "write_intent": WriteActionIntent(
                SendCommunityMessageCommand(
                    recipient_refs=("participant.local",), body="Inert data"
                )
            )
        },
        {"kind": "connector.write"},
    ],
)
def test_preview_refuses_execution_authority(change) -> None:
    planner, _, _ = _planner()
    request = _request()
    with pytest.raises(EffectPlanningError) as error:
        planner.plan_proposal_preview(
            replace(request, steps=(replace(request.steps[0], **change),)), mode=WorkMode.DRY_RUN
        )
    assert error.value.code == "preview_authority_invalid"


def test_preview_does_not_accept_a_non_write_catalog_capability() -> None:
    planner, _, _ = _planner()
    request = _request()
    with pytest.raises(EffectPlanningError) as error:
        planner.plan_proposal_preview(
            replace(
                request,
                steps=(replace(request.steps[0], capability_id="cap.community.read-membership"),),
            ),
            mode=WorkMode.DRY_RUN,
        )
    assert error.value.code == "preview_capability_invalid"
