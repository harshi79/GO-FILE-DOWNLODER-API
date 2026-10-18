"""Application assembly and lifecycle.

The bot runs in a single asyncio process together with a tiny HTTP server
exposing ``/health`` (used by Render/Docker health checks). Long polling needs
no public inbound Telegram traffic — the health port is only for the platform.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from gofile_bot.config import Settings, get_health_port
from gofile_bot.gofile.client import GoFileClient
from gofile_bot.health import start_health_server, stop_health_server
from gofile_bot.services.jobs import JobStore
from gofile_bot.services.transfer import TransferService, cleanup_stale_tmpdirs
from gofile_bot.telegram import handlers

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]


def build_application(settings: Settings) -> Application:
    """Build the fully wired python-telegram-bot Application."""
    app = Application.builder().token(settings.bot_token).concurrent_updates(True).build()

    gofile = GoFileClient(settings)
    app.bot_data["settings"] = settings
    app.bot_data["gofile"] = gofile
    app.bot_data["jobs"] = JobStore()
    app.bot_data["transfer"] = TransferService(app.bot, gofile)

    app.add_handler(CommandHandler("start", handlers.start_cmd))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("gofile", handlers.gofile_cmd))
    app.add_handler(CallbackQueryHandler(handlers.download_cb, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(handlers.cancel_cb, pattern=r"^cx:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"gofile\.io"),
            handlers.link_hint,
        )
    )
    app.add_error_handler(handlers.on_error)
    return app


async def _startup(app: Application) -> None:
    """Start the GoFile client and sweep stale temp directories."""
    await app.bot_data["gofile"].start()
    removed = cleanup_stale_tmpdirs()
    if removed:
        logger.info("removed %d stale temporary directories", removed)
    me = await app.bot.get_me()
    logger.info("bot started as @%s", me.username)


async def _shutdown(app: Application) -> None:
    await app.bot_data["gofile"].close()
    logger.info("bot stopped")


async def run_async(settings: Settings) -> None:
    """Run bot + health server on one event loop until SIGINT/SIGTERM."""
    port = get_health_port()
    application = build_application(settings)

    # Same order PTB's run_polling uses internally.
    await application.initialize()
    await _startup(application)

    health_runner = await start_health_server(port)

    if application.updater:
        await application.updater.start_polling(
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
    await application.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX event loops
            logger.warning("signal handler for %s not available on this platform", sig)

    logger.info("bot is running — health check on port %d (/health)", port)
    try:
        await stop_event.wait()
    except asyncio.CancelledError:  # pragma: no cover - defensive
        pass
    logger.info("shutdown signal received")

    try:
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await _shutdown(application)
    finally:
        await application.shutdown()
        await stop_health_server(health_runner)


def run(settings: Settings) -> None:
    """Run the bot until interrupted (blocking wrapper around :func:`run_async`)."""
    logger.info("starting…")
    asyncio.run(run_async(settings))
