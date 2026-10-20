"""Application assembly and lifecycle.

The bot runs in a single asyncio process together with a tiny HTTP server
exposing ``/health`` (used by Render/Docker health checks). Long polling needs
no public inbound Telegram traffic — the health port is only for the platform.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from telegram import BotCommand
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from gofile_bot.config import Settings, get_health_port, MAX_UPDATE_QUEUE
from gofile_bot.downloaders import build_registry
from gofile_bot.gofile.client import GoFileClient
from gofile_bot.health import start_health_server, stop_health_server
from gofile_bot.polling import (
    InstanceLock,
    InstanceLockError,
    PollingConflictGuard,
    start_guarded_polling,
)
from gofile_bot.services.jobs import JobStore
from gofile_bot.services.resources import MetadataTasks
from gofile_bot.downloaders.telegram_zip import TelegramZipDownloader
from gofile_bot.services.transfer import TransferService, cleanup_stale_tmpdirs
from gofile_bot.telegram import handlers

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]

BOT_COMMANDS = [
    BotCommand("start", "Welcome message"),
    BotCommand("download", "Download from any supported link"),
    BotCommand("gofile", "Download a GoFile share"),
    BotCommand("extract", "Safely extract a ZIP (20 MB max)"),
    BotCommand("status", "Queue and current job"),
    BotCommand("cancel", "Stop your queued or running job"),
    BotCommand("links", "Get fresh browser download links"),
    BotCommand("supported", "List supported sources"),
    BotCommand("help", "Usage and limits"),
    BotCommand("ping", "Check the bot is alive"),
]

#: Pasted text that might contain a downloadable link.
_LINK_TEXT_RE = (
    r"(?i)https?://|www\.|gofile\.io/|pixeldrain\.com/|mediafire\.com/|"
    r"drive\.google\.com/|docs\.google\.com/|dropbox\.com/|1drv\.ms/|"
    r"onedrive\.live\.com/|archive\.org/"
)


def build_application(settings: Settings) -> Application:
    """Build the fully wired python-telegram-bot Application."""
    builder = (Application.builder().token(settings.bot_token)
               .concurrent_updates(False).update_queue(asyncio.Queue(maxsize=MAX_UPDATE_QUEUE))
               .request(HTTPXRequest(connection_pool_size=8, connect_timeout=10,
                                     read_timeout=15, write_timeout=30, pool_timeout=5)))
    if settings.telegram_api_url:
        origin = settings.telegram_api_url.rstrip("/")
        builder = (builder.base_url(f"{origin}/bot")
                   .base_file_url(f"{origin}/file/bot").local_mode(True))
    app = builder.build()

    gofile = GoFileClient(settings)
    registry = build_registry(settings, gofile)
    from gofile_bot.downloaders.base import DownloaderRegistry
    registry = DownloaderRegistry(registry.all() + [TelegramZipDownloader(
        app.bot, local_api_url=settings.telegram_api_url,
    )])
    app.bot_data["settings"] = settings
    app.bot_data["gofile"] = gofile
    app.bot_data["downloaders"] = registry
    app.bot_data["jobs"] = JobStore()
    app.bot_data["metadata_tasks"] = MetadataTasks(capacity=2)
    uploader = None
    if settings.enable_mtproto:
        from gofile_bot.services.mtproto import MTProtoUploader

        uploader = MTProtoUploader(settings)
    app.bot_data["mtproto"] = uploader
    app.bot_data["transfer"] = TransferService(
        app.bot, registry, max_file_bytes=settings.max_file_bytes,
        local_mode=bool(settings.telegram_api_url), download_dir=settings.download_dir,
        job_timeout=settings.job_timeout,
        max_concurrent=settings.max_concurrent_downloads,
        mtproto_uploader=uploader,
    )

    app.add_handler(CommandHandler("start", handlers.start_cmd))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("ping", handlers.ping_cmd))
    app.add_handler(CommandHandler("gofile", handlers.gofile_cmd))
    app.add_handler(CommandHandler(["download", "dl"], handlers.download_cmd))
    app.add_handler(CommandHandler("links", handlers.links_cmd))
    app.add_handler(CommandHandler(["extract", "unzip"], handlers.extract_cmd))
    app.add_handler(CommandHandler("status", handlers.status_cmd))
    app.add_handler(CommandHandler("cancel", handlers.cancel_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handlers.uploaded_zip))
    app.add_handler(CallbackQueryHandler(handlers.extract_cb, pattern=r"^zx:"))
    app.add_handler(CommandHandler(["supported", "sites"], handlers.supported_cmd))
    app.add_handler(CallbackQueryHandler(handlers.download_cb, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(handlers.cancel_cb, pattern=r"^cx:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(_LINK_TEXT_RE),
            handlers.auto_link,
        )
    )
    app.add_error_handler(handlers.on_error)
    return app


async def _startup(app: Application) -> None:
    """Start the downloader clients and sweep stale temp directories."""
    await app.bot_data["gofile"].start()
    await app.bot_data["downloaders"].start()
    uploader = app.bot_data.get("mtproto")
    if uploader is not None:
        await uploader.start(expected_bot_id=app.bot.id)
        logger.info("MTProto bot uploads enabled (hard cap: 2,000,000,000 bytes per file)")
    removed = cleanup_stale_tmpdirs(
        max_age_seconds=app.bot_data["settings"].job_timeout + 3600,
        directory=app.bot_data["settings"].download_dir,
    )
    if removed:
        logger.info("removed %d stale temporary directories", removed)
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception:  # pragma: no cover - menu is cosmetic
        logger.warning("could not register bot command menu", exc_info=True)
    me = await app.bot.get_me()
    providers = ", ".join(p.label for p in app.bot_data["downloaders"].all())
    logger.info("bot started as @%s — sources: %s", me.username, providers)


async def _shutdown(app: Application) -> None:
    try:
        uploader = app.bot_data.get("mtproto")
        if uploader is not None:
            await uploader.close()
    finally:
        try:
            await app.bot_data["downloaders"].close()
        finally:
            await app.bot_data["gofile"].close()
    logger.info("bot stopped")


async def run_async(settings: Settings) -> None:
    """Run bot + health server on one event loop until SIGINT/SIGTERM."""
    # Two pollers for one BOT_TOKEN fight over every long poll (Telegram
    # answers the loser with 409 Conflict). Refuse to start a second copy on
    # this host instead of half-working; lock files never go stale because
    # the kernel releases ``flock`` when a process dies.
    lock = InstanceLock(settings.bot_token)
    if not lock.acquire():
        raise InstanceLockError(lock.busy_message())
    try:
        await _run_locked(settings)
    finally:
        lock.release()


async def _run_locked(settings: Settings) -> None:
    """Start the bot, health server and poller; cleanup on any exit path."""
    port = get_health_port()
    application = build_application(settings)

    health_runner = None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX event loops
            logger.warning("signal handler for %s not available on this platform", sig)

    try:
        # Start both clients before accepting commands. A bad MTProto login
        # fails visibly rather than silently falling back to 48 MiB delivery.
        await application.initialize()
        await _startup(application)
        health_runner = await start_health_server(port)
        if stop_event.is_set():
            # SIGTERM arrived while starting up (deploy/scale-down). Do not
            # grab updates just to drop them again on the way out.
            logger.info("shutdown requested during startup; not starting polling")
        elif application.updater:
            # Conflict (409) is expected while an old instance is still being
            # stopped during a deploy: log one actionable warning, not a
            # traceback per retry.
            await start_guarded_polling(
                application.updater,
                allowed_updates=ALLOWED_UPDATES,
                drop_pending_updates=True,
                guard=PollingConflictGuard(),
            )
        await application.start()
        logger.info("bot is running — health check on port %d (/health)", port)
        await stop_event.wait()
    finally:
        # Stop admission first; don't wait six hours for jobs during deployment.
        data = getattr(application, "bot_data", {})
        if data.get("transfer") is not None:
            data["transfer"].begin_shutdown()
        if data.get("metadata_tasks") is not None:
            await data["metadata_tasks"].close()
        # Also close clients when authorization or another startup step fails.
        try:
            if application.updater and application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
        finally:
            try:
                await _shutdown(application)
            finally:
                try:
                    await application.shutdown()
                finally:
                    if health_runner is not None:
                        await stop_health_server(health_runner)


def run(settings: Settings) -> None:
    """Run the bot until interrupted (blocking wrapper around :func:`run_async`)."""
    logger.info("starting…")
    asyncio.run(run_async(settings))
