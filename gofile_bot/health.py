"""Tiny HTTP health server for container/platform health checks.

Render (and Docker) probe an HTTP endpoint to decide whether the service is
alive. This serves ``GET /health`` (and ``/``) with a plain ``200 OK`` and
runs on the same asyncio event loop as the bot, so the container stays a
single process.
"""

from __future__ import annotations

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


def create_health_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    return app


async def start_health_server(port: int) -> web.AppRunner:
    """Bind the health app on 0.0.0.0:*port* and return the runner."""
    runner = web.AppRunner(create_health_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("health server listening on 0.0.0.0:%d (/health)", port)
    return runner


async def stop_health_server(runner: web.AppRunner | None) -> None:
    if runner is not None:
        await runner.cleanup()
