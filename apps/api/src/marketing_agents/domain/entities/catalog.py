"""Organization, template, deployment, capability, policy, and trigger entities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from marketing_agents.domain.enums import Effect, TriggerKind

from ._validation import frozen_mapping, require_id, require_text, require_unique


@dataclass(frozen=True, slots=True)
class Department:
    id: str
    display_name: str
    display_order: int

    def __post_init__(self) -> None:
        require_id(self.id, "department ID")
        require_text(self.display_name, "department display name", maximum=160)
        if self.display_order < 1:
            raise ValueError("department display order must be positive")


@dataclass(frozen=True, slots=True)
class FunctionTeam:
    id: str
    department_id: str
    display_name: str
    display_order: int

    def __post_init__(self) -> None:
        require_id(self.id, "function ID")
        require_id(self.department_id, "department ID")
        require_text(self.display_name, "function display name", maximum=160)
        if self.display_order < 1:
            raise ValueError("function display order must be positive")


@dataclass(frozen=True, slots=True)
class ToolCapability:
    id: str
    connector_family: str
    effect: Effect
    request_schema_id: str
    result_schema_id: str
    idempotency_support: str

    def __post_init__(self) -> None:
        for field_name in ("id", "request_schema_id", "result_schema_id"):
            require_id(getattr(self, field_name), field_name)
        require_text(self.connector_family, "connector family", maximum=100)
        if self.idempotency_support not in {
            "not_applicable",
            "required",
            "supported",
            "unavailable",
        }:
            raise ValueError("unsupported idempotency classification")
        if self.effect is Effect.WRITE and self.idempotency_support != "required":
            raise ValueError("write capabilities must require provider idempotency in v1")


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    id: str
    required_roles: frozenset[str]
    expires_after: timedelta
    allow_self_approval: bool

    def __post_init__(self) -> None:
        require_id(self.id, "approval policy ID")
        if self.expires_after < timedelta(minutes=1) or self.expires_after > timedelta(days=1):
            raise ValueError("approval expiry must be finite and bounded")
        if not self.required_roles:
            raise ValueError("approval policy must identify at least one role")
        for role in self.required_roles:
            require_id(role, "approval role")


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    id: str
    department_id: str
    function_id: str
    display_name: str
    purpose: str
    instruction_ref: str
    input_schema_id: str
    output_schema_id: str
    capability_ids: tuple[str, ...]
    trigger_kinds: tuple[TriggerKind, ...]
    approval_policy_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "department_id",
            "function_id",
            "instruction_ref",
            "input_schema_id",
            "output_schema_id",
            "approval_policy_id",
        ):
            require_id(getattr(self, field_name), field_name)
        require_text(self.display_name, "template display name", maximum=160)
        require_text(self.purpose, "template purpose", maximum=600)
        require_unique(self.capability_ids, "capability IDs")
        if not self.capability_ids or not self.trigger_kinds:
            raise ValueError("template capabilities and trigger kinds must be nonempty")


@dataclass(frozen=True, slots=True)
class AgentInstance:
    id: str
    template_id: str
    enabled: bool
    configuration_revision: int
    connector_bindings: Mapping[str, str]
    source_ordinal: int

    def __post_init__(self) -> None:
        require_id(self.id, "instance ID")
        require_id(self.template_id, "template ID")
        if self.configuration_revision < 1 or self.source_ordinal < 1:
            raise ValueError("configuration revision and source ordinal must be positive")
        for family, binding in self.connector_bindings.items():
            require_id(family, "connector family")
            require_id(binding, "connector binding ID")
        object.__setattr__(self, "connector_bindings", frozen_mapping(self.connector_bindings))


@dataclass(frozen=True, slots=True)
class TriggerDefinition:
    id: str
    instance_id: str
    kind: TriggerKind
    configuration: Mapping[str, Any]
    enabled: bool

    def __post_init__(self) -> None:
        require_id(self.id, "trigger ID")
        require_id(self.instance_id, "instance ID")
        object.__setattr__(self, "configuration", frozen_mapping(self.configuration))
