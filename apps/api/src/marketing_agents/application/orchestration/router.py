"""Pure deterministic instance routing over structural catalog snapshots."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities._validation import require_id
from marketing_agents.domain.enums import TriggerKind

ROUTING_HASH_DOMAIN = b"marketing-agents:deterministic-instance-routing:v1\x00"
_CATALOG_HASH = re.compile(r"^catalog-sha256-v1:[0-9a-f]{64}$")
_MAX_ROUTING_SLOTS = 20


class RoutingError(ValueError):
    """A stable fail-closed routing error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RoutingTemplateSource(Protocol):
    """Structural catalog view required from a template source."""

    @property
    def id(self) -> str: ...

    @property
    def display_order(self) -> int: ...

    @property
    def allowed_tool_capability_ids(self) -> Sequence[str]: ...

    @property
    def supported_trigger_types(self) -> Sequence[str]: ...


class RoutingInstanceVariantSource(Protocol):
    """Structural deployment variant view used only for stable ordering."""

    @property
    def source_ordinal(self) -> int: ...


class RoutingInstanceSource(Protocol):
    """Structural catalog view required from an instance source."""

    @property
    def id(self) -> str: ...

    @property
    def template_id(self) -> str: ...

    @property
    def display_order(self) -> int: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def variant(self) -> RoutingInstanceVariantSource | None: ...

    @property
    def configuration_revision(self) -> int: ...


@dataclass(frozen=True, slots=True)
class RoutingSlot:
    """One workflow role and its ordered template preferences."""

    key: str
    source_order: int
    template_priorities: tuple[str, ...]
    required_capability_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_stable_id(self.key, "routing slot key", "invalid_slot")
        _require_positive_int(self.source_order, "routing slot source order", "invalid_slot")
        _require_id_tuple(
            self.required_capability_ids,
            "routing slot required capabilities",
            "invalid_slot",
            allow_empty=True,
        )
        _require_id_tuple(
            self.template_priorities,
            "routing slot template priorities",
            "invalid_slot",
            allow_empty=False,
        )


@dataclass(frozen=True, slots=True)
class WorkflowRoutingDefinition:
    """Trusted workflow-owned routing policy pinned to one catalog release."""

    workflow_id: str
    workflow_version: int
    catalog_content_hash: str
    eligible_trigger_kinds: tuple[TriggerKind, ...]
    eligible_target_template_ids: tuple[str, ...]
    required_slots: tuple[RoutingSlot, ...] = ()

    def __post_init__(self) -> None:
        _require_stable_id(self.workflow_id, "workflow ID", "invalid_workflow")
        _require_positive_int(self.workflow_version, "workflow version", "invalid_workflow")
        if _CATALOG_HASH.fullmatch(self.catalog_content_hash) is None:
            raise RoutingError(
                "invalid_catalog_hash",
                "workflow routing must pin a canonical compiled-catalog hash",
            )
        if not isinstance(self.eligible_trigger_kinds, tuple) or not self.eligible_trigger_kinds:
            raise RoutingError(
                "invalid_workflow", "eligible trigger kinds must be a nonempty immutable tuple"
            )
        if any(not isinstance(kind, TriggerKind) for kind in self.eligible_trigger_kinds):
            raise RoutingError(
                "invalid_workflow", "eligible trigger kinds must use TriggerKind values"
            )
        if len(self.eligible_trigger_kinds) != len(set(self.eligible_trigger_kinds)):
            raise RoutingError(
                "ambiguous_workflow", "eligible trigger kinds may be declared only once"
            )
        _require_id_tuple(
            self.eligible_target_template_ids,
            "eligible target templates",
            "invalid_workflow",
            allow_empty=False,
        )
        if not isinstance(self.required_slots, tuple):
            raise RoutingError(
                "invalid_workflow", "required routing slots must be an immutable tuple"
            )
        if any(not isinstance(slot, RoutingSlot) for slot in self.required_slots):
            raise RoutingError(
                "invalid_workflow", "required routing slots must use RoutingSlot values"
            )
        if len(self.required_slots) > _MAX_ROUTING_SLOTS:
            raise RoutingError(
                "routing_slot_limit_exceeded",
                f"a workflow may declare at most {_MAX_ROUTING_SLOTS} routing slots",
            )
        keys = [slot.key for slot in self.required_slots]
        orders = [slot.source_order for slot in self.required_slots]
        if len(keys) != len(set(keys)) or len(orders) != len(set(orders)):
            raise RoutingError(
                "ambiguous_workflow",
                "routing slot keys and source orders must be unique",
            )
        object.__setattr__(
            self,
            "required_slots",
            tuple(sorted(self.required_slots, key=lambda slot: (slot.source_order, slot.key))),
        )


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """Explicit trusted routing metadata; content and model output are excluded."""

    target_instance_id: str
    trigger_id: str
    trigger_source: str
    trigger_kind: TriggerKind

    def __post_init__(self) -> None:
        _require_stable_id(self.target_instance_id, "target instance ID", "invalid_request")
        _require_stable_id(self.trigger_id, "trigger ID", "invalid_request")
        _require_stable_id(self.trigger_source, "trigger source", "invalid_request")
        if not isinstance(self.trigger_kind, TriggerKind):
            raise RoutingError("invalid_request", "trigger kind must use TriggerKind")


@dataclass(frozen=True, slots=True)
class SelectedInstanceSnapshot:
    """Immutable configuration identity retained by a deterministic plan."""

    instance_id: str
    template_id: str
    configuration_revision: int
    display_order: int
    source_ordinal: int | None


@dataclass(frozen=True, slots=True)
class RoutingAssignment:
    """One routing slot assigned to one selected instance snapshot."""

    slot_key: str
    instance_id: str
    template_id: str
    required_capability_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """Minimal selected instance set and deterministic slot assignments."""

    workflow_id: str
    workflow_version: int
    catalog_content_hash: str
    target_instance_id: str
    selected_instances: tuple[SelectedInstanceSnapshot, ...]
    assignments: tuple[RoutingAssignment, ...]
    semantic_hash: str


@dataclass(frozen=True, slots=True)
class _TemplateSnapshot:
    id: str
    display_order: int
    allowed_capabilities: frozenset[str]
    supported_triggers: frozenset[TriggerKind]


@dataclass(frozen=True, slots=True)
class _InstanceSnapshot:
    id: str
    template_id: str
    display_order: int
    enabled: bool
    source_ordinal: int | None
    configuration_revision: int

    def public_snapshot(self) -> SelectedInstanceSnapshot:
        return SelectedInstanceSnapshot(
            instance_id=self.id,
            template_id=self.template_id,
            configuration_revision=self.configuration_revision,
            display_order=self.display_order,
            source_ordinal=self.source_ordinal,
        )


_AssignmentKey = tuple[int, int, int, str]
_UNASSIGNED_PRIORITY = 2**31 - 1


@dataclass(frozen=True, slots=True)
class _CoverState:
    selection: tuple[_InstanceSnapshot, ...]
    assignment_priorities: tuple[int, ...]


class DeterministicInstanceRouter:
    """Snapshot structural sources and solve exact minimum instance set cover."""

    def __init__(
        self,
        *,
        catalog_content_hash: str,
        templates: Sequence[RoutingTemplateSource],
        instances: Sequence[RoutingInstanceSource],
        capability_ids: Collection[str],
    ) -> None:
        if _CATALOG_HASH.fullmatch(catalog_content_hash) is None:
            raise RoutingError("invalid_catalog_hash", "catalog hash is not canonical")
        self._catalog_content_hash = catalog_content_hash
        self._capability_ids = _snapshot_capabilities(capability_ids)
        self._templates = _snapshot_templates(templates, self._capability_ids)
        self._instances = _snapshot_instances(instances, self._templates)

    def route(
        self,
        request: RoutingRequest,
        definition: WorkflowRoutingDefinition,
    ) -> RoutingResult:
        """Return the globally minimal additional set under deterministic tie breaks."""

        if definition.catalog_content_hash != self._catalog_content_hash:
            raise RoutingError(
                "catalog_drift", "workflow and router catalog snapshots do not match"
            )
        if request.trigger_kind not in definition.eligible_trigger_kinds:
            raise RoutingError(
                "unsupported_trigger", "workflow does not support the requested trigger kind"
            )
        self._validate_definition_references(definition)

        target = self._instances.get(request.target_instance_id)
        if target is None:
            raise RoutingError("unsupported_target", "requested target instance does not exist")
        target_template = self._templates[target.template_id]
        if not target.enabled:
            raise RoutingError("unsupported_target", "requested target instance is disabled")
        if target.template_id not in definition.eligible_target_template_ids:
            raise RoutingError(
                "unsupported_target", "target template is not eligible for this workflow"
            )
        if request.trigger_kind not in target_template.supported_triggers:
            raise RoutingError(
                "unsupported_trigger", "target template does not support the trigger kind"
            )

        slots = definition.required_slots
        target_mask = self._coverage_mask(target, slots)
        full_mask = (1 << len(slots)) - 1
        additional_candidates = tuple(
            instance
            for instance in sorted(self._instances.values(), key=_instance_order)
            if instance.id != target.id
            and instance.enabled
            and self._coverage_mask(instance, slots)
        )
        selected_additional = self._minimum_cover(
            target=target,
            target_mask=target_mask,
            full_mask=full_mask,
            candidates=additional_candidates,
            slots=slots,
        )
        selected = (target, *selected_additional)
        assignments = self._assign_slots(selected, slots)
        public_selected = (
            target.public_snapshot(),
            *(item.public_snapshot() for item in selected_additional),
        )
        semantic_payload = {
            "version": 1,
            "workflow": {
                "id": definition.workflow_id,
                "version": definition.workflow_version,
                "catalog_content_hash": definition.catalog_content_hash,
            },
            "request": {
                "target_instance_id": request.target_instance_id,
                "trigger_id": request.trigger_id,
                "trigger_source": request.trigger_source,
                "trigger_kind": request.trigger_kind.value,
            },
            "slots": [
                {
                    "key": slot.key,
                    "source_order": slot.source_order,
                    "required_capability_ids": list(slot.required_capability_ids),
                    "template_priorities": list(slot.template_priorities),
                }
                for slot in slots
            ],
            "selected_instances": [
                {
                    "instance_id": item.instance_id,
                    "template_id": item.template_id,
                    "configuration_revision": item.configuration_revision,
                    "display_order": item.display_order,
                    "source_ordinal": item.source_ordinal,
                }
                for item in public_selected
            ],
            "assignments": [
                {
                    "slot_key": item.slot_key,
                    "instance_id": item.instance_id,
                    "template_id": item.template_id,
                    "required_capability_ids": list(item.required_capability_ids),
                }
                for item in assignments
            ],
        }
        semantic_hash = hashlib.sha256(
            ROUTING_HASH_DOMAIN + canonical_json_bytes(semantic_payload)
        ).hexdigest()
        return RoutingResult(
            workflow_id=definition.workflow_id,
            workflow_version=definition.workflow_version,
            catalog_content_hash=self._catalog_content_hash,
            target_instance_id=target.id,
            selected_instances=public_selected,
            assignments=assignments,
            semantic_hash=semantic_hash,
        )

    def _validate_definition_references(self, definition: WorkflowRoutingDefinition) -> None:
        for template_id in definition.eligible_target_template_ids:
            if template_id not in self._templates:
                raise RoutingError(
                    "catalog_drift", "workflow target template is absent from the catalog"
                )
        for slot in definition.required_slots:
            missing_capabilities = set(slot.required_capability_ids) - self._capability_ids
            if missing_capabilities:
                raise RoutingError(
                    "missing_capability",
                    "routing slot requires a capability absent from the catalog",
                )
            if any(template_id not in self._templates for template_id in slot.template_priorities):
                raise RoutingError(
                    "catalog_drift", "routing slot references a template absent from the catalog"
                )

    def _coverage_mask(
        self,
        instance: _InstanceSnapshot,
        slots: tuple[RoutingSlot, ...],
    ) -> int:
        template = self._templates[instance.template_id]
        mask = 0
        for index, slot in enumerate(slots):
            if (
                instance.template_id in slot.template_priorities
                and set(slot.required_capability_ids) <= template.allowed_capabilities
            ):
                mask |= 1 << index
        return mask

    def _minimum_cover(
        self,
        *,
        target: _InstanceSnapshot,
        target_mask: int,
        full_mask: int,
        candidates: tuple[_InstanceSnapshot, ...],
        slots: tuple[RoutingSlot, ...],
    ) -> tuple[_InstanceSnapshot, ...]:
        if target_mask == full_mask:
            return ()
        coverage = {candidate.id: self._coverage_mask(candidate, slots) for candidate in candidates}
        initial_priorities = self._priority_vector(target, slots)
        states: dict[int, list[_CoverState]] = {
            target_mask: [_CoverState(selection=(), assignment_priorities=initial_priorities)]
        }
        for candidate in candidates:
            candidate_mask = coverage[candidate.id]
            candidate_priorities = self._priority_vector(candidate, slots)
            for covered, cover_states in tuple(states.items()):
                combined = covered | candidate_mask
                if combined == covered:
                    continue
                for state in tuple(cover_states):
                    proposed = _CoverState(
                        selection=(*state.selection, candidate),
                        assignment_priorities=tuple(
                            min(current, new)
                            for current, new in zip(
                                state.assignment_priorities,
                                candidate_priorities,
                                strict=True,
                            )
                        ),
                    )
                    self._retain_exact_state(states, combined, proposed)
        final_states = states.get(full_mask)
        if final_states is None:
            raise RoutingError(
                "missing_capability",
                "enabled eligible instances cannot cover every required routing slot",
            )
        final_state = min(
            final_states,
            key=lambda state: (
                state.assignment_priorities,
                _selection_order(state.selection),
            ),
        )
        return tuple(sorted(final_state.selection, key=_instance_order))

    @staticmethod
    def _retain_exact_state(
        states: dict[int, list[_CoverState]],
        mask: int,
        proposed: _CoverState,
    ) -> None:
        """Retain the exact Pareto frontier needed by lexicographic slot ties.

        States with the same mask have identical future coverage choices. Adding a
        candidate applies component-wise ``min`` to template-priority ranks, while
        the same later instance extends both canonically ordered selections. A state
        is therefore pruned only when it loses on both dimensions. Count is retained
        first because the same future additions preserve a count deficit.
        """

        current = states.get(mask)
        if current is None:
            states[mask] = [proposed]
            return
        minimum_count = len(current[0].selection)
        proposed_count = len(proposed.selection)
        if proposed_count > minimum_count:
            return
        if proposed_count < minimum_count:
            states[mask] = [proposed]
            return
        if any(_cover_state_dominates(item, proposed) for item in current):
            return
        states[mask] = [item for item in current if not _cover_state_dominates(proposed, item)]
        states[mask].append(proposed)

    def _priority_vector(
        self,
        instance: _InstanceSnapshot,
        slots: tuple[RoutingSlot, ...],
    ) -> tuple[int, ...]:
        priorities: list[int] = []
        for slot in slots:
            priorities.append(
                slot.template_priorities.index(instance.template_id)
                if self._coverage_mask(instance, (slot,)) == 1
                else _UNASSIGNED_PRIORITY
            )
        return tuple(priorities)

    def _assign_slots(
        self,
        selected: tuple[_InstanceSnapshot, ...],
        slots: tuple[RoutingSlot, ...],
    ) -> tuple[RoutingAssignment, ...]:
        assignments: list[RoutingAssignment] = []
        for slot in slots:
            eligible = [item for item in selected if self._coverage_mask(item, (slot,)) == 1]
            if not eligible:
                raise RoutingError(
                    "missing_capability", "selected instances do not cover a routing slot"
                )
            chosen = min(eligible, key=lambda item: self._assignment_key(item, slot))
            assignments.append(
                RoutingAssignment(
                    slot_key=slot.key,
                    instance_id=chosen.id,
                    template_id=chosen.template_id,
                    required_capability_ids=slot.required_capability_ids,
                )
            )
        return tuple(assignments)

    @staticmethod
    def _assignment_key(
        instance: _InstanceSnapshot, slot: RoutingSlot
    ) -> tuple[int, int, int, str]:
        return (
            slot.template_priorities.index(instance.template_id),
            instance.display_order,
            instance.source_ordinal or 0,
            instance.id,
        )


def _snapshot_capabilities(capability_ids: Collection[str]) -> frozenset[str]:
    if isinstance(capability_ids, str):
        raise RoutingError("ambiguous_catalog", "capability IDs must be a collection")
    values = tuple(capability_ids)
    if len(values) != len(set(values)):
        raise RoutingError("ambiguous_catalog", "catalog capability IDs must be unique")
    for capability_id in values:
        _require_stable_id(capability_id, "capability ID", "catalog_drift")
    return frozenset(values)


def _snapshot_templates(
    sources: Sequence[RoutingTemplateSource], capability_ids: frozenset[str]
) -> dict[str, _TemplateSnapshot]:
    snapshots: dict[str, _TemplateSnapshot] = {}
    for source in sources:
        template_id = source.id
        _require_stable_id(template_id, "template ID", "catalog_drift")
        if template_id in snapshots:
            raise RoutingError("ambiguous_catalog", "catalog template IDs must be unique")
        _require_positive_int(source.display_order, "template display order", "catalog_drift")
        allowed = tuple(source.allowed_tool_capability_ids)
        if len(allowed) != len(set(allowed)):
            raise RoutingError("ambiguous_catalog", "template capability allowlist must be unique")
        for capability_id in allowed:
            _require_stable_id(capability_id, "template capability ID", "catalog_drift")
        if not set(allowed) <= capability_ids:
            raise RoutingError(
                "catalog_drift", "template allowlist references an unknown capability"
            )
        triggers: list[TriggerKind] = []
        for raw_trigger in source.supported_trigger_types:
            try:
                trigger = TriggerKind(raw_trigger)
            except ValueError as exc:
                raise RoutingError(
                    "catalog_drift", "template has an unsupported trigger kind"
                ) from exc
            triggers.append(trigger)
        if not triggers or len(triggers) != len(set(triggers)):
            raise RoutingError(
                "ambiguous_catalog", "template trigger support must be nonempty and unique"
            )
        snapshots[template_id] = _TemplateSnapshot(
            id=template_id,
            display_order=source.display_order,
            allowed_capabilities=frozenset(allowed),
            supported_triggers=frozenset(triggers),
        )
    return snapshots


def _snapshot_instances(
    sources: Sequence[RoutingInstanceSource], templates: dict[str, _TemplateSnapshot]
) -> dict[str, _InstanceSnapshot]:
    snapshots: dict[str, _InstanceSnapshot] = {}
    for source in sources:
        instance_id = source.id
        _require_stable_id(instance_id, "instance ID", "catalog_drift")
        if instance_id in snapshots:
            raise RoutingError("ambiguous_catalog", "catalog instance IDs must be unique")
        _require_stable_id(source.template_id, "instance template ID", "catalog_drift")
        if source.template_id not in templates:
            raise RoutingError("catalog_drift", "instance template is absent from the catalog")
        _require_positive_int(source.display_order, "instance display order", "catalog_drift")
        if not isinstance(source.enabled, bool):
            raise RoutingError("catalog_drift", "instance enabled state must be explicit")
        _require_positive_int(
            source.configuration_revision, "configuration revision", "catalog_drift"
        )
        ordinal = None if source.variant is None else source.variant.source_ordinal
        if ordinal is not None:
            _require_positive_int(ordinal, "instance source ordinal", "catalog_drift")
        snapshots[instance_id] = _InstanceSnapshot(
            id=instance_id,
            template_id=source.template_id,
            display_order=source.display_order,
            enabled=source.enabled,
            source_ordinal=ordinal,
            configuration_revision=source.configuration_revision,
        )
    return snapshots


def _instance_order(instance: _InstanceSnapshot) -> tuple[int, int, str]:
    return (instance.display_order, instance.source_ordinal or 0, instance.id)


def _selection_order(
    selection: tuple[_InstanceSnapshot, ...],
) -> tuple[tuple[int, int, str], ...]:
    return tuple(_instance_order(item) for item in selection)


def _cover_state_dominates(left: _CoverState, right: _CoverState) -> bool:
    """Return whether future candidates cannot make ``right`` win a tie."""

    priority_dominates = all(
        first <= second
        for first, second in zip(
            left.assignment_priorities,
            right.assignment_priorities,
            strict=True,
        )
    )
    return priority_dominates and _selection_order(left.selection) <= _selection_order(
        right.selection
    )


def _require_stable_id(value: str, field: str, code: str) -> None:
    try:
        require_id(value, field)
    except ValueError as exc:
        raise RoutingError(code, str(exc)) from exc


def _require_positive_int(value: int, field: str, code: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RoutingError(code, f"{field} must be a positive integer")


def _require_id_tuple(
    values: tuple[str, ...],
    field: str,
    code: str,
    *,
    allow_empty: bool,
) -> None:
    if not isinstance(values, tuple) or (not values and not allow_empty):
        raise RoutingError(code, f"{field} must be a nonempty immutable tuple")
    if len(values) != len(set(values)):
        raise RoutingError("ambiguous_workflow", f"{field} may not contain duplicates")
    for value in values:
        _require_stable_id(value, field, code)
