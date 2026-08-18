"""Typed trust boundary for every externally supplied content fragment."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExternalContentKind(StrEnum):
    POST = "post"
    COMMENT = "comment"
    EMAIL = "email"
    TRANSCRIPT = "transcript"
    WEBPAGE = "webpage"
    WEBHOOK = "webhook"
    USER_INPUT = "user_input"
    CONNECTOR_RESULT = "connector_result"
    PRIOR_ARTIFACT = "prior_artifact"


class UntrustedContentPart(BaseModel):
    """External text carried as immutable, provenance-labeled data only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trust_class: Literal["untrusted_external"] = "untrusted_external"
    kind: ExternalContentKind
    source_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=65_536)
    provenance_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
