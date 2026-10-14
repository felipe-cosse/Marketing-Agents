"""HTTP transport diagnostics without URL, body, header, or exception logging."""

from __future__ import annotations

from time import monotonic
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.observability import LocalTelemetry


class LocalTelemetryMiddleware:
    def __init__(self, app: ASGIApp, *, telemetry: LocalTelemetry) -> None:
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation_id = f"request.{uuid4().hex}"
        started = monotonic()
        status = 500

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            with self.telemetry.bind():
                await self.app(scope, receive, observed_send)
        finally:
            self.telemetry.request(status, max(0, monotonic() - started), correlation_id)
