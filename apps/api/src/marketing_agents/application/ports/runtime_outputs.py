"""Immutable output-schema and provider facts declared before a controlled call."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.domain.validation import frozen_json_mapping, require_id


@dataclass(frozen=True, slots=True)
class RuntimeOutputContract:
    """Exact schema and bounded provider identity used to validate one output."""

    schema_id: str
    schema_version: str
    schema: Mapping[str, Any] = field(repr=False)
    schema_hash: str = field(init=False)
    classification: DataClassification
    provider_kind: Literal["llm", "connector", "planner"]
    provider_mode: Literal["mock", "real", "local"]
    provider_name: str
    provider_version: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.schema_id, "runtime output schema ID"),
            (self.schema_version, "runtime output schema version"),
            (self.provider_name, "runtime output provider name"),
            (self.provider_version, "runtime output provider version"),
        ):
            require_id(value, name)
        if type(self.classification) is not DataClassification:
            raise ValueError("runtime output classification must use the exact enum")
        if self.provider_kind not in {"llm", "connector", "planner"}:
            raise ValueError("runtime output provider kind is unsupported")
        if self.provider_mode not in {"mock", "real", "local"}:
            raise ValueError("runtime output provider mode is unsupported")
        encoded_schema = canonical_json_bytes(self.schema)
        plain_schema = json.loads(encoded_schema)
        if not isinstance(plain_schema, dict):
            raise ValueError("runtime output JSON Schema must be an object")
        object.__setattr__(
            self, "schema", frozen_json_mapping(plain_schema, "runtime output JSON Schema")
        )
        object.__setattr__(
            self,
            "schema_hash",
            canonical_schema_hash(plain_schema),
        )


__all__ = ["RuntimeOutputContract"]
