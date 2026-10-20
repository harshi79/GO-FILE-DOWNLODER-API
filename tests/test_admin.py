"""Usage persistence, admin access control and the /admin panel."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gofile_bot.config import Settings, parse_admin_ids
from gofile_bot.services.usage import Overview, UsageStore, UserStats
from gofile_bot.telegram import admin, menus
from gofile_bot.telegram.rich import RichMessage


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_parse_admin_ids_accepts_common_separators():
    assert parse_admin_ids("1, 2 3;4") == (1, 2, 3, 4)
    assert parse_admin_ids(" 7 , 7 ") == (7,)
    assert parse_admin_ids("") == ()


@pytest.mark.parametrize("raw", ["abc", "1,2,x", "-5", "0", "1.5"])
def test_parse_admin_ids_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_admin_ids(raw)


def test_settings_defaults_are_not_admin_enabled():
    settings = Settings(bot_token="123:TEST")
    assert settings.admin_user_ids == ()
    assert settings.usage_db == "data/usage.sqlite3"


# ---------------------------------------------------------------------------
# UsageStore
# ---------------------------------------------------------------------------
@pytest.fixture
async def store(tmp_path):
    usage = UsageStore(tmp_path / "usage.sqlite3")
    await usage.open()
    yield usage
    await usage.close()


async def test_tracking_and_aggregates(store):
    await store.touch_user(1, username="alice", first_name="Alice", command="start")
    await store.touch_user(1, command="help")
    await store.record_download(1, provider="gdrive", bytes=500, duration=1.5)
    await store.record_download(1, provider="gdrive", status="failed")
    await store.record_download(2, provider="gofile", bytes=100)

    stats = await store.user(1)
    assert stats.downloads == 1 and stats.failed == 1
    assert stats.bytes == 500 and stats.commands == 2
    assert stats.username == "alice" and stats.first_name == "Alice"
    assert stats.name == "Alice"
    assert stats.mention_html.startswith('<a href="tg://user?id=1">')

    overview = await store.overview()
    assert overview.users == 2
    assert overview.downloads == 2 and overview.failed == 1
    assert overview.bytes == 600
    assert overview.new_users_24h == 2
    assert overview.downloads_24h == 3
    assert {entry[0] for entry in overview.providers} == {"gdrive", "gofile"}


async def test_list_users_orders_and_pages(store):
    for user_id in range(1, 11):
        await store.touch_user(user_id, first_name=f"u{user_id}")
        for _ in range(user_id):
            await store.record_download(user_id, bytes=10)

    page0, total = await store.list_users(page=0, per_page=4)
    assert total == 10
    assert [stats.user_id for stats in page0] == [10, 9, 8, 7]

    page1, _ = await store.list_users(page=1, per_page=4)
    assert [stats.user_id for stats in page1] == [6, 5, 4, 3]

    recent, _ = await store.list_users(page=0, per_page=3, order="recent")
    assert len(recent) == 3

    by_bytes, _ = await store.list_users(page=0, per_page=2, order="bytes")
    assert by_bytes[0].user_id == 10


async def test_recent_downloads(store):
    await store.touch_user(5, first_name="Eve")
    await store.record_download(5, provider="dropbox", bytes=42)
    records = await store.recent_downloads(5)
    assert records[0].user_id == 5
    assert records[0].user_name == "Eve"
    assert records[0].provider == "dropbox"
    assert records[0].bytes == 42


async def test_bans_roundtrip(store):
    await store.touch_user(9, first_name="Bad")
    await store.ban(9, banned_by=1, reason="spam")
    assert store.is_banned(9)
    assert 9 in store.banned_ids
    stats = await store.user(9)
    assert stats.banned is True and stats.ban_reason == "spam"
    banned, total = await store.banned()
    assert total == 1 and banned[0].user_id == 9
    assert (await store.overview()).active_bans == 1

    assert await store.unban(9) is True
    assert not store.is_banned(9)
    assert await store.unban(9) is False


async def test_reset_and_purge(store):
    await store.touch_user(3, first_name="C")
    await store.record_download(3, bytes=100)
    await store.reset_user(3)
    stats = await store.user(3)
    assert stats.downloads == 0 and stats.bytes == 0
    assert await store.recent_downloads() == []
    assert await store.purge_downloads(1) == 0


async def test_store_persists_across_reopen(tmp_path):
    path = tmp_path / "usage.sqlite3"
    first = UsageStore(path)
    await first.open()
    assert first.using_memory is False
    await first.touch_user(4, first_name="Dora")
    await first.record_download(4, bytes=10)
    await first.ban(4, reason="testing")
    await first.close()

    second = UsageStore(path)
    await second.open()
    stats = await second.user(4)
    assert stats.downloads == 1 and stats.bytes == 10
    assert second.is_banned(4)
    await second.close()


async def test_unwritable_path_falls_back_to_memory():
    usage = UsageStore("/proc/definitely-not-writable/usage.sqlite3")
    await usage.open()
    assert usage.using_memory is True
    await usage.touch_user(1)
    assert (await usage.overview()).users == 1
    await usage.close()


async def test_operations_after_close_are_safe():
    usage = UsageStore(":memory:")
    await usage.open()
    await usage.close()
    await usage.touch_user(1)
    assert await usage.user(1) is None
    assert (await usage.overview()).users == 0


# ---------------------------------------------------------------------------
# Handlers / fakes
# ---------------------------------------------------------------------------
class FakeBot:
    def __init__(self):
        self.edits: list[dict] = []
        self.sent: list[dict] = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return True

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1)


def make_context(store, *, admins=(7,)):
    settings = Settings(
        bot_token="123:TEST",
        admin_user_ids=admins,
        usage_db=":memory:",
    )
    bot = FakeBot()
    application = SimpleNamespace(
        bot=bot,
        bot_data={
            "settings": settings,
            "usage": store,
            "transfer": SimpleNamespace(
                snapshot=lambda: {"active": 1, "queued": 2, "capacity": 8}
            ),
            "started_at": 0,
        },
    )
    return SimpleNamespace(application=application), bot


def make_message(text=""):
    return SimpleNamespace(
        chat_id=50,
        message_id=99,
        text=text,
        reply_text=AsyncMock(),
        reply_html=AsyncMock(),
    )


def make_update(*, user_id=7, text="", message=None):
    message = message or make_message(text)
    return (
        SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, username="op", first_name="Op"),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message,
            message=message,
            callback_query=None,
        ),
        message,
    )


def make_callback(data, *, user_id=7):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat_id=50, message_id=99),
        answer=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_message=None,
        message=None,
    )


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
async def test_admin_panel_renders_for_admin(store):
    context, bot = make_context(store)
    update, message = make_update(text="/admin")
    await admin.admin_cmd(update, context)
    html = message.reply_html.await_args.args[0]
    assert "Admin panel" in html
    assert "Users:" in html
    assert "Transfer slot: 1 active" in html


async def test_non_admin_is_refused(store):
    context, _bot = make_context(store)
    update, message = make_update(user_id=999, text="/admin")
    await admin.admin_cmd(update, context)
    message.reply_text.assert_awaited()
    assert "operator only" in message.reply_text.await_args.args[0]
    message.reply_html.assert_not_awaited()


async def test_unconfigured_panel_explains_how_to_enable(store):
    context, _bot = make_context(store, admins=())
    update, message = make_update(user_id=1, text="/admin")
    await admin.admin_cmd(update, context)
    assert "ADMIN_USER_IDS" in message.reply_html.await_args.args[0]


async def test_admin_panel_requires_private_chat(store):
    context, _bot = make_context(store)
    update, message = make_update(text="/admin")
    update.effective_chat = SimpleNamespace(type="group")
    await admin.admin_cmd(update, context)
    message.reply_text.assert_awaited()
    assert "private chat" in message.reply_text.await_args.args[0]


async def test_non_admin_callback_gets_no_data(store):
    context, bot = make_context(store)
    update = make_callback("ad:home", user_id=999)
    await admin.admin_cb(update, context)
    update.callback_query.answer.assert_awaited()
    assert update.callback_query.answer.await_args.kwargs["show_alert"] is True
    assert bot.edits == []


async def test_admin_callback_refreshes_panel(store):
    context, bot = make_context(store)
    update = make_callback("ad:home")
    await admin.admin_cb(update, context)
    assert bot.edits and "Admin panel" in bot.edits[-1]["text"]


# ---------------------------------------------------------------------------
# Panel navigation and moderation
# ---------------------------------------------------------------------------
async def test_users_page_lists_users(store):
    await store.touch_user(1, first_name="Alice")
    await store.record_download(1, bytes=10)
    await store.touch_user(2, first_name="Bob")
    context, bot = make_context(store)
    await admin.admin_cb(make_callback("ad:users:0"), context)
    assert "Users" in bot.edits[-1]["text"]
    assert "Alice" in bot.edits[-1]["text"]
    markup = bot.edits[-1]["reply_markup"]
    assert markup is not None


async def test_user_view_ban_unban_and_reset(store):
    await store.touch_user(5, first_name="Sam")
    await store.record_download(5, bytes=10)
    context, bot = make_context(store)

    await admin.admin_cb(make_callback("ad:user:5"), context)
    assert "Sam" in bot.edits[-1]["text"]

    await admin.admin_cb(make_callback("ad:ban:5"), context)
    assert store.is_banned(5)
    assert "banned" in bot.edits[-1]["text"].lower()

    await admin.admin_cb(make_callback("ad:unban:5"), context)
    assert not store.is_banned(5)

    await admin.admin_cb(make_callback("ad:reset:5"), context)
    assert (await store.user(5)).downloads == 0


async def test_bans_and_recent_and_settings_views(store):
    await store.touch_user(6, first_name="Rita")
    await store.record_download(6, provider="pixeldrain", bytes=2048)
    await store.ban(6, reason="abuse")
    context, bot = make_context(store)

    await admin.admin_cb(make_callback("ad:bans:0"), context)
    assert "Banned users" in bot.edits[-1]["text"]

    await admin.admin_cb(make_callback("ad:recent"), context)
    assert "Recent downloads" in bot.edits[-1]["text"]

    await admin.admin_cb(make_callback("ad:settings"), context)
    assert "Settings" in bot.edits[-1]["text"]

    await admin.admin_cb(make_callback("ad:noop"), context)
    assert len(bot.edits) == 3  # noop only answers the callback


async def test_ban_and_unban_commands(store):
    context, _bot = make_context(store)
    update, message = make_update(text="/ban 9 advertising")
    await admin.ban_cmd(update, context)
    assert store.is_banned(9)
    assert (await store.user(9)).ban_reason == "advertising"
    assert "Banned" in message.reply_text.await_args.args[0]

    update, message = make_update(text="/unban 9")
    await admin.unban_cmd(update, context)
    assert not store.is_banned(9)
    assert "Unbanned" in message.reply_text.await_args.args[0]


async def test_ban_command_requires_admin(store):
    context, _bot = make_context(store)
    update, message = make_update(user_id=999, text="/ban 5")
    await admin.ban_cmd(update, context)
    assert not store.is_banned(5)
    assert "operator only" in message.reply_text.await_args.args[0]


async def test_ban_command_validates_arguments(store):
    context, _bot = make_context(store)
    update, message = make_update(text="/ban")
    await admin.ban_cmd(update, context)
    assert "Use /ban" in message.reply_text.await_args.args[0]


# ---------------------------------------------------------------------------
# Menu rendering
# ---------------------------------------------------------------------------
def test_overview_menu_contains_rich_table():
    overview = Overview(
        users=3,
        new_users_24h=1,
        downloads=5,
        failed=1,
        bytes=1234,
        downloads_24h=2,
        bytes_24h=100,
        active_bans=1,
        providers=[("gdrive", 3, 1200)],
    )
    menu = admin.overview_menu(
        overview, settings=None, snapshot={"active": 0, "queued": 0}, started_at=None
    )
    assert isinstance(menu.rich, RichMessage)
    blocks = menu.rich.to_dict()["blocks"]
    assert blocks[0]["type"] == "heading"
    assert any(block["type"] == "table" for block in blocks)
    assert "Users: 3" in menu.html
    assert menu.fallback_markup is not None


def test_user_menu_marks_banned_users():
    menu = admin.user_menu(UserStats(user_id=5, first_name="Sam", banned=True, ban_reason="x"))
    html = menu.html
    assert "Sam" in html and "banned" in html
    callback_data = [
        button.callback_data
        for row in menu.fallback_markup.inline_keyboard
        for button in row
    ]
    assert "ad:unban:5" in callback_data


def test_empty_views_are_friendly():
    users = admin.users_menu([], page=0, total=0)
    assert "No users" in users.html
    bans = admin.bans_menu([], page=0, total=0)
    assert "Nobody is banned" in bans.html
    recent = admin.recent_menu([])
    assert "None yet" in recent.html


def test_settings_menu_flags_memory_fallback():
    menu = admin.settings_menu(None, usage_persistent=False)
    assert "in-memory" in menu.html


def test_menus_start_and_help_marksup():
    start = menus.start_menu(48 * 1024 * 1024)
    assert "Hi!" in start.html
    assert start.fallback_markup is not None
    help_menu = menus.help_menu(48 * 1024 * 1024)
    assert "Commands" in help_menu.html
    assert any(
        block["type"] == "table" for block in help_menu.rich.to_dict()["blocks"]
    )


async def test_corrupt_database_falls_back_to_memory(tmp_path):
    path = tmp_path / "usage.sqlite3"
    path.write_text("this is not a sqlite database")
    usage = UsageStore(path)
    await usage.open()
    assert usage.using_memory is True
    await usage.touch_user(1)
    assert (await usage.overview()).users == 1
    await usage.close()
