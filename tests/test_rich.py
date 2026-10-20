"""Rich message builders, safety trimming and the send/edit wrappers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest, TimedOut

from gofile_bot.services.status import StatusMessage
from gofile_bot.telegram import rich
from gofile_bot.telegram.rich import (
    RichMessage,
    bold,
    bullets,
    buttons,
    cell,
    code,
    details,
    divider,
    expandable_quote,
    footer,
    heading,
    italic,
    link,
    math,
    numbered,
    paragraph,
    pre,
    quote,
    rich_button,
    spoiler,
    table,
)


class FakeBot:
    """Minimal Bot: rich endpoint + HTML fallbacks."""

    def __init__(self, *, error=None, response=None):
        self.calls: list[tuple[str, dict, object]] = []
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.error = error
        self.response = response if response is not None else {"message_id": 42}

    @property
    def can_go_rich(self) -> bool:
        return True

    async def do_api_request(self, endpoint, api_kwargs=None, return_type=None, **_):
        self.calls.append((endpoint, api_kwargs or {}, return_type))
        if self.error is not None:
            raise self.error
        return self.response

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=99)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return True


class NoRichBot:
    """A Bot without the ``do_api_request`` escape hatch (legacy wiring)."""

    def __init__(self):
        self.edited: list[dict] = []
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)


# ---------------------------------------------------------------------------
# Text + block builders
# ---------------------------------------------------------------------------
def test_inline_text_helpers():
    assert bold("x") == {"type": "bold", "text": "x"}
    assert italic("x") == {"type": "italic", "text": "x"}
    assert spoiler("x") == {"type": "spoiler", "text": "x"}
    assert link("docs", "https://x") == {"type": "url", "text": "docs", "url": "https://x"}
    assert math("x^2") == {"type": "mathematical_expression", "expression": "x^2"}
    assert code("a<b") == {"type": "code", "text": "a<b"}


def test_paragraph_coerces_rich_text_lists():
    block = paragraph(["Hello ", bold("world"), " — ", italic("again")])
    assert block["type"] == "paragraph"
    assert block["text"][1] == {"type": "bold", "text": "world"}


def test_heading_clamps_size():
    assert heading("T", 0)["size"] == 1
    assert heading("T", 9)["size"] == 6
    assert heading("T", "3")["size"] == 3


def test_basic_blocks():
    assert pre("x=1", language="python") == {
        "type": "pre",
        "text": "x=1",
        "language": "python",
    }
    assert "language" not in pre("x=1")
    assert divider() == {"type": "divider"}
    assert footer("bye") == {"type": "footer", "text": "bye"}
    assert quote(paragraph("a"), credit="me")["credit"] == "me"
    assert expandable_quote("long", credit="src")["credit"] == "src"
    assert details("More", paragraph("hidden"), is_open=True)["is_open"] is True
    assert "is_open" not in details("More", paragraph("hidden"))


def test_lists_and_task_lists():
    block = bullets("one", ["two ", bold("bold")])
    assert block["type"] == "list"
    assert block["items"][0]["blocks"][0]["text"] == "one"
    assert block["items"][1]["blocks"][0]["text"][1]["type"] == "bold"
    assert "has_checkbox" not in block["items"][0]

    tasks = bullets("a", "b", checklist=True, checked=True)
    assert tasks["items"][0]["has_checkbox"] is True
    assert tasks["items"][0]["is_checked"] is True

    ordered = numbered("a", "b")
    assert ordered["items"][0]["value"] == 1
    assert ordered["items"][1]["type"] == "1"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def test_table_shape_and_required_cell_fields():
    block = table(
        ["Name", "Size"],
        [["a.zip", "1 MB"], ["b.zip", "2 MB"]],
        caption="files",
        compact=True,
        aligns=["left", "right"],
    )
    assert block["type"] == "table"
    assert block["is_bordered"] is True and block["is_striped"] is True
    assert block["is_compact"] is True
    assert block["caption"] == "files"
    header, first, second = block["cells"]
    assert header[0]["is_header"] is True
    assert header[1]["is_header"] is True
    for row in block["cells"]:
        for value in row:
            assert value["align"] in ("left", "center", "right")
            assert value["valign"] in ("top", "middle", "bottom")
    assert first[1]["align"] == "right"


def test_table_pads_missing_cells_without_text():
    block = table(["A", "B", "C"], [["only one"]])
    row = block["cells"][1]
    assert "text" in row[0]
    assert all("text" not in value for value in row[1:])


def test_table_caps_columns_at_twenty():
    block = table(None, [[str(i) for i in range(30)]])
    assert len(block["cells"][0]) == rich.MAX_TABLE_COLUMNS


def test_table_caps_rows():
    block = table(["n"], [[str(i)] for i in range(rich.MAX_TABLE_ROWS + 5)])
    assert len(block["cells"]) == rich.MAX_TABLE_ROWS


def test_cell_rejects_bad_alignment():
    value = cell("x", align="diagonal", valign="sideways")
    assert value["align"] == "left"
    assert value["valign"] == "middle"


def test_table_rowspan_colspan_and_invisible_cells():
    value = cell(None, header=True, colspan=2, align="right", valign="top")
    assert "text" not in value
    assert value["is_header"] is True
    assert value["colspan"] == 2
    assert value["align"] == "right"


# ---------------------------------------------------------------------------
# Buttons (Bot API 10.3)
# ---------------------------------------------------------------------------
def test_buttons_block_limits_and_defaults():
    row = [rich_button(f"b{i}", callback_data=f"c{i}") for i in range(12)]
    block = buttons(row)
    assert block["type"] == "buttons"
    assert len(block["buttons"]) == 8
    assert block["align"] == "center"


def test_rich_button_variants():
    assert rich_button("Go", url="https://x", style="primary") == {
        "text": "Go",
        "url": "https://x",
        "style": "primary",
    }
    assert rich_button("Go", callback_data="x" * 80)["callback_data"] == "x" * 64


# ---------------------------------------------------------------------------
# Document validation + trimming
# ---------------------------------------------------------------------------
def test_document_payload_flags():
    doc = RichMessage().add(paragraph("hi"))
    assert doc.to_dict() == {"blocks": [{"type": "paragraph", "text": "hi"}]}
    doc.is_rtl = True
    doc.skip_entity_detection = True
    payload = doc.to_dict()
    assert payload["is_rtl"] is True
    assert payload["skip_entity_detection"] is True
    assert bool(RichMessage()) is False
    assert bool(doc) is True


def test_document_enforces_character_budget():
    doc = RichMessage().add(paragraph("a" * 20_000), paragraph("b" * 20_000))
    blocks = doc.to_dict()["blocks"]
    total = sum(len(block["text"]) for block in blocks)
    assert total <= rich.MAX_RICH_CHARS
    assert blocks[-1]["text"].endswith("…")


def test_document_enforces_block_limit():
    doc = RichMessage()
    doc.extend([paragraph(str(i)) for i in range(rich.MAX_RICH_BLOCKS + 20)])
    assert len(doc.to_dict()["blocks"]) == rich.MAX_RICH_BLOCKS


def test_document_enforces_nesting_limit():
    inner = paragraph("deep")
    for _ in range(30):
        inner = details("more", inner)
    blocks = RichMessage().add(inner).to_dict()["blocks"]

    def depth(node):
        if not isinstance(node, dict) or "blocks" not in node:
            return 0
        return 1 + max((depth(child) for child in node["blocks"]), default=0)

    assert depth(blocks[0]) <= rich.MAX_RICH_DEPTH


def test_document_sanitizes_table_cells():
    block = {"type": "table", "cells": [[{"text": "x"}, "raw"]]}
    cells = RichMessage().add(block).to_dict()["blocks"][0]["cells"][0]
    assert cells[0]["align"] == "left" and cells[0]["valign"] == "middle"
    assert cells[1]["text"] == "raw"


def test_html_fallback_escapes_and_renders():
    doc = RichMessage().add(
        heading("Title", 2),
        paragraph("a < b & c"),
        bullets("one", "two"),
        table(["A", "B"], [["1 < 2", "ok"]]),
        buttons([rich_button("Open", url="https://example.com/?a=1&b=2")]),
        footer("bye"),
    )
    html = doc.to_html()
    assert "<b>Title</b>" in html
    assert "a &lt; b &amp; c" in html
    assert "1 &lt; 2" in html
    assert "https://example.com/?a=1&amp;b=2" in html
    assert "&lt;pre&gt;" not in html


def test_html_fallback_is_bounded():
    doc = RichMessage().add(paragraph("x" * 30_000))
    assert len(doc.to_html()) <= 4096


def test_html_fallback_for_empty_document():
    assert RichMessage().to_html() == "…"


# ---------------------------------------------------------------------------
# send_rich / reply_rich / edit_rich
# ---------------------------------------------------------------------------
async def test_send_rich_uses_do_api_request():
    bot = FakeBot(response={"message_id": 7})
    doc = RichMessage().add(paragraph("hello"))
    sent = await rich.send_rich(bot, 123, doc)
    assert sent == {"message_id": 7}
    endpoint, payload, return_type = bot.calls[0]
    assert endpoint == "sendRichMessage"
    assert payload["chat_id"] == 123
    assert payload["rich_message"]["blocks"][0]["text"] == "hello"
    assert "disable_notification" not in payload


async def test_send_rich_includes_markup_as_json():
    bot = FakeBot()
    doc = RichMessage().add(paragraph("hello"))
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Go", callback_data="x")]])
    await rich.send_rich(bot, 123, doc, reply_markup=markup, disable_notification=True)
    payload = bot.calls[0][1]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "x"
    assert payload["disable_notification"] is True


async def test_send_rich_falls_back_on_error():
    bot = FakeBot(error=BadRequest("Bad Request: method not found"))
    doc = RichMessage().add(paragraph("hello"))
    await rich.send_rich(bot, 123, doc, fallback_html="<b>hello</b>")
    assert bot.sent[0]["text"] == "<b>hello</b>"
    assert bot.sent[0]["parse_mode"] == "HTML"
    assert rich.rich_supported(bot) is False


async def test_send_rich_falls_back_without_support():
    bot = NoRichBot()
    doc = RichMessage().add(paragraph("hello"))
    await rich.send_rich(bot, 1, doc)
    assert bot.sent[0]["text"] == "hello"


async def test_reply_rich_falls_back_to_reply_html():
    message = SimpleNamespace(chat_id=5, message_thread_id=None, reply_html=AsyncMock())
    bot = NoRichBot()
    await rich.reply_rich(message, bot, RichMessage().add(paragraph("hi")))
    message.reply_html.assert_awaited_once()
    assert message.reply_html.await_args.args[0] == "hi"


async def test_reply_rich_uses_thread_id():
    message = SimpleNamespace(chat_id=5, message_thread_id=9, reply_html=AsyncMock())
    bot = FakeBot()
    await rich.reply_rich(message, bot, RichMessage().add(paragraph("hi")))
    assert bot.calls[0][1]["message_thread_id"] == 9


async def test_edit_rich_uses_rich_message_payload():
    bot = FakeBot(response=True)
    doc = RichMessage().add(paragraph("new"))
    assert await rich.edit_rich(bot, 10, 20, doc) is True
    endpoint, payload, _ = bot.calls[0]
    assert endpoint == "editMessageText"
    assert payload["chat_id"] == 10
    assert payload["message_id"] == 20
    assert payload["rich_message"]["blocks"][0]["text"] == "new"


async def test_edit_rich_falls_back_to_html():
    bot = FakeBot(error=TimedOut("timeout"))
    doc = RichMessage().add(paragraph("new"))
    assert await rich.edit_rich(bot, 10, 20, doc, fallback_html="<b>new</b>") is True
    assert bot.edited[0]["text"] == "<b>new</b>"
    assert bot.edited[0]["parse_mode"] == "HTML"


async def test_edit_rich_swallows_not_modified():
    bot = NoRichBot()

    async def not_modified(**kwargs):
        raise BadRequest("Bad Request: message is not modified")

    bot.edit_message_text = not_modified
    assert await rich.edit_rich(bot, 1, 2, RichMessage().add(paragraph("x"))) is False


async def test_support_is_disabled_after_unsupported_error():
    rich.reset_rich_support()
    failing = FakeBot(error=BadRequest("Bad Request: unsupported method"))
    doc = RichMessage().add(paragraph("x"))
    await rich.send_rich(failing, 1, doc)
    assert rich.rich_supported(failing) is False
    fresh = FakeBot()
    await rich.send_rich(fresh, 1, doc)
    assert fresh.calls == []  # the process-wide probe skips rich entirely
    rich.reset_rich_support()
    assert rich.rich_supported(fresh) is True


# ---------------------------------------------------------------------------
# StatusMessage integration
# ---------------------------------------------------------------------------
async def test_status_message_edits_rich(monkeypatch):
    bot = FakeBot(response=True)
    status = StatusMessage(bot, 10, 20, min_interval=0)
    doc = RichMessage().add(paragraph("progress"))
    assert await status.edit("<b>progress</b>", force=True, rich=doc) is True
    assert bot.calls[0][0] == "editMessageText"
    assert bot.calls[0][1]["rich_message"]["blocks"][0]["text"] == "progress"


async def test_status_message_uses_html_when_rich_unavailable():
    bot = NoRichBot()
    status = StatusMessage(bot, 10, 20, min_interval=0)
    assert await status.edit("<b>progress</b>", force=True, rich=RichMessage().add(paragraph("x")))
    assert bot.edited[0]["text"] == "<b>progress</b>"


async def test_status_message_rich_failure_falls_back_to_html(monkeypatch):
    bot = FakeBot(error=BadRequest("Bad Request: can't parse rich message"))
    status = StatusMessage(bot, 10, 20, min_interval=0)
    assert await status.edit(
        "<b>progress</b>", force=True, rich=RichMessage().add(paragraph("x"))
    )
    assert bot.edited[-1]["text"] == "<b>progress</b>"
    rich.reset_rich_support()


@pytest.mark.parametrize(
    "value,expected",
    [("plain", "plain"), (7, "7"), (None, "None")],
)
def test_text_coercion(value, expected):
    assert rich.text(value) == expected


def test_html_fallback_never_splits_tags_or_entities():
    doc = RichMessage().add(paragraph("&" * 5000))
    html = doc.to_html()
    assert len(html) <= 4096
    assert not html.rstrip("…").endswith("&")
    assert "<" not in html.replace("&lt;", "")
