"""ORCH-03: workflows retain an explicit routing-free dependency graph."""

from __future__ import annotations

from dataclasses import fields

import pytest
from marketing_agents.domain.graph import (
    DependencyGraph,
    DependencyGraphError,
    TopologyStep,
)


def _workflow_steps() -> tuple[TopologyStep, ...]:
    return (
        TopologyStep("collect", 1),
        TopologyStep("research", 2, ("collect",)),
        TopologyStep("draft", 3, ("research",)),
        TopologyStep("review", 4, ("research",)),
        TopologyStep("result", 5, ("review", "draft"), terminal_result=True),
    )


def _build(steps: tuple[TopologyStep, ...]) -> DependencyGraph:
    return DependencyGraph.build(steps, workflow_max_steps=10, global_max_steps=20)


def test_orch_03_builds_explicit_graph_with_deterministic_topology_and_ancestors() -> None:
    graph = _build(_workflow_steps())

    assert graph.roots == ("collect",)
    assert graph.topological_order == ("collect", "research", "draft", "review", "result")
    assert graph.terminal_results == ("result",)
    assert graph.step("result").dependency_keys == ("draft", "review")
    assert graph.ancestors("result") == {"collect", "research", "draft", "review"}
    assert graph.is_ancestor("research", "result")
    assert not graph.is_ancestor("draft", "review")
    assert len(graph.semantic_hash) == 64
    assert set(graph.semantic_hash) <= set("0123456789abcdef")


def test_orch_03_input_and_dependency_order_do_not_change_semantic_graph() -> None:
    original = _build(_workflow_steps())
    shuffled = _build(
        (
            TopologyStep("result", 5, ("draft", "review"), terminal_result=True),
            TopologyStep("review", 4, ("research",)),
            TopologyStep("collect", 1),
            TopologyStep("draft", 3, ("research",)),
            TopologyStep("research", 2, ("collect",)),
        )
    )
    changed_edge = _build(
        (
            TopologyStep("collect", 1),
            TopologyStep("research", 2, ("collect",)),
            TopologyStep("draft", 3, ("research",)),
            TopologyStep("review", 4, ("collect",)),
            TopologyStep("result", 5, ("draft", "review"), terminal_result=True),
        )
    )
    changed_order = _build(
        (
            TopologyStep("collect", 1),
            TopologyStep("research", 2, ("collect",)),
            TopologyStep("draft", 30, ("research",)),
            TopologyStep("review", 4, ("research",)),
            TopologyStep("result", 50, ("draft", "review"), terminal_result=True),
        )
    )

    assert shuffled.steps == original.steps
    assert shuffled.topological_order == original.topological_order
    assert shuffled.semantic_hash == original.semantic_hash
    assert changed_edge.semantic_hash != original.semantic_hash
    assert changed_order.semantic_hash != original.semantic_hash


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (
            lambda: _build(
                (TopologyStep("same", 1), TopologyStep("same", 2, terminal_result=True))
            ),
            "duplicate_step_key",
        ),
        (
            lambda: _build((TopologyStep("result", 1, ("missing",), terminal_result=True),)),
            "unknown_dependency",
        ),
        (lambda: TopologyStep("self", 1, ("self",)), "self_dependency"),
        (
            lambda: TopologyStep("duplicate", 1, ("parent", "parent")),
            "duplicate_dependency",
        ),
        (
            lambda: _build(
                (
                    TopologyStep("left", 1, ("right",)),
                    TopologyStep("right", 2, ("left",), terminal_result=True),
                )
            ),
            "graph_cycle",
        ),
        (
            lambda: DependencyGraph.build(
                (TopologyStep("one", 1), TopologyStep("two", 2, ("one",), True)),
                workflow_max_steps=1,
                global_max_steps=2,
            ),
            "graph_limit_exceeded",
        ),
        (
            lambda: DependencyGraph.build(
                (TopologyStep("result", 1, terminal_result=True),),
                workflow_max_steps=0,
                global_max_steps=1,
            ),
            "invalid_graph_limit",
        ),
    ],
)
def test_orch_03_duplicate_unknown_self_cycle_and_limits_fail_closed(
    factory: object, code: str
) -> None:
    with pytest.raises(DependencyGraphError) as captured:
        factory()  # type: ignore[operator]
    assert captured.value.code == code


@pytest.mark.parametrize(
    ("steps", "code"),
    [
        ((TopologyStep("only", 1),), "terminal_result_missing"),
        (
            (
                TopologyStep("premature", 1, terminal_result=True),
                TopologyStep("result", 2, ("premature",), terminal_result=True),
            ),
            "terminal_has_dependents",
        ),
        (
            (
                TopologyStep("root", 1),
                TopologyStep("result", 2, ("root",), terminal_result=True),
                TopologyStep("dead-root", 3),
                TopologyStep("dead-end", 4, ("dead-root",)),
            ),
            "no_terminal_result_path",
        ),
    ],
)
def test_orch_03_missing_terminal_dependent_terminal_and_dead_branch_fail_closed(
    steps: tuple[TopologyStep, ...], code: str
) -> None:
    with pytest.raises(DependencyGraphError) as captured:
        _build(steps)
    assert captured.value.code == code


def test_orch_03_graph_contract_contains_no_agent_selection_or_routing_fields() -> None:
    assert {field.name for field in fields(TopologyStep)} == {
        "key",
        "source_order",
        "dependency_keys",
        "terminal_result",
    }
    forbidden = {
        "agent_id",
        "instance_id",
        "template_id",
        "capability_id",
        "effect",
        "connector_family",
        "model",
    }
    assert forbidden.isdisjoint(field.name for field in fields(TopologyStep))
