"""Application assembly and lifecycle."""

from __future__ import annotations

import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from gofile_bot.config import Settings
from gofile_bot.gofile.client import GoFileClient
from gofile_bot.services.jobs import JobStore
from gofile_bot.services.transfer import TransferService, cleanup_stale_tmpdirs
from gofile_bot.telegram import handlers

logger = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    await app.bot_data["gofile"].start()
    removed = cleanup_stale_tmpdirs()
    if removed:
        logger.info("removed %d stale temporary directories", removed)
    me = await app.bot.get_me()
    logger.info("bot started as @%s", me.username)


async def _post_shutdown(app: Application) -> None:
    await app.bot_data["gofile"].close()
    logger.info("bot stopped")


def build_application(settings: Settings) -> Application:
    """Build the fully wired python-telegram-bot Application."""
    app = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

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


def run(settings: Settings) -> None:
    """Run the bot with long polling until interrupted."""
    app = build_application(settings)
    logger.info("starting long polling…")
    app.run_polling(
        allowed_updates=[
            "message",
            "callback_query",
        ],
        drop_pending_updates=True,
    )
