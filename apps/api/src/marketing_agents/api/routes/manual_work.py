"""Authorized, asynchronous manual dry-run admission transport."""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.dependencies import (
    ManualDryRunExecutor,
    get_manual_dry_run_executor,
    require_manual_work_operator_principal,
)
from marketing_agents.api.schemas.manual_work import (
    ManualDryRunInput,
    ManualDryRunResponse,
    ManualWorkHttpError,
    ManualWorkProblem,
    ManualWorkRequestValidationError,
)
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunResult,
    ManualDryRunServiceError,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.security.redaction import SecretValue

_INSTANCE_ID_PATTERN = r"^inst\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+\.[0-9]{2}$"
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{8,240}$")
_MAX_IDEMPOTENCY_KEY_LENGTH = 240
_MAX_MANUAL_REQUEST_BYTES = 1_048_576
_MAX_MANUAL_INPUT_DEPTH = 64
_MAX_MANUAL_REQUEST_DEPTH = _MAX_MANUAL_INPUT_DEPTH + 1
_NO_STORE = "no-store"
_VARY = "Authorization"
_SUBMIT_TIMEOUT_SECONDS = 5.0
_MANUAL_DRY_RUN_PATH_PATTERN = re.compile(r"^/api/v1/agent-instances/[^/]+/dry-runs$")
_IDEMPOTENCY_KEY_OPENAPI_PARAMETER: dict[str, object] = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": False,
    "description": (
        "An optional opaque retry key. Reuse is valid only for the exact same admission."
    ),
    "schema": {
        "type": "string",
        "pattern": _IDEMPOTENCY_KEY_PATTERN.pattern,
        "minLength": 8,
        "maxLength": _MAX_IDEMPOTENCY_KEY_LENGTH,
    },
}
_RESPONSE_HEADERS: dict[str, dict[str, object]] = {
    "Cache-Control": {
        "description": "Manual-admission responses must not be stored.",
        "schema": {"type": "string", "const": _NO_STORE},
    },
    "Vary": {
        "description": "Shared caches must separate authorization contexts.",
        "schema": {"type": "string", "const": _VARY},
    },
}
_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_202_ACCEPTED: {
        "description": "The manual work receipt was accepted for asynchronous processing.",
        "headers": _RESPONSE_HEADERS,
    },
    status.HTTP_400_BAD_REQUEST: {
        "model": ManualWorkHttpError,
        "description": "An authentication or idempotency header is malformed.",
    },
    status.HTTP_401_UNAUTHORIZED: {
        "model": ManualWorkHttpError,
        "description": "Authentication is required.",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": ManualWorkHttpError | ManualWorkProblem,
        "description": "The authenticated principal may not create manual work.",
    },
    status.HTTP_404_NOT_FOUND: {
        "model": ManualWorkProblem,
        "description": "A selected manual-admission resource does not exist.",
        "headers": _RESPONSE_HEADERS,
    },
    status.HTTP_409_CONFLICT: {
        "model": ManualWorkProblem,
        "description": "The retry key conflicts or a selected resource is unavailable.",
        "headers": _RESPONSE_HEADERS,
    },
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
        "model": ManualWorkHttpError,
        "description": "The request is not application/json.",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ManualWorkProblem | ManualWorkRequestValidationError,
        "description": "The request shape or manual-work input is invalid.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ManualWorkHttpError | ManualWorkProblem,
        "description": "Manual admission is unavailable or violated its contract.",
        "headers": _RESPONSE_HEADERS,
    },
}

_FORBIDDEN_CODES = frozenset(
    {
        "manual_work_forbidden",
        "manual_work_human_required",
        "manual_work_operator_role_missing",
    }
)
_NOT_FOUND_CODES = frozenset(
    {
        "campaign_brief_not_found",
        "campaign_brief_unknown",
        "demo_scenario_not_found",
        "demo_scenario_unknown",
        "instance_not_found",
        "instance_unknown",
    }
)
_IDEMPOTENCY_CONFLICT_CODES = frozenset(
    {
        "idempotency_conflict",
        "manual_idempotency_conflict",
    }
)
_STATE_CONFLICT_CODES = frozenset(
    {
        "campaign_brief_disabled",
        "demo_scenario_disabled",
        "instance_disabled",
        "manual_trigger_unavailable",
    }
)
_INPUT_CODES = frozenset(
    {
        "campaign_brief_invalid",
        "campaign_brief_forbidden",
        "campaign_brief_required",
        "demo_scenario_invalid",
        "execution_mode_invalid",
        "input_byte_limit",
        "input_field_too_large",
        "input_invalid",
        "input_redaction_invalid",
        "input_schema_invalid",
        "input_secret_not_retainable",
        "invalid_json",
        "json_depth_limit",
        "manual_command_invalid",
        "manual_input_invalid",
        "manual_work_command_invalid",
        "work_mode_not_allowed",
    }
)

router = APIRouter(prefix="/api/v1/agent-instances", tags=["manual-work"])


class ManualWorkRequestBoundsMiddleware:
    """Bound raw manual JSON before FastAPI buffers or recursively parses it."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or _MANUAL_DRY_RUN_PATH_PATTERN.fullmatch(str(scope.get("path", ""))) is None
        ):
            await self._app(scope, receive, send)
            return
        if self._declared_length_is_invalid(scope):
            await self._reject(scope, receive, send)
            return

        buffered = bytearray()
        depth = 0
        in_string = False
        escaped = False
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self._reject(scope, receive, send)
                return
            chunk = message.get("body", b"")
            if type(chunk) is not bytes or len(buffered) + len(chunk) > _MAX_MANUAL_REQUEST_BYTES:
                await self._reject(scope, receive, send)
                return
            buffered.extend(chunk)
            for byte in chunk:
                if in_string:
                    if escaped:
                        escaped = False
                    elif byte == 0x5C:
                        escaped = True
                    elif byte == 0x22:
                        in_string = False
                elif byte == 0x22:
                    in_string = True
                elif byte in {0x5B, 0x7B}:
                    depth += 1
                    if depth > _MAX_MANUAL_REQUEST_DEPTH:
                        await self._reject(scope, receive, send)
                        return
                elif byte in {0x5D, 0x7D}:
                    depth -= 1
            if not message.get("more_body", False):
                break

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
        return str(parsed) != raw or parsed < 0 or parsed > _MAX_MANUAL_REQUEST_BYTES

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="dry_run_input_invalid",
            message="manual dry-run input is invalid",
        )
        await response(scope, receive, send)


def _json_content_type(request: Request) -> None:
    values = request.headers.getlist("content-type")
    media_type = values[0].split(";", 1)[0].strip().casefold() if len(values) == 1 else ""
    if media_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="manual dry-run creation requires application/json",
            headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
        )


def _idempotency_key(request: Request) -> SecretValue | None:
    values = request.headers.getlist("idempotency-key")
    if not values:
        return None
    if (
        len(values) != 1
        or len(values[0]) > _MAX_IDEMPOTENCY_KEY_LENGTH
        or _IDEMPOTENCY_KEY_PATTERN.fullmatch(values[0]) is None
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must contain one valid opaque retry key",
            headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
        )
    return SecretValue(values[0])


def _problem_response(
    *,
    status_code: int,
    code: str,
    message: str,
    pointer: str | None = None,
) -> JSONResponse:
    problem = ManualWorkProblem(code=code, message=message, pointer=pointer)
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json", by_alias=True, exclude_none=True),
        headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
    )


def _service_problem(error: ManualDryRunServiceError) -> JSONResponse:
    if error.code in _FORBIDDEN_CODES:
        return _problem_response(
            status_code=status.HTTP_403_FORBIDDEN,
            code="dry_run_forbidden",
            message="manual dry-run creation is forbidden",
        )
    if error.code in _NOT_FOUND_CODES:
        return _problem_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="manual_resource_not_found",
            message="a selected manual dry-run resource was not found",
        )
    if error.code in _IDEMPOTENCY_CONFLICT_CODES:
        return _problem_response(
            status_code=status.HTTP_409_CONFLICT,
            code="idempotency_conflict",
            message="the retry key is already bound to different work",
        )
    if error.code in _STATE_CONFLICT_CODES:
        return _problem_response(
            status_code=status.HTTP_409_CONFLICT,
            code="dry_run_conflict",
            message="a selected resource cannot currently accept manual work",
        )
    if error.code in _INPUT_CODES:
        return _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="dry_run_input_invalid",
            message="manual dry-run input is invalid",
            pointer=error.pointer,
        )
    return _problem_response(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="manual_work_unavailable",
        message="manual dry-run creation is temporarily unavailable",
    )


def _transport_mode(mode: WorkMode) -> str:
    if mode is WorkMode.DRY_RUN:
        return "dry_run"
    if mode is WorkMode.MOCK_EXECUTION:
        return "mock_execute"
    raise TypeError("manual service returned an unsupported work mode")


def _input_depth_is_bounded(value: object) -> bool:
    """Check JSON depth iteratively so the check itself cannot exhaust recursion."""

    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_MANUAL_INPUT_DEPTH:
            return False
        if type(current) is dict:
            pending.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            pending.extend((item, depth + 1) for item in current)
    return True


def _result_is_bound(
    result: object,
    *,
    command: ManualDryRunCommand,
    principal: AuthenticatedPrincipal,
) -> bool:
    if type(result) is not ManualDryRunResult:
        return False
    work_item = result.work_item
    run = result.run
    try:
        principal.verify_integrity()
        if type(work_item) is not WorkItem or type(run) is not Run:
            return False
        replace(result)
        replace(work_item)
        replace(run)
        exact_payload = canonical_json_bytes(work_item.admitted_payload) == canonical_json_bytes(
            command.input_payload
        )
    except (TypeError, ValueError):
        return False
    return (
        type(result.disposition) is WorkRunReceiptDisposition
        and type(result.mode) is WorkMode
        and result.mode is command.mode
        and result.event_id == work_item.event_id
        and work_item.instance_id == command.instance_id
        and work_item.source == "manual"
        and work_item.mode is command.mode
        and (
            work_item.brief_id == command.campaign_brief_id
            if command.campaign_brief_id is not None
            else command.demo_scenario_id is not None or work_item.brief_id is None
        )
        and exact_payload
        and run.work_item_id == work_item.id
        and run.configuration_revision == work_item.configuration_revision
    )


@router.post(
    "/{instance_id}/dry-runs",
    response_model=ManualDryRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="createAgentInstanceDryRun",
    openapi_extra={"parameters": [_IDEMPOTENCY_KEY_OPENAPI_PARAMETER]},
    responses=_RESPONSES,
)
async def create_agent_instance_dry_run(
    instance_id: Annotated[str, Path(pattern=_INSTANCE_ID_PATTERN)],
    request: Request,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_manual_work_operator_principal),
    ],
    _content_type: Annotated[None, Depends(_json_content_type)],
    idempotency_key: Annotated[SecretValue | None, Depends(_idempotency_key)],
    executor: Annotated[ManualDryRunExecutor, Depends(get_manual_dry_run_executor)],
    body: ManualDryRunInput,
) -> JSONResponse:
    if not _input_depth_is_bounded(body.input):
        return _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="dry_run_input_invalid",
            message="manual dry-run input is invalid",
            pointer="/input",
        )
    mode = WorkMode.DRY_RUN if body.execution_mode == "dry_run" else WorkMode.MOCK_EXECUTION
    try:
        command = ManualDryRunCommand(
            instance_id=instance_id,
            input_payload=body.input,
            mode=mode,
            idempotency_key=idempotency_key,
            campaign_brief_id=body.campaign_brief_id,
            demo_scenario_id=body.demo_scenario_id,
            correlation_id=f"correlation.manual-api.{secrets.token_hex(16)}",
        )
    except (TypeError, ValueError):
        return _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="dry_run_input_invalid",
            message="manual dry-run input is invalid",
        )
    try:
        result = await asyncio.wait_for(
            executor.submit(command, principal=principal),
            timeout=_SUBMIT_TIMEOUT_SECONDS,
        )
    except ManualDryRunServiceError as error:
        return _service_problem(error)
    except Exception:
        return _problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="manual_work_unavailable",
            message="manual dry-run creation is temporarily unavailable",
        )
    if not _result_is_bound(result, command=command, principal=principal):
        return _problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="manual_work_unavailable",
            message="manual dry-run creation is temporarily unavailable",
        )
    response = ManualDryRunResponse(
        status="accepted",
        disposition=result.disposition.value,
        event_id=result.event_id,
        work_id=result.work_item.id,
        run_id=result.run.id,
        execution_mode=_transport_mode(result.mode),
        instance_url=f"/api/v1/agent-instances/{instance_id}",
        run_url=f"/api/v1/runs/{result.run.id}",
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=response.model_dump(mode="json", by_alias=True),
        headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
    )
