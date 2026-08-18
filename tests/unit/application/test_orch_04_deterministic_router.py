"""ORCH-04: deterministic exact-minimum instance routing."""

from __future__ import annotations

import ast
import itertools
import random
from dataclasses import dataclass, fields, replace
from pathlib import Path

import pytest
from marketing_agents.application.orchestration import (
    DeterministicInstanceRouter,
    RoutingError,
    RoutingRequest,
    RoutingSlot,
    WorkflowRoutingDefinition,
)
from marketing_agents.application.orchestration import router as router_module
from marketing_agents.domain.enums import TriggerKind
from marketing_agents.infrastructure.catalog import compile_catalog

ROOT = Path(__file__).resolve().parents[3]
CATALOG_HASH = "catalog-sha256-v1:" + "a" * 64


@dataclass
class TemplateSource:
    id: str
    display_order: int
    allowed_tool_capability_ids: tuple[str, ...]
    supported_trigger_types: tuple[str, ...] = ("manual",)


@dataclass
class VariantSource:
    source_ordinal: int


@dataclass
class InstanceSource:
    id: str
    template_id: str
    display_order: int
    enabled: bool = True
    variant: VariantSource | None = None
    configuration_revision: int = 1


def _request(
    target: str = "inst.target",
    *,
    trigger_kind: TriggerKind = TriggerKind.MANUAL,
    trigger_id: str = "trigger.manual.1",
) -> RoutingRequest:
    return RoutingRequest(
        target_instance_id=target,
        trigger_id=trigger_id,
        trigger_source="operator.local",
        trigger_kind=trigger_kind,
    )


def _definition(
    *slots: RoutingSlot,
    target_templates: tuple[str, ...] = ("tpl.target",),
    triggers: tuple[TriggerKind, ...] = (TriggerKind.MANUAL,),
    catalog_hash: str = CATALOG_HASH,
) -> WorkflowRoutingDefinition:
    return WorkflowRoutingDefinition(
        workflow_id="workflow.test",
        workflow_version=3,
        catalog_content_hash=catalog_hash,
        eligible_trigger_kinds=triggers,
        eligible_target_template_ids=target_templates,
        required_slots=tuple(slots),
    )


def _slot(
    key: str,
    order: int,
    capability: str,
    *template_priorities: str,
) -> RoutingSlot:
    return RoutingSlot(
        key=key,
        source_order=order,
        required_capability_ids=(capability,),
        template_priorities=tuple(template_priorities),
    )


def _router(
    templates: list[TemplateSource],
    instances: list[InstanceSource],
    capabilities: tuple[str, ...],
    *,
    catalog_hash: str = CATALOG_HASH,
) -> DeterministicInstanceRouter:
    return DeterministicInstanceRouter(
        catalog_content_hash=catalog_hash,
        templates=templates,
        instances=instances,
        capability_ids=capabilities,
    )


def _bruteforce_route_oracle(
    *,
    templates: list[TemplateSource],
    instances: list[InstanceSource],
    request: RoutingRequest,
    definition: WorkflowRoutingDefinition,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Independently enumerate every small candidate subset under the contract."""

    template_by_id = {item.id: item for item in templates}
    instance_by_id = {item.id: item for item in instances}
    target = instance_by_id[request.target_instance_id]
    slots = definition.required_slots

    def instance_order(item: InstanceSource) -> tuple[int, int, str]:
        ordinal = 0 if item.variant is None else item.variant.source_ordinal
        return (item.display_order, ordinal, item.id)

    def covers(item: InstanceSource, slot: RoutingSlot) -> bool:
        template = template_by_id[item.template_id]
        return (
            item.enabled
            and item.template_id in slot.template_priorities
            and set(slot.required_capability_ids) <= set(template.allowed_tool_capability_ids)
        )

    candidates = tuple(
        sorted(
            (
                item
                for item in instances
                if item.id != target.id
                and item.enabled
                and any(covers(item, slot) for slot in slots)
            ),
            key=instance_order,
        )
    )
    best: (
        tuple[
            tuple[int, tuple[int, ...], tuple[tuple[int, int, str], ...]],
            tuple[InstanceSource, ...],
        ]
        | None
    ) = None
    for count in range(len(candidates) + 1):
        for subset in itertools.combinations(candidates, count):
            selected = (target, *subset)
            eligible_by_slot = tuple(
                tuple(item for item in selected if covers(item, slot)) for slot in slots
            )
            if any(not eligible for eligible in eligible_by_slot):
                continue
            priority_vector = tuple(
                min(slot.template_priorities.index(item.template_id) for item in eligible)
                for slot, eligible in zip(slots, eligible_by_slot, strict=True)
            )
            objective = (
                count,
                priority_vector,
                tuple(instance_order(item) for item in subset),
            )
            if best is None or objective < best[0]:
                best = (objective, subset)
        if best is not None:
            break
    assert best is not None
    additional = best[1]
    selected = (target, *additional)
    assignments = tuple(
        min(
            (item for item in selected if covers(item, slot)),
            key=lambda item: (
                slot.template_priorities.index(item.template_id),
                *instance_order(item),
            ),
        ).id
        for slot in slots
    )
    return (target.id, *(item.id for item in additional)), assignments


def test_orch_04_real_community_01_covers_two_slots_and_02_is_excluded() -> None:
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    router = DeterministicInstanceRouter(
        catalog_content_hash=catalog.content_hash,
        templates=catalog.templates,
        instances=catalog.instances,
        capability_ids=tuple(item.id for item in catalog.tool_capabilities),
    )
    cohort_template = "tpl.community.education.course-cohort-onboarder"
    target = "inst.community.education.material-builder.01"
    definition = WorkflowRoutingDefinition(
        workflow_id="workflow.community.education.material-onboarding",
        workflow_version=1,
        catalog_content_hash=catalog.content_hash,
        eligible_trigger_kinds=(TriggerKind.MANUAL,),
        eligible_target_template_ids=("tpl.community.education.material-builder",),
        required_slots=(
            _slot("membership", 10, "cap.community.read-membership", cohort_template),
            _slot("welcome", 20, "cap.messaging.send-message", cohort_template),
        ),
    )

    result = router.route(_request(target), definition)

    selected_ids = tuple(item.instance_id for item in result.selected_instances)
    assert selected_ids == (
        target,
        "inst.community.education.course-cohort-onboarder.01",
    )
    assert "inst.community.education.course-cohort-onboarder.02" not in selected_ids
    assert [item.instance_id for item in result.assignments] == [
        "inst.community.education.course-cohort-onboarder.01",
        "inst.community.education.course-cohort-onboarder.01",
    ]
    assert result.selected_instances[1].configuration_revision == 1
    assert len(result.semantic_hash) == 64


def test_orch_04_exact_global_minimum_beats_greedy_template_priority() -> None:
    templates = [
        TemplateSource("tpl.target", 1, ()),
        TemplateSource("tpl.priority.a", 2, ("cap.a",)),
        TemplateSource("tpl.priority.b", 3, ("cap.b",)),
        TemplateSource("tpl.multi", 99, ("cap.a", "cap.b")),
    ]
    instances = [
        InstanceSource("inst.target", "tpl.target", 1),
        InstanceSource("inst.priority.a", "tpl.priority.a", 2),
        InstanceSource("inst.priority.b", "tpl.priority.b", 3),
        InstanceSource("inst.multi", "tpl.multi", 99),
    ]
    router = _router(templates, instances, ("cap.a", "cap.b"))
    definition = _definition(
        _slot("a", 1, "cap.a", "tpl.priority.a", "tpl.multi"),
        _slot("b", 2, "cap.b", "tpl.priority.b", "tpl.multi"),
    )

    result = router.route(_request(), definition)

    assert [item.instance_id for item in result.selected_instances] == [
        "inst.target",
        "inst.multi",
    ]
    assert {item.instance_id for item in result.assignments} == {"inst.multi"}


def test_orch_04_matches_bruteforce_oracle_for_small_cover_spaces() -> None:
    rng = random.Random(20_260_818)
    scenarios: list[
        tuple[
            list[TemplateSource],
            list[InstanceSource],
            tuple[str, ...],
            WorkflowRoutingDefinition,
        ]
    ] = []

    tie_templates = [
        TemplateSource("tpl.target", 1, ()),
        TemplateSource("tpl.worker", 2, ("cap.a", "cap.b")),
    ]
    tie_instances = [
        InstanceSource("inst.target", "tpl.target", 99),
        InstanceSource("inst.worker.z", "tpl.worker", 10, variant=VariantSource(2)),
        InstanceSource("inst.worker.b", "tpl.worker", 10, variant=VariantSource(1)),
        InstanceSource("inst.worker.a", "tpl.worker", 10, variant=VariantSource(1)),
    ]
    scenarios.append(
        (
            tie_templates,
            tie_instances,
            ("cap.a", "cap.b"),
            _definition(
                _slot("a", 1, "cap.a", "tpl.worker"),
                _slot("b", 2, "cap.b", "tpl.worker"),
            ),
        )
    )

    for scenario_index in range(24):
        capabilities = tuple(f"cap.seeded.{index}" for index in range(3))
        worker_templates: list[TemplateSource] = []
        for template_index in range(5):
            allowed = tuple(
                capability
                for capability_index, capability in enumerate(capabilities)
                if rng.random() < 0.45 or capability_index == template_index
            )
            worker_templates.append(
                TemplateSource(
                    f"tpl.seeded.{scenario_index}.{template_index}",
                    template_index + 2,
                    allowed,
                )
            )
        templates = [TemplateSource("tpl.target", 1, ()), *worker_templates]
        instances = [InstanceSource("inst.target", "tpl.target", 50)]
        for template_index, template in enumerate(worker_templates):
            instances.append(
                InstanceSource(
                    f"inst.seeded.{scenario_index}.{template_index}",
                    template.id,
                    rng.randint(1, 5),
                    enabled=template_index < 3 or rng.random() < 0.8,
                    variant=VariantSource(rng.randint(1, 3)),
                )
            )
        instances.append(
            InstanceSource(
                f"inst.seeded.{scenario_index}.0.alt",
                worker_templates[0].id,
                rng.randint(1, 5),
                variant=VariantSource(rng.randint(1, 3)),
            )
        )
        slots: list[RoutingSlot] = []
        for slot_index, capability in enumerate(capabilities):
            priorities = [
                template.id
                for template in worker_templates
                if capability in template.allowed_tool_capability_ids
            ]
            rng.shuffle(priorities)
            slots.append(
                _slot(
                    f"slot.seeded.{slot_index}",
                    slot_index + 1,
                    capability,
                    *priorities,
                )
            )
        rng.shuffle(templates)
        rng.shuffle(instances)
        rng.shuffle(slots)
        scenarios.append((templates, instances, capabilities, _definition(*slots)))

    request = _request()
    observed_additional_counts: set[int] = set()
    for templates, instances, capabilities, definition in scenarios:
        result = _router(templates, instances, capabilities).route(request, definition)
        expected_selected, expected_assignments = _bruteforce_route_oracle(
            templates=templates,
            instances=instances,
            request=request,
            definition=definition,
        )
        assert tuple(item.instance_id for item in result.selected_instances) == expected_selected
        assert tuple(item.instance_id for item in result.assignments) == expected_assignments
        observed_additional_counts.add(len(result.selected_instances) - 1)
    assert observed_additional_counts == {1, 2}


def test_orch_04_exact_dp_retains_partial_ties_that_later_reverse() -> None:
    """A later C supersedes s1, so A's better s2 priority must remain available."""

    templates = [
        TemplateSource("tpl.target", 1, ()),
        TemplateSource("tpl.a", 2, ("cap.s1", "cap.s2")),
        TemplateSource("tpl.b", 3, ("cap.s1", "cap.s2")),
        TemplateSource("tpl.c", 4, ("cap.s1", "cap.s3")),
    ]
    instances = [
        InstanceSource("inst.target", "tpl.target", 1),
        InstanceSource("inst.a", "tpl.a", 1),
        InstanceSource("inst.b", "tpl.b", 2),
        InstanceSource("inst.c", "tpl.c", 3),
    ]
    definition = _definition(
        _slot("s1", 1, "cap.s1", "tpl.c", "tpl.b", "tpl.a"),
        _slot("s2", 2, "cap.s2", "tpl.a", "tpl.b"),
        _slot("s3", 3, "cap.s3", "tpl.c"),
    )

    result = _router(templates, instances, ("cap.s1", "cap.s2", "cap.s3")).route(
        _request(), definition
    )

    assert [item.instance_id for item in result.selected_instances] == [
        "inst.target",
        "inst.a",
        "inst.c",
    ]
    assert [item.instance_id for item in result.assignments] == [
        "inst.c",
        "inst.a",
        "inst.c",
    ]


def test_orch_04_tertiary_instance_tie_survives_later_priority_equalization() -> None:
    early = router_module._InstanceSnapshot("inst.early", "tpl.early", 1, True, None, 1)
    dominant = router_module._InstanceSnapshot("inst.dominant", "tpl.dominant", 2, True, None, 1)
    future = router_module._InstanceSnapshot("inst.future", "tpl.future", 3, True, None, 1)
    early_state = router_module._CoverState((early,), (3, 3, 3, 2**31 - 1))
    dominant_state = router_module._CoverState((dominant,), (2, 0, 3, 2**31 - 1))
    states = {0b0111: [early_state]}

    DeterministicInstanceRouter._retain_exact_state(states, 0b0111, dominant_state)

    assert states[0b0111] == [early_state, dominant_state]
    future_priorities = (0, 0, 2**31 - 1, 0)
    extended = [
        router_module._CoverState(
            (*state.selection, future),
            tuple(
                min(current, new)
                for current, new in zip(state.assignment_priorities, future_priorities, strict=True)
            ),
        )
        for state in states[0b0111]
    ]
    winner = min(
        extended,
        key=lambda state: (
            state.assignment_priorities,
            router_module._selection_order(state.selection),
        ),
    )
    assert [item.id for item in winner.selection] == ["inst.early", "inst.future"]


def test_orch_04_only_explicit_target_must_support_external_trigger() -> None:
    templates = [
        TemplateSource("tpl.target", 1, (), ("webhook",)),
        TemplateSource("tpl.read-collaborator", 2, ("cap.read",), ("manual",)),
    ]
    instances = [
        InstanceSource("inst.target", "tpl.target", 1),
        InstanceSource("inst.read-collaborator", "tpl.read-collaborator", 2),
    ]
    definition = _definition(
        _slot("read", 1, "cap.read", "tpl.read-collaborator"),
        triggers=(TriggerKind.WEBHOOK,),
    )

    result = _router(templates, instances, ("cap.read",)).route(
        _request(trigger_kind=TriggerKind.WEBHOOK), definition
    )

    assert [item.instance_id for item in result.selected_instances] == [
        "inst.target",
        "inst.read-collaborator",
    ]


def test_orch_04_template_then_display_ordinal_and_stable_id_break_ties() -> None:
    templates = [
        TemplateSource("tpl.target", 1, ()),
        TemplateSource("tpl.preferred", 99, ("cap.work",)),
        TemplateSource("tpl.other", 1, ("cap.work",)),
    ]
    instances = [
        InstanceSource("inst.target", "tpl.target", 1),
        InstanceSource("inst.other", "tpl.other", 1),
        InstanceSource("inst.preferred.high-display", "tpl.preferred", 20),
        InstanceSource("inst.preferred.z", "tpl.preferred", 10, variant=VariantSource(2)),
        InstanceSource("inst.preferred.b", "tpl.preferred", 10, variant=VariantSource(1)),
        InstanceSource("inst.preferred.a", "tpl.preferred", 10, variant=VariantSource(1)),
    ]
    definition = _definition(_slot("work", 1, "cap.work", "tpl.preferred", "tpl.other"))

    forward = _router(templates, instances, ("cap.work",)).route(_request(), definition)
    reverse = _router(list(reversed(templates)), list(reversed(instances)), ("cap.work",)).route(
        _request(), definition
    )

    assert [item.instance_id for item in forward.selected_instances] == [
        "inst.target",
        "inst.preferred.a",
    ]
    assert forward == reverse


def test_orch_04_enabled_eligible_target_is_retained_without_extra_slots() -> None:
    target = InstanceSource(
        "inst.target", "tpl.target", 5, variant=VariantSource(1), configuration_revision=7
    )
    template = TemplateSource("tpl.target", 5, ())
    router = _router([template], [target], ())

    result = router.route(_request(), _definition())

    assert result.target_instance_id == "inst.target"
    assert result.assignments == ()
    assert result.selected_instances[0].configuration_revision == 7
    assert len(result.selected_instances) == 1


def test_orch_04_template_only_slot_needs_no_tool_capability() -> None:
    router = _router(
        [
            TemplateSource("tpl.target", 1, ()),
            TemplateSource("tpl.transform", 2, ()),
        ],
        [
            InstanceSource("inst.target", "tpl.target", 1),
            InstanceSource("inst.transform", "tpl.transform", 2),
        ],
        (),
    )
    definition = _definition(
        RoutingSlot(
            key="transform",
            source_order=1,
            template_priorities=("tpl.transform",),
        )
    )

    result = router.route(_request(), definition)

    assert [item.instance_id for item in result.selected_instances] == [
        "inst.target",
        "inst.transform",
    ]
    assert result.assignments[0].required_capability_ids == ()


def test_orch_04_sources_are_snapshotted_and_semantic_hash_covers_trusted_inputs() -> None:
    template = TemplateSource("tpl.target", 1, ())
    instance = InstanceSource("inst.target", "tpl.target", 1, configuration_revision=2)
    router = _router([template], [instance], ())
    definition = _definition()
    first = router.route(_request(), definition)

    instance.configuration_revision = 999
    template.supported_trigger_types = ("webhook",)
    snapshotted = router.route(_request(), definition)
    changed_trigger = router.route(_request(trigger_id="trigger.manual.2"), definition)
    changed_revision = _router(
        [TemplateSource("tpl.target", 1, ())],
        [InstanceSource("inst.target", "tpl.target", 1, configuration_revision=3)],
        (),
    ).route(_request(), definition)

    assert snapshotted == first
    assert first.selected_instances[0].configuration_revision == 2
    assert changed_trigger.semantic_hash != first.semantic_hash
    assert changed_revision.semantic_hash != first.semantic_hash


def test_orch_04_rejects_ambiguous_or_over_limit_workflow_definitions() -> None:
    with pytest.raises(RoutingError, match="at most 20") as over_limit:
        _definition(
            *(_slot(f"slot.{index}", index + 1, "cap.work", "tpl.worker") for index in range(21))
        )
    assert over_limit.value.code == "routing_slot_limit_exceeded"

    with pytest.raises(RoutingError) as duplicate_key:
        _definition(
            _slot("same", 1, "cap.a", "tpl.a"),
            _slot("same", 2, "cap.b", "tpl.b"),
        )
    assert duplicate_key.value.code == "ambiguous_workflow"

    with pytest.raises(RoutingError) as duplicate_priority:
        _slot("work", 1, "cap.work", "tpl.a", "tpl.a")
    assert duplicate_priority.value.code == "ambiguous_workflow"


def test_orch_04_rejects_drift_unsupported_target_and_trigger() -> None:
    templates = [
        TemplateSource("tpl.target", 1, (), ("manual",)),
        TemplateSource("tpl.other", 2, (), ("webhook",)),
    ]
    instances = [
        InstanceSource("inst.target", "tpl.target", 1),
        InstanceSource("inst.disabled", "tpl.target", 2, enabled=False),
        InstanceSource("inst.other", "tpl.other", 3),
    ]
    router = _router(templates, instances, ())

    cases = [
        (_request("inst.unknown"), _definition(), "unsupported_target"),
        (_request("inst.disabled"), _definition(), "unsupported_target"),
        (_request("inst.other"), _definition(), "unsupported_target"),
        (
            _request(trigger_kind=TriggerKind.WEBHOOK),
            _definition(triggers=(TriggerKind.MANUAL, TriggerKind.WEBHOOK)),
            "unsupported_trigger",
        ),
        (
            _request(),
            _definition(catalog_hash="catalog-sha256-v1:" + "b" * 64),
            "catalog_drift",
        ),
    ]
    for request, definition, expected_code in cases:
        with pytest.raises(RoutingError) as failure:
            router.route(request, definition)
        assert failure.value.code == expected_code


def test_orch_04_rejects_missing_capability_and_uncoverable_slot() -> None:
    router = _router(
        [TemplateSource("tpl.target", 1, ()), TemplateSource("tpl.worker", 2, ())],
        [
            InstanceSource("inst.target", "tpl.target", 1),
            InstanceSource("inst.worker", "tpl.worker", 2),
        ],
        ("cap.known",),
    )
    for definition in (
        _definition(_slot("unknown", 1, "cap.unknown", "tpl.worker")),
        _definition(_slot("uncovered", 1, "cap.known", "tpl.worker")),
    ):
        with pytest.raises(RoutingError) as failure:
            router.route(_request(), definition)
        assert failure.value.code == "missing_capability"


def test_orch_04_rejects_ambiguous_or_structurally_drifted_catalog_sources() -> None:
    target_template = TemplateSource("tpl.target", 1, ())
    target = InstanceSource("inst.target", "tpl.target", 1)

    with pytest.raises(RoutingError) as duplicate_template:
        _router([target_template, replace(target_template)], [target], ())
    assert duplicate_template.value.code == "ambiguous_catalog"

    with pytest.raises(RoutingError) as duplicate_instance:
        _router([target_template], [target, replace(target)], ())
    assert duplicate_instance.value.code == "ambiguous_catalog"

    with pytest.raises(RoutingError) as unknown_template:
        _router(
            [target_template],
            [InstanceSource("inst.worker", "tpl.missing", 2)],
            (),
        )
    assert unknown_template.value.code == "catalog_drift"

    with pytest.raises(RoutingError) as unknown_allowlist_capability:
        _router([TemplateSource("tpl.target", 1, ("cap.missing",))], [target], ())
    assert unknown_allowlist_capability.value.code == "catalog_drift"


def test_orch_04_request_and_module_exclude_untrusted_or_adapter_inputs() -> None:
    assert [field.name for field in fields(RoutingRequest)] == [
        "target_instance_id",
        "trigger_id",
        "trigger_source",
        "trigger_kind",
    ]
    router_path = ROOT / "apps/api/src/marketing_agents/application/orchestration/router.py"
    tree = ast.parse(router_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    forbidden = ("infrastructure", "persistence", "repository", "provider", "connector")
    assert not any(part in imported for imported in imports for part in forbidden)
    source = router_path.read_text(encoding="utf-8")
    for forbidden_field in ("payload", "prompt", "chat", "message", "model_output"):
        assert forbidden_field not in {field.name for field in fields(RoutingRequest)}
        assert f"request.{forbidden_field}" not in source
