"""FastAPI application factory without import-time infrastructure side effects."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from marketing_agents import __version__
from marketing_agents.api.routes.health import router as health_router
from marketing_agents.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    application = FastAPI(
        title="Marketing Agents API",
        version=__version__,
        docs_url="/docs" if active_settings.app_env != "production" else None,
        redoc_url=None,
    )
    application.state.settings = active_settings
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(active_settings.trusted_hosts)
    )
    application.include_router(health_router)
    return application
