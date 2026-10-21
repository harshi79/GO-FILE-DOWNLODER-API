"""Rich (Bot API 10.1 – 10.3) menus with plain-HTML fallbacks.

Each function returns a :class:`~gofile_bot.telegram.rich.Menu` holding both
representations, so a call site stays one line::

    menu = menus.start_menu(limit)
    await reply_rich(message, bot, menu.rich, fallback_html=menu.html,
                     fallback_markup=menu.fallback_markup)

The rich version uses headings, tables, task lists, expandable details and
inline rich buttons (10.3).  The HTML version is what older Bot API servers
and clients receive, and is also what the unit tests assert on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from gofile_bot.config import MAX_FOLDER_FILES, MAX_ZIP_BYTES
from gofile_bot.telegram import formatting as fmt
from gofile_bot.telegram.rich import (
    RichMessage,
    bold,
    bullets,
    code,
    details,
    divider,
    expandable_quote,
    footer,
    heading,
    numbered,
    paragraph,
    table,
)


@dataclass
class Menu:
    """A message prepared in both rich and HTML form."""

    rich: RichMessage
    html: str
    fallback_markup: InlineKeyboardMarkup | None = None


def _ago(timestamp: float) -> str:
    if not timestamp:
        return "unknown"
    delta = max(0, int(time.time() - timestamp))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86_400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86_400}d ago"


def _day(timestamp: float) -> str:
    if not timestamp:
        return "unknown"
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp))


def _intro_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📚 Supported sources", callback_data="rc:supported"),
                InlineKeyboardButton("📖 Help", callback_data="rc:help"),
            ],
            [InlineKeyboardButton("📊 My status", callback_data="rc:status")],
        ]
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
def start_menu(limit: int) -> Menu:
    rich = RichMessage()
    rich.add(
        heading("📥 File Downloader", 1),
        paragraph(
            ["Paste a share link and I'll fetch it for you — ",
             bold("no commands needed"), "."]
        ),
        paragraph(
            [
                "Sources: ",
                bold("GoFile · Google Drive · Dropbox · OneDrive · Pasteview · MediaFire · "
                     "Pixeldrain · archive.org · direct links"),
            ]
        ),
        divider(),
        heading("How it works", 3),
        numbered(
            "Paste a link (or use /download <url>).",
            "Check the file name, size and type.",
            "Tap 「 DOWNLOAD 」 — the file arrives right here.",
        ),
        details(
            "🔒 Privacy & limits",
            bullets(
                "Only public links work — private/login-only content stays closed.",
                f"Upload limit: {fmt.human_size(limit)} per file; /links skips Telegram uploads.",
                "One active transfer at a time, with a fair queue.",
                "Temporary files are deleted after every job.",
            ),
        ),
        footer(fmt.MADE_BY),
    )

    html = (
        "👋 <b>Hi!</b> I download files from share links and send them right here.\n"
        "\n"
        "Just paste a link — GoFile, Google Drive, Dropbox, OneDrive, "
        "MediaFire, Pasteview, Pixeldrain, archive.org, or a direct file link — and tap "
        "「 DOWNLOAD 」.\n"
        "\n"
        "<b>Examples</b>\n"
        "https://gofile.io/d/xxxxxx\n"
        "https://drive.google.com/file/d/…/view\n"
        "/download https://example.com/file.zip\n"
        "\n"
        "<b>Password-protected GoFile shares</b>\n"
        "/gofile https://gofile.io/d/xxxxxx mypassword\n"
        "\n"
        f"📦 Send a ZIP (≤{fmt.human_size(MAX_ZIP_BYTES)}) or use /extract &lt;url&gt;.\n"
        "Use /status to check the queue and /cancel to stop your job.\n"
        "\n"
        f"Type /help for details, /supported for all sources.\n\n{fmt.MADE_BY}"
    )
    return Menu(rich, html, _intro_markup())


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
_COMMANDS = (
    ("/start", "Welcome message"),
    ("/download <url>", "Download from any supported link (alias /dl)"),
    ("/gofile <url> [password]", "GoFile shares, including protected ones"),
    ("/links <url> [password]", "Fresh browser download links"),
    ("/extract <url> [password]", "Safely extract a ZIP (alias /unzip)"),
    ("/supported", "Every source the bot understands"),
    ("/status", "Queue, limits and your current job"),
    ("/me", "Your personal usage statistics"),
    ("/cancel", "Stop your queued or running job"),
    ("/ping", "Check the bot is alive"),
)


def help_menu(limit: int) -> Menu:
    rows = [[code(command), description] for command, description in _COMMANDS]
    rich = RichMessage()
    rich.add(
        heading("📖 Help", 1),
        paragraph("Everything this bot can do, in one place."),
        table(
            ["Command", "What it does"],
            rows,
            caption="Tap a command to copy it from the message.",
            compact=True,
            aligns=["left", "left"],
        ),
        heading("How a download works", 3),
        numbered(
            "Paste a link (no command needed) or use /download.",
            "The bot shows the file name, size, type and source.",
            "Tap 「 DOWNLOAD 」 and the file is sent to this chat.",
        ),
        heading("Good to know", 3),
        bullets(
            "Only public links work — private/login-only content can't be opened.",
            f"Upload limit: {fmt.human_size(limit)} per file; /links avoids Telegram uploads.",
            f"Folder shares: the first {MAX_FOLDER_FILES} sendable files are delivered.",
            "Tap Cancel at any time while a download is running.",
            "Passwords stay only in the temporary in-memory job (never on disk).",
        ),
        details(
            "📦 ZIP extraction limits",
            bullets(
                f"Archive and expanded contents: {fmt.human_size(MAX_ZIP_BYTES)} each.",
                "At most 20 files / 100 entries; no nested extraction.",
                "Encrypted, split, corrupt and unsafe archives are rejected.",
            ),
            is_open=False,
        ),
        expandable_quote(
            "Only download content you have the right to access, and respect "
            "each service's terms."
        ),
        footer(fmt.MADE_BY),
    )

    html = (
        "📖 <b>Help</b>\n\n<b>Commands</b>\n"
        + "\n".join(
            f"{fmt.esc(command)} — {fmt.esc(description)}"
            for command, description in _COMMANDS
        )
        + "\n\n<b>How it works</b>\n"
        "1. Paste a link (no command needed) or use /download.\n"
        "2. The bot shows the file name, size and type.\n"
        "3. Tap 「 DOWNLOAD 」 and the bot sends the file here.\n"
        "\n<b>Supported sources</b>\n"
        "GoFile · Google Drive · Dropbox · OneDrive · Pasteview · MediaFire · Pixeldrain · "
        "Internet Archive · direct http(s) file links\n"
        "\n<b>Good to know</b>\n"
        "• Only public links work — private/login-only content can't be opened.\n"
        f"• Upload limit: {fmt.human_size(limit)} per file. /links avoids Telegram uploads.\n"
        f"• Folder shares: the first {MAX_FOLDER_FILES} sendable files are delivered.\n"
        "• You can tap Cancel while a download is running.\n"
        f"• ZIPs: at most {fmt.human_size(MAX_ZIP_BYTES)} compressed AND expanded, 20 files.\n"
        "• One active transfer globally; a bounded queue protects the server.\n"
        "• Passwords stay only in the temporary in-memory job (not on disk).\n"
        f"\n{fmt.MADE_BY}"
    )
    return Menu(rich, html, _intro_markup())


# ---------------------------------------------------------------------------
# /supported
# ---------------------------------------------------------------------------
_SOURCE_NOTES = {
    "gofile": "Files & folders; password-protected shares supported",
    "gdrive": "Public files; Docs → PDF, Sheets → XLSX, Slides → PPTX",
    "dropbox": "Shared links; folders arrive as a .zip",
    "onedrive": "Personal + SharePoint “anyone with the link” files",
    "pixeldrain": "Files and lists via the official API",
    "pasteview": "Public pastes as UTF-8 .txt files (not linked files)",
    "mediafire": "Single public files",
    "archive": "Item pages resolve to their file list",
    "direct": "Any public http(s) URL that points at a file",
    "telegram": "ZIP documents you upload to the chat",
}


def supported_menu(providers) -> Menu:
    rows = [
        [provider.label, _SOURCE_NOTES.get(provider.name, provider.description)]
        for provider in providers
        if provider.name != "telegram"
    ]
    examples = [p.examples[0] for p in providers if p.examples][:6]
    rich = RichMessage()
    rich.add(
        heading("📥 Supported sources", 1),
        paragraph("Paste any of these links — I detect the source automatically."),
        table(
            ["Source", "What it handles"],
            rows,
            compact=True,
            striped=True,
            aligns=["left", "left"],
        ),
    )
    if examples:
        rich.add(
            heading("Examples", 3),
            bullets(*[code(example) for example in examples]),
        )
    rich.add(
        details(
            "🔐 What never works",
            bullets(
                "Private content behind a login (no credentials are ever requested).",
                "Drive/OneDrive folders, torrents and DRM video sites.",
                "Password-protected shares without the password.",
            ),
        ),
        footer(fmt.MADE_BY),
    )

    lines = ["📥 <b>Supported sources</b>\n"]
    for provider in providers:
        lines.append(f"• <b>{fmt.esc(provider.label)}</b> — {fmt.esc(provider.description)}")
        for example in provider.examples:
            lines.append(f"   <code>{fmt.esc(example)}</code>")
    lines.append("\nJust paste a link, or use /download &lt;url&gt;.")
    return Menu(rich, "\n".join(lines))


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------
def status_menu(*, mode: str, limit: int, state: dict, job, own_text: str = "") -> Menu:
    phase = getattr(job, "phase", None)
    position = state.get("position")
    rows = [
        ["Mode", mode],
        ["Delivery cap", fmt.human_size(limit)],
        ["Active transfers", str(state.get("active", 0))],
        ["Waiting in queue", str(state.get("queued", 0))],
        ["Queue capacity", f"{state.get('capacity', 0)} jobs"],
    ]
    if job is not None:
        rows.append(["Your job", phase or "unknown"])
        if position:
            rows.append(["Your queue position", str(position)])
    else:
        rows.append(["Your job", "none — send a link to start"])

    rich = RichMessage()
    rich.add(
        heading("📊 Bot status", 1),
        table(
            ["Item", "Value"],
            rows,
            compact=True,
            aligns=["left", "left"],
            header_aligns=["left", "left"],
        ),
        details(
            "ℹ️ Limits",
            bullets(
                "One active transfer globally; up to 8 accepted jobs.",
                f"ZIP extraction: {fmt.human_size(MAX_ZIP_BYTES)} input and output.",
                "Download ceiling: 2,000,000,000 bytes.",
                "Queue wait limit: 10 minutes.",
            ),
        ),
        footer(fmt.MADE_BY),
    )

    html = (
        f"Mode: {fmt.esc(mode)} · configured delivery cap: {fmt.human_size(limit)}\n"
        f"📊 Active: {state.get('active', 0)} · Waiting: {state.get('queued', 0)} · "
        f"Capacity: {state.get('capacity', 0)} jobs\n"
        + fmt.esc(own_text or "You have no queued/running transfer.")
        + "\n📦 ZIP: 20 MB input + output cap. Hard download ceiling: 2,000,000,000 bytes."
    )
    return Menu(rich, html)


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------
def profile_menu(stats, *, mode: str, limit: int) -> Menu:
    rows = [
        ["Downloads", str(stats.downloads)],
        ["Failed jobs", str(stats.failed)],
        ["Data received", fmt.human_size(stats.bytes)],
        ["Commands used", str(stats.commands)],
        ["First seen", _day(stats.first_seen)],
        ["Last active", _ago(stats.last_seen)],
        ["Account", "blocked 🚫" if stats.banned else "active ✅"],
    ]
    rich = RichMessage()
    rich.add(
        heading("👤 Your usage", 1),
        paragraph([bold(stats.name), f" · id {stats.user_id}"]),
        table(
            ["Metric", "Value"],
            rows,
            compact=True,
            aligns=["left", "right"],
            header_aligns=["left", "left"],
        ),
        paragraph(
            [
                "Downloads count only files that were actually delivered. ",
                f"Current upload cap: {fmt.human_size(limit)} ({mode}).",
            ]
        ),
        footer(fmt.MADE_BY),
    )
    html = (
        f"👤 <b>Your usage</b>\n"
        f"Name: {fmt.esc(stats.name)} · id {stats.user_id}\n"
        f"📥 Downloads: {stats.downloads} · ⚠️ Failed: {stats.failed}\n"
        f"💾 Data received: {fmt.human_size(stats.bytes)}\n"
        f"⌨️ Commands used: {stats.commands}\n"
        f"📅 First seen: {_day(stats.first_seen)} · Last active: {_ago(stats.last_seen)}\n"
        f"Account: {'blocked 🚫' if stats.banned else 'active ✅'}"
    )
    return Menu(rich, html)
