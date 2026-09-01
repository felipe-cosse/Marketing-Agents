"""Typed transport projections for safe deterministic demo discovery and intake."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class DemoScenarioApiModel(BaseModel):
    """Server-owned camel-case response boundary."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class DemoScenarioInputModel(BaseModel):
    """Alias-only caller input without orchestration authority fields."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=False,
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
    )


class DemoScenarioRunInput(DemoScenarioInputModel):
    overrides: dict[str, Any] = Field(default_factory=dict, max_length=32)


class DemoScenarioSelectedAgentView(DemoScenarioApiModel):
    template_id: str
    instance_id: str


class DemoScenarioExpectedBehaviorView(DemoScenarioApiModel):
    state_path: tuple[str, ...]
    model_calls: int = Field(ge=0)
    connector_calls: int = Field(ge=0)
    external_actions: int = Field(ge=0)
    approvals: int = Field(ge=0)
    external_writes: int = Field(ge=0)


class DemoScenarioView(DemoScenarioApiModel):
    id: str
    version: int = Field(ge=1)
    display_name: str
    description: str
    workflow_id: str
    effect: Literal["read_only", "mutating"]
    mode: Literal["deterministic_mock"]
    selected_agents: tuple[DemoScenarioSelectedAgentView, ...]
    input_schema: dict[str, Any]
    preset: dict[str, Any]
    safe_submit_verb: str
    expected: DemoScenarioExpectedBehaviorView


class DemoScenarioListResponse(DemoScenarioApiModel):
    items: tuple[DemoScenarioView, ...]


class DemoScenarioRunResponse(DemoScenarioApiModel):
    status: Literal["accepted"]
    disposition: Literal["created", "replayed"]
    scenario_id: str
    event_id: str
    work_id: str
    run_id: str
    execution_mode: Literal["dry_run", "mock_execute"]
    instance_url: str
    run_url: str
    timeline_url: str
    artifacts_url: str


__all__ = [
    "DemoScenarioExpectedBehaviorView",
    "DemoScenarioListResponse",
    "DemoScenarioRunInput",
    "DemoScenarioRunResponse",
    "DemoScenarioSelectedAgentView",
    "DemoScenarioView",
]
