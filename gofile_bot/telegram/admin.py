"""Operator panel: live usage overview, user management and bans.

Access is restricted to the numeric Telegram ids listed in ``ADMIN_USER_IDS``.
Because Telegram ids are easy to guess, non-admins only ever receive a polite
"operator only" reply — the panel is never rendered for them, and no stats
leak through callback answers.

The panel itself is built with :mod:`gofile_bot.telegram.rich` (tables,
headings, compact layout) and always carries a complete HTML fallback, so it
works on older Bot API servers and clients. It is driven by callbacks
prefixed ``ad:``. All numbers come from
:class:`gofile_bot.services.usage.UsageStore`.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from gofile_bot.config import (
    HARD_MAX_FILE_BYTES,
    MAX_FOLDER_FILES,
    MAX_PENDING_TRANSFERS,
    MAX_ZIP_BYTES,
    Settings,
)
from gofile_bot.services.usage import Overview, UserStats
from gofile_bot.telegram import formatting as fmt
from gofile_bot.telegram import menus
from gofile_bot.telegram.rich import (
    RichMessage,
    bold,
    bullets,
    details,
    divider,
    edit_rich,
    footer,
    heading,
    paragraph,
    reply_rich,
    send_rich,
    table,
)

logger = logging.getLogger(__name__)

PER_PAGE = 8
NOT_CONFIGURED_TEXT = (
    "🛠 The admin panel is not configured.\n"
    "Set <code>ADMIN_USER_IDS</code> to your numeric Telegram id "
    "(comma-separated for several operators) and restart the bot."
)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def admin_ids(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, ...]:
    settings = context.application.bot_data.get("settings")
    return tuple(getattr(settings, "admin_user_ids", ()) or ())


def is_admin(user_id: int | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return user_id is not None and user_id in admin_ids(context)


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer without leaking statistics or confirming guessed ids."""
    user = getattr(update, "effective_user", None)
    query = update.callback_query
    if query is not None:
        await query.answer(text="Operator only.", show_alert=True)
        return
    message = update.effective_message
    if message is None:
        return
    if is_admin(user.id if user else None, context):
        await message.reply_text("📊 Statistics are unavailable right now.")
    elif not admin_ids(context):
        await message.reply_html(NOT_CONFIGURED_TEXT)
    else:
        await message.reply_text(
            "🔒 The admin panel is for the bot operator only.\n"
            "Send /help to see what this bot can do for you."
        )


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def _home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👥 Users", callback_data="ad:users:0"),
                InlineKeyboardButton("🏆 Top", callback_data="ad:top:0"),
            ],
            [
                InlineKeyboardButton("🚫 Bans", callback_data="ad:bans:0"),
                InlineKeyboardButton("🕘 Recent", callback_data="ad:recent"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="ad:settings"),
                InlineKeyboardButton("🔄 Refresh", callback_data="ad:home"),
            ],
        ]
    )


def _back_keyboard(
    extra: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append(
        [
            InlineKeyboardButton("⬅️ Panel", callback_data="ad:home"),
            InlineKeyboardButton("🔄 Refresh", callback_data="ad:home"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _pager(prefix: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("⬅️", callback_data=f"{prefix}:{max(0, page - 1)}"),
        InlineKeyboardButton(f"· {page + 1}/{total_pages} ·", callback_data="ad:noop"),
        InlineKeyboardButton(
            "➡️", callback_data=f"{prefix}:{min(total_pages - 1, page + 1)}"
        ),
    ]


def _user_keyboard(user_id: int, *, banned: bool) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton("✅ Unban", callback_data=f"ad:unban:{user_id}")
        if banned
        else InlineKeyboardButton("🚫 Ban", callback_data=f"ad:ban:{user_id}")
    )
    return _back_keyboard(
        [
            [
                toggle,
                InlineKeyboardButton("♻️ Reset stats", callback_data=f"ad:reset:{user_id}"),
            ],
            [InlineKeyboardButton("👥 All users", callback_data="ad:users:0")],
        ]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pages(total: int, per_page: int = PER_PAGE) -> int:
    return max(1, (total + per_page - 1) // per_page)


def _mode(settings: Settings | None) -> str:
    if settings is None:
        return "unknown"
    if getattr(settings, "enable_mtproto", False):
        return "MTProto"
    if getattr(settings, "telegram_api_url", None):
        return "Local Bot API"
    return "Cloud API"


def _limit(settings: Settings | None) -> int:
    return settings.max_file_bytes if settings is not None else 0


def _ago(timestamp: float) -> str:
    if not timestamp:
        return "never"
    delta = max(0, int(time.time() - timestamp))
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86_400:
        return f"{delta // 3600}h"
    return f"{delta // 86_400}d"


def _uptime(started_at: float | None) -> str:
    if not started_at:
        return "unknown"
    return fmt.human_duration(max(0, time.time() - started_at))


def _day(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp)) if timestamp else "—"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def overview_menu(
    overview: Overview,
    *,
    settings: Settings | None,
    snapshot: dict,
    started_at: float | None,
) -> menus.Menu:
    mode = _mode(settings)
    rows = [
        ["👥 Users", f"{overview.users} ({overview.new_users_24h} new in 24h)"],
        ["📥 Downloads", f"{overview.downloads} total · {overview.downloads_24h} in 24h"],
        ["⚠️ Failed jobs", str(overview.failed)],
        [
            "💾 Delivered",
            f"{fmt.human_size(overview.bytes)} · {fmt.human_size(overview.bytes_24h)} today",
        ],
        ["🚫 Banned users", str(overview.active_bans)],
        [
            "⚙️ Transfer slot",
            f"{snapshot.get('active', 0)} active · {snapshot.get('queued', 0)} queued",
        ],
        ["🕒 Uptime", _uptime(started_at)],
        ["📦 Mode", f"{mode} · {fmt.human_size(_limit(settings))} cap"],
    ]

    rich = RichMessage().add(
        heading("🛠 Admin panel", 1),
        paragraph(["Live overview of ", bold("bot usage"), " and operator tools."]),
        table(["Metric", "Value"], rows, compact=True, aligns=["left", "left"]),
    )
    if overview.providers:
        rich.add(
            heading("Sources", 3),
            table(
                ["Source", "Downloads", "Data"],
                [
                    [_provider_label(provider), str(count), fmt.human_size(size)]
                    for provider, count, size in overview.providers
                ],
                compact=True,
                aligns=["left", "right", "right"],
                header_aligns=["left", "left", "left"],
            ),
        )
    rich.add(
        details(
            "ℹ️ About these numbers",
            bullets(
                "Counters come from the usage database, not from chat history.",
                "Downloads count only jobs that completed delivery.",
                "Manage access from the Users list, or with /ban <id> [reason].",
                "Limits (upload cap, ZIP, queue) are compile-time constants.",
            ),
        ),
        footer("Admin-only panel"),
    )

    html = (
        "🛠 <b>Admin panel</b>\n"
        f"👥 Users: {overview.users} ({overview.new_users_24h} new in 24h)\n"
        f"📥 Downloads: {overview.downloads} total · {overview.downloads_24h} in 24h\n"
        f"⚠️ Failed jobs: {overview.failed}\n"
        f"💾 Delivered: {fmt.human_size(overview.bytes)} "
        f"({fmt.human_size(overview.bytes_24h)} in 24h)\n"
        f"🚫 Banned users: {overview.active_bans}\n"
        f"⚙️ Transfer slot: {snapshot.get('active', 0)} active · "
        f"{snapshot.get('queued', 0)} queued\n"
        f"🕒 Uptime: {_uptime(started_at)}\n"
        f"📦 Mode: {mode} · {fmt.human_size(_limit(settings))} cap\n"
        "\n<b>Sources</b>: "
        + (
            " · ".join(
                f"{fmt.esc(provider)} {count} ({fmt.human_size(size)})"
                for provider, count, size in overview.providers
            )
            or "no downloads yet"
        )
    )
    return menus.Menu(rich, html, _home_keyboard())


def users_menu(
    users: list[UserStats], *, page: int, total: int, order: str = "downloads"
) -> menus.Menu:
    pages = _pages(total)
    page = max(0, min(page, pages - 1))
    order_label = {
        "downloads": "Most downloads first",
        "recent": "Most recently active first",
        "bytes": "Most data first",
        "new": "Newest users first",
    }.get(order, "Most downloads first")

    if not users:
        rich = RichMessage().add(
            heading(f"👥 Users · {page + 1}/{pages}", 2),
            paragraph("No users recorded yet — activity is tracked from the next update."),
            footer(f"{total} users"),
        )
        return menus.Menu(rich, "👥 <b>Users</b>\nNo users recorded yet.", _back_keyboard())

    rich = RichMessage().add(
        heading(f"👥 Users · {page + 1}/{pages}", 2),
        paragraph(f"{total} known users. {order_label}."),
        table(
            ["User", "📥", "⚠️", "💾", "Seen"],
            [
                [
                    f"{stats.name} · {stats.user_id}",
                    str(stats.downloads),
                    str(stats.failed),
                    fmt.human_size(stats.bytes),
                    _ago(stats.last_seen),
                ]
                for stats in users
            ],
            caption="Tap a user below for details, ban or reset",
            compact=True,
            aligns=["left", "right", "right", "right", "right"],
            header_aligns=["left", "left", "left", "left", "left"],
        ),
        footer("Downloads · failed · data delivered · last seen"),
    )

    html_lines = [f"👥 <b>Users</b> · page {page + 1}/{pages} · {total} total", ""]
    for stats in users:
        flag = " 🚫" if stats.banned else ""
        html_lines.append(
            f"• <b>{fmt.esc(stats.name)}</b> ({stats.user_id}){flag} — "
            f"{stats.downloads} ok / {stats.failed} failed · "
            f"{fmt.human_size(stats.bytes)} · seen {_ago(stats.last_seen)}"
        )

    rows = [
        [
            InlineKeyboardButton(
                f"{('🚫 ' if stats.banned else '')}{stats.name[:24]} · {stats.downloads}",
                callback_data=f"ad:user:{stats.user_id}",
            )
        ]
        for stats in users
    ]
    rows.append(_pager("ad:users", page, pages))
    rows.append([InlineKeyboardButton("⬅️ Panel", callback_data="ad:home")])
    return menus.Menu(rich, "\n".join(html_lines), InlineKeyboardMarkup(rows))


def user_menu(stats: UserStats) -> menus.Menu:
    rows = [
        ["🆔 Telegram id", str(stats.user_id)],
        ["👤 Username", f"@{stats.username}" if stats.username else "—"],
        ["📥 Downloads", str(stats.downloads)],
        ["⚠️ Failed jobs", str(stats.failed)],
        ["💾 Data received", fmt.human_size(stats.bytes)],
        ["⌨️ Commands", str(stats.commands)],
        ["📅 First seen", _day(stats.first_seen)],
        ["🕒 Last active", _ago(stats.last_seen)],
        [
            "🚫 Status",
            ("banned" + (f" — {stats.ban_reason}" if stats.ban_reason else ""))
            if stats.banned
            else "active ✅",
        ],
    ]
    rich = RichMessage().add(
        heading(f"👤 {stats.name}", 2),
        divider(),
        table(["Field", "Value"], rows, compact=True, aligns=["left", "left"]),
        paragraph(
            "Banning blocks all new download requests from this user. "
            "Reset clears their counters and download history."
        ),
        footer("Changes apply immediately"),
    )
    html = (
        f"👤 <b>{fmt.esc(stats.name)}</b>\n"
        + "\n".join(f"{field}: {fmt.esc(str(value))}" for field, value in rows)
    )
    return menus.Menu(rich, html, _user_keyboard(stats.user_id, banned=stats.banned))


def bans_menu(users: list[UserStats], *, page: int, total: int) -> menus.Menu:
    pages = _pages(total)
    page = max(0, min(page, pages - 1))
    if not users:
        rich = RichMessage().add(
            heading("🚫 Banned users", 2),
            paragraph("Nobody is banned right now. 🎉"),
            footer(f"{total} bans"),
        )
        return menus.Menu(
            rich, "🚫 <b>Banned users</b>\nNobody is banned right now.", _back_keyboard()
        )

    rich = RichMessage().add(
        heading(f"🚫 Banned users · {page + 1}/{pages}", 2),
        table(
            ["User", "Reason"],
            [
                [f"{stats.name} · {stats.user_id}", stats.ban_reason or "—"]
                for stats in users
            ],
            compact=True,
            aligns=["left", "left"],
        ),
        footer(f"{total} ban(s) · tap Unban to restore access"),
    )
    html = "🚫 <b>Banned users</b>\n" + "\n".join(
        f"• {fmt.esc(stats.name)} ({stats.user_id}) — {fmt.esc(stats.ban_reason or '—')}"
        for stats in users
    )
    rows = [
        [
            InlineKeyboardButton(
                f"✅ Unban {stats.name[:20]}", callback_data=f"ad:unban:{stats.user_id}"
            )
        ]
        for stats in users
    ]
    rows.append(_pager("ad:bans", page, pages))
    rows.append([InlineKeyboardButton("⬅️ Panel", callback_data="ad:home")])
    return menus.Menu(rich, html, InlineKeyboardMarkup(rows))


def recent_menu(records) -> menus.Menu:
    if not records:
        rich = RichMessage().add(
            heading("🕘 Recent downloads", 2),
            paragraph("No downloads recorded yet."),
        )
        return menus.Menu(
            rich, "🕘 <b>Recent downloads</b>\nNone yet.", _back_keyboard()
        )

    rich = RichMessage().add(
        heading("🕘 Recent downloads", 2),
        table(
            ["User", "Source", "Size", "When"],
            [
                [
                    ("✅ " if _ok(record.status) else "⚠️ ") + record.user_name[:20],
                    _provider_label(record.provider or "?"),
                    fmt.human_size(record.bytes),
                    _ago(record.created_at),
                ]
                for record in records
            ],
            compact=True,
            aligns=["left", "left", "right", "right"],
            header_aligns=["left", "left", "left", "left"],
        ),
        footer("Newest first · at most 10 entries"),
    )
    html = "🕘 <b>Recent downloads</b>\n" + "\n".join(
        f"{'✅' if _ok(record.status) else '⚠️'} {fmt.esc(record.user_name)} · "
        f"{fmt.esc(record.provider or '?')} · {fmt.human_size(record.bytes)} · "
        f"{_ago(record.created_at)}"
        for record in records
    )
    return menus.Menu(rich, html, _back_keyboard())


def settings_menu(settings: Settings | None, *, usage_persistent: bool) -> menus.Menu:
    rows = [
        ["📦 Upload transport", _mode(settings)],
        ["📏 Delivery cap", fmt.human_size(_limit(settings))],
        [
            "🌐 Direct links",
            "enabled"
            if getattr(settings, "enable_direct_downloads", True)
            else "disabled",
        ],
        ["⚡ MTProto", "on" if getattr(settings, "enable_mtproto", False) else "off"],
        ["🧩 ZIP cap", f"{fmt.human_size(MAX_ZIP_BYTES)} in/out · 20 files"],
        ["📁 Folder cap", f"{MAX_FOLDER_FILES} files · 300 MB budget"],
        ["👥 Accepted jobs", str(MAX_PENDING_TRANSFERS)],
        ["🧱 Hard ceiling", f"{HARD_MAX_FILE_BYTES:,} bytes"],
        ["👑 Admins", str(len(getattr(settings, "admin_user_ids", ()) or ()))],
        [
            "💾 Stats",
            "SQLite (persistent)" if usage_persistent else "in-memory only ⚠️",
        ],
    ]
    rich = RichMessage().add(
        heading("⚙️ Settings", 2),
        paragraph("Runtime configuration (read-only here)."),
        table(["Setting", "Value"], rows, compact=True, aligns=["left", "left"]),
        details(
            "How to change these",
            bullets(
                "Edit the environment variables and redeploy.",
                "ADMIN_USER_IDS — comma-separated numeric Telegram ids.",
                "USAGE_DB / DATA_DIR — where usage.sqlite3 is stored.",
                "ENABLE_MTPROTO + API_ID/API_HASH — up to 2 GB uploads.",
                "Limits are constants in config.py, not environment switches.",
            ),
        ),
        footer("Read-only panel"),
    )
    html = "⚙️ <b>Settings</b>\n" + "\n".join(
        f"{field}: {fmt.esc(str(value))}" for field, value in rows
    )
    return menus.Menu(rich, html, _back_keyboard())


_PROVIDER_LABELS = {
    "gofile": "GoFile",
    "gdrive": "Google Drive",
    "dropbox": "Dropbox",
    "onedrive": "OneDrive",
    "pixeldrain": "Pixeldrain",
    "mediafire": "MediaFire",
    "archive": "Archive.org",
    "direct": "Direct link",
    "telegram": "Telegram ZIP",
}


def _provider_label(name: str) -> str:
    return _PROVIDER_LABELS.get(name, name.replace("_", " ").title())


def _ok(status: str) -> bool:
    return status in ("ok", "complete", "success")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
async def _show(
    update: Update, context: ContextTypes.DEFAULT_TYPE, menu: menus.Menu
) -> None:
    """Edit the panel in place for callbacks, or reply for commands."""
    bot = context.application.bot
    query = update.callback_query
    if query is not None:
        message = query.message
        if message is None:  # pragma: no cover - inaccessible message
            return
        updated = await edit_rich(
            bot,
            message.chat_id,
            message.message_id,
            menu.rich,
            fallback_html=menu.html,
            reply_markup=menu.fallback_markup,
        )
        if not updated:
            await send_rich(
                bot,
                message.chat_id,
                menu.rich,
                fallback_html=menu.html,
                reply_markup=menu.fallback_markup,
            )
        return
    message = update.effective_message
    if message is not None:
        await reply_rich(
            message,
            bot,
            menu.rich,
            fallback_html=menu.html,
            reply_markup=menu.fallback_markup,
        )


def _store(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data.get("usage")


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings | None:
    return context.application.bot_data.get("settings")


def _snapshot(context: ContextTypes.DEFAULT_TYPE) -> dict:
    transfer = context.application.bot_data.get("transfer")
    snapshot = getattr(transfer, "snapshot", None)
    if callable(snapshot):
        try:
            return snapshot()
        except Exception:  # pragma: no cover - defensive
            logger.debug("transfer snapshot failed", exc_info=True)
    return {"active": 0, "queued": 0, "position": None, "capacity": 0}


async def _render_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = _store(context)
    if store is None:
        await deny(update, context)
        return
    overview = await store.overview()
    menu = overview_menu(
        overview,
        settings=_settings(context),
        snapshot=_snapshot(context),
        started_at=context.application.bot_data.get("started_at"),
    )
    await _show(update, context, menu)


async def _show_user(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    store = _store(context)
    if store is None:
        await deny(update, context)
        return
    stats = await store.user(user_id)
    if stats is None:
        stats = UserStats(user_id=user_id, banned=store.is_banned(user_id))
    await _show(update, context, user_menu(stats))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/admin [user_id] — the operator panel (private chats only)."""
    user = update.effective_user
    if not is_admin(user.id if user else None, context):
        await deny(update, context)
        return
    chat = update.effective_chat
    if chat is not None and getattr(chat, "type", "private") != "private":
        message = update.effective_message
        if message is not None:
            await message.reply_text("🔒 Open the admin panel in a private chat with me.")
        return
    args = (getattr(update.effective_message, "text", "") or "").split()[1:]
    if args and args[0].lstrip("-").isdigit():
        await _show_user(update, context, int(args[0]))
        return
    await _render_home(update, context)


async def _moderation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, unban: bool
) -> None:
    user = update.effective_user
    message = update.effective_message
    if not is_admin(user.id if user else None, context):
        await deny(update, context)
        return
    store = _store(context)
    if store is None or message is None:
        if message is not None:
            await message.reply_text("Usage storage is unavailable, so bans can't be changed.")
        return
    args = (message.text or "").split()[1:]
    if not args or not args[0].lstrip("-").isdigit():
        await message.reply_text(
            f"Use /{'unban' if unban else 'ban'} <user_id>"
            + ("" if unban else " [reason]")
        )
        return
    target = int(args[0])
    if unban:
        changed = await store.unban(target)
        await message.reply_text(
            f"✅ Unbanned <code>{target}</code>"
            if changed
            else f"ℹ️ <code>{target}</code> was not banned.",
            parse_mode="HTML",
        )
        return
    reason = " ".join(args[1:]).strip()[:200]
    await store.ban(target, banned_by=user.id if user else 0, reason=reason)
    logger.info("admin %s banned user %s (%s)", getattr(user, "id", 0), target, reason)
    await message.reply_text(
        f"🚫 Banned <code>{target}</code>"
        + (f"\nReason: {fmt.esc(reason)}" if reason else ""),
        parse_mode="HTML",
    )


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ban <user_id> [reason] — block a user immediately."""
    await _moderation(update, context, unban=False)


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unban <user_id> — restore access."""
    await _moderation(update, context, unban=True)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    user = query.from_user
    if not is_admin(user.id if user else None, context):
        await deny(update, context)
        return
    action = (query.data or "")[3:]
    store = _store(context)
    if store is None:
        await query.answer(text="Statistics are unavailable.", show_alert=True)
        return

    if action == "noop":
        await query.answer()
        return
    if action in ("", "home"):
        await query.answer()
        await _render_home(update, context)
        return
    if action == "recent":
        records = await store.recent_downloads(10)
        await query.answer()
        await _show(update, context, recent_menu(records))
        return
    if action == "settings":
        await query.answer()
        await _show(
            update,
            context,
            settings_menu(_settings(context), usage_persistent=not store.using_memory),
        )
        return

    verb, _, raw = action.partition(":")
    page = _int(raw)
    if verb in ("users", "top"):
        order = "downloads" if verb == "top" else "recent"
        users, total = await store.list_users(page=page, per_page=PER_PAGE, order=order)
        await query.answer()
        await _show(update, context, users_menu(users, page=page, total=total, order=order))
        return
    if verb == "bans":
        users, total = await store.banned(page=page, per_page=PER_PAGE)
        await query.answer()
        await _show(update, context, bans_menu(users, page=page, total=total))
        return
    if verb == "user":
        await query.answer()
        await _show_user(update, context, page)
        return
    if verb == "ban":
        await store.ban(
            page, banned_by=user.id if user else 0, reason="via admin panel"
        )
        logger.info(
            "admin %s banned user %s from the panel", getattr(user, "id", 0), page
        )
        await query.answer(text=f"Banned {page}")
        await _show_user(update, context, page)
        return
    if verb == "unban":
        changed = await store.unban(page)
        await query.answer(text=f"Unbanned {page}" if changed else "Not banned")
        await _show_user(update, context, page)
        return
    if verb == "reset":
        await store.reset_user(page)
        await query.answer(text=f"Stats reset for {page}")
        await _show_user(update, context, page)
        return

    await query.answer(text="Unknown panel action.")
    logger.info("unknown admin callback %r", query.data)


def _int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
