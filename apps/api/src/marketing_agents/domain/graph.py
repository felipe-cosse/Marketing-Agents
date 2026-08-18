"""Routing-free deterministic dependency-graph domain contract."""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Sequence
from dataclasses import dataclass

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities._validation import require_id

GRAPH_HASH_DOMAIN = b"marketing-agents:dependency-graph:v1\x00"


class DependencyGraphError(ValueError):
    """A stable structural error raised before a workflow can be planned."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TopologyStep:
    """One workflow-local step and its explicit structural dependencies."""

    key: str
    source_order: int
    dependency_keys: tuple[str, ...] = ()
    terminal_result: bool = False

    def __post_init__(self) -> None:
        try:
            require_id(self.key, "topology step key")
        except ValueError as exc:
            raise DependencyGraphError("invalid_step_key", str(exc)) from exc
        if (
            not isinstance(self.source_order, int)
            or isinstance(self.source_order, bool)
            or self.source_order < 1
        ):
            raise DependencyGraphError(
                "invalid_source_order", "topology source order must be a positive integer"
            )
        if not isinstance(self.dependency_keys, tuple):
            raise DependencyGraphError(
                "invalid_dependencies", "topology dependencies must be an immutable tuple"
            )
        if not isinstance(self.terminal_result, bool):
            raise DependencyGraphError(
                "invalid_terminal_flag", "terminal result must be an explicit boolean"
            )

        normalized: list[str] = []
        seen: set[str] = set()
        for dependency_key in self.dependency_keys:
            try:
                require_id(dependency_key, "topology dependency key")
            except ValueError as exc:
                raise DependencyGraphError("invalid_dependency_key", str(exc)) from exc
            if dependency_key == self.key:
                raise DependencyGraphError(
                    "self_dependency", "a topology step cannot depend on itself"
                )
            if dependency_key in seen:
                raise DependencyGraphError(
                    "duplicate_dependency", "a topology dependency may be declared only once"
                )
            seen.add(dependency_key)
            normalized.append(dependency_key)
        object.__setattr__(self, "dependency_keys", tuple(sorted(normalized)))


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """A validated DAG whose identity contains topology but no routing decisions."""

    steps: tuple[TopologyStep, ...]
    topological_order: tuple[str, ...]
    roots: tuple[str, ...]
    terminal_results: tuple[str, ...]
    semantic_hash: str

    @classmethod
    def build(
        cls,
        steps: Sequence[TopologyStep],
        *,
        workflow_max_steps: int,
        global_max_steps: int,
    ) -> DependencyGraph:
        """Validate and normalize an explicit dependency graph deterministically."""

        for limit in (workflow_max_steps, global_max_steps):
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise DependencyGraphError(
                    "invalid_graph_limit", "graph step limits must be positive integers"
                )

        normalized_steps = tuple(sorted(steps, key=lambda item: (item.source_order, item.key)))
        if not normalized_steps:
            raise DependencyGraphError("graph_empty", "a dependency graph must contain a step")
        if len(normalized_steps) > min(workflow_max_steps, global_max_steps):
            raise DependencyGraphError(
                "graph_limit_exceeded", "dependency graph exceeds a configured step limit"
            )

        step_by_key: dict[str, TopologyStep] = {}
        for step in normalized_steps:
            if step.key in step_by_key:
                raise DependencyGraphError(
                    "duplicate_step_key", "topology step keys must be unique"
                )
            step_by_key[step.key] = step

        outgoing: dict[str, set[str]] = {key: set() for key in step_by_key}
        indegree: dict[str, int] = {}
        for step in normalized_steps:
            indegree[step.key] = len(step.dependency_keys)
            for dependency_key in step.dependency_keys:
                if dependency_key not in step_by_key:
                    raise DependencyGraphError(
                        "unknown_dependency", "a dependency refers to an unknown topology step"
                    )
                outgoing[dependency_key].add(step.key)

        ready = [
            (step.source_order, step.key) for step in normalized_steps if indegree[step.key] == 0
        ]
        heapq.heapify(ready)
        ordered_keys: list[str] = []
        while ready:
            _, step_key = heapq.heappop(ready)
            ordered_keys.append(step_key)
            for dependent_key in sorted(
                outgoing[step_key],
                key=lambda key: (step_by_key[key].source_order, key),
            ):
                indegree[dependent_key] -= 1
                if indegree[dependent_key] == 0:
                    dependent = step_by_key[dependent_key]
                    heapq.heappush(ready, (dependent.source_order, dependent.key))
        if len(ordered_keys) != len(normalized_steps):
            raise DependencyGraphError("graph_cycle", "dependency graph contains a cycle")

        roots = tuple(step.key for step in normalized_steps if not step.dependency_keys)
        terminal_results = tuple(step.key for step in normalized_steps if step.terminal_result)
        if not terminal_results:
            raise DependencyGraphError(
                "terminal_result_missing", "dependency graph needs an explicit terminal result"
            )
        if any(outgoing[key] for key in terminal_results):
            raise DependencyGraphError(
                "terminal_has_dependents", "a terminal result must be a graph sink"
            )

        reaches_terminal = set(terminal_results)
        pending = list(terminal_results)
        while pending:
            step_key = pending.pop()
            for dependency_key in step_by_key[step_key].dependency_keys:
                if dependency_key not in reaches_terminal:
                    reaches_terminal.add(dependency_key)
                    pending.append(dependency_key)
        if reaches_terminal != set(step_by_key):
            raise DependencyGraphError(
                "no_terminal_result_path",
                "every topology step must lie on a path to a terminal result",
            )

        semantic_payload = {
            "version": 1,
            "steps": [
                {
                    "key": step.key,
                    "source_order": step.source_order,
                    "dependency_keys": list(step.dependency_keys),
                    "terminal_result": step.terminal_result,
                }
                for step in normalized_steps
            ],
        }
        semantic_hash = hashlib.sha256(
            GRAPH_HASH_DOMAIN + canonical_json_bytes(semantic_payload)
        ).hexdigest()
        return cls(
            steps=normalized_steps,
            topological_order=tuple(ordered_keys),
            roots=roots,
            terminal_results=terminal_results,
            semantic_hash=semantic_hash,
        )

    def step(self, step_key: str) -> TopologyStep:
        """Return one structural step without resolving an agent or capability."""

        for step in self.steps:
            if step.key == step_key:
                return step
        raise DependencyGraphError("unknown_step", "topology step does not exist")

    def ancestors(self, step_key: str) -> frozenset[str]:
        """Return every transitive predecessor, excluding the requested step."""

        target = self.step(step_key)
        ancestors: set[str] = set()
        pending = list(target.dependency_keys)
        while pending:
            dependency_key = pending.pop()
            if dependency_key in ancestors:
                continue
            ancestors.add(dependency_key)
            pending.extend(self.step(dependency_key).dependency_keys)
        return frozenset(ancestors)

    def is_ancestor(self, ancestor_key: str, step_key: str) -> bool:
        """Check a declared transitive dependency without routing side effects."""

        self.step(ancestor_key)
        return ancestor_key in self.ancestors(step_key)
