"""Immutable request-schema facts declared before a controlled READ call."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from marketing_agents.application.policies.json_schema import (
    CompiledJsonSchema,
    compile_json_schema,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.domain.validation import require_id


@dataclass(frozen=True, slots=True)
class RuntimeInputContract:
    """Exact schema used to validate one adapter input before reservation."""

    schema_id: str
    schema_version: str
    schema: Mapping[str, Any] = field(repr=False)
    schema_hash: str = field(init=False)
    classification: DataClassification
    _compiled: CompiledJsonSchema = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_id(self.schema_id, "runtime input schema ID")
        require_id(self.schema_version, "runtime input schema version")
        if type(self.classification) is not DataClassification:
            raise ValueError("runtime input classification must use the exact enum")
        compiled = compile_json_schema(self.schema, expected_schema_id=self.schema_id)
        object.__setattr__(self, "schema", compiled.schema)
        object.__setattr__(self, "schema_hash", canonical_schema_hash(compiled.schema))
        object.__setattr__(self, "_compiled", compiled)

    def validate(self, payload: object, *, max_depth: int = 64) -> None:
        """Validate one strict input instance at the stable input pointer root."""

        self._compiled.validate(
            payload,
            pointer_root="/input",
            max_depth=max_depth,
        )


__all__ = ["RuntimeInputContract"]
