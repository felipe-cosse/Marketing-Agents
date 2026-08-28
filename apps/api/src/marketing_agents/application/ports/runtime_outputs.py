"""Immutable output-schema and provider facts declared before a controlled call."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from marketing_agents.application.policies.json_schema import compile_json_schema
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.domain.validation import require_id


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
        compiled_schema = compile_json_schema(
            self.schema,
            expected_schema_id=self.schema_id,
        )
        object.__setattr__(self, "schema", compiled_schema.schema)
        object.__setattr__(
            self,
            "schema_hash",
            canonical_schema_hash(compiled_schema.schema),
        )


__all__ = ["RuntimeOutputContract"]
