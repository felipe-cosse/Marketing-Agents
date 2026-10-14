"""Standalone campaign briefs; no workflow execution is implied by creation."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.correlation import request_correlation_id
from marketing_agents.api.dependencies import (
    require_manual_work_operator_principal,
    require_runtime_resource_reader_principal,
)
from marketing_agents.api.errors import problem_details
from marketing_agents.api.schemas.campaign_briefs import (
    CampaignBriefCreated,
    CampaignBriefInput,
    CampaignBriefResource,
)
from marketing_agents.api.schemas.problems import ProblemDetails
from marketing_agents.api.strict_json import (
    StrictJsonTransportError,
    strict_json_route_path,
    strict_json_transport_headers_are_valid,
    validate_strict_json_body,
)
from marketing_agents.application.services.campaign_briefs import (
    BRIEF_ID_PATTERN,
    IDEMPOTENCY_KEY_PATTERN,
    MAX_BRIEF_BYTES,
    MAX_BRIEF_DEPTH,
    CampaignBriefError,
    CampaignBriefResult,
    CampaignBriefView,
    CreateCampaignBriefCommand,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.security.redaction import SecretValue

_HEADERS = {"Cache-Control": "no-store", "Vary": "Authorization"}
_PATH = "/api/v1/campaign-briefs"
_ERRORS = {
    "campaign_brief_transport_invalid": (400, "campaign brief request transport is invalid"),
    "campaign_brief_payload_too_large": (413, "campaign brief payload is too large"),
    "campaign_brief_media_type_unsupported": (
        415,
        "campaign brief creation requires application/json",
    ),
    "campaign_brief_invalid": (422, "campaign brief input is invalid"),
    "campaign_brief_retry_key_invalid": (400, "one valid Idempotency-Key is required"),
    "campaign_brief_secret_not_retainable": (422, "secret-classified brief content is forbidden"),
    "campaign_brief_forbidden": (403, "campaign brief access is forbidden"),
    "campaign_brief_not_found": (404, "campaign brief was not found"),
    "campaign_brief_idempotency_conflict": (409, "retry key is bound to different brief content"),
    "campaign_brief_unavailable": (503, "campaign brief service is unavailable"),
}
_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ProblemDetails} for code in (400, 401, 403, 404, 409, 413, 415, 422, 503)
}


class CampaignBriefExecutor(Protocol):
    async def create(
        self, *, principal: AuthenticatedPrincipal, command: CreateCampaignBriefCommand
    ) -> CampaignBriefResult: ...

    async def read(
        self, *, principal: AuthenticatedPrincipal, brief_id: str
    ) -> CampaignBriefView: ...


def get_campaign_brief_executor(request: Request) -> CampaignBriefExecutor:
    service = getattr(request.app.state, "campaign_brief_service", None)
    if not callable(getattr(service, "create", None)) or not callable(
        getattr(service, "read", None)
    ):
        raise HTTPException(503, "campaign brief service is unavailable", headers=_HEADERS)
    return cast(CampaignBriefExecutor, service)


def _problem(code: str, request: Request) -> JSONResponse:
    safe_code = code if code in _ERRORS else "campaign_brief_unavailable"
    status_code, message = _ERRORS[safe_code]
    value = problem_details(
        status_code=status_code,
        code=safe_code,
        detail=message,
        correlation_id=request_correlation_id(request),
    )
    return JSONResponse(
        status_code=status_code,
        headers=_HEADERS,
        media_type="application/problem+json",
        content=value.model_dump(mode="json", exclude_none=True),
    )


class CampaignBriefRequestBoundsMiddleware:
    """Reject ambiguous/oversized JSON before framework deserialization."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or strict_json_route_path(scope).rstrip("/") != _PATH
        ):
            await self._app(scope, receive, send)
            return
        if not strict_json_transport_headers_are_valid(scope):
            await _problem("campaign_brief_media_type_unsupported", Request(scope))(
                scope, receive, send
            )
            return
        lengths = [
            value for key, value in scope.get("headers", ()) if key.lower() == b"content-length"
        ]
        try:
            if lengths and (
                len(lengths) != 1
                or not lengths[0].isdigit()
                or str(int(lengths[0])).encode("ascii") != lengths[0]
            ):
                raise ValueError("invalid length")
            if lengths and int(lengths[0]) > MAX_BRIEF_BYTES:
                raise CampaignBriefError("campaign_brief_payload_too_large")
            buffer = bytearray()
            while True:
                message = await receive()
                chunk = message.get("body", b"")
                if message["type"] != "http.request" or type(chunk) is not bytes:
                    raise ValueError("invalid body")
                if len(buffer) + len(chunk) > MAX_BRIEF_BYTES:
                    raise CampaignBriefError("campaign_brief_payload_too_large")
                buffer.extend(chunk)
                if not message.get("more_body", False):
                    break
            validate_strict_json_body(bytes(buffer), max_depth=MAX_BRIEF_DEPTH + 1)
        except CampaignBriefError as error:
            await _problem(error.code, Request(scope))(scope, receive, send)
            return
        except (ValueError, StrictJsonTransportError):
            await _problem("campaign_brief_transport_invalid", Request(scope))(scope, receive, send)
            return
        delivered = False

        async def buffered_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(buffer), "more_body": False}
            return await receive()

        await self._app(scope, buffered_receive, send)


def _resource(view: CampaignBriefView) -> CampaignBriefResource:
    if (
        type(view) is not CampaignBriefView
        or re.fullmatch(BRIEF_ID_PATTERN, view.id) is None
        or type(view.revision) is not int
        or view.revision != 1
        or type(view.expired) is not bool
        or (view.details is None) != view.expired
    ):
        raise ValueError("brief service result is invalid")
    return CampaignBriefResource(
        id=view.id,
        revision=1,
        created_at=view.created_at,
        expires_at=view.expires_at,
        classification=view.classification,
        expired=view.expired,
        details=None if view.details is None else json.loads(canonical_json_bytes(view.details)),
    )


router = APIRouter(prefix=_PATH, tags=["campaign-briefs"])


@router.post(
    "",
    response_model=CampaignBriefCreated,
    status_code=201,
    responses={
        **_RESPONSES,
        200: {"model": CampaignBriefCreated, "description": "Exact durable replay"},
    },
    openapi_extra={
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {
                    "type": "string",
                    "minLength": 8,
                    "maxLength": 240,
                    "pattern": IDEMPOTENCY_KEY_PATTERN,
                },
            }
        ]
    },
)
async def create_campaign_brief(
    request: Request,
    body: CampaignBriefInput,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_manual_work_operator_principal)],
    service: Annotated[CampaignBriefExecutor, Depends(get_campaign_brief_executor)],
) -> JSONResponse:
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or re.fullmatch(IDEMPOTENCY_KEY_PATTERN, keys[0]) is None:
        return _problem("campaign_brief_retry_key_invalid", request)
    try:
        command = CreateCampaignBriefCommand(
            title=body.title,
            objective=body.objective,
            constraints=body.constraints,
            source_refs=tuple(body.source_refs),
            idempotency_key=SecretValue(keys[0]),
            correlation_id=request_correlation_id(request),
        )
        async with asyncio.timeout(5):
            result = await service.create(principal=principal, command=command)
        if type(result) is not CampaignBriefResult or result.disposition not in {
            "created",
            "replayed",
        }:
            raise ValueError("brief service result is invalid")
        response = CampaignBriefCreated(
            disposition=result.disposition, resource=_resource(result.resource)
        )
        return JSONResponse(
            status_code=201 if result.disposition == "created" else 200,
            content=response.model_dump(mode="json", by_alias=True),
            headers={**_HEADERS, "Location": f"{_PATH}/{result.resource.id}"},
        )
    except CampaignBriefError as error:
        return _problem(error.code, request)
    except Exception:
        return _problem("campaign_brief_unavailable", request)


@router.get("/{brief_id}", response_model=CampaignBriefResource, responses=_RESPONSES)
async def read_campaign_brief(
    request: Request,
    brief_id: Annotated[str, Path(pattern=BRIEF_ID_PATTERN, max_length=70)],
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_runtime_resource_reader_principal)
    ],
    service: Annotated[CampaignBriefExecutor, Depends(get_campaign_brief_executor)],
) -> JSONResponse:
    try:
        async with asyncio.timeout(5):
            result = await service.read(principal=principal, brief_id=brief_id)
        if result.id != brief_id:
            raise ValueError("brief result does not match requested resource")
        return JSONResponse(
            content=_resource(result).model_dump(mode="json", by_alias=True), headers=_HEADERS
        )
    except CampaignBriefError as error:
        return _problem(error.code, request)
    except Exception:
        return _problem("campaign_brief_unavailable", request)
