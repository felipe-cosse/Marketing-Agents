"""Safe local-session projection for the same-origin control plane."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Authority = Annotated[
    str,
    Field(
        min_length=1,
        max_length=240,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$",
    ),
]


class SessionResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    actor_id: Authority
    roles: tuple[Authority, ...] = Field(min_length=1, max_length=64)
    scopes: tuple[Authority, ...] = Field(max_length=128)
    auth_mode: Literal["local"]
    environment: Literal["local", "test", "production"]
    model_mode: Literal["mock", "real"]
    connector_mode: str = Field(min_length=1, max_length=64)
    network_permission: bool
    warning: Literal["Local identity — not production authentication"]
    csrf_token: str = Field(min_length=32, max_length=128, repr=False)
    csrf_header_name: Literal["X-CSRF-Token"]


__all__ = ["SessionResponse"]
