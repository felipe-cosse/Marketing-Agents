"""Typed deployment-only instance-configuration API contracts."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from pydantic.alias_generators import to_camel


def _nonnullable_optional_json_schema(schema: dict[str, Any]) -> None:
    """Keep runtime presence tracking while documenting a non-null property value."""

    schema.pop("default", None)
    nonnull = [item for item in schema.get("anyOf", ()) if item.get("type") != "null"]
    if len(nonnull) == 1:
        title = schema.get("title")
        schema.clear()
        schema.update(nonnull[0])
        if title is not None:
            schema["title"] = title


class InstanceConfigurationApiModel(BaseModel):
    """Camel-case response boundary that permits field-name construction internally."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class InstanceConfigurationInputModel(BaseModel):
    """Alias-only request boundary; undocumented snake-case transport keys are forbidden."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=False,
        validate_by_alias=True,
        validate_by_name=False,
    )


class TriggerBindingInput(InstanceConfigurationInputModel):
    type: Literal["manual", "webhook", "schedule"]
    enabled: StrictBool = True
    event_source: str | None = Field(default=None, min_length=1, max_length=100)
    cron: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    misfire_policy: Literal["skip", "run_once"] | None = None
    misfire_grace_seconds: StrictInt | None = Field(default=None, ge=0, le=86_400)


class ConnectorBindingInput(InstanceConfigurationInputModel):
    connector_family: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    binding_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    enabled: StrictBool = True


class ScheduleBindingInput(InstanceConfigurationInputModel):
    cron: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=100)
    misfire_policy: Literal["skip", "run_once"]
    misfire_grace_seconds: StrictInt = Field(ge=0, le=86_400)


class InstanceConfigurationPatchInput(InstanceConfigurationInputModel):
    """Partial replacement request; explicit null clears nullable deployment fields."""

    enabled: StrictBool | None = Field(
        default=None,
        json_schema_extra=_nonnullable_optional_json_schema,
    )
    variant_label: str | None = Field(default=None, min_length=1, max_length=100)
    trigger_bindings: tuple[TriggerBindingInput, ...] | None = Field(
        default=None,
        max_length=16,
        json_schema_extra=_nonnullable_optional_json_schema,
    )
    connector_bindings: dict[str, ConnectorBindingInput] | None = Field(
        default=None,
        max_length=16,
        json_schema_extra=_nonnullable_optional_json_schema,
    )
    schedule: ScheduleBindingInput | None = None

    @model_validator(mode="after")
    def validate_patch_shape(self) -> Self:
        supplied = self.model_fields_set
        if not supplied:
            raise ValueError("configuration patch must contain at least one field")
        for required_when_present in ("enabled", "trigger_bindings", "connector_bindings"):
            if required_when_present in supplied and getattr(self, required_when_present) is None:
                raise ValueError(f"{required_when_present} cannot be null")
        return self


class TriggerBindingView(InstanceConfigurationApiModel):
    type: Literal["manual", "webhook", "schedule"]
    enabled: bool
    event_source: str | None
    cron: str | None
    timezone: str | None
    misfire_policy: Literal["skip", "run_once"] | None
    misfire_grace_seconds: int | None = Field(ge=0, le=86_400)


class ConnectorBindingView(InstanceConfigurationApiModel):
    connector_family: str
    binding_id: str
    enabled: bool


class ScheduleBindingView(InstanceConfigurationApiModel):
    cron: str
    timezone: str
    misfire_policy: Literal["skip", "run_once"]
    misfire_grace_seconds: int = Field(ge=0, le=86_400)


class InstanceConfigurationView(InstanceConfigurationApiModel):
    instance_id: str
    enabled: bool
    variant_label: str | None
    trigger_bindings: tuple[TriggerBindingView, ...]
    connector_bindings: dict[str, ConnectorBindingView]
    schedule: ScheduleBindingView | None
    configuration_revision: int = Field(ge=1)


class InstanceConfigurationResponse(InstanceConfigurationApiModel):
    projection_version: Literal["instance-configuration-v1"] = "instance-configuration-v1"
    configuration: InstanceConfigurationView


class InstanceConfigurationSchemaResponse(InstanceConfigurationApiModel):
    projection_version: Literal["instance-configuration-schema-v1"] = (
        "instance-configuration-schema-v1"
    )
    instance_id: str
    template_id: str
    configuration_schema: dict[str, Any]


class InstanceConfigurationProblem(InstanceConfigurationApiModel):
    code: str
    message: str
    current_revision: int | None = Field(default=None, ge=1)


class InstanceConfigurationHttpError(InstanceConfigurationApiModel):
    detail: str


class InstanceConfigurationRequestFieldError(InstanceConfigurationApiModel):
    pointer: str
    code: str
    message: str


class InstanceConfigurationRequestValidationDetail(InstanceConfigurationApiModel):
    code: Literal["request_validation_failed"]
    message: Literal["request validation failed"]
    field_errors: tuple[InstanceConfigurationRequestFieldError, ...] = Field(
        alias="field_errors",
        max_length=32,
    )


class InstanceConfigurationRequestValidationError(InstanceConfigurationApiModel):
    detail: InstanceConfigurationRequestValidationDetail
