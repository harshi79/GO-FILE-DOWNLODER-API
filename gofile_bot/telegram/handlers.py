"""Bot handlers: commands, download/cancel callbacks, and error logging."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from gofile_bot.config import MAX_FOLDER_FILES
from gofile_bot.errors import (
    BotError,
    InvalidURLError,
    JobBusyError,
    JobExpiredError,
)
from gofile_bot.gofile.client import GoFileClient
from gofile_bot.gofile.models import RemoteFolder
from gofile_bot.gofile.urls import parse_share
from gofile_bot.services.jobs import Job, JobStore
from gofile_bot.services.transfer import TransferService
from gofile_bot.telegram import formatting as fmt
from gofile_bot.telegram.keyboards import download_keyboard

logger = logging.getLogger(__name__)

START_TEXT = (
    "👋 Hi! I download files from GoFile links and send them right here.\n"
    "\n"
    "Send me a link like this:\n"
    "/gofile https://gofile.io/d/xxxxxx\n"
    "\n"
    "For password-protected folders add the password:\n"
    "/gofile https://gofile.io/d/xxxxxx mypassword\n"
    "\n"
    "Type /help for details."
)

HELP_TEXT = (
    "📖 <b>Help</b>\n"
    "\n"
    "<b>Commands</b>\n"
    "/start — welcome message\n"
    "/help — this message\n"
    "/gofile &lt;url&gt; — fetch a GoFile share\n"
    "/gofile &lt;url&gt; &lt;password&gt; — password-protected shares\n"
    "\n"
    "<b>How it works</b>\n"
    "1. Send /gofile with a gofile.io link.\n"
    "2. The bot shows the file name, size and type.\n"
    "3. Tap 「 DOWNLOAD 」 and the bot sends the file here.\n"
    "\n"
    "<b>Good to know</b>\n"
    "• Only gofile.io share links are accepted.\n"
    "• Files must fit Telegram's upload limit (about 50 MB).\n"
    f"• Folder shares: the first {MAX_FOLDER_FILES} sendable files are delivered.\n"
    "• You can tap Cancel while a download is running.\n"
    "• Passwords are used to open the share and are never stored.\n"
    "\n"
    "Links: send /gofile with a link from gofile.io/d/…"
)

USAGE_HINT = (
    "Use /gofile with a GoFile link:\n"
    "/gofile https://gofile.io/d/xxxxxx"
)


def _bot_data(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data


def _jobs(context: ContextTypes.DEFAULT_TYPE) -> JobStore:
    return _bot_data(context)["jobs"]


def _gofile(context: ContextTypes.DEFAULT_TYPE) -> GoFileClient:
    return _bot_data(context)["gofile"]


def _transfer(context: ContextTypes.DEFAULT_TYPE) -> TransferService:
    return _bot_data(context)["transfer"]


def parse_command_args(text: str) -> tuple[str, str | None]:
    """Split ``/gofile <url> [password]`` into url and optional password.

    The password is everything after the first space of the argument text, so
    it may itself contain spaces.
    """
    parts = (text or "").split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        raise InvalidURLError("missing URL")
    url, sep, password = rest.partition(" ")
    return url.strip(), (password.strip() or None) if sep else None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(START_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_html(HELP_TEXT)


async def gofile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /gofile <url> [password]."""
    message = update.message
    if message is None:
        return

    jobs = _jobs(context)

    try:
        url, password = parse_command_args(message.text or "")
        content_id = parse_share(url)
    except BotError as exc:
        await message.reply_text(exc.user_message)
        return

    if update.effective_user and jobs.active_for_user(update.effective_user.id):
        await message.reply_text(JobBusyError.user_message)
        return

    status = await message.reply_text(fmt.fetch_status_text())

    job = jobs.create(
        user_id=update.effective_user.id if update.effective_user else 0,
        chat_id=message.chat_id,
        status_message_id=status.message_id,
        content_id=content_id,
        password=password,
    )

    status_helper = _status(context, job)
    try:
        content = await _gofile(context).fetch_content(
            content_id,
            password=password,
        )
        job.content = content
    except BotError as exc:
        logger.info("fetch failed for %s: %s", content_id, exc.detail or exc)
        await status_helper.edit(exc.user_message, None, force=True)
        return
    except Exception:
        logger.exception("unexpected error fetching %s", content_id)
        await status_helper.edit(
            "⚠️ Something went wrong while contacting GoFile. Please try again.",
            None,
            force=True,
        )
        return

    extra = ""
    if isinstance(content, RemoteFolder) and content.file_count > MAX_FOLDER_FILES:
        extra = (
            f"\n\nℹ️ Large folder: the first {MAX_FOLDER_FILES} sendable "
            "files will be delivered."
        )
    await status_helper.edit(
        fmt.info_text(content) + extra,
        download_keyboard(job.id),
        force=True,
    )


def _status(context: ContextTypes.DEFAULT_TYPE, job: Job):
    from gofile_bot.services.status import StatusMessage

    return StatusMessage(context.application.bot, job.chat_id, job.status_message_id)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
async def download_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    job_id = (query.data or "")[3:]
    job = _jobs(context).get(job_id)
    if job is None:
        await query.answer(text=JobExpiredError.user_message, show_alert=True)
        return

    user_id = query.from_user.id if query.from_user else 0
    if job.user_id != user_id:
        await query.answer(
            text="This button isn't for you. Send your own /gofile link.",
            show_alert=True,
        )
        return

    if job.running:
        await query.answer(text=JobBusyError.user_message, show_alert=True)
        return

    await query.answer(text="Starting download…")

    job.cancel_event.clear()
    job.running = True
    try:
        await _transfer(context).run(job)
    except Exception:  # pragma: no cover - transfer.run already contains errors
        logger.exception("transfer crashed for job %s", job.id)
        job.running = False


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    job_id = (query.data or "")[3:]
    job = _jobs(context).get(job_id)
    if job is None:
        await query.answer(text=JobExpiredError.user_message, show_alert=True)
        return

    user_id = query.from_user.id if query.from_user else 0
    if job.user_id != user_id:
        await query.answer(
            text="This button isn't for you.", show_alert=True
        )
        return

    job.cancel_event.set()
    await query.answer(text="Cancelling…")


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
async def link_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Friendly nudge when someone pastes a link without the command."""
    if update.message and not (update.message.text or "").startswith("/"):
        await update.message.reply_text(USAGE_HINT)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing update", exc_info=context.error)
