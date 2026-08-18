"""Versioned exact-action envelope and domain-separated authorization hash."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from marketing_agents.domain.canonical_json import canonical_json_bytes

ACTION_HASH_DOMAIN = b"marketing-agents:external-action:v1\x00"


class CanonicalExternalAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonicalization_version: Literal[1] = 1
    action_id: str = Field(min_length=1, max_length=200)
    authorization_set_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    step_id: str = Field(min_length=1, max_length=200)
    template_id: str = Field(min_length=1, max_length=200)
    instance_id: str = Field(min_length=1, max_length=240)
    action_type: str = Field(min_length=1, max_length=120)
    capability_id: str = Field(min_length=1, max_length=200)
    connector_family: str = Field(min_length=1, max_length=100)
    binding_id: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=500)
    payload_schema_id: str = Field(min_length=1, max_length=240)
    minimized_payload: dict[str, JsonValue]

    @field_validator("destination")
    @classmethod
    def require_normalized_destination(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("destination must already be normalized")
        return value

    def authorization_projection(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


def canonical_action_hash(action: CanonicalExternalAction) -> str:
    return hashlib.sha256(
        ACTION_HASH_DOMAIN + canonical_json_bytes(action.authorization_projection())
    ).hexdigest()
