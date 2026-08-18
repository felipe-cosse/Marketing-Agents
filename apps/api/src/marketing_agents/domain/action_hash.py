"""Versioned exact and semantic external-action hashes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.validation import (
    frozen_json_mapping,
    require_digest,
    require_id,
)

ACTION_HASH_DOMAIN = b"marketing-agents:external-action:v1\x00"
SEMANTIC_ACTION_HASH_DOMAIN = b"marketing-agents:external-action-semantic:v1\x00"


def _plain_json_object(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Return a detached JSON object suitable for canonical projections."""

    return cast(dict[str, JsonValue], json.loads(canonical_json_bytes(value)))


class SemanticExternalAction(BaseModel):
    """Stable action meaning with run-local and proposal-local identity excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonicalization_version: Literal[1] = 1
    template_id: str = Field(min_length=1, max_length=200)
    instance_id: str = Field(min_length=1, max_length=240)
    action_type: str = Field(min_length=1, max_length=120)
    capability_id: str = Field(min_length=1, max_length=200)
    connector_family: str = Field(min_length=1, max_length=100)
    binding_id: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=500)
    payload_schema_id: str = Field(min_length=1, max_length=240)
    minimized_payload: Mapping[str, JsonValue]

    @field_validator("destination")
    @classmethod
    def require_normalized_destination(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("destination must already be normalized")
        return value

    @field_validator("minimized_payload", mode="after")
    @classmethod
    def freeze_payload(cls, value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return cast(
            Mapping[str, JsonValue],
            frozen_json_mapping(value, "external action minimized payload"),
        )

    def semantic_projection(self) -> dict[str, JsonValue]:
        return {
            "canonicalization_version": self.canonicalization_version,
            "template_id": self.template_id,
            "instance_id": self.instance_id,
            "action_type": self.action_type,
            "capability_id": self.capability_id,
            "connector_family": self.connector_family,
            "binding_id": self.binding_id,
            "destination": self.destination,
            "payload_schema_id": self.payload_schema_id,
            "minimized_payload": _plain_json_object(self.minimized_payload),
        }


def semantic_action_hash(action: SemanticExternalAction) -> str:
    """Hash stable action meaning without random or run-local identifiers."""

    return hashlib.sha256(
        SEMANTIC_ACTION_HASH_DOMAIN + canonical_json_bytes(action.semantic_projection())
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ExternalActionKeyMaterial:
    """Stable RUN-05 input; key derivation and persistence remain RUN-05 owned."""

    run_id: str
    plan_hash: str
    proposal_revision: int
    step_key: str
    action_type: str
    binding_id: str
    semantic_action_hash: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.run_id, "run ID"),
            (self.step_key, "step key"),
            (self.action_type, "action type"),
            (self.binding_id, "binding ID"),
        ):
            require_id(value, field_name)
        require_digest(self.plan_hash, "plan hash")
        require_digest(self.semantic_action_hash, "semantic action hash")
        if (
            not isinstance(self.proposal_revision, int)
            or isinstance(self.proposal_revision, bool)
            or self.proposal_revision < 1
        ):
            raise ValueError("proposal revision must be a positive integer")


class CanonicalExternalAction(BaseModel):
    """One exact action authorization envelope, including immutable plan scope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonicalization_version: Literal[1] = 1
    action_id: str = Field(min_length=1, max_length=200)
    authorization_set_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_revision: int = Field(ge=1)
    step_id: str = Field(min_length=1, max_length=200)
    step_key: str = Field(min_length=1, max_length=240)
    template_id: str = Field(min_length=1, max_length=200)
    instance_id: str = Field(min_length=1, max_length=240)
    action_type: str = Field(min_length=1, max_length=120)
    capability_id: str = Field(min_length=1, max_length=200)
    connector_family: str = Field(min_length=1, max_length=100)
    binding_id: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=500)
    payload_schema_id: str = Field(min_length=1, max_length=240)
    minimized_payload: Mapping[str, JsonValue]
    semantic_action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("destination")
    @classmethod
    def require_normalized_destination(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("destination must already be normalized")
        return value

    @field_validator("proposal_revision")
    @classmethod
    def reject_boolean_revision(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("proposal revision must be a positive integer")
        return value

    @field_validator("minimized_payload", mode="after")
    @classmethod
    def freeze_payload(cls, value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return cast(
            Mapping[str, JsonValue],
            frozen_json_mapping(value, "external action minimized payload"),
        )

    @model_validator(mode="after")
    def require_current_semantic_hash(self) -> CanonicalExternalAction:
        if self.semantic_action_hash != semantic_action_hash(self.semantic_action()):
            raise ValueError("semantic action hash is not current")
        return self

    def semantic_action(self) -> SemanticExternalAction:
        return SemanticExternalAction(
            canonicalization_version=self.canonicalization_version,
            template_id=self.template_id,
            instance_id=self.instance_id,
            action_type=self.action_type,
            capability_id=self.capability_id,
            connector_family=self.connector_family,
            binding_id=self.binding_id,
            destination=self.destination,
            payload_schema_id=self.payload_schema_id,
            minimized_payload=_plain_json_object(self.minimized_payload),
        )

    def key_material(self) -> ExternalActionKeyMaterial:
        return ExternalActionKeyMaterial(
            run_id=self.run_id,
            plan_hash=self.plan_hash,
            proposal_revision=self.proposal_revision,
            step_key=self.step_key,
            action_type=self.action_type,
            binding_id=self.binding_id,
            semantic_action_hash=self.semantic_action_hash,
        )

    def authorization_projection(self) -> dict[str, JsonValue]:
        return {
            "canonicalization_version": self.canonicalization_version,
            "action_id": self.action_id,
            "authorization_set_id": self.authorization_set_id,
            "run_id": self.run_id,
            "plan_hash": self.plan_hash,
            "proposal_revision": self.proposal_revision,
            "step_id": self.step_id,
            "step_key": self.step_key,
            "template_id": self.template_id,
            "instance_id": self.instance_id,
            "action_type": self.action_type,
            "capability_id": self.capability_id,
            "connector_family": self.connector_family,
            "binding_id": self.binding_id,
            "destination": self.destination,
            "payload_schema_id": self.payload_schema_id,
            "minimized_payload": _plain_json_object(self.minimized_payload),
            "semantic_action_hash": self.semantic_action_hash,
        }


def canonical_action_hash(action: CanonicalExternalAction) -> str:
    """Hash the complete identity-bound action approved by SAFE-02."""

    return hashlib.sha256(
        ACTION_HASH_DOMAIN + canonical_json_bytes(action.authorization_projection())
    ).hexdigest()
