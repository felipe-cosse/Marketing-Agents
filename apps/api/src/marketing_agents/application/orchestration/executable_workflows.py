"""Immutable exact-match contracts shared by admission and worker composition.

Read-role result payloads are observations, never dispatch authority. Explicit
write workflows retain a distinct handler and still require per-action approval.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from marketing_agents.application.policies.json_schema import compile_json_schema
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.domain.validation import frozen_json_mapping, require_digest, require_id

if TYPE_CHECKING:
    from marketing_agents.application.services.incoming_work_validation import (
        WorkflowAdmissionDefinition,
    )

_HASH_DOMAIN = b"marketing-agents:executable-catalog-workflow:v1\x00"


class ExecutableWorkflowRegistryError(ValueError):
    """A payload-safe registry lookup or contract mismatch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExecutableWorkflowHandler(StrEnum):
    CATALOG_ROLE = "catalog_role"
    CATALOG_WRITE = "catalog_write"
    DEMO_READ = "demo_read"
    DEMO_EMAIL = "demo_email"


class CatalogRoleExecutionKind(StrEnum):
    MODEL = "model"
    LOCAL_TRANSFORM = "local_transform"
    WRITE = "write"


def catalog_role_workflow_id(template_id: str, trigger_kind: TriggerKind) -> str:
    """Retain existing manual/webhook IDs; schedule has its own exact identity."""
    require_id(template_id, "workflow template ID")
    if not template_id.startswith("tpl.") or type(trigger_kind) is not TriggerKind:
        raise ValueError("catalog workflow requires an exact template and trigger kind")
    identifier = f"workflow.{trigger_kind.value}.{template_id.removeprefix('tpl.')}.v1"
    require_id(identifier, "catalog workflow ID")
    return identifier


@dataclass(frozen=True, slots=True)
class ExecutableWorkflowDefinition:
    id: str
    version: int
    catalog_content_hash: str
    handler_kind: ExecutableWorkflowHandler
    template_id: str
    target_instance_ids: tuple[str, ...]
    eligible_trigger_kinds: tuple[TriggerKind, ...]
    allowed_modes: tuple[WorkMode, ...]
    input_schema_id: str
    input_schema: Mapping[str, Any] = field(repr=False)
    output_schema_id: str
    output_schema: Mapping[str, Any] = field(repr=False)
    execution_kind: CatalogRoleExecutionKind | None = None
    capability_id: str | None = None
    expected_model_calls: int = 0
    # None denotes mode-dependent write expectations, not a zero-call claim.
    expected_connector_calls: int | None = 0
    expected_external_actions: int | None = 0
    expected_approvals: int | None = 0
    expected_steps: int = 1
    demo_scenario_id: str | None = None
    source_definition_hash: str | None = field(default=None, repr=False)
    input_schema_hash: str = field(init=False)
    output_schema_hash: str = field(init=False)
    definition_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "executable workflow ID"),
            (self.template_id, "executable workflow template ID"),
            (self.input_schema_id, "workflow input schema ID"),
            (self.output_schema_id, "workflow output schema ID"),
        ):
            require_id(value, name)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("workflow version must be a positive integer")
        if not self.catalog_content_hash.startswith("catalog-sha256-v1:"):
            raise ValueError("workflow catalog hash version is invalid")
        require_digest(self.catalog_content_hash.removeprefix("catalog-sha256-v1:"), "catalog hash")
        if type(self.handler_kind) is not ExecutableWorkflowHandler:
            raise ValueError("workflow handler must use the exact enum")
        for values, item_type, name in (
            (self.target_instance_ids, str, "workflow target instances"),
            (self.eligible_trigger_kinds, TriggerKind, "workflow trigger kinds"),
            (self.allowed_modes, WorkMode, "workflow modes"),
        ):
            if (
                type(values) is not tuple
                or not values
                or any(type(item) is not item_type for item in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{name} must be a nonempty exact unique tuple")
        for instance_id in self.target_instance_ids:
            require_id(instance_id, "workflow target instance ID")
        if type(self.expected_model_calls) is not int or not 0 <= self.expected_model_calls <= 100:
            raise ValueError("workflow model count must be a bounded integer")
        for count in (
            self.expected_connector_calls,
            self.expected_external_actions,
            self.expected_approvals,
        ):
            if count is None and self.handler_kind is ExecutableWorkflowHandler.CATALOG_WRITE:
                continue
            if type(count) is not int or not 0 <= count <= 100:
                raise ValueError("workflow operation counts must be bounded integers")
        if type(self.expected_steps) is not int or not 1 <= self.expected_steps <= 100:
            raise ValueError("workflow step count must be bounded and positive")
        if self.capability_id is not None:
            require_id(self.capability_id, "workflow capability ID")
        if self.handler_kind in {
            ExecutableWorkflowHandler.CATALOG_ROLE,
            ExecutableWorkflowHandler.CATALOG_WRITE,
        }:
            self._validate_catalog_role()
        elif (
            self.demo_scenario_id is None
            or self.source_definition_hash is None
            or self.execution_kind is not None
            or self.capability_id is not None
            or len(self.target_instance_ids) != 1
            or self.eligible_trigger_kinds != (TriggerKind.MANUAL,)
        ):
            raise ValueError("demo workflow requires its original exact scenario contract")
        else:
            require_id(self.demo_scenario_id, "workflow demo scenario ID")
            require_digest(self.source_definition_hash, "demo workflow definition hash")
        for attribute in ("input", "output"):
            schema = frozen_json_mapping(getattr(self, f"{attribute}_schema"), "workflow schema")
            schema_id = getattr(self, f"{attribute}_schema_id")
            if schema.get("$id") != schema_id:
                raise ValueError("workflow schema must retain its exact embedded identity")
            compile_json_schema(schema, expected_schema_id=schema_id)
            object.__setattr__(self, f"{attribute}_schema", schema)
            object.__setattr__(self, f"{attribute}_schema_hash", canonical_schema_hash(schema))
        material = {
            "id": self.id,
            "version": self.version,
            "catalog_content_hash": self.catalog_content_hash,
            "handler_kind": self.handler_kind.value,
            "template_id": self.template_id,
            "target_instance_ids": sorted(self.target_instance_ids),
            "trigger_kinds": sorted(kind.value for kind in self.eligible_trigger_kinds),
            "modes": sorted(mode.value for mode in self.allowed_modes),
            "input_schema_id": self.input_schema_id,
            "input_schema_hash": self.input_schema_hash,
            "output_schema_id": self.output_schema_id,
            "output_schema_hash": self.output_schema_hash,
            "execution_kind": None if self.execution_kind is None else self.execution_kind.value,
            "capability_id": self.capability_id,
            "expected_steps": self.expected_steps,
            "expected_model_calls": self.expected_model_calls,
            "expected_connector_calls": self.expected_connector_calls,
            "expected_external_actions": self.expected_external_actions,
            "expected_approvals": self.expected_approvals,
        }
        object.__setattr__(
            self,
            "definition_hash",
            self.source_definition_hash
            or hashlib.sha256(_HASH_DOMAIN + canonical_json_bytes(material)).hexdigest(),
        )

    def _validate_catalog_role(self) -> None:
        if (
            self.demo_scenario_id is not None
            or self.source_definition_hash is not None
            or type(self.execution_kind) is not CatalogRoleExecutionKind
            or len(self.eligible_trigger_kinds) != 1
            or self.id != catalog_role_workflow_id(self.template_id, self.eligible_trigger_kinds[0])
            or self.version != 1
            or self.expected_steps != 1
        ):
            raise ValueError("catalog workflows require one exact bounded role contract")
        if self.handler_kind is ExecutableWorkflowHandler.CATALOG_WRITE:
            if (
                self.execution_kind is not CatalogRoleExecutionKind.WRITE
                or self.capability_id is None
                or self.capability_id
                in {
                    "cap.model.generate-structured",
                    "cap.artifact.transform-deterministic",
                }
                or self.expected_model_calls != 0
                or self.expected_connector_calls is not None
                or self.expected_external_actions is not None
                or self.expected_approvals is not None
            ):
                raise ValueError("catalog writes require explicit mode-dependent action execution")
            return
        if (
            self.execution_kind is CatalogRoleExecutionKind.WRITE
            or self.expected_connector_calls != 0
            or self.expected_external_actions != 0
            or self.expected_approvals != 0
        ):
            raise ValueError("catalog read workflows cannot acquire connector authority")
        if self.execution_kind is CatalogRoleExecutionKind.MODEL:
            if (
                self.capability_id != "cap.model.generate-structured"
                or self.expected_model_calls != 1
            ):
                raise ValueError("model role workflow must bind its one model capability")
        elif (
            self.expected_model_calls != 0
            or self.capability_id != "cap.artifact.transform-deterministic"
        ):
            raise ValueError("local role workflow cannot acquire model or connector authority")

    def admission_definition(self) -> WorkflowAdmissionDefinition:
        # Avoid importing the service-package facade while this registry is
        # itself imported by configuration services during composition.
        from marketing_agents.application.services.incoming_work_validation import (
            CampaignBriefPolicy,
            WorkflowAdmissionDefinition,
        )

        return WorkflowAdmissionDefinition(
            id=self.id,
            eligible_template_ids=(self.template_id,),
            eligible_trigger_kinds=self.eligible_trigger_kinds,
            allowed_modes=self.allowed_modes,
            input_schema_ids_by_template={self.template_id: self.input_schema_id},
            campaign_brief_policy=CampaignBriefPolicy.FORBIDDEN,
        )


class ExecutableWorkflowRegistry:
    """Exact definitions, not prefix-based aliases or payload-selected handlers."""

    __slots__ = ("_catalog_content_hash", "_definitions", "_demos", "_roles")

    def __init__(self, definitions: Iterable[ExecutableWorkflowDefinition]) -> None:
        indexed: dict[str, ExecutableWorkflowDefinition] = {}
        roles: dict[tuple[str, TriggerKind], ExecutableWorkflowDefinition] = {}
        demos: dict[str, ExecutableWorkflowDefinition] = {}
        for definition in definitions:
            if type(definition) is not ExecutableWorkflowDefinition:
                raise ValueError("executable registry requires exact definitions")
            if definition.id in indexed:
                raise ValueError("executable workflow IDs must be unique")
            indexed[definition.id] = definition
            if definition.handler_kind in {
                ExecutableWorkflowHandler.CATALOG_ROLE,
                ExecutableWorkflowHandler.CATALOG_WRITE,
            }:
                key = (definition.template_id, definition.eligible_trigger_kinds[0])
                if key in roles:
                    raise ValueError("catalog workflow routes must be unique")
                roles[key] = definition
            else:
                scenario_id = definition.demo_scenario_id
                if scenario_id is None or scenario_id in demos:
                    raise ValueError("executable demo scenario IDs must be unique")
                demos[scenario_id] = definition
        hashes = {definition.catalog_content_hash for definition in indexed.values()}
        if not indexed or len(hashes) != 1:
            raise ValueError("executable workflows must pin one nonempty catalog release")
        self._catalog_content_hash = next(iter(hashes))
        self._definitions = MappingProxyType(indexed)
        self._roles = MappingProxyType(roles)
        self._demos = MappingProxyType(demos)

    @property
    def catalog_content_hash(self) -> str:
        return self._catalog_content_hash

    def list(self) -> tuple[ExecutableWorkflowDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def get(self, workflow_id: str) -> ExecutableWorkflowDefinition:
        if type(workflow_id) is str and workflow_id in self._definitions:
            return self._definitions[workflow_id]
        raise ExecutableWorkflowRegistryError("workflow_unknown", "workflow is not registered")

    def for_catalog_role(
        self, template_id: str, trigger_kind: TriggerKind
    ) -> ExecutableWorkflowDefinition:
        if type(template_id) is str and type(trigger_kind) is TriggerKind:
            definition = self._roles.get((template_id, trigger_kind))
            if definition is not None:
                return definition
        raise ExecutableWorkflowRegistryError("workflow_unknown", "workflow is not registered")

    def for_demo(self, scenario_id: str) -> ExecutableWorkflowDefinition:
        if type(scenario_id) is str and scenario_id in self._demos:
            return self._demos[scenario_id]
        raise ExecutableWorkflowRegistryError("workflow_unknown", "workflow is not registered")

    def require_match(
        self,
        workflow_id: str,
        *,
        instance_id: str,
        template_id: str,
        trigger_kind: TriggerKind,
        mode: WorkMode,
        catalog_content_hash: str,
    ) -> ExecutableWorkflowDefinition:
        definition = self.get(workflow_id)
        if (
            type(instance_id) is not str
            or instance_id not in definition.target_instance_ids
            or template_id != definition.template_id
            or type(trigger_kind) is not TriggerKind
            or trigger_kind not in definition.eligible_trigger_kinds
            or type(mode) is not WorkMode
            or mode not in definition.allowed_modes
            or catalog_content_hash != definition.catalog_content_hash
        ):
            raise ExecutableWorkflowRegistryError(
                "workflow_binding_mismatch", "workflow does not match the admitted route"
            )
        return definition
