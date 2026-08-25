"""FastAPI application factory without import-time infrastructure side effects."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from marketing_agents import __version__
from marketing_agents.api.dependencies import ApprovalDecisionExecutor
from marketing_agents.api.errors import safe_request_validation_error
from marketing_agents.api.routes.approvals import router as approvals_router
from marketing_agents.api.routes.health import router as health_router
from marketing_agents.application.ports.identity import IdentityProvider
from marketing_agents.application.ports.readiness import ReadinessProbe
from marketing_agents.config import Settings, get_settings
from marketing_agents.infrastructure.adapters.identity import LocalIdentityProvider
from marketing_agents.infrastructure.readiness import LocalReadinessProbe


def create_app(
    settings: Settings | None = None,
    identity_provider: IdentityProvider | None = None,
    approval_decision_service: ApprovalDecisionExecutor | None = None,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    application = FastAPI(
        title="Marketing Agents API",
        version=__version__,
        docs_url="/docs" if active_settings.app_env != "production" else None,
        redoc_url=None,
    )
    application.state.settings = active_settings
    application.state.identity_provider = (
        identity_provider
        if identity_provider is not None
        else LocalIdentityProvider(active_settings)
    )
    application.state.approval_decision_service = approval_decision_service
    application.state.readiness_probe = (
        readiness_probe if readiness_probe is not None else LocalReadinessProbe(active_settings)
    )
    application.add_exception_handler(
        RequestValidationError,
        safe_request_validation_error,
    )
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(active_settings.trusted_hosts)
    )
    application.include_router(health_router)
    application.include_router(approvals_router)
    return application
