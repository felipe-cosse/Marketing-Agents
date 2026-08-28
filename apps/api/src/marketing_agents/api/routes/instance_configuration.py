"""Authorized deployment-only configuration schema and optimistic PATCH routes."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.dependencies import (
    InstanceConfigurationExecutor,
    get_instance_configuration_executor,
    require_catalog_principal,
    require_instance_configuration_admin_principal,
)
from marketing_agents.api.instance_configuration_etag import instance_configuration_etag
from marketing_agents.api.schemas.instance_configuration import (
    ConnectorBindingView,
    InstanceConfigurationHttpError,
    InstanceConfigurationPatchInput,
    InstanceConfigurationProblem,
    InstanceConfigurationRequestValidationError,
    InstanceConfigurationResponse,
    InstanceConfigurationSchemaResponse,
    InstanceConfigurationView,
    ScheduleBindingView,
    TriggerBindingView,
)
from marketing_agents.api.strict_json import (
    StrictJsonTransportError,
    strict_json_route_path,
    strict_json_transport_headers_are_valid,
    validate_strict_json_body,
)
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationSchema,
    InstanceConfigurationServiceError,
    InstanceConfigurationUpdateResult,
    UpdateInstanceConfigurationCommand,
)
from marketing_agents.domain.enums import MisfirePolicy, TriggerKind
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.instance_configuration import (
    InstanceConfiguration,
    InstanceConfigurationPatch,
    InstanceConnectorBinding,
    InstanceSchedule,
    InstanceTriggerBinding,
    PatchValue,
)

_INSTANCE_ID_PATTERN = r"^inst\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+\.[0-9]{2}$"
_NO_STORE = "no-store"
_VARY = "Authorization"
_MAX_IF_MATCH_LENGTH = 200
_MAX_CONFIGURATION_REQUEST_BYTES = 1_048_576
_MAX_CONFIGURATION_JSON_DEPTH = 64
_QUERY_TIMEOUT_SECONDS = 5.0
_CONFIGURATION_ETAG_PATTERN = re.compile(r'^"instance-configuration-v1-[1-9][0-9]*"$')
_CONFIGURATION_PATH_PATTERN = re.compile(r"^/api/v1/agent-instances/[^/]+/configuration$")
_IF_MATCH_OPENAPI_PARAMETER: dict[str, object] = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": "One exact strong ETag from the current instance configuration revision.",
    "schema": {
        "type": "string",
        "pattern": _CONFIGURATION_ETAG_PATTERN.pattern,
        "maxLength": _MAX_IF_MATCH_LENGTH,
    },
}
_SUCCESS_HEADERS: dict[str, dict[str, object]] = {
    "Cache-Control": {
        "description": "Configuration responses must not be stored.",
        "schema": {"type": "string", "const": _NO_STORE},
    },
    "ETag": {
        "description": "Strong revision validator required by the next PATCH.",
        "schema": {
            "type": "string",
            "pattern": _CONFIGURATION_ETAG_PATTERN.pattern,
            "maxLength": _MAX_IF_MATCH_LENGTH,
        },
    },
    "Vary": {
        "description": "Shared caches must separate authorization contexts.",
        "schema": {"type": "string", "const": _VARY},
    },
}
_ERROR_HEADERS: dict[str, dict[str, object]] = {
    "Cache-Control": _SUCCESS_HEADERS["Cache-Control"],
    "Vary": _SUCCESS_HEADERS["Vary"],
}
_PATCH_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_200_OK: {
        "description": "The exact resulting deployment configuration.",
        "headers": _SUCCESS_HEADERS,
    },
    status.HTTP_400_BAD_REQUEST: {
        "model": InstanceConfigurationHttpError,
        "description": "The conditional or authentication header shape is malformed.",
    },
    status.HTTP_401_UNAUTHORIZED: {
        "model": InstanceConfigurationHttpError,
        "description": "Authentication is required.",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": InstanceConfigurationHttpError | InstanceConfigurationProblem,
        "description": "The authenticated principal may not mutate configuration.",
    },
    status.HTTP_404_NOT_FOUND: {
        "model": InstanceConfigurationProblem,
        "description": "The selected agent instance does not exist.",
        "headers": _ERROR_HEADERS,
    },
    status.HTTP_409_CONFLICT: {
        "model": InstanceConfigurationProblem,
        "description": "The supplied revision validator is stale.",
        "headers": _ERROR_HEADERS,
    },
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
        "model": InstanceConfigurationHttpError,
        "description": "The request is not application/json.",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": InstanceConfigurationProblem | InstanceConfigurationRequestValidationError,
        "description": "The request shape or deployment configuration is invalid.",
    },
    status.HTTP_428_PRECONDITION_REQUIRED: {
        "model": InstanceConfigurationHttpError,
        "description": "If-Match is required.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": InstanceConfigurationHttpError | InstanceConfigurationProblem,
        "description": "The configuration service is unavailable or violated its contract.",
    },
}
_SCHEMA_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_200_OK: {
        "description": "The structural deployment configuration schema.",
        "headers": _ERROR_HEADERS,
    },
    status.HTTP_400_BAD_REQUEST: {
        "model": InstanceConfigurationHttpError,
        "description": "The authentication header shape is malformed.",
    },
    status.HTTP_401_UNAUTHORIZED: {
        "model": InstanceConfigurationHttpError,
        "description": "Authentication is required.",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": InstanceConfigurationHttpError | InstanceConfigurationProblem,
        "description": "The authenticated principal may not read configuration schemas.",
    },
    status.HTTP_404_NOT_FOUND: {
        "model": InstanceConfigurationProblem,
        "description": "The selected agent instance does not exist.",
        "headers": _ERROR_HEADERS,
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": InstanceConfigurationRequestValidationError,
        "description": "The instance path parameter is invalid.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": InstanceConfigurationHttpError | InstanceConfigurationProblem,
        "description": "The configuration service is unavailable or violated its contract.",
    },
}

router = APIRouter(prefix="/api/v1/agent-instances", tags=["instance-configuration"])


def _json_content_type(request: Request) -> None:
    if not strict_json_transport_headers_are_valid(request.scope):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="instance configuration requires application/json",
        )


def _plain_json(value: Any) -> Any:
    if value is None or type(value) in {str, int, bool, float}:
        return value
    if isinstance(value, Mapping):
        plain: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be exact strings")
            plain[key] = _plain_json(item)
        return plain
    if type(value) in {tuple, list}:
        return [_plain_json(item) for item in value]
    raise TypeError("value is not JSON-safe")


def _configuration_view(configuration: InstanceConfiguration) -> InstanceConfigurationView:
    if type(configuration) is not InstanceConfiguration:
        raise TypeError("configuration service returned the wrong boundary type")
    return InstanceConfigurationView(
        instance_id=configuration.instance_id,
        enabled=configuration.enabled,
        variant_label=configuration.variant_label,
        trigger_bindings=tuple(
            TriggerBindingView(
                type=binding.kind.value,
                enabled=binding.enabled,
                event_source=binding.event_source,
                cron=binding.cron,
                timezone=binding.timezone,
                misfire_policy=(
                    None if binding.misfire_policy is None else binding.misfire_policy.value
                ),
                misfire_grace_seconds=binding.misfire_grace_seconds,
            )
            for binding in configuration.trigger_bindings
        ),
        connector_bindings={
            family: ConnectorBindingView(
                connector_family=binding.connector_family,
                binding_id=binding.binding_id,
                enabled=binding.enabled,
            )
            for family, binding in configuration.connector_bindings.items()
        },
        schedule=(
            None
            if configuration.schedule is None
            else ScheduleBindingView(
                cron=configuration.schedule.cron,
                timezone=configuration.schedule.timezone,
                misfire_policy=configuration.schedule.misfire_policy.value,
                misfire_grace_seconds=configuration.schedule.misfire_grace_seconds,
            )
        ),
        configuration_revision=configuration.configuration_revision,
    )


def _response(configuration: InstanceConfiguration) -> InstanceConfigurationResponse:
    return InstanceConfigurationResponse(configuration=_configuration_view(configuration))


def _problem_response(
    *,
    status_code: int,
    code: str,
    message: str,
    current_revision: int | None = None,
) -> JSONResponse:
    problem = InstanceConfigurationProblem(
        code=code,
        message=message,
        current_revision=current_revision,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json", by_alias=True),
        headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
    )


class InstanceConfigurationRequestBoundsMiddleware:
    """Bound and validate configuration JSON before FastAPI parses it."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "PATCH"
            or _CONFIGURATION_PATH_PATTERN.fullmatch(strict_json_route_path(scope)) is None
        ):
            await self._app(scope, receive, send)
            return
        if not strict_json_transport_headers_are_valid(scope):
            response = JSONResponse(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                content={"detail": "instance configuration requires application/json"},
                headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
            )
            await response(scope, receive, send)
            return
        if self._declared_length_is_invalid(scope):
            await self._reject(scope, receive, send)
            return

        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self._reject(scope, receive, send)
                return
            chunk = message.get("body", b"")
            if (
                type(chunk) is not bytes
                or len(buffered) + len(chunk) > _MAX_CONFIGURATION_REQUEST_BYTES
            ):
                await self._reject(scope, receive, send)
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break
        try:
            validate_strict_json_body(
                bytes(buffered),
                max_depth=_MAX_CONFIGURATION_JSON_DEPTH,
            )
        except StrictJsonTransportError:
            await self._reject(scope, receive, send)
            return

        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": bytes(buffered),
                    "more_body": False,
                }
            return await receive()

        await self._app(scope, bounded_receive, send)

    @staticmethod
    def _declared_length_is_invalid(scope: Scope) -> bool:
        values = [
            value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
        ]
        if not values:
            return False
        if len(values) != 1:
            return True
        try:
            raw = values[0].decode("ascii")
            parsed = int(raw)
        except (UnicodeDecodeError, ValueError):
            return True
        return str(parsed) != raw or parsed < 0 or parsed > _MAX_CONFIGURATION_REQUEST_BYTES

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="configuration_invalid",
            message="instance configuration is invalid",
        )
        await response(scope, receive, send)


def _service_problem(error: InstanceConfigurationServiceError) -> JSONResponse:
    forbidden_codes = {
        "configuration_read_forbidden",
        "configuration_human_required",
        "configuration_admin_role_missing",
    }
    input_codes = {
        "configuration_command_invalid",
        "configuration_patch_empty",
        "configuration_invalid",
        "configuration_schedule_invalid",
    }
    if error.code in forbidden_codes:
        status_code = status.HTTP_403_FORBIDDEN
        code = "configuration_forbidden"
        message = "instance configuration is forbidden"
    elif error.code == "instance_not_found":
        status_code = status.HTTP_404_NOT_FOUND
        code = "instance_not_found"
        message = "agent instance was not found"
    elif error.code == "configuration_revision_conflict":
        status_code = status.HTTP_409_CONFLICT
        code = error.code
        message = "instance configuration revision changed"
    elif error.code in input_codes:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        code = "configuration_invalid"
        message = "instance configuration is invalid"
    else:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        code = "configuration_unavailable"
        message = "instance configuration is temporarily unavailable"
    return _problem_response(
        status_code=status_code,
        code=code,
        message=message,
        current_revision=(
            error.current_revision if status_code == status.HTTP_409_CONFLICT else None
        ),
    )


def _require_if_match(values: list[str] | None) -> str:
    if values is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match is required",
        )
    if (
        len(values) != 1
        or len(values[0]) > _MAX_IF_MATCH_LENGTH
        or not values[0]
        or values[0] != values[0].strip()
        or "," in values[0]
        or values[0] == "*"
        or values[0].startswith("W/")
        or _CONFIGURATION_ETAG_PATTERN.fullmatch(values[0]) is None
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match must contain one exact strong configuration ETag",
        )
    return values[0]


def _patch(body: InstanceConfigurationPatchInput) -> InstanceConfigurationPatch:
    supplied = body.model_fields_set
    if "enabled" in supplied:
        if body.enabled is None:
            raise ValueError("a supplied enabled state cannot be null")
        enabled_patch = PatchValue.of(body.enabled)
    else:
        enabled_patch = PatchValue.omitted()
    triggers = tuple(
        InstanceTriggerBinding(
            kind=TriggerKind(item.type),
            enabled=item.enabled,
            event_source=item.event_source,
            cron=item.cron,
            timezone=item.timezone,
            misfire_policy=(
                None if item.misfire_policy is None else MisfirePolicy(item.misfire_policy)
            ),
            misfire_grace_seconds=item.misfire_grace_seconds,
        )
        for item in (body.trigger_bindings or ())
    )
    connectors = {
        family: InstanceConnectorBinding(
            connector_family=item.connector_family,
            binding_id=item.binding_id,
            enabled=item.enabled,
        )
        for family, item in (body.connector_bindings or {}).items()
    }
    schedule = (
        None
        if body.schedule is None
        else InstanceSchedule(
            cron=body.schedule.cron,
            timezone=body.schedule.timezone,
            misfire_policy=MisfirePolicy(body.schedule.misfire_policy),
            misfire_grace_seconds=body.schedule.misfire_grace_seconds,
        )
    )
    return InstanceConfigurationPatch(
        enabled=enabled_patch,
        variant_label=(
            PatchValue.of(body.variant_label)
            if "variant_label" in supplied
            else PatchValue.omitted()
        ),
        trigger_bindings=(
            PatchValue.of(triggers) if "trigger_bindings" in supplied else PatchValue.omitted()
        ),
        connector_bindings=(
            PatchValue.of(connectors) if "connector_bindings" in supplied else PatchValue.omitted()
        ),
        schedule=(PatchValue.of(schedule) if "schedule" in supplied else PatchValue.omitted()),
    )


@router.get(
    "/{instance_id}/configuration-schema",
    response_model=InstanceConfigurationSchemaResponse,
    operation_id="getAgentInstanceConfigurationSchema",
    responses=_SCHEMA_RESPONSES,
)
async def get_instance_configuration_schema(
    instance_id: Annotated[str, Path(pattern=_INSTANCE_ID_PATTERN)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[
        InstanceConfigurationExecutor,
        Depends(get_instance_configuration_executor),
    ],
) -> JSONResponse:
    try:
        schema = await asyncio.wait_for(
            executor.schema(instance_id, principal=principal),
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
        if type(schema) is not InstanceConfigurationSchema or schema.instance_id != instance_id:
            raise TypeError("configuration service returned an invalid schema")
        response = InstanceConfigurationSchemaResponse(
            instance_id=schema.instance_id,
            template_id=schema.template_id,
            configuration_schema=_plain_json(schema.configuration_schema),
        )
    except InstanceConfigurationServiceError as error:
        return _service_problem(error)
    except Exception:
        return _problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="configuration_unavailable",
            message="instance configuration is temporarily unavailable",
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=response.model_dump(mode="json", by_alias=True),
        headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
    )


@router.patch(
    "/{instance_id}/configuration",
    response_model=InstanceConfigurationResponse,
    operation_id="updateAgentInstanceConfiguration",
    openapi_extra={"parameters": [_IF_MATCH_OPENAPI_PARAMETER]},
    responses=_PATCH_RESPONSES,
)
async def update_instance_configuration(
    instance_id: Annotated[str, Path(pattern=_INSTANCE_ID_PATTERN)],
    body: InstanceConfigurationPatchInput,
    request: Request,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_instance_configuration_admin_principal),
    ],
    executor: Annotated[
        InstanceConfigurationExecutor,
        Depends(get_instance_configuration_executor),
    ],
    _content_type: Annotated[None, Depends(_json_content_type)],
) -> JSONResponse:
    supplied_etag = _require_if_match(request.headers.getlist("if-match") or None)
    try:
        current = await asyncio.wait_for(
            executor.read(instance_id, principal=principal),
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
        if type(current) is not InstanceConfiguration or current.instance_id != instance_id:
            raise TypeError("configuration service returned an invalid projection")
    except InstanceConfigurationServiceError as error:
        return _service_problem(error)
    except Exception:
        return _problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="configuration_unavailable",
            message="instance configuration is temporarily unavailable",
        )
    expected_etag = instance_configuration_etag(current.configuration_revision)
    if supplied_etag != expected_etag:
        return _problem_response(
            status_code=status.HTTP_409_CONFLICT,
            code="configuration_revision_conflict",
            message="instance configuration revision changed",
            current_revision=current.configuration_revision,
        )
    try:
        patch = _patch(body)
        expected_candidate = patch.apply(current)
        expected_changed = expected_candidate != current
        expected_configuration = (
            expected_candidate.with_revision(current.configuration_revision + 1)
            if expected_changed
            else current
        )
        command = UpdateInstanceConfigurationCommand(
            instance_id=instance_id,
            expected_revision=current.configuration_revision,
            patch=patch,
            correlation_id=f"correlation.instance-configuration.{secrets.token_hex(16)}",
        )
    except (TypeError, ValueError):
        return _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="configuration_invalid",
            message="instance configuration is invalid",
        )
    try:
        result = await asyncio.wait_for(
            executor.update(command, principal=principal),
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
    except InstanceConfigurationServiceError as error:
        return _service_problem(error)
    except Exception:
        return _problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="configuration_unavailable",
            message="instance configuration is temporarily unavailable",
        )
    if (
        type(result) is not InstanceConfigurationUpdateResult
        or result.changed is not expected_changed
        or result.configuration != expected_configuration
    ):
        return _problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="configuration_unavailable",
            message="instance configuration is temporarily unavailable",
        )
    response = _response(result.configuration)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=response.model_dump(mode="json", by_alias=True),
        headers={
            "Cache-Control": _NO_STORE,
            "ETag": instance_configuration_etag(result.configuration.configuration_revision),
            "Vary": _VARY,
        },
    )
