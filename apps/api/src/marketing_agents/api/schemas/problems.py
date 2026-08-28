"""Strict, shared RFC 9457-style API problem projections."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

ResourceVersion = (
    Annotated[int, Field(ge=0, le=2**63 - 1)]
    | Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$",
        ),
    ]
)


class ProblemFieldError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pointer: str = Field(
        min_length=1,
        max_length=1_000,
        pattern=r"^/(?:body|path|query|header|cookie|request|input(?:/[A-Za-z0-9_.-]{1,100}){0,64})$",
    )
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=240)


class ProblemDetails(BaseModel):
    """The only process-wide non-success response vocabulary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=120)
    status: int = Field(ge=400, le=599)
    detail: str = Field(min_length=1, max_length=500)
    instance: str = Field(min_length=1, max_length=512)
    code: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    field_errors: tuple[ProblemFieldError, ...] | None = Field(default=None, max_length=32)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)
    current_resource_version: ResourceVersion | None = None


__all__ = ["ProblemDetails", "ProblemFieldError"]
