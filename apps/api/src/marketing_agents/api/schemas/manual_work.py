"""Typed, non-authoritative transport contracts for manual dry-run admission."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ManualWorkInputModel(BaseModel):
    """Alias-only input boundary; callers cannot smuggle orchestration authority."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=False,
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
    )


class ManualDryRunInput(ManualWorkInputModel):
    input: dict[str, Any]
    execution_mode: Literal["dry_run", "mock_execute"] = "dry_run"
    campaign_brief_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$",
    )
    demo_scenario_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$",
    )


class ManualWorkApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class ManualDryRunResponse(ManualWorkApiModel):
    status: Literal["accepted"]
    disposition: Literal["created", "replayed"]
    event_id: str
    work_id: str
    run_id: str
    execution_mode: Literal["dry_run", "mock_execute"]
    instance_url: str
    run_url: str


class ManualWorkProblem(ManualWorkApiModel):
    code: str
    message: str
    pointer: str | None = Field(
        default=None,
        max_length=1_000,
        pattern=r"^/input(?:/[A-Za-z0-9_.-]{1,100}){0,64}$",
    )


class ManualWorkHttpError(ManualWorkApiModel):
    detail: str


class ManualWorkRequestFieldError(ManualWorkApiModel):
    pointer: str
    code: str
    message: str


class ManualWorkRequestValidationDetail(ManualWorkApiModel):
    code: Literal["request_validation_failed"]
    message: Literal["request validation failed"]
    field_errors: tuple[ManualWorkRequestFieldError, ...] = Field(
        alias="field_errors",
        max_length=32,
    )


class ManualWorkRequestValidationError(ManualWorkApiModel):
    detail: ManualWorkRequestValidationDetail
