"""Dedicated schedule routes over the existing deployment configuration authority."""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Annotated, Any, Literal, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.correlation import request_correlation_id
from marketing_agents.api.dependencies import (
    require_catalog_principal,
    require_instance_configuration_admin_principal,
)
from marketing_agents.api.errors import problem_details
from marketing_agents.api.instance_configuration_etag import instance_configuration_etag
from marketing_agents.api.schemas.problems import ProblemDetails
from marketing_agents.api.schemas.schedules import (
    ScheduleCreateInput,
    ScheduleListResponse,
    ScheduleOccurrenceListResponse,
    ScheduleOccurrenceResponse,
    SchedulePatchInput,
    ScheduleResponse,
    ScheduleToggleInput,
)
from marketing_agents.api.strict_json import (
    StrictJsonTransportError,
    strict_json_route_path,
    strict_json_transport_headers_are_valid,
    validate_strict_json_body,
)
from marketing_agents.application.services.instance_configuration import (
    InstanceConfigurationServiceError,
)
from marketing_agents.application.services.schedule_control import (
    ScheduleControlError,
    ScheduleMutation,
    ScheduleOccurrencePage,
    SchedulePage,
    SchedulePageQuery,
    ScheduleResource,
)
from marketing_agents.domain.entities import Schedule, ScheduleOccurrence
from marketing_agents.domain.enums import MisfirePolicy
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.instance_configuration import InstanceSchedule, ScheduledInput
from marketing_agents.domain.validation import require_utc

_PATH = re.compile(r"^/api/v1/schedules(?:/[^/]+(?:/(?:enable|disable))?)?/?$")
_ETAG = re.compile(r'^"instance-configuration-v1-([1-9][0-9]{0,17})"$')
_MAX_BYTES = 65536
_TIMEOUT_SECONDS = 5.0
_HEADERS = {"Cache-Control": "no-store", "Vary": "Authorization"}
_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ProblemDetails} for code in (400, 401, 403, 404, 409, 413, 415, 422, 428, 503)
}
_IF_MATCH = {
    "parameters": [
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "description": (
                "Current instance configuration ETag (also returned by schedule GET). "
                "Worker lease/version changes do not change this configuration fence."
            ),
            "schema": {"type": "string", "pattern": _ETAG.pattern, "maxLength": 80},
        }
    ]
}
router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"], responses=_RESPONSES)


class ScheduleControlExecutor(Protocol):
    async def read(
        self, schedule_id: str, *, principal: AuthenticatedPrincipal
    ) -> ScheduleResource: ...
    async def list_schedules(
        self, query: SchedulePageQuery, *, principal: AuthenticatedPrincipal
    ) -> SchedulePage: ...
    async def occurrences(
        self, schedule_id: str, query: SchedulePageQuery, *, principal: AuthenticatedPrincipal
    ) -> ScheduleOccurrencePage: ...
    async def mutate(
        self, command: ScheduleMutation, *, principal: AuthenticatedPrincipal
    ) -> ScheduleResource: ...


def get_schedule_control_executor(request: Request) -> ScheduleControlExecutor:
    try:
        value = getattr(request.app.state, "schedule_control_service", None)
        methods = tuple(
            getattr(value, name, None)
            for name in ("read", "list_schedules", "occurrences", "mutate")
        )
    except Exception:
        value, methods = None, ()
    if (
        value is None
        or len(methods) != 4
        or any(
            not callable(method) or not inspect.iscoroutinefunction(method) for method in methods
        )
    ):
        raise HTTPException(503, detail={"code": "schedule_unavailable"})
    return cast(ScheduleControlExecutor, value)


Reader = Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)]
Admin = Annotated[AuthenticatedPrincipal, Depends(require_instance_configuration_admin_principal)]
Executor = Annotated[ScheduleControlExecutor, Depends(get_schedule_control_executor)]
ScheduleId = Annotated[str, Path(pattern=r"^schedule\.configuration\.[0-9a-f]{64}$")]


def _problem(request: Request, status: int, code: str, revision: int | None = None) -> JSONResponse:
    value = problem_details(
        status_code=status,
        correlation_id=request_correlation_id(request),
        code=code,
        current_resource_version=revision,
        detail=(
            "Schedule outcome is unconfirmed. Inspect the schedule and configuration before "
            "retrying; a mutation may already have committed."
            if status == 503
            else None
        ),
    )
    return JSONResponse(
        status_code=status,
        content=value.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
        headers=_HEADERS,
    )


def _failure(request: Request, error: Exception) -> JSONResponse:
    code = (
        cast(ScheduleControlError | InstanceConfigurationServiceError, error).code
        if type(error) in (ScheduleControlError, InstanceConfigurationServiceError)
        else "schedule_unavailable"
    )
    statuses = {
        "schedule_forbidden": 403,
        "schedule_not_found": 404,
        "instance_not_found": 404,
        "schedule_exists": 409,
        "schedule_revision_conflict": 409,
        "configuration_revision_conflict": 409,
        "schedule_instance_disabled": 409,
        "schedule_disabled": 409,
        "schedule_input_required": 409,
        "schedule_input_invalid": 422,
        "schedule_query_invalid": 422,
        "schedule_cursor_invalid": 422,
        "configuration_invalid": 422,
        "configuration_schedule_invalid": 422,
    }
    status = statuses.get(code, 503) if type(code) is str else 503
    revision = getattr(error, "current_revision", None) if status == 409 else None
    if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
        revision = None
    return _problem(request, status, code if status != 503 else "schedule_unavailable", revision)


def _query(request: Request, allowed: set[str]) -> None:
    pairs = request.query_params.multi_items()
    if any(name not in allowed for name, _ in pairs) or len(pairs) != len(
        {name for name, _ in pairs}
    ):
        raise HTTPException(422, detail={"code": "schedule_query_invalid"})


def _revision(request: Request) -> int:
    values = request.headers.getlist("if-match")
    if not values:
        raise HTTPException(428, detail={"code": "schedule_precondition_required"})
    match = _ETAG.fullmatch(values[0]) if len(values) == 1 and len(values[0]) <= 80 else None
    if match is None:
        raise HTTPException(400, detail={"code": "schedule_precondition_invalid"})
    return int(match[1])


def _view(value: ScheduleResource, identifier: str | None = None) -> ScheduleResponse:
    if type(value) is not ScheduleResource or type(value.schedule) is not Schedule:
        raise ValueError("invalid schedule result")
    row = value.schedule
    if identifier is not None and row.id != identifier:
        raise ValueError("schedule result scope changed")
    if (
        value.configuration_revision != row.configuration_revision
        or type(value.preview_utc) is not tuple
        or len(value.preview_utc) != 3
        or value.preview_utc[0] != row.next_run_at_utc
    ):
        raise ValueError("schedule result revision or preview changed")
    for instant in value.preview_utc:
        require_utc(instant, "schedule preview")
    if tuple(sorted(set(value.preview_utc))) != value.preview_utc:
        raise ValueError("unordered schedule preview")
    return ScheduleResponse(
        id=row.id,
        instance_id=row.instance_id,
        trigger_id=row.trigger_id,
        workflow_id=row.workflow_id,
        cron=row.cron,
        timezone=row.timezone,
        misfire_policy=row.misfire_policy.value,
        misfire_grace_seconds=row.misfire_grace_seconds,
        enabled=row.enabled,
        configuration_revision=value.configuration_revision,
        version=row.version,
        recurrence_version=row.recurrence_version,
        next_run_at_utc=row.next_run_at_utc,
        last_scheduled_at_utc=row.last_scheduled_at_utc,
        preview_utc=value.preview_utc,
    )


def _response(
    value: ScheduleResource, *, identifier: str | None = None, created: bool = False
) -> JSONResponse:
    view = _view(value, identifier)
    headers = {**_HEADERS, "ETag": instance_configuration_etag(value.configuration_revision)}
    if created:
        headers["Location"] = "/api/v1/schedules/" + view.id
    return JSONResponse(
        status_code=201 if created else 200,
        content=view.model_dump(mode="json", by_alias=True),
        headers=headers,
    )


class ScheduleControlRequestBoundsMiddleware:
    """Reject ambiguous/oversized JSON before body-model parsing or executor resolution."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in ("POST", "PATCH")
            or _PATH.fullmatch(strict_json_route_path(scope)) is None
        ):
            await self._app(scope, receive, send)
            return

        async def reject(status: int, code: str) -> None:
            await _problem(Request(scope), status, code)(scope, receive, send)

        if not strict_json_transport_headers_are_valid(scope):
            await reject(415, "schedule_json_required")
            return
        lengths = [
            value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
        ]
        if lengths:
            try:
                raw = lengths[0].decode("ascii")
                length = int(raw)
                if len(lengths) != 1 or length < 0 or str(length) != raw:
                    raise ValueError
            except (ValueError, UnicodeError):
                await reject(400, "schedule_transport_invalid")
                return
            if length > _MAX_BYTES:
                await reject(413, "schedule_body_too_large")
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await reject(400, "schedule_transport_invalid")
                return
            chunk = message.get("body", b"")
            if type(chunk) is not bytes or len(body) + len(chunk) > _MAX_BYTES:
                await reject(413, "schedule_body_too_large")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        try:
            validate_strict_json_body(bytes(body), max_depth=24)
        except StrictJsonTransportError:
            await reject(422, "schedule_input_invalid")
            return
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self._app(scope, bounded_receive, send)


@router.get(
    "",
    response_model=ScheduleListResponse,
    operation_id="listSchedules",
    description=(
        "Configuration-owned schedules ordered by stable ID; default 20, maximum 100. "
        "Keyset cursors are endpoint scoped, not frozen snapshots."
    ),
)
async def list_schedules(
    request: Request,
    principal: Reader,
    executor: Executor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> JSONResponse:
    _query(request, {"limit", "cursor"})
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            page = await executor.list_schedules(
                SchedulePageQuery(limit, cursor), principal=principal
            )
        if (
            type(page) is not SchedulePage
            or type(page.items) is not tuple
            or len(page.items) > limit
        ):
            raise ValueError("invalid page")
        result = ScheduleListResponse(
            items=tuple(_view(item) for item in page.items), next_cursor=page.next_cursor
        )
        content = result.model_dump(mode="json", by_alias=True)
        if page.next_cursor is None:
            content.pop("nextCursor")
        return JSONResponse(
            content=content,
            headers=_HEADERS,
        )
    except Exception as error:
        return _failure(request, error)


@router.get(
    "/{schedule_id}",
    response_model=ScheduleResponse,
    operation_id="getSchedule",
    description=(
        "Safe recurrence and current configuration revision. Previews start at persisted next "
        "due time; a disabled schedule does not enqueue these previews."
    ),
)
async def get_schedule(
    schedule_id: ScheduleId, request: Request, principal: Reader, executor: Executor
) -> JSONResponse:
    _query(request, set())
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            resource = await executor.read(schedule_id, principal=principal)
        return _response(resource, identifier=schedule_id)
    except Exception as error:
        return _failure(request, error)


async def _mutate(
    request: Request,
    principal: AuthenticatedPrincipal,
    executor: ScheduleControlExecutor,
    operation: Literal["create", "patch", "enable", "disable"],
    target: str,
    body: ScheduleCreateInput | SchedulePatchInput | ScheduleToggleInput,
) -> JSONResponse:
    _query(request, set())
    revision = _revision(request)
    try:
        parameters = (
            body.schedule if isinstance(body, (ScheduleCreateInput, SchedulePatchInput)) else None
        )
        payload = (
            body.scheduled_input
            if isinstance(body, (ScheduleCreateInput, SchedulePatchInput))
            else None
        )
        command = ScheduleMutation(
            operation,
            target,
            revision,
            request_correlation_id(request),
            None
            if parameters is None
            else InstanceSchedule(
                parameters.cron,
                parameters.timezone,
                MisfirePolicy(parameters.misfire_policy),
                parameters.misfire_grace_seconds,
            ),
            None if payload is None else ScheduledInput(payload.input),
        )
    except (ValueError, TypeError):
        return _problem(request, 422, "schedule_input_invalid")
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            result = await executor.mutate(command, principal=principal)
        if operation == "create" and result.schedule.instance_id != target:
            raise ValueError("created schedule scope changed")
        return _response(
            result,
            identifier=None if operation == "create" else target,
            created=operation == "create",
        )
    except Exception as error:
        return _failure(request, error)


@router.post(
    "",
    status_code=201,
    response_model=ScheduleResponse,
    operation_id="createSchedule",
    openapi_extra=_IF_MATCH,
    description=(
        "Create the single configuration-owned active schedule for an enabled instance. Supply "
        "the instance configuration If-Match. Existing schedules and disabled instances return "
        "409; work is not executed inline."
    ),
)
async def create_schedule(
    body: ScheduleCreateInput, request: Request, principal: Admin, executor: Executor
) -> JSONResponse:
    return await _mutate(request, principal, executor, "create", body.instance_id, body)


@router.patch(
    "/{schedule_id}",
    response_model=ScheduleResponse,
    operation_id="updateSchedule",
    openapi_extra=_IF_MATCH,
    description=(
        "Replace recurrence and/or saved input under the current configuration If-Match. "
        "Disabled schedules return 409; explicitly enable before editing. Other instance "
        "fields and trigger kinds are preserved."
    ),
)
async def update_schedule(
    schedule_id: ScheduleId,
    body: SchedulePatchInput,
    request: Request,
    principal: Admin,
    executor: Executor,
) -> JSONResponse:
    return await _mutate(request, principal, executor, "patch", schedule_id, body)


@router.post(
    "/{schedule_id}/enable",
    response_model=ScheduleResponse,
    operation_id="enableSchedule",
    openapi_extra=_IF_MATCH,
    description=(
        "Restore retained recurrence with the saved sealed input. Disabled instances return "
        "409; this never enables the whole agent instance."
    ),
)
async def enable_schedule(
    schedule_id: ScheduleId,
    body: ScheduleToggleInput,
    request: Request,
    principal: Admin,
    executor: Executor,
) -> JSONResponse:
    return await _mutate(request, principal, executor, "enable", schedule_id, body)


@router.post(
    "/{schedule_id}/disable",
    response_model=ScheduleResponse,
    operation_id="disableSchedule",
    openapi_extra=_IF_MATCH,
    description=(
        "Disable only the schedule binding. Preserve the instance, other triggers, saved input, "
        "durable schedule and occurrence history. Already admitted work is not cancelled."
    ),
)
async def disable_schedule(
    schedule_id: ScheduleId,
    body: ScheduleToggleInput,
    request: Request,
    principal: Admin,
    executor: Executor,
) -> JSONResponse:
    return await _mutate(request, principal, executor, "disable", schedule_id, body)


@router.get(
    "/{schedule_id}/occurrences",
    response_model=ScheduleOccurrenceListResponse,
    operation_id="listScheduleOccurrences",
    description=(
        "Persisted historical occurrences ordered by (scheduled UTC instant, ID). Default 20, "
        "maximum 100; scoped keyset cursor, not a frozen snapshot. This never submits work or "
        "advances the schedule."
    ),
)
async def list_occurrences(
    schedule_id: ScheduleId,
    request: Request,
    principal: Reader,
    executor: Executor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> JSONResponse:
    _query(request, {"limit", "cursor"})
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            page = await executor.occurrences(
                schedule_id, SchedulePageQuery(limit, cursor), principal=principal
            )
        if (
            type(page) is not ScheduleOccurrencePage
            or type(page.items) is not tuple
            or len(page.items) > limit
        ):
            raise ValueError("invalid occurrence page")
        views = []
        for row in page.items:
            if type(row) is not ScheduleOccurrence or row.schedule_id != schedule_id:
                raise ValueError("occurrence scope changed")
            views.append(
                ScheduleOccurrenceResponse(
                    id=row.id,
                    schedule_id=row.schedule_id,
                    scheduled_for_utc=row.scheduled_for_utc,
                    scheduled_local=row.scheduled_local,
                    timezone=row.timezone,
                    timezone_fold=row.timezone_fold,
                    recurrence_version=row.recurrence_version,
                    state=row.state.value,
                    work_item_id=row.work_item_id,
                    run_id=row.run_id,
                    misfire_policy_applied=None
                    if row.misfire_policy_applied is None
                    else row.misfire_policy_applied.value,
                    first_missed_at_utc=row.first_missed_at_utc,
                    last_missed_at_utc=row.last_missed_at_utc,
                    missed_count=row.missed_count,
                )
            )
        result = ScheduleOccurrenceListResponse(items=tuple(views), next_cursor=page.next_cursor)
        content = result.model_dump(mode="json", by_alias=True)
        if page.next_cursor is None:
            content.pop("nextCursor")
        return JSONResponse(
            content=content,
            headers=_HEADERS,
        )
    except Exception as error:
        return _failure(request, error)
