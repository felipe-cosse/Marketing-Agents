"""Immutable public contracts for deterministic product demos."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.validation import frozen_json_mapping, require_digest, require_id


class DemoScenarioRegistryError(LookupError):
    """Stable scenario-discovery error safe to expose through an API boundary."""

    def __init__(self, code: str, message: str) -> None:
        require_id(code, "demo registry error code")
        super().__init__(message)
        self.code = code


class DemoScenarioInputError(ValueError):
    """Stable scenario-input error with an optional JSON pointer."""

    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        require_id(code, "demo input error code")
        if pointer is not None and (not pointer.startswith("/") or len(pointer) > 1_000):
            raise ValueError("demo input error pointer is invalid")
        super().__init__(message)
        self.code = code
        self.pointer = pointer


@dataclass(frozen=True, slots=True)
class DemoSelectedAgent:
    """One trusted catalog template/instance pair selected by a scenario."""

    instance_id: str
    template_id: str

    def __post_init__(self) -> None:
        require_id(self.instance_id, "demo selected instance ID")
        require_id(self.template_id, "demo selected template ID")


@dataclass(frozen=True, slots=True)
class DemoScenarioStep:
    """One trusted DAG node and its exact capability/effect declaration."""

    key: str
    source_order: int
    dependency_keys: tuple[str, ...]
    terminal_result: bool
    kind: str
    selected_instance_id: str
    capability_id: str
    effect: Literal["read", "write"]

    def __post_init__(self) -> None:
        for value, name in (
            (self.key, "demo step key"),
            (self.kind, "demo step kind"),
            (self.selected_instance_id, "demo step selected instance ID"),
            (self.capability_id, "demo step capability ID"),
        ):
            require_id(value, name)
        if type(self.source_order) is not int or not 1 <= self.source_order <= 10_000:
            raise ValueError("demo step source order must be bounded and positive")
        if (
            type(self.dependency_keys) is not tuple
            or len(self.dependency_keys) > 20
            or len(self.dependency_keys) != len(set(self.dependency_keys))
        ):
            raise ValueError("demo step dependencies must be a bounded unique tuple")
        for dependency in self.dependency_keys:
            require_id(dependency, "demo step dependency key")
        if type(self.terminal_result) is not bool or self.effect not in {"read", "write"}:
            raise ValueError("demo step effect or terminal disposition is invalid")


@dataclass(frozen=True, slots=True)
class DemoScenarioDefinition:
    """Complete trusted definition for one bounded deterministic demo."""

    id: str
    version: int
    display_name: str
    description: str
    selected_agents: tuple[DemoSelectedAgent, ...]
    primary_instance_id: str
    steps: tuple[DemoScenarioStep, ...]
    workflow_id: str
    effect: Literal["read_only", "mutating"]
    input_schema_id: str
    input_schema: Mapping[str, Any] = field(repr=False)
    output_schema_id: str
    output_schema: Mapping[str, Any] = field(repr=False)
    fixture: Mapping[str, Any] = field(repr=False)
    expected_state_path: tuple[str, ...]
    expected_model_calls: int
    expected_connector_calls: int
    expected_external_actions: int
    expected_approvals: int
    safe_submit_verb: str
    definition_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "demo scenario ID"),
            (self.primary_instance_id, "demo primary instance ID"),
            (self.workflow_id, "demo workflow ID"),
            (self.input_schema_id, "demo input schema ID"),
            (self.output_schema_id, "demo output schema ID"),
        ):
            require_id(value, name)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("demo scenario version must be positive")
        for value, name, maximum in (
            (self.display_name, "demo display name", 120),
            (self.description, "demo description", 1_000),
            (self.safe_submit_verb, "demo submit verb", 80),
        ):
            if (
                type(value) is not str
                or value != value.strip()
                or not value
                or len(value) > maximum
            ):
                raise ValueError(f"{name} must be trimmed and bounded")
        verb = self.safe_submit_verb.casefold()
        safe_markers = ("draft", "review", "simulate", "propose", "approve")
        unsafe_markers = ("publish", "send", "subscribe", "enroll", "crm", "cms", "calendar")
        if not any(marker in verb for marker in safe_markers) or any(
            marker in verb for marker in unsafe_markers
        ):
            raise ValueError("demo submit verb must communicate a safe bounded action")
        if self.effect not in {"read_only", "mutating"}:
            raise ValueError("demo scenario effect is unsupported")
        if (
            type(self.selected_agents) is not tuple
            or not self.selected_agents
            or len(self.selected_agents) > 16
            or any(type(agent) is not DemoSelectedAgent for agent in self.selected_agents)
            or len({agent.instance_id for agent in self.selected_agents})
            != len(self.selected_agents)
        ):
            raise ValueError("demo selected agents must be a nonempty unique tuple")
        if self.primary_instance_id not in {agent.instance_id for agent in self.selected_agents}:
            raise ValueError("demo primary instance must be one selected agent")
        selected_instance_ids = {agent.instance_id for agent in self.selected_agents}
        if (
            type(self.steps) is not tuple
            or not self.steps
            or len(self.steps) > 20
            or any(type(step) is not DemoScenarioStep for step in self.steps)
            or any(step.selected_instance_id not in selected_instance_ids for step in self.steps)
        ):
            raise ValueError("demo scenario steps must bind selected agents")
        if self.effect == "read_only" and any(step.effect != "read" for step in self.steps):
            raise ValueError("read-only demo scenarios cannot contain write steps")
        if self.effect == "mutating" and all(step.effect != "write" for step in self.steps):
            raise ValueError("mutating demo scenarios require at least one write step")
        DependencyGraph.build(
            tuple(
                TopologyStep(
                    step.key,
                    step.source_order,
                    step.dependency_keys,
                    terminal_result=step.terminal_result,
                )
                for step in self.steps
            ),
            workflow_max_steps=20,
            global_max_steps=20,
        )
        allowed_states = {
            "received",
            "validated",
            "planned",
            "awaiting_approval",
            "executing",
            "completed",
            "failed",
            "cancelled",
            "rejected",
        }
        if (
            type(self.expected_state_path) is not tuple
            or not 2 <= len(self.expected_state_path) <= 16
            or self.expected_state_path[0] != "received"
            or any(state not in allowed_states for state in self.expected_state_path)
        ):
            raise ValueError("demo state path is invalid")
        expected_counts = (
            self.expected_model_calls,
            self.expected_connector_calls,
            self.expected_external_actions,
            self.expected_approvals,
        )
        if any(type(value) is not int or not 0 <= value <= 10_000 for value in expected_counts):
            raise ValueError("demo expected call counts must be bounded nonnegative integers")

        input_schema = frozen_json_mapping(self.input_schema, "demo input schema")
        output_schema = frozen_json_mapping(self.output_schema, "demo output schema")
        fixture = frozen_json_mapping(self.fixture, "demo fixture")
        if input_schema.get("$id") != self.input_schema_id:
            raise ValueError("demo input schema ID differs from its definition")
        if output_schema.get("$id") != self.output_schema_id:
            raise ValueError("demo output schema ID differs from its definition")
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "output_schema", output_schema)
        object.__setattr__(self, "fixture", fixture)
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "id": self.id,
                    "version": self.version,
                    "display_name": self.display_name,
                    "description": self.description,
                    "safe_submit_verb": self.safe_submit_verb,
                    "selected_agents": tuple(
                        {
                            "instance_id": agent.instance_id,
                            "template_id": agent.template_id,
                        }
                        for agent in self.selected_agents
                    ),
                    "primary_instance_id": self.primary_instance_id,
                    "steps": tuple(
                        {
                            "key": step.key,
                            "source_order": step.source_order,
                            "dependency_keys": step.dependency_keys,
                            "terminal_result": step.terminal_result,
                            "kind": step.kind,
                            "selected_instance_id": step.selected_instance_id,
                            "capability_id": step.capability_id,
                            "effect": step.effect,
                        }
                        for step in self.steps
                    ),
                    "workflow_id": self.workflow_id,
                    "effect": self.effect,
                    "input_schema": input_schema,
                    "output_schema": output_schema,
                    "fixture": fixture,
                    "expected_state_path": self.expected_state_path,
                    "expected_counts": expected_counts,
                }
            )
        ).hexdigest()
        require_digest(digest, "demo definition hash")
        object.__setattr__(self, "definition_hash", digest)

    @property
    def instance_id(self) -> str:
        """Compatibility convenience for the primary manual-admission instance."""

        return self.primary_instance_id

    @property
    def template_id(self) -> str:
        """Compatibility convenience for the primary manual-admission template."""

        for agent in self.selected_agents:
            if agent.instance_id == self.primary_instance_id:
                return agent.template_id
        raise AssertionError("validated primary demo instance lost its template")


__all__ = [
    "DemoScenarioDefinition",
    "DemoScenarioInputError",
    "DemoScenarioRegistryError",
    "DemoScenarioStep",
    "DemoSelectedAgent",
]
