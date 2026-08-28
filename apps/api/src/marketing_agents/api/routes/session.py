"""Authenticated same-origin local session bootstrap."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from marketing_agents.api.csrf import ProcessLocalCsrfToken
from marketing_agents.api.dependencies import require_authenticated_principal
from marketing_agents.api.schemas.session import SessionResponse
from marketing_agents.config import Settings
from marketing_agents.domain.identity import AuthenticatedPrincipal

router = APIRouter(prefix="/api/v1", tags=["session"])


def _process_state(request: Request) -> tuple[Settings, ProcessLocalCsrfToken]:
    settings = getattr(request.app.state, "settings", None)
    csrf_token = getattr(request.app.state, "csrf_token", None)
    if type(settings) is not Settings or type(csrf_token) is not ProcessLocalCsrfToken:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "session_unavailable"},
        )
    return settings, csrf_token


@router.get(
    "/session",
    response_model=SessionResponse,
    operation_id="getSession",
    responses={
        status.HTTP_200_OK: {
            "description": "The private process-local control-plane session.",
            "headers": {
                "Cache-Control": {
                    "schema": {"type": "string", "const": "no-store"},
                }
            },
        }
    },
)
async def get_session(
    request: Request,
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_authenticated_principal),
    ],
) -> SessionResponse:
    settings, csrf_token = _process_state(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization, Origin"
    return SessionResponse(
        actor_id=principal.actor_id,
        roles=tuple(sorted(principal.roles)),
        scopes=tuple(sorted(principal.scopes)),
        auth_mode=settings.auth_mode,
        environment=settings.app_env,
        model_mode="mock" if settings.llm_provider == "mock" else "real",
        connector_mode=settings.connector_mode,
        network_permission=settings.allow_external_network,
        warning="Local identity — not production authentication",
        csrf_token=csrf_token.token_for_same_origin_session(),
        csrf_header_name="X-CSRF-Token",
    )


__all__ = ["router"]
