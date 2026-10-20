"""Rich messages for Telegram Bot API 10.1 – 10.3.

Bot API 10.1 (June 2026) added ``sendRichMessage`` with a structured block
tree: headings, lists, tables, quotes, details, media, maths and footnotes.
10.2 added the outgoing ``InputRichBlock`` family and media attachments, and
10.3 added **buttons inside rich messages**, compact tables, expandable
quotes and document blocks.

:mod:`python-telegram-bot` 22.8 does not wrap these methods yet, so this
module talks to them through :meth:`telegram.Bot.do_api_request` (the official
escape hatch for methods a library version does not know) and always keeps a
plain-HTML fallback for older Bot API servers or chat clients.

Everything here is pure data: builders return JSON-ready dicts, and
:class:`RichMessage` validates the documented limits before sending
(32 768 UTF-8 characters, 500 blocks, 16 nesting levels, 20 table columns).

Reference: https://core.telegram.org/bots/api#rich-messages
"""

from __future__ import annotations

import html as html_module
import logging
import re
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

from telegram import InlineKeyboardMarkup, Message
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.warnings import PTBUserWarning

logger = logging.getLogger(__name__)

#: Documented rich-message limits.
MAX_RICH_CHARS = 32_768
MAX_RICH_BLOCKS = 500
MAX_RICH_DEPTH = 16
MAX_TABLE_COLUMNS = 20
MAX_TABLE_ROWS = 100

#: How long to stop trying rich messages after the server rejects the method.
RICH_RETRY_SECONDS = 6 * 60 * 60

TableAlign = str  # "left" | "center" | "right"
TableValign = str  # "top" | "middle" | "bottom"

_SUPPORTED_ALIGNS = ("left", "center", "right")
_SUPPORTED_VALIGNS = ("top", "middle", "bottom")


# ---------------------------------------------------------------------------
# Rich text
# ---------------------------------------------------------------------------
def text(value: Any) -> Any:
    """Coerce *value* into a valid ``RichText`` value."""
    if isinstance(value, str | int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return [text(item) for item in value]
    if isinstance(value, dict):
        return value
    return str(value)


def _styled(kind: str, value: Any) -> dict:
    return {"type": kind, "text": text(value)}


def bold(value: Any) -> dict:
    return _styled("bold", value)


def italic(value: Any) -> dict:
    return _styled("italic", value)


def underline(value: Any) -> dict:
    return _styled("underline", value)


def strikethrough(value: Any) -> dict:
    return _styled("strikethrough", value)


def spoiler(value: Any) -> dict:
    return _styled("spoiler", value)


def marked(value: Any) -> dict:
    return _styled("marked", value)


def code(value: Any) -> dict:
    return _styled("code", value)


def subscript(value: Any) -> dict:
    return _styled("subscript", value)


def superscript(value: Any) -> dict:
    return _styled("superscript", value)


def link(value: Any, url: str) -> dict:
    return {"type": "url", "text": text(value), "url": url}


def math(expression: str) -> dict:
    return {"type": "mathematical_expression", "expression": expression}


# ---------------------------------------------------------------------------
# Blocks (InputRichBlock)
# ---------------------------------------------------------------------------
def paragraph(value: Any) -> dict:
    return {"type": "paragraph", "text": text(value)}


def heading(value: Any, size: int = 3) -> dict:
    return {"type": "heading", "text": text(value), "size": max(1, min(6, int(size)))}


def pre(value: Any, language: str | None = None) -> dict:
    block = {"type": "pre", "text": text(value)}
    if language:
        block["language"] = language
    return block


def footer(value: Any) -> dict:
    return {"type": "footer", "text": text(value)}


def divider() -> dict:
    return {"type": "divider"}


def mathematical_expression(expression: str) -> dict:
    return {"type": "mathematical_expression", "expression": expression}


def quote(*blocks: dict, credit: Any | None = None) -> dict:
    block = {"type": "blockquote", "blocks": [b for b in blocks if b]}
    if credit is not None:
        block["credit"] = text(credit)
    return block


def expandable_quote(value: Any, credit: Any | None = None) -> dict:
    block = {"type": "expandable_blockquote", "text": text(value)}
    if credit is not None:
        block["credit"] = text(credit)
    return block


def details(summary: Any, *blocks: dict, is_open: bool = False) -> dict:
    block = {
        "type": "details",
        "summary": text(summary),
        "blocks": [b for b in blocks if b],
    }
    if is_open:
        block["is_open"] = True
    return block


#: Block types accepted as list-item bodies (anything else is RichText).
_BLOCK_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "pre",
        "footer",
        "divider",
        "mathematical_expression",
        "anchor",
        "list",
        "blockquote",
        "expandable_blockquote",
        "details",
        "table",
        "buttons",
        "document",
    }
)


def bullets(*items: Any, checklist: bool = False, checked: bool = False) -> dict:
    """Bullet (or task) list.

    An item is either RichText (``"text"`` or ``["with ", bold("style")]``) or
    a list of block dicts for multi-block items.
    """
    entries = []
    for item in items:
        if (
            isinstance(item, list | tuple)
            and item
            and all(isinstance(part, dict) and part.get("type") in _BLOCK_TYPES for part in item)
        ):
            blocks = list(item)
        else:
            blocks = [paragraph(item)]
        entry: dict = {"blocks": blocks}
        if checklist:
            entry["has_checkbox"] = True
            if checked:
                entry["is_checked"] = True
        entries.append(entry)
    return {"type": "list", "items": entries}


def numbered(*items: Any, style: str = "1") -> dict:
    """Ordered list; *style* is ``1``, ``a``, ``A``, ``i`` or ``I``."""
    block = bullets(*items)
    for index, entry in enumerate(block["items"], start=1):
        entry["value"] = index
        entry["type"] = style
    return block


def cell(
    value: Any,
    *,
    header: bool = False,
    align: TableAlign = "left",
    valign: TableValign = "middle",
    colspan: int | None = None,
    rowspan: int | None = None,
) -> dict:
    """One table cell. *value* may be ``None`` for an invisible cell."""
    if align not in _SUPPORTED_ALIGNS:
        align = "left"
    if valign not in _SUPPORTED_VALIGNS:
        valign = "middle"
    data: dict = {"align": align, "valign": valign}
    if value is not None:
        data["text"] = text(value)
    if header:
        data["is_header"] = True
    if colspan and colspan > 1:
        data["colspan"] = int(colspan)
    if rowspan and rowspan > 1:
        data["rowspan"] = int(rowspan)
    return data


def table(
    header: list[Any] | None,
    rows: list[list[Any]],
    *,
    caption: Any | None = None,
    bordered: bool = True,
    striped: bool = True,
    compact: bool = False,
    aligns: list[TableAlign] | None = None,
    header_aligns: list[TableAlign] | None = None,
) -> dict:
    """Build a rich table.

    ``aligns`` sets per-column alignment for body cells; ``header_aligns``
    defaults to the same values.  Rich tables support up to 20 columns.
    """
    width = max(
        [len(header) if header else 0] + [len(row) for row in rows] + [1]
    )
    width = min(width, MAX_TABLE_COLUMNS)
    aligns = list(aligns or [])[:width]
    aligns += ["left"] * (width - len(aligns))
    header_aligns = list(header_aligns or aligns)[:width]
    header_aligns += aligns[len(header_aligns):]

    cells: list[list[dict]] = []
    if header:
        cells.append(
            [
                cell(value, header=True, align=header_aligns[index])
                for index, value in enumerate(header[:width])
            ]
        )
    body_rows = MAX_TABLE_ROWS - (1 if header else 0)
    for row in rows[:body_rows]:
        cells.append(
            [
                cell(
                    row[index] if index < len(row) else None,
                    align=aligns[index],
                )
                for index in range(width)
            ]
        )

    block: dict = {
        "type": "table",
        "cells": cells,
        "is_bordered": bordered,
        "is_striped": striped,
    }
    if compact:
        block["is_compact"] = True
    if caption is not None:
        block["caption"] = caption if isinstance(caption, dict) else text(caption)
    return block


def buttons(
    row: list[dict],
    *,
    align: TableAlign = "center",
) -> dict:
    """A row of 1–8 buttons inside a rich message (Bot API 10.3).

    Each button is ``{"text": ..., "callback_data": ...}``,
    ``{"text": ..., "url": ...}``, or a full ``RichMessageButton`` dict.
    """
    if align not in _SUPPORTED_ALIGNS:
        align = "center"
    return {"type": "buttons", "buttons": row[:8], "align": align}


def rich_button(
    label: Any,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
) -> dict:
    button: dict = {"text": text(label)}
    if callback_data is not None:
        button["callback_data"] = callback_data[:64]
    elif url is not None:
        button["url"] = url
    if style:
        button["style"] = style
    return button


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------
@dataclass
class RichMessage:
    """An ordered set of rich blocks, validated against the API limits."""

    blocks: list[dict] = field(default_factory=list)
    is_rtl: bool = False
    skip_entity_detection: bool = False

    def add(self, *blocks: dict) -> "RichMessage":
        self.blocks.extend(block for block in blocks if block)
        return self

    def extend(self, blocks: list[dict]) -> "RichMessage":
        self.blocks.extend(block for block in blocks if block)
        return self

    def __bool__(self) -> bool:
        return bool(self.blocks)

    def to_dict(self) -> dict:
        blocks = _sanitize_blocks(self.blocks)
        payload: dict = {"blocks": blocks}
        if self.is_rtl:
            payload["is_rtl"] = True
        if self.skip_entity_detection:
            payload["skip_entity_detection"] = True
        return payload

    def to_html(self) -> str:
        """Plain-HTML fallback (regular messages, older Bot API servers)."""
        rendered = _blocks_to_html(self.blocks)
        return _clip_html(rendered) or "…"


# ---------------------------------------------------------------------------
# Validation / fallback rendering
# ---------------------------------------------------------------------------
def _trim_rich(value: Any, budget: dict) -> Any:
    """Trim a RichText tree so the total text fits the 32 768-char limit."""
    if budget["left"] <= 0:
        return "…"
    if isinstance(value, str):
        if len(value) <= budget["left"]:
            budget["left"] -= len(value)
            return value
        keep = max(0, budget["left"] - 1)
        trimmed = value[:keep].rstrip() + "…"
        budget["left"] = 0
        return trimmed
    if isinstance(value, list):
        return [_trim_rich(item, budget) for item in value]
    if isinstance(value, dict):
        data = dict(value)
        if "text" in data:
            data["text"] = _trim_rich(data["text"], budget)
        if "expression" in data and isinstance(data["expression"], str):
            if len(data["expression"]) <= budget["left"]:
                budget["left"] -= len(data["expression"])
            else:
                data["expression"] = data["expression"][: max(0, budget["left"])]
                budget["left"] = 0
        return data
    return value


def _sanitize_cell(value: Any, budget: dict | None = None) -> dict:
    data = value if isinstance(value, dict) else cell(value)
    data.setdefault("align", "left")
    data.setdefault("valign", "middle")
    if data["align"] not in _SUPPORTED_ALIGNS:
        data["align"] = "left"
    if data["valign"] not in _SUPPORTED_VALIGNS:
        data["valign"] = "middle"
    if budget is not None and "text" in data:
        data["text"] = _trim_rich(data["text"], budget)
    return data


def _sanitize_blocks(
    blocks: list[dict],
    *,
    depth: int = 0,
    counter: dict | None = None,
    budget: dict | None = None,
) -> list[dict]:
    counter = counter if counter is not None else {"count": 0}
    budget = budget if budget is not None else {"left": MAX_RICH_CHARS}
    out: list[dict] = []
    for raw in blocks:
        if counter["count"] >= MAX_RICH_BLOCKS or not isinstance(raw, dict):
            break
        block = dict(raw)
        counter["count"] += 1

        for key in ("text", "summary", "expression"):
            if key in block:
                block[key] = _trim_rich(block[key], budget)
        if "caption" in block:
            block["caption"] = _trim_rich(block["caption"], budget)

        kind = block.get("type")
        if kind == "table":
            rows = []
            for row in (block.get("cells") or [])[:MAX_TABLE_ROWS]:
                rows.append(
                    [_sanitize_cell(value, budget) for value in row[:MAX_TABLE_COLUMNS]]
                )
            block["cells"] = rows
        elif kind == "list":
            items = []
            for item in block.get("items") or []:
                if counter["count"] >= MAX_RICH_BLOCKS:
                    break
                counter["count"] += 1
                entry = dict(item)
                entry["blocks"] = _sanitize_blocks(
                    entry.get("blocks") or [],
                    depth=depth + 1,
                    counter=counter,
                    budget=budget,
                )
                items.append(entry)
            block["items"] = items
        elif kind == "buttons":
            block["buttons"] = [
                dict(button) for button in (block.get("buttons") or [])[:8]
                if isinstance(button, dict)
            ]
        elif "blocks" in block:
            if depth + 1 >= MAX_RICH_DEPTH:
                block["blocks"] = []
            else:
                block["blocks"] = _sanitize_blocks(
                    block.get("blocks") or [],
                    depth=depth + 1,
                    counter=counter,
                    budget=budget,
                )
        out.append(block)
    return out


def _rich_to_html(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return html_module.escape(value, quote=False)
    if isinstance(value, list | tuple):
        return "".join(_rich_to_html(item) for item in value)
    if not isinstance(value, dict):
        return html_module.escape(str(value), quote=False)
    kind = value.get("type")
    inner = _rich_to_html(value.get("text")) if "text" in value else ""
    if kind == "bold":
        return f"<b>{inner}</b>"
    if kind == "italic":
        return f"<i>{inner}</i>"
    if kind == "underline":
        return f"<u>{inner}</u>"
    if kind == "strikethrough":
        return f"<s>{inner}</s>"
    if kind == "spoiler":
        return f"<tg-spoiler>{inner}</tg-spoiler>"
    if kind == "code":
        return f"<code>{inner}</code>"
    if kind == "url":
        url = html_module.escape(str(value.get("url") or ""), quote=True)
        return f'<a href="{url}">{inner}</a>'
    if kind == "mathematical_expression":
        return f"<code>{html_module.escape(str(value.get('expression') or ''))}</code>"
    if kind == "custom_emoji":
        return html_module.escape(str(value.get("alternative_text") or ""))
    return inner


def _rich_to_plain(value: Any) -> str:
    """Plain text of a RichText tree (used inside <pre> table fallbacks)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return "".join(_rich_to_plain(item) for item in value)
    if not isinstance(value, dict):
        return str(value)
    if "text" in value:
        return _rich_to_plain(value["text"])
    if "expression" in value:
        return str(value["expression"])
    return ""


def _table_to_html(block: dict) -> str:
    lines: list[str] = []
    for row in block.get("cells") or []:
        parts = []
        for value in row:
            rendered = _rich_to_plain(value.get("text")) if isinstance(value, dict) else ""
            if rendered:
                parts.append(rendered)
        if parts:
            lines.append(" · ".join(parts))
    caption = block.get("caption")
    if caption:
        lines.append(_rich_to_plain(caption))
    return "<pre>" + html_module.escape("\n".join(lines)) + "</pre>"


def _block_to_html(block: dict) -> str:
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "paragraph":
        return _rich_to_html(block.get("text"))
    if kind == "heading":
        return f"<b>{_rich_to_html(block.get('text'))}</b>"
    if kind == "pre":
        return f"<pre>{_rich_to_html(block.get('text'))}</pre>"
    if kind == "footer":
        return f"<i>{_rich_to_html(block.get('text'))}</i>"
    if kind == "divider":
        return "──────────"
    if kind == "blockquote":
        body = "\n".join(_block_to_html(item) for item in block.get("blocks") or [])
        credit = block.get("credit")
        if credit:
            body += f"\n— {_rich_to_html(credit)}"
        return f"<blockquote>{body}</blockquote>"
    if kind == "expandable_blockquote":
        return f"<blockquote>{_rich_to_html(block.get('text'))}</blockquote>"
    if kind == "details":
        body = "\n".join(_block_to_html(item) for item in block.get("blocks") or [])
        return f"<b>{_rich_to_html(block.get('summary'))}</b>\n{body}"
    if kind == "list":
        lines = []
        for index, item in enumerate(block.get("items") or [], start=1):
            body = " ".join(_block_to_html(part) for part in item.get("blocks") or [])
            marker = "☐" if item.get("has_checkbox") else f"{index}."
            lines.append(f"{marker} {body}")
        return "\n".join(lines)
    if kind == "table":
        return _table_to_html(block)
    if kind == "buttons":
        lines = []
        for button in block.get("buttons") or []:
            label = _rich_to_html(button.get("text"))
            url = button.get("url")
            lines.append(f"▸ {label}" + (f" — {html_module.escape(url)}" if url else ""))
        return "\n".join(lines)
    if kind == "document":
        caption = block.get("caption") or {}
        return _rich_to_html(caption.get("text") if isinstance(caption, dict) else caption)
    if kind == "mathematical_expression":
        return f"<code>{html_module.escape(str(block.get('expression') or ''))}</code>"
    if "blocks" in block:
        return "\n".join(_block_to_html(item) for item in block.get("blocks") or [])
    if "text" in block:
        return _rich_to_html(block.get("text"))
    return ""


def _clip_html(rendered: str, limit: int = 4096) -> str:
    """Trim an HTML string to Telegram's message limit without splitting tags."""
    if len(rendered) <= limit:
        return rendered
    cut = rendered[: limit - 1]
    cut = re.sub(r"&[a-zA-Z0-9#]*$", "", cut)  # unfinished &amp; / &#x1F600;
    cut = re.sub(r"<[^>]*$", "", cut)  # unfinished <b ...>
    return cut.rstrip() + "…"


def _blocks_to_html(blocks: list[dict]) -> str:
    parts = [part for part in (_block_to_html(block) for block in blocks) if part]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
class _RichSupport:
    """Process-wide feature probe with a retry window."""

    def __init__(self) -> None:
        self.disabled_until = 0.0
        self.consecutive_failures = 0

    def available(self, bot: Any) -> bool:
        if time.monotonic() < self.disabled_until:
            return False
        return callable(getattr(bot, "do_api_request", None))

    def note_success(self) -> None:
        self.consecutive_failures = 0

    def note_failure(self, exc: BaseException) -> None:
        message = str(exc).lower()
        # A network blip or a benign "not modified" must not disable a feature
        # that works fine — only the server rejecting the method/payload should.
        if (isinstance(exc, NetworkError) and not isinstance(exc, BadRequest)) or any(
            marker in message
            for marker in (
                "message is not modified",
                "message can't be edited",
                "message to edit not found",
            )
        ):
            return
        self.consecutive_failures += 1
        unsupported = (
            "unknown method" in message
            or "not found" in message
            or "unsupported" in message
            or "method not" in message
            or "invalid method" in message
            or isinstance(exc, AttributeError | TypeError)
        )
        if unsupported or self.consecutive_failures >= 3:
            self.disabled_until = time.monotonic() + RICH_RETRY_SECONDS
            logger.warning(
                "rich messages disabled for %ss after: %s", RICH_RETRY_SECONDS, exc
            )

    def reset(self) -> None:
        self.disabled_until = 0.0
        self.consecutive_failures = 0


_support = _RichSupport()


def rich_supported(bot: Any) -> bool:
    """True when rich messages should be attempted for *bot*."""
    return _support.available(bot)


def reset_rich_support() -> None:
    """Clear the probe cache (used by tests and after a Bot API upgrade)."""
    _support.reset()


async def _call_api(
    bot: Any,
    endpoint: str,
    payload: dict,
    *,
    return_type: type | None = None,
) -> Any:
    """Call a Bot API method PTB has no dedicated wrapper for.

    PTB 22.8 exposes camelCase aliases for methods it *does* know, and
    ``do_api_request`` nudges callers towards them. ``editMessageText`` is the
    only such endpoint here (rich edits still need the raw payload), so the
    cosmetic warning is suppressed for the duration of the call.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=f"Please use 'Bot.{endpoint}'", category=PTBUserWarning
        )
        return await bot.do_api_request(
            endpoint, api_kwargs=payload, return_type=return_type
        )


def _markup_dict(reply_markup: Any) -> dict | None:
    if reply_markup is None:
        return None
    if isinstance(reply_markup, InlineKeyboardMarkup):
        return reply_markup.to_dict()
    if isinstance(reply_markup, dict):
        return reply_markup
    to_dict = getattr(reply_markup, "to_dict", None)
    return to_dict() if callable(to_dict) else None


async def send_rich(
    bot: Any,
    chat_id: int | str,
    rich: RichMessage,
    *,
    fallback_html: str | None = None,
    reply_markup: Any = None,
    message_thread_id: int | None = None,
    disable_notification: bool = False,
) -> Any:
    """Send *rich* with ``sendRichMessage``, falling back to HTML text.

    Returns the sent message (a :class:`telegram.Message` when rich messages
    work, otherwise whatever ``sendMessage`` returns).
    """
    fallback = fallback_html if fallback_html is not None else rich.to_html()
    if rich and rich_supported(bot):
        payload: dict = {
            "chat_id": chat_id,
            "rich_message": rich.to_dict(),
            "disable_notification": disable_notification or None,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        markup = _markup_dict(reply_markup)
        if markup is not None:
            payload["reply_markup"] = markup
        try:
            sent = await _call_api(
                bot,
                "sendRichMessage",
                {key: value for key, value in payload.items() if value is not None},
                return_type=Message,
            )
        except (TelegramError, AttributeError, TypeError, ValueError) as exc:
            _support.note_failure(exc)
            logger.info("sendRichMessage failed, using HTML fallback: %s", exc)
        else:
            _support.note_success()
            return sent

    return await bot.send_message(
        chat_id=chat_id,
        text=fallback or "…",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
        message_thread_id=message_thread_id,
    )


async def reply_rich(
    message: Any,
    bot: Any,
    rich: RichMessage,
    *,
    fallback_html: str | None = None,
    reply_markup: Any = None,
) -> Any:
    """Reply to *message* with a rich message (or its HTML fallback)."""
    fallback = fallback_html if fallback_html is not None else rich.to_html()
    if rich and rich_supported(bot):
        payload: dict = {"chat_id": message.chat_id, "rich_message": rich.to_dict()}
        markup = _markup_dict(reply_markup)
        if markup is not None:
            payload["reply_markup"] = markup
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        try:
            sent = await _call_api(
                bot, "sendRichMessage", payload, return_type=Message
            )
        except (TelegramError, AttributeError, TypeError, ValueError) as exc:
            _support.note_failure(exc)
            logger.info("sendRichMessage failed, using HTML fallback: %s", exc)
        else:
            _support.note_success()
            return sent
    return await message.reply_html(fallback or "…", reply_markup=reply_markup)


async def edit_rich(
    bot: Any,
    chat_id: int | str,
    message_id: int,
    rich: RichMessage,
    *,
    fallback_html: str | None = None,
    reply_markup: Any = None,
) -> bool:
    """Edit a message into a rich message; returns True on success.

    Falls back to ``editMessageText`` with HTML, and swallows Telegram's
    benign "message is not modified" / "message can't be edited" errors.
    """
    fallback = fallback_html if fallback_html is not None else rich.to_html()
    if rich and rich_supported(bot):
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": rich.to_dict(),
        }
        markup = _markup_dict(reply_markup)
        if markup is not None:
            payload["reply_markup"] = markup
        try:
            await _call_api(bot, "editMessageText", payload)
        except (TelegramError, AttributeError, TypeError, ValueError) as exc:
            _support.note_failure(exc)
            logger.info("rich editMessageText failed, using HTML: %s", exc)
        else:
            _support.note_success()
            return True

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=fallback or "…",
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("HTML edit fallback failed: %s", exc)
        return False
    except TelegramError as exc:
        logger.debug("HTML edit fallback failed: %s", exc)
        return False
    return True
