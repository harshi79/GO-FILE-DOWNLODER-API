"""Bot handlers: commands, download/cancel callbacks, and error logging.

One flow powers every source: pick the provider for a link (forced by
``/gofile``, or auto-detected for ``/download`` and pasted links), resolve it
to an info card with a DOWNLOAD button, then let the transfer service stream
the file. Providers live in :mod:`gofile_bot.downloaders`.
"""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

from gofile_bot.config import MAX_FOLDER_FILES
from gofile_bot.downloaders.base import DownloaderRegistry
from gofile_bot.errors import (
    BotError,
    InvalidURLError,
    JobBusyError,
    JobExpiredError,
    UnsupportedURLError,
)
from gofile_bot.gofile.models import RemoteFolder
from gofile_bot.gofile.urls import parse_share
from gofile_bot.services.jobs import Job, JobStore
from gofile_bot.services.transfer import TransferService
from gofile_bot.telegram import formatting as fmt
from gofile_bot.telegram.keyboards import download_keyboard

logger = logging.getLogger(__name__)

START_TEXT = (
    "👋 Hi! I download files from share links and send them right here.\n"
    "\n"
    "Just paste a link — GoFile, Google Drive, Dropbox, OneDrive, "
    "MediaFire, Pixeldrain, archive.org, or a direct file link — and tap "
    "「 DOWNLOAD 」.\n"
    "\n"
    "Examples:\n"
    "https://gofile.io/d/xxxxxx\n"
    "https://drive.google.com/file/d/…/view\n"
    "/download https://example.com/file.zip\n"
    "\n"
    "Password-protected GoFile shares:\n"
    "/gofile https://gofile.io/d/xxxxxx mypassword\n"
    "\n"
    "Type /help for details, /supported for all sources.\n"
    "\n"
    f"{fmt.MADE_BY}"
)

HELP_TEXT = (
    "📖 <b>Help</b>\n"
    "\n"
    "<b>Commands</b>\n"
    "/start — welcome message\n"
    "/help — this message\n"
    "/download &lt;url&gt; — download from any supported link (alias /dl)\n"
    "/gofile &lt;url&gt; [password] — GoFile shares\n"
    "/supported — every source I can download from\n"
    "/ping — check I'm alive\n"
    "\n"
    "<b>How it works</b>\n"
    "1. Paste a link (no command needed) or use /download.\n"
    "2. The bot shows the file name, size and type.\n"
    "3. Tap 「 DOWNLOAD 」 and the bot sends the file here.\n"
    "\n"
    "<b>Supported sources</b>\n"
    "GoFile · Google Drive · Dropbox · OneDrive · MediaFire · Pixeldrain · "
    "Internet Archive · direct http(s) file links\n"
    "\n"
    "<b>Good to know</b>\n"
    "• Only public links work — private/login-only content can't be opened.\n"
    "• Files must fit Telegram's upload limit (about 50 MB).\n"
    f"• Folder shares: the first {MAX_FOLDER_FILES} sendable files are delivered.\n"
    "• You can tap Cancel while a download is running.\n"
    "• Passwords are used to open the share and are never stored.\n"
    "\n"
    f"{fmt.MADE_BY}"
)

USAGE_HINT = (
    "Use /download with a link, or just paste one:\n"
    "/download https://gofile.io/d/xxxxxx"
)

DOWNLOAD_USAGE = (
    "Use it like this:\n"
    "/download <url>\n"
    "…or simply paste the link. /supported lists all sources."
)

_PASTED_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE_HOST_RE = re.compile(
    r"\b(?:www\.)?(?:gofile\.io|pixeldrain\.com|mediafire\.com|"
    r"drive\.google\.com|docs\.google\.com|dropbox\.com|1drv\.ms|"
    r"onedrive\.live\.com|archive\.org)/[^\s<>\"']+",
    re.IGNORECASE,
)
_TRAILING_PUNCT = ").,]}'\""

#: Extensions that make a pasted generic URL "look like a file" (so random
#: web-page links pasted in chat don't trigger the direct downloader).
_FILE_EXTENSIONS = (
    ".7z", ".apk", ".avi", ".bin", ".bz2", ".csv", ".dmg", ".doc", ".docx",
    ".epub", ".exe", ".flac", ".gif", ".gz", ".iso", ".jpg", ".jpeg", ".m4a",
    ".mkv", ".mov", ".mp3", ".mp4", ".msi", ".ogg", ".pdf", ".png", ".ppt",
    ".pptx", ".rar", ".tar", ".wav", ".webm", ".webp", ".xls", ".xlsx", ".zip",
)


def extract_first_url(text: str) -> str | None:
    """Find the first URL in *text* (scheme optional for known hosts)."""
    match = _PASTED_URL_RE.search(text or "")
    if match:
        return match.group(0).rstrip(_TRAILING_PUNCT)
    match = _BARE_HOST_RE.search(text or "")
    if match:
        return "https://" + match.group(0).rstrip(_TRAILING_PUNCT)
    return None


def _bot_data(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data


def _jobs(context: ContextTypes.DEFAULT_TYPE) -> JobStore:
    return _bot_data(context)["jobs"]


def _downloaders(context: ContextTypes.DEFAULT_TYPE) -> DownloaderRegistry:
    data = _bot_data(context)
    registry = data.get("downloaders")
    if registry is None:
        # Legacy wiring (tests, embeds): wrap the bare GoFile client.
        from gofile_bot.downloaders.gofile_provider import GoFileDownloader

        registry = DownloaderRegistry([GoFileDownloader(data["gofile"])])
        data["downloaders"] = registry
    return registry


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


def looks_like_supported_link(url: str, registry: DownloaderRegistry) -> bool:
    """True when a *pasted* link should auto-start the download flow.

    Known hosts always qualify; generic (direct) URLs only when the path
    ends in a file extension, so pasted articles/pages stay untouched.
    """
    provider = registry.find(url)
    if provider is None:
        return False
    if provider.name != "direct":
        return True
    path = url.split("?", 1)[0].lower()
    return path.endswith(_FILE_EXTENSIONS)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(START_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_html(HELP_TEXT)


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("🏓 Pong! I'm alive.")


async def supported_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    lines = ["📥 <b>Supported sources</b>\n"]
    for provider in _downloaders(context).all():
        lines.append(f"• <b>{fmt.esc(provider.label)}</b> — {fmt.esc(provider.description)}")
        for example in provider.examples:
            lines.append(f"   <code>{fmt.esc(example)}</code>")
    lines.append("\nJust paste a link, or use /download &lt;url&gt;.")
    await update.message.reply_html("\n".join(lines))


async def gofile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /gofile <url> [password]."""
    message = update.message
    if message is None:
        return
    try:
        url, password = parse_command_args(message.text or "")
        content_id = parse_share(url)
    except BotError as exc:
        await message.reply_text(exc.user_message)
        return
    await _start_link_flow(
        update, context, url, password=password,
        provider_name="gofile", content_id=content_id,
    )


async def download_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /download <url> [password] (alias /dl) — any supported source."""
    message = update.message
    if message is None:
        return
    try:
        url, password = parse_command_args(message.text or "")
    except BotError:
        await message.reply_text(DOWNLOAD_USAGE)
        return
    await _start_link_flow(update, context, url, password=password)


async def _start_link_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    password: str | None = None,
    provider_name: str | None = None,
    content_id: str = "",
) -> None:
    """Resolve *url* with its provider and show the info card."""
    message = update.message
    if message is None:
        return

    jobs = _jobs(context)
    registry = _downloaders(context)
    provider = (
        registry.get(provider_name) if provider_name else registry.find(url)
    )
    if provider is None:
        await message.reply_text(UnsupportedURLError.user_message)
        return

    if update.effective_user and jobs.active_for_user(update.effective_user.id):
        await message.reply_text(JobBusyError.user_message)
        return

    status = await message.reply_text(fmt.fetch_status_text(provider.label))

    job = jobs.create(
        user_id=update.effective_user.id if update.effective_user else 0,
        chat_id=message.chat_id,
        status_message_id=status.message_id,
        content_id=content_id,
        password=password,
        provider=provider.name,
        source_url=url,
    )

    status_helper = _status(context, job)
    try:
        content = await provider.resolve(url, password=password)
        job.content = content
    except BotError as exc:
        logger.info(
            "resolve failed (%s) for %s: %s", provider.name, url, exc.detail or exc
        )
        await status_helper.edit(exc.user_message, None, force=True)
        return
    except Exception:
        logger.exception("unexpected error resolving %s via %s", url, provider.name)
        await status_helper.edit(
            f"⚠️ Something went wrong while contacting {provider.label}. "
            "Please try again.",
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
        fmt.info_text(content, provider.label) + extra,
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
            text="This button isn't for you. Send your own link.",
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
async def auto_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pasted links: start the download flow, no command needed."""
    message = update.message
    if message is None or not message.text or message.text.startswith("/"):
        return
    url = extract_first_url(message.text)
    if url is None:
        return
    if looks_like_supported_link(url, _downloaders(context)):
        await _start_link_flow(update, context, url)
    elif "gofile.io" in message.text:
        # Malformed GoFile link — keep the original friendly nudge.
        await message.reply_text(USAGE_HINT)


# Backwards-compatible alias (the original handler name).
link_hint = auto_link


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing update", exc_info=context.error)
