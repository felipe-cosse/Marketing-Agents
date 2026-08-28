"""Raw-body authenticated webhook admission transport."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.correlation import request_correlation_id
from marketing_agents.api.dependencies import (
    WebhookAdmissionExecutor,
    get_webhook_admission_executor,
)
from marketing_agents.api.schemas.webhooks import (
    WebhookAdmissionResponse,
    WebhookDeliveryResponse,
    WebhookHttpError,
    WebhookProblem,
)
from marketing_agents.application.ports.webhooks import (
    WEBHOOK_SOURCE_ID_PATTERN,
    WEBHOOK_TRIGGER_ID_PATTERN,
)
from marketing_agents.application.services.webhook_intake import (
    MAX_WEBHOOK_BODY_BYTES,
    WebhookAdmissionCommand,
    WebhookAdmissionResult,
    WebhookAdmissionServiceError,
)
from marketing_agents.domain.webhook import WebhookReceipt

_SOURCE_PATTERN = WEBHOOK_SOURCE_ID_PATTERN
_TRIGGER_PATTERN = WEBHOOK_TRIGGER_ID_PATTERN
_WEBHOOK_PATH_PATTERN = re.compile(r"^/api/v1/webhooks/[^/]+/[^/]+$")
_NO_STORE = "no-store"
_SUBMIT_TIMEOUT_SECONDS = 5.0

_SIGNATURE_PARAMETERS: list[dict[str, object]] = [
    {
        "name": "X-Webhook-Timestamp",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "pattern": r"^(?:0|[1-9][0-9]{0,11})$"},
    },
    {
        "name": "X-Webhook-Signature",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "pattern": r"^v1=[0-9a-f]{64}$"},
    },
]
_REQUEST_BODY: dict[str, object] = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["eventId", "input"],
                "properties": {
                    "eventId": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 240,
                        "pattern": r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$",
                    },
                    "input": {"type": "object"},
                },
            }
        }
    },
}
_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_202_ACCEPTED: {"description": "Webhook work was accepted asynchronously."},
    status.HTTP_400_BAD_REQUEST: {
        "model": WebhookHttpError,
        "description": "The transport request is malformed.",
    },
    status.HTTP_401_UNAUTHORIZED: {
        "model": WebhookProblem,
        "description": "Webhook signature authentication failed.",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": WebhookProblem,
        "description": "The source or trigger has no enabled server-owned binding.",
    },
    status.HTTP_409_CONFLICT: {
        "model": WebhookProblem,
        "description": "The authenticated source event conflicts with its original body.",
    },
    status.HTTP_413_CONTENT_TOO_LARGE: {
        "model": WebhookProblem,
        "description": "The raw webhook body exceeds its fixed bound.",
    },
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
        "model": WebhookHttpError,
        "description": "The content type or encoding is unsupported.",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": WebhookProblem,
        "description": "The authenticated envelope or mapped workflow input is invalid.",
    },
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": WebhookProblem,
        "description": "The authenticated webhook source exhausted its admission window.",
        "headers": {"Retry-After": {"schema": {"type": "integer", "minimum": 1, "maximum": 3_600}}},
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": WebhookProblem,
        "description": "Webhook admission is unavailable or violated its contract.",
    },
}

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


def _problem_response(
    *,
    status_code: int,
    code: str,
    message: str,
    pointer: str | None = None,
) -> JSONResponse:
    problem = WebhookProblem(code=code, message=message, pointer=pointer)
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json", by_alias=True, exclude_none=True),
        headers={"Cache-Control": _NO_STORE},
    )


class WebhookRequestBoundsMiddleware:
    """Cap exact webhook bytes before Starlette buffers the request body."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or _WEBHOOK_PATH_PATTERN.fullmatch(str(scope.get("path", ""))) is None
        ):
            await self._app(scope, receive, send)
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
            if type(chunk) is not bytes or len(buffered) + len(chunk) > MAX_WEBHOOK_BODY_BYTES:
                await self._reject(scope, receive, send)
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(buffered), "more_body": False}
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
        return str(parsed) != raw or parsed < 0 or parsed > MAX_WEBHOOK_BODY_BYTES

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = _problem_response(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code="webhook_body_too_large",
            message="webhook body exceeds the allowed size",
        )
        await response(scope, receive, send)


def _require_json_transport(request: Request) -> None:
    content_types = request.headers.getlist("content-type")
    encodings = request.headers.getlist("content-encoding")
    if len(content_types) != 1 or encodings:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="webhook requires unencoded application/json",
            headers={"Cache-Control": _NO_STORE},
        )
    parts = [part.strip() for part in content_types[0].split(";")]
    if (
        parts[0].casefold() != "application/json"
        or (len(parts) == 2 and parts[1].casefold() != "charset=utf-8")
        or len(parts) > 2
    ):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="webhook requires unencoded application/json",
            headers={"Cache-Control": _NO_STORE},
        )


def _received_headers(request: Request) -> tuple[tuple[str, str], ...]:
    try:
        return tuple(
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in request.scope.get("headers", ())
        )
    except (AttributeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="webhook headers are malformed",
            headers={"Cache-Control": _NO_STORE},
        ) from None


def _service_problem(error: WebhookAdmissionServiceError) -> JSONResponse:
    if error.code == "webhook_authentication_failed":
        return _problem_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="webhook_authentication_failed",
            message="webhook authentication failed",
        )
    if error.code == "webhook_binding_forbidden":
        return _problem_response(
            status_code=status.HTTP_403_FORBIDDEN,
            code="webhook_forbidden",
            message="webhook source is not enabled for this trigger",
        )
    if error.code in {"webhook_idempotency_conflict", "idempotency_conflict"}:
        return _problem_response(
            status_code=status.HTTP_409_CONFLICT,
            code="webhook_idempotency_conflict",
            message="the authenticated event identity is already bound to different content",
        )
    if error.code == "webhook_rate_limited" and error.retry_after_seconds is not None:
        response = _problem_response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="webhook_rate_limited",
            message="webhook admission rate limit exceeded",
        )
        response.headers["Retry-After"] = str(error.retry_after_seconds)
        return response
    if error.code in {
        "input_byte_limit",
        "input_field_too_large",
        "input_invalid",
        "input_redaction_invalid",
        "input_schema_invalid",
        "input_secret_not_retainable",
        "json_depth_limit",
        "webhook_command_invalid",
        "webhook_envelope_invalid",
    }:
        return _problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="webhook_input_invalid",
            message="webhook input is invalid",
            pointer=error.pointer,
        )
    return _problem_response(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="webhook_unavailable",
        message="webhook admission is temporarily unavailable",
    )


def _result_is_bound(
    result: object,
    *,
    source: str,
    trigger_id: str,
) -> bool:
    if type(result) is not WebhookAdmissionResult:
        return False
    try:
        result.__post_init__()
    except (TypeError, ValueError):
        return False
    receipt = result.receipt
    return (
        type(receipt) is WebhookReceipt
        and receipt.source == source
        and receipt.trigger_id == trigger_id
        and bool(receipt.deliveries)
    )


@router.post(
    "/{source}/{trigger_id}",
    response_model=WebhookAdmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="admitWebhookEvent",
    openapi_extra={"parameters": _SIGNATURE_PARAMETERS, "requestBody": _REQUEST_BODY},
    responses=_RESPONSES,
)
async def admit_webhook_event(
    source: Annotated[str, Path(pattern=_SOURCE_PATTERN)],
    trigger_id: Annotated[str, Path(pattern=_TRIGGER_PATTERN)],
    request: Request,
    _transport: Annotated[None, Depends(_require_json_transport)],
    executor: Annotated[WebhookAdmissionExecutor, Depends(get_webhook_admission_executor)],
) -> JSONResponse:
    try:
        command = WebhookAdmissionCommand(
            source=source,
            trigger_id=trigger_id,
            raw_body=await request.body(),
            received_headers=_received_headers(request),
            correlation_id=request_correlation_id(request),
        )
    except WebhookAdmissionServiceError as error:
        return _service_problem(error)
    except Exception:
        return _problem_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="webhook_request_invalid",
            message="webhook request is malformed",
        )
    try:
        result = await asyncio.wait_for(executor.submit(command), timeout=_SUBMIT_TIMEOUT_SECONDS)
    except WebhookAdmissionServiceError as error:
        return _service_problem(error)
    except Exception:
        return _problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="webhook_unavailable",
            message="webhook admission is temporarily unavailable",
        )
    if not _result_is_bound(result, source=source, trigger_id=trigger_id):
        return _problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="webhook_unavailable",
            message="webhook admission is temporarily unavailable",
        )
    receipt = result.receipt
    response = WebhookAdmissionResponse(
        status="accepted",
        disposition=result.disposition.value,
        source=receipt.source,
        event_id=receipt.event_id,
        receipt_id=receipt.id,
        deliveries=tuple(
            WebhookDeliveryResponse(
                instance_id=item.instance_id,
                work_id=item.work_item_id,
                run_id=item.run_id,
                instance_url=f"/api/v1/agent-instances/{item.instance_id}",
                run_url=f"/api/v1/runs/{item.run_id}",
            )
            for item in receipt.deliveries
        ),
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=response.model_dump(mode="json", by_alias=True),
        headers={"Cache-Control": _NO_STORE},
    )


__all__ = ["WebhookRequestBoundsMiddleware", "router"]
