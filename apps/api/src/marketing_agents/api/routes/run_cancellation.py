"""Bounded, authenticated cancellation with explicit best-effort response semantics."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.correlation import request_correlation_id
from marketing_agents.api.dependencies import (
    RunCancellationExecutor,
    get_run_cancellation_executor,
    require_run_cancellation_operator_principal,
)
from marketing_agents.api.errors import problem_details
from marketing_agents.api.schemas.problems import ProblemDetails
from marketing_agents.api.schemas.run_cancellation import (
    RunCancellationInput,
    RunCancellationResponse,
)
from marketing_agents.api.strict_json import (
    StrictJsonTransportError,
    strict_json_route_path,
    strict_json_transport_headers_are_valid,
    validate_strict_json_body,
)
from marketing_agents.application.services.run_cancellation_command import (
    RunCancellationCommand,
    RunCancellationCommandError,
    validate_cancellation_outcome,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal

_PATH = re.compile(r"^/api/v1/runs/[^/]+/cancel/?$")
_MAX_BYTES = 1_024
_CANCELLATION_TIMEOUT_SECONDS = 5.0
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Vary": "Authorization"}
router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


def _problem(request: Request, status_code: int, code: str) -> JSONResponse:
    problem = problem_details(
        status_code=status_code,
        correlation_id=request_correlation_id(request),
        code=code,
        detail=(
            "Cancellation outcome is unconfirmed. Inspect the run and timeline before retrying; "
            "cancellation may already have committed."
            if status_code == 503
            else None
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
        headers=_PRIVATE_HEADERS,
    )


class RunCancellationRequestBoundsMiddleware:
    """Bound and disambiguate JSON before FastAPI body buffering and validation."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or _PATH.fullmatch(strict_json_route_path(scope)) is None
        ):
            await self._app(scope, receive, send)
            return

        async def reject(status_code: int, code: str) -> None:
            await _problem(Request(scope), status_code, code)(scope, receive, send)

        if not strict_json_transport_headers_are_valid(scope):
            await reject(415, "cancellation_json_required")
            return
        lengths = [
            value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
        ]
        if lengths:
            try:
                if len(lengths) != 1:
                    raise ValueError("ambiguous length")
                raw = lengths[0].decode("ascii")
                length = int(raw)
                if length < 0 or str(length) != raw:
                    raise ValueError("invalid length")
            except (UnicodeDecodeError, ValueError):
                await reject(400, "cancellation_transport_invalid")
                return
            if length > _MAX_BYTES:
                await reject(413, "cancellation_body_too_large")
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await reject(400, "cancellation_transport_invalid")
                return
            chunk = message.get("body", b"")
            if type(chunk) is not bytes or len(body) + len(chunk) > _MAX_BYTES:
                await reject(413, "cancellation_body_too_large")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        try:
            validate_strict_json_body(bytes(body), max_depth=8)
        except StrictJsonTransportError:
            await reject(422, "cancellation_input_invalid")
            return
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self._app(scope, bounded_receive, send)


@router.post(
    "/{run_id}/cancel",
    operation_id="cancelRun",
    response_model=RunCancellationResponse,
    responses={
        code: {"model": ProblemDetails} for code in (400, 401, 403, 404, 409, 413, 415, 422, 503)
    },
    description=(
        "A human operator may cancel queued work and fence future calls. In-flight or completed "
        "effects are not reversed. Preserved IDs may be in flight or already terminal, and effect "
        "counts are snapshots at cancellation, not final totals. An unavailable response may "
        "follow a committed cancellation: inspect the run/timeline before retrying."
    ),
)
async def cancel_run(
    request: Request,
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_run_cancellation_operator_principal)
    ],
    executor: Annotated[RunCancellationExecutor, Depends(get_run_cancellation_executor)],
    run_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$")],
    body: RunCancellationInput,
) -> RunCancellationResponse | JSONResponse:
    del body
    if request.query_params:
        raise HTTPException(422, detail={"code": "cancellation_input_invalid"})
    try:
        async with asyncio.timeout(_CANCELLATION_TIMEOUT_SECONDS):
            outcome = await executor.request(
                RunCancellationCommand(run_id, request_correlation_id(request)), principal=principal
            )
        validate_cancellation_outcome(outcome, run_id=run_id)
        result = RunCancellationResponse(
            run_id=outcome.run.id,
            state="cancelled",
            version=outcome.run.version,
            cancelled_at=outcome.cancelled_at,
            cancelled_step_ids=outcome.cancelled_step_ids,
            preserved_step_ids=outcome.preserved_step_ids,
            cancelled_action_ids=outcome.cancelled_action_ids,
            preserved_action_ids=outcome.preserved_action_ids,
            succeeded_effect_count_at_cancellation=outcome.succeeded_effect_count,
            outcome_unknown_effect_count_at_cancellation=outcome.outcome_unknown_effect_count,
            effects_reversed=False,
            run_url=f"/api/v1/runs/{run_id}",
            timeline_url=f"/api/v1/runs/{run_id}/timeline",
        )
    except RunCancellationCommandError as error:
        error_code = error.code if type(error.code) is str else "cancellation_unavailable"
        status_code = {
            "cancellation_forbidden": 403,
            "cancellation_input_invalid": 422,
            "run_not_found": 404,
            "cancellation_conflict": 409,
        }.get(error_code, 503)
        return _problem(
            request, status_code, error_code if status_code != 503 else "cancellation_unavailable"
        )
    except Exception:
        return _problem(request, 503, "cancellation_unavailable")
    response.headers.update(_PRIVATE_HEADERS)
    return result
