"""Serve the fully composed loopback API without implicit initialization."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from marketing_agents.config import get_settings
from marketing_agents.workers.runtime.composition import RuntimeNotReady, build_runtime
from marketing_agents.workers.runtime.unix_listener import unix_listener


class LocalAPIServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Allow the outer runtime/socket cleanup to finish after graceful stop.

        Uvicorn normally re-raises SIGTERM after its server shuts down, before
        the caller's finally blocks. This executable owns that final cleanup.
        """
        previous = {
            signum: signal.signal(signum, self.handle_exit)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            yield
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


async def serve() -> None:
    settings = get_settings()
    runtime = await build_runtime(settings)
    try:
        server = LocalAPIServer(
            uvicorn.Config(
                runtime.create_app(),
                host=settings.api_host,
                port=settings.api_port,
                access_log=False,
                proxy_headers=False,
                timeout_graceful_shutdown=20,
            )
        )
        socket_path = os.environ.get("MARKETING_AGENTS_API_SOCKET")
        if socket_path is None:
            await server.serve()
        else:
            # Passing a prebound socket avoids Uvicorn's UDS branch chmod(0666).
            # Settings retain the original loopback-only local identity contract.
            with unix_listener(socket_path) as listener:
                await server.serve(sockets=[listener])
    finally:
        await runtime.close()


def main() -> int:
    try:
        asyncio.run(serve())
    except (RuntimeNotReady, ValueError, OSError):
        print(
            "api_startup_failed: verify transport, migration, catalog, settings and paired key",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
