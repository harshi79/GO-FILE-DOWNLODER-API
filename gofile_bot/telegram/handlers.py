"""Bot handlers: commands, download/cancel callbacks, and error logging.

One flow powers every source: pick the provider for a link (forced by
``/gofile``, or auto-detected for ``/download`` and pasted links), resolve it
to an info card with a DOWNLOAD button, then let the transfer service stream
the file. Providers live in :mod:`gofile_bot.downloaders`.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import ContextTypes

from gofile_bot.config import (
    HARD_MAX_FILE_BYTES,
    MAX_FILE_BYTES,
    MAX_FOLDER_FILES,
    MAX_ZIP_BYTES,
)
from gofile_bot.downloaders.base import DownloaderRegistry
from gofile_bot.errors import (
    BotError,
    InvalidURLError,
    JobBusyError,
    JobExpiredError,
    UnsupportedURLError,
    ZipSafetyError,
)
from gofile_bot.gofile.models import (
    RemoteFile,
    RemoteFolder,
    bounded_content,
    collect_files,
)
from gofile_bot.gofile.urls import parse_share
from gofile_bot.services.jobs import Job, JobStore
from gofile_bot.services.resources import cancelable
from gofile_bot.services.transfer import TransferService
from gofile_bot.services.zip_extract import is_zip
from gofile_bot.telegram import formatting as fmt
from gofile_bot.telegram import menus
from gofile_bot.telegram.keyboards import download_keyboard, link_keyboard
from gofile_bot.telegram.rich import edit_rich, reply_rich, send_rich

logger = logging.getLogger(__name__)

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
    r"\b(?:www\.)?(?:pasteview\.com|gofile\.io|pixeldrain\.com|mediafire\.com|"
    r"drive\.google\.com|docs\.google\.com|dropbox\.com|1drv\.ms|1drv\.com|"
    r"onedrive\.live\.com|onedrive\.com|(?:[\w-]+\.)*sharepoint\.com|"
    r"(?:[\w-]+\.)*archive\.org)/[^\s<>\"']+",
    re.IGNORECASE,
)
_TRAILING_PUNCT = ").,]}'\""

#: Extensions that make a pasted generic URL "look like a file" (so random
#: web-page links pasted in chat don't trigger the direct downloader).
_FILE_EXTENSIONS = (
    ".7z",
    ".apk",
    ".avi",
    ".bin",
    ".bz2",
    ".csv",
    ".dmg",
    ".doc",
    ".docx",
    ".epub",
    ".exe",
    ".flac",
    ".gif",
    ".gz",
    ".iso",
    ".jpg",
    ".jpeg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".msi",
    ".ogg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".tar",
    ".wav",
    ".webm",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
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


def _usage(context: ContextTypes.DEFAULT_TYPE):
    return _bot_data(context).get("usage")


def _module_mode(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, int]:
    """(transport label, delivery cap) for menus."""
    settings = _bot_data(context).get("settings")
    if settings is None:
        return "Cloud API", MAX_FILE_BYTES
    mode = (
        "MTProto"
        if settings.enable_mtproto
        else "Local Bot API"
        if settings.telegram_api_url
        else "Cloud API"
    )
    return mode, min(settings.max_file_bytes, HARD_MAX_FILE_BYTES)


async def _track(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, command: str | None = None
) -> None:
    """Best-effort activity tracking; never blocks or breaks a handler."""
    usage = _usage(context)
    user = getattr(update, "effective_user", None)
    if user is None:
        query = getattr(update, "callback_query", None)
        user = getattr(query, "from_user", None) if query is not None else None
    if usage is None or user is None:
        return
    try:
        await usage.touch_user(
            user.id,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            command=command,
        )
    except Exception:  # pragma: no cover - stats must never break the bot
        logger.debug("usage tracking failed", exc_info=True)


def _is_banned(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> bool:
    usage = _usage(context)
    return bool(usage is not None and user_id is not None and usage.is_banned(user_id))


async def _reply_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, menu) -> None:
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None:
        return
    await reply_rich(
        message,
        context.application.bot,
        menu.rich,
        fallback_html=menu.html,
        reply_markup=menu.fallback_markup,
    )


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
    await _track(update, context, command="start")
    user = update.effective_user
    if _is_banned(context, user.id if user else None):
        await update.message.reply_text("🚫 You are blocked from using this bot.")
        return
    if update.message:
        _mode, limit = _module_mode(context)
        await _reply_menu(update, context, menus.start_menu(limit))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _track(update, context, command="help")
    if update.message:
        _mode, limit = _module_mode(context)
        await _reply_menu(update, context, menus.help_menu(limit))


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("🏓 Pong! I'm alive.")


async def supported_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _track(update, context, command="supported")
    if not update.message:
        return
    await _reply_menu(update, context, menus.supported_menu(_downloaders(context).all()))


async def me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/me — the caller's own download statistics."""
    await _track(update, context, command="me")
    user = update.effective_user
    if update.message is None or user is None:
        return
    usage = _usage(context)
    try:
        stats = await usage.user(user.id) if usage is not None else None
    except Exception:  # pragma: no cover - stats must never break /me
        logger.debug("could not read usage stats", exc_info=True)
        stats = None
    if stats is None:
        from gofile_bot.services.usage import UserStats

        stats = UserStats(user_id=user.id)
    mode, limit = _module_mode(context)
    await _reply_menu(update, context, menus.profile_menu(stats, mode=mode, limit=limit))


async def _dispatch_metadata(update, context, factory):
    user = getattr(update, "effective_user", None)
    if _is_banned(context, user.id if user else None):
        await update.message.reply_text("🚫 You are blocked from using this bot.")
        return

    async def safe_request():
        try:
            import asyncio

            async with asyncio.timeout(40):
                await factory()
        except BotError as exc:
            await update.message.reply_text(exc.user_message)
        except TimeoutError:
            await update.message.reply_text(
                "⌛ That request timed out. Please try again shortly."
            )

    tasks = _bot_data(context).get("metadata_tasks")
    if tasks is None:  # Legacy embeds/tests: same flow without background scheduling.
        await safe_request()
        return
    try:
        tasks.submit(
            update.effective_user.id, safe_request, context.application.create_task
        )
    except BotError as exc:
        await update.message.reply_text(exc.user_message)


async def links_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await _dispatch_metadata(update, context, lambda: _links_flow(update, context))


async def _links_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve fresh links without downloading to the server or uploading to Telegram."""
    message = update.message
    if message is None:
        return
    try:
        url, password = parse_command_args(message.text or "")
    except BotError:
        await message.reply_text(
            "Use /links <url> [password] to get fresh download links."
        )
        return
    provider = _downloaders(context).find(url)
    if provider is None:
        await message.reply_text(UnsupportedURLError.user_message)
        return
    try:
        content = bounded_content(await provider.resolve(url, password=password))
    except BotError as exc:
        await message.reply_text(exc.user_message)
        return
    except Exception:
        logger.exception("link resolution failed via %s", provider.name)
        await message.reply_text(
            "⚠️ Couldn't fetch download links. Please try again later."
        )
        return
    files = collect_files(content)
    delivered = 0
    for file in files:
        try:
            parsed = urlparse(file.link)
            safe = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            safe = False
        if not safe:
            continue
        # Separate bounded messages keep a folder from exceeding Telegram's
        # 4096-character limit. No account cookies or API tokens are exposed.
        href = html.escape(file.link, quote=True)
        text = (
            f"📄 <b>{fmt.esc(file.name[:200])}</b> · {fmt.human_size(file.size)}\n"
            f'<a href="{href}">Download file</a>'
        )
        if len(text) > 3800:
            continue
        await message.reply_html(
            text, disable_web_page_preview=True, reply_markup=link_keyboard(file.link)
        )
        delivered += 1
        if delivered >= MAX_FOLDER_FILES:
            break
    if not delivered:
        await message.reply_text(
            "⚠️ No browser download links are available for this share."
        )
        return
    await message.reply_text(
        "ℹ️ These are fresh host-provided links, not permanent hosted copies. "
        "They may expire or require the host's browser session (especially GoFile). "
        "If a link is denied, open the original share in your browser or use /download. "
        "Run /links again to refresh. At most 10 links are shown."
    )


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
        update,
        context,
        url,
        password=password,
        provider_name="gofile",
        content_id=content_id,
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


async def extract_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    try:
        url, password = parse_command_args(update.message.text or "")
    except BotError:
        await update.message.reply_text(
            "Use /extract <ZIP-url> [share-password], or send a ZIP document (≤20 MB)."
        )
        return
    await _start_link_flow(update, context, url, password=password, action="extract")


async def uploaded_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.document:
        return
    user = getattr(update, "effective_user", None)
    if _is_banned(context, user.id if user else None):
        await message.reply_text("🚫 You are blocked from using this bot.")
        return
    await _track(update, context)
    document = message.document
    file = RemoteFile(
        id=document.file_id,
        name=(document.file_name or "archive.zip")[:256],
        size=document.file_size or 0,
        mimetype=document.mime_type or "application/zip",
        link="",
    )
    if not is_zip(file):
        await message.reply_text(
            "📦 Send a .zip file, or /extract <ZIP-url>. Other archives aren't supported."
        )
        return
    if file.size > MAX_ZIP_BYTES:
        await message.reply_text(ZipSafetyError.user_message)
        return
    jobs = _jobs(context)
    if update.effective_user and jobs.active_for_user(update.effective_user.id):
        await message.reply_text(JobBusyError.user_message)
        return
    status = await message.reply_text(
        "📦 ZIP ready. Extraction checks size, paths and file count before sending anything."
    )
    try:
        job = jobs.create(
            user_id=update.effective_user.id,
            chat_id=message.chat_id,
            status_message_id=status.message_id,
            provider="telegram",
        )
    except BotError as exc:
        await message.reply_text(exc.user_message)
        return
    job.content = file
    job.action = "extract"
    await _status(context, job).edit(
        fmt.info_text(file, "Uploaded ZIP")
        + "\n\nSafety limits: 20 MB archive + 20 MB total extracted; at most 20 files.",
        download_keyboard(job.id, extract_only=True),
        force=True,
        rich=fmt.info_rich(
            file,
            "Uploaded ZIP",
            note="Safety limits: 20 MB archive + 20 MB total extracted; at most 20 files.",
        ),
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    service = _transfer(context)
    job = _jobs(context).active_for_user(update.effective_user.id)
    state = service.snapshot(job)
    own = f"Your job: {job.phase}." if job else "You have no queued/running transfer."
    if state["position"]:
        own += f" Queue position: {state['position']}."
    mode, delivery_limit = _module_mode(context)
    menu = menus.status_menu(
        mode=mode, limit=delivery_limit, state=state, job=job, own_text=own
    )
    await _reply_menu(update, context, menu)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    job = _jobs(context).active_for_user(update.effective_user.id)
    if job:
        job.cancel_event.set()
        await update.message.reply_text(
            "🛑 Stopping your job and cleaning up temporary files…"
        )
    else:
        await update.message.reply_text(
            "You have no queued or running transfer to cancel."
        )


async def _start_link_flow(update, context, url, **kwargs):
    await _track(update, context)
    await _dispatch_metadata(
        update, context, lambda: _resolve_link_flow(update, context, url, **kwargs)
    )


async def _resolve_link_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    password: str | None = None,
    provider_name: str | None = None,
    content_id: str = "",
    action: str = "download",
) -> None:
    """Resolve *url* with its provider and show the info card."""
    message = update.message
    if message is None:
        return

    jobs = _jobs(context)
    registry = _downloaders(context)
    provider = registry.get(provider_name) if provider_name else registry.find(url)
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

    job.action = action
    job.running = True
    job.phase = "resolving"
    status_helper = _status(context, job)
    try:
        content = bounded_content(
            await cancelable(
                provider.resolve(url, password=password), job.cancel_event, 30
            )
        )
        settings = _bot_data(context).get("settings")
        limit = min(
            settings.max_file_bytes if settings else MAX_FILE_BYTES, HARD_MAX_FILE_BYTES
        )
        if (
            action == "extract"
            and isinstance(content, RemoteFile)
            and content.size > MAX_ZIP_BYTES
        ):
            raise ZipSafetyError()
        if action == "extract" and (
            not isinstance(content, RemoteFile) or not is_zip(content)
        ):
            raise ZipSafetyError()
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
    finally:
        job.running = False
        job.phase = "ready" if job.content is not None else "failed"

    # A file that exceeds the configured upload transport is shown with an
    # explanation but no Download button: pressing it could only fail after a
    # pointless download.  /links still works, and the card stays informative.
    over_limit = (
        isinstance(content, RemoteFile)
        and content.size > limit
        and action != "extract"
    )
    if over_limit:
        await status_helper.edit(
            fmt.info_text(content, provider.label)
            + fmt.too_big_note(content.size, limit),
            None,
            force=True,
            rich=fmt.info_rich(
                content,
                provider.label,
                note=fmt.too_big_note(content.size, limit).strip(),
            ),
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
        download_keyboard(
            job.id,
            can_extract=isinstance(content, RemoteFile)
            and is_zip(content)
            and content.size <= MAX_ZIP_BYTES,
            extract_only=action == "extract",
        ),
        force=True,
        rich=fmt.info_rich(content, provider.label, note=extra.strip()),
    )


def _status(context: ContextTypes.DEFAULT_TYPE, job: Job):
    from gofile_bot.services.status import StatusMessage

    return StatusMessage(context.application.bot, job.chat_id, job.status_message_id)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
async def download_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _action_cb(update, context, "download")


async def extract_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _action_cb(update, context, "extract")


async def _action_cb(update, context, action):
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

    if _is_banned(context, user_id):
        await query.answer(text="🚫 You are blocked from using this bot.", show_alert=True)
        return
    await _track(update, context)

    active = _jobs(context).active_for_user(user_id)
    if job.running or (active is not None and active.id != job.id):
        await query.answer(text=JobBusyError.user_message, show_alert=True)
        return

    if job.content is None:
        await query.answer(
            text="File information isn't ready. Send the link again.", show_alert=True
        )
        return
    if action == "extract" and (
        not isinstance(job.content, RemoteFile)
        or not is_zip(job.content)
        or job.content.size > MAX_ZIP_BYTES
    ):
        await query.answer(
            text="ZIP extraction is limited to 20 MB input and output.", show_alert=True
        )
        return
    job.action = action
    service = _transfer(context)
    if isinstance(service, TransferService) and hasattr(
        context.application, "create_task"
    ):
        try:
            service.submit(job, context.application.create_task)
        except BotError as exc:
            await query.answer(text=exc.user_message, show_alert=True)
            return
        await query.answer(text="Request accepted. /status shows your queue position.")
        return

    # Reserve the user before the first await, so rapid presses on two old
    # cards cannot start simultaneous transfers for the same user.
    job.cancel_event.clear()
    job.running = True
    try:
        await query.answer(text="Starting download…")
        await _transfer(context).run(job)
    except Exception:  # pragma: no cover - transfer.run already contains errors
        logger.exception("transfer crashed for job %s", job.id)
        job.running = False


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline buttons on the rich /start and /help menus (``rc:`` prefix)."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    action = (query.data or "")[3:]
    mode, limit = _module_mode(context)
    if action == "supported":
        menu = menus.supported_menu(_downloaders(context).all())
    elif action == "help":
        menu = menus.help_menu(limit)
    elif action == "status":
        service = _transfer(context)
        job = _jobs(context).active_for_user(update.effective_user.id)
        state = service.snapshot(job)
        own = f"Your job: {job.phase}." if job else "You have no queued/running transfer."
        if state["position"]:
            own += f" Queue position: {state['position']}."
        menu = menus.status_menu(mode=mode, limit=limit, state=state, job=job, own_text=own)
    elif action == "profile":
        usage = _usage(context)
        user = update.effective_user
        stats = await usage.user(user.id) if usage is not None and user else None
        if stats is None:
            from gofile_bot.services.usage import UserStats

            stats = UserStats(user_id=user.id if user else 0)
        menu = menus.profile_menu(stats, mode=mode, limit=limit)
    else:
        await query.answer()
        return

    await query.answer()
    chat_id = getattr(query.message, "chat_id", None)
    message_id = getattr(query.message, "message_id", None)
    if chat_id is None or message_id is None:
        return
    if await edit_rich(
        context.application.bot,
        chat_id,
        message_id,
        menu.rich,
        fallback_html=menu.html,
        reply_markup=menu.fallback_markup,
    ):
        return
    await send_rich(
        context.application.bot,
        chat_id,
        menu.rich,
        fallback_html=menu.html,
        reply_markup=menu.fallback_markup,
    )


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
        await query.answer(text="This button isn't for you.", show_alert=True)
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
