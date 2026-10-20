"""Handler tests: command parsing and the /gofile flow with fakes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gofile_bot.errors import InvalidURLError
from gofile_bot.gofile.models import RemoteFile
from gofile_bot.services.jobs import JobStore
from gofile_bot.telegram import handlers


def test_parse_command_args_url_only():
    url, password = handlers.parse_command_args("/gofile https://gofile.io/d/AbCdEf12")
    assert url == "https://gofile.io/d/AbCdEf12"
    assert password is None


def test_parse_command_args_with_password():
    url, password = handlers.parse_command_args(
        "/gofile@MyBot https://gofile.io/d/AbCdEf12 my secret pass"
    )
    assert url == "https://gofile.io/d/AbCdEf12"
    assert password == "my secret pass"


def test_parse_command_args_missing():
    with pytest.raises(InvalidURLError):
        handlers.parse_command_args("/gofile")
    with pytest.raises(InvalidURLError):
        handlers.parse_command_args("/gofile    ")


def make_context(*, content=None, transfer_run=None):
    jobs = JobStore()
    gofile = SimpleNamespace(fetch_content=AsyncMock(return_value=content))
    transfer = SimpleNamespace(run=transfer_run or AsyncMock())
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_document=AsyncMock())
    application = SimpleNamespace(
        bot=bot, bot_data={"jobs": jobs, "gofile": gofile, "transfer": transfer}
    )
    return SimpleNamespace(application=application), jobs


def make_update(text: str, user_id: int = 7):
    status_message = SimpleNamespace(message_id=100)
    message = SimpleNamespace(
        text=text,
        chat_id=50,
        message_id=99,
        reply_text=AsyncMock(return_value=status_message),
        reply_html=AsyncMock(),
    )
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id),
    )
    return update


def make_file() -> RemoteFile:
    return RemoteFile(
        id="fid",
        name="doc.pdf",
        size=1000,
        mimetype="application/pdf",
        link="https://store-1.gofile.io/download/web/fid/doc.pdf",
    )


async def test_gofile_cmd_success():
    context, jobs = make_context(content=make_file())
    update = make_update("/gofile https://gofile.io/d/AbCdEf12")

    await handlers.gofile_cmd(update, context)

    update.message.reply_text.assert_awaited()
    assert len(jobs) == 1
    job = next(iter(jobs._jobs.values()))
    assert job.content is not None and job.content.name == "doc.pdf"
    context.application.bot.edit_message_text.assert_awaited()
    final_text = context.application.bot.edit_message_text.await_args.kwargs["text"]
    assert "doc.pdf" in final_text
    assert "1000 B" in final_text
    markup = context.application.bot.edit_message_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"dl:{job.id}"


async def test_gofile_cmd_invalid_url():
    context, jobs = make_context(content=make_file())
    update = make_update("/gofile https://evil.com/x")

    await handlers.gofile_cmd(update, context)

    reply = update.message.reply_text.await_args.args[0]
    assert "GoFile link" in reply
    assert len(jobs) == 0
    context.application.bot.edit_message_text.assert_not_awaited()


async def test_gofile_cmd_fetch_error_edits_status():
    from gofile_bot.errors import ContentNotFoundError

    context, _jobs_unused = make_context(content=None)
    context.application.bot_data["gofile"].fetch_content.side_effect = (
        ContentNotFoundError("gone")
    )
    update = make_update("/gofile https://gofile.io/d/AbCdEf12")

    await handlers.gofile_cmd(update, context)

    final_text = context.application.bot.edit_message_text.await_args.kwargs["text"]
    assert "doesn't exist" in final_text


async def test_gofile_cmd_busy():
    context, jobs = make_context(content=make_file())
    job = jobs.create(
        user_id=7, chat_id=50, status_message_id=1, content_id="x", password=None
    )
    job.running = True
    update = make_update("/gofile https://gofile.io/d/AbCdEf12")

    await handlers.gofile_cmd(update, context)

    reply = update.message.reply_text.await_args.args[0]
    assert "already have a download" in reply
    assert len(jobs) == 1


async def test_download_cb_requires_matching_user():
    context, jobs = make_context(content=make_file(), transfer_run=AsyncMock())
    job = jobs.create(
        user_id=7, chat_id=50, status_message_id=1, content_id="x", password=None
    )
    job.content = make_file()
    query = SimpleNamespace(
        data=f"dl:{job.id}",
        from_user=SimpleNamespace(id=999),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)

    await handlers.download_cb(update, context)

    query.answer.assert_awaited()
    assert "isn't for you" in query.answer.await_args.kwargs["text"]
    context.application.bot_data["transfer"].run.assert_not_awaited()


async def test_download_cb_runs_transfer():
    run = AsyncMock()
    context, jobs = make_context(content=make_file(), transfer_run=run)
    job = jobs.create(
        user_id=7, chat_id=50, status_message_id=1, content_id="x", password=None
    )
    job.content = make_file()
    query = SimpleNamespace(
        data=f"dl:{job.id}",
        from_user=SimpleNamespace(id=7),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)

    await handlers.download_cb(update, context)

    run.assert_awaited_once_with(job)
    assert job.running is True  # the real TransferService.run() resets the flag


async def test_cancel_cb_sets_event():
    context, jobs = make_context(content=make_file())
    job = jobs.create(
        user_id=7, chat_id=50, status_message_id=1, content_id="x", password=None
    )
    query = SimpleNamespace(
        data=f"cx:{job.id}",
        from_user=SimpleNamespace(id=7),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)

    await handlers.cancel_cb(update, context)

    assert job.cancelled


async def test_expired_button():
    context, _jobs_unused = make_context(content=make_file())
    query = SimpleNamespace(
        data="dl:deadbeef",
        from_user=SimpleNamespace(id=7),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)

    await handlers.download_cb(update, context)

    assert "expired" in query.answer.await_args.kwargs["text"].lower()


async def test_start_and_help():
    context, _jobs_unused = make_context()
    update = make_update("/start")
    await handlers.start_cmd(update, context)
    assert "Hi!" in update.message.reply_html.await_args.args[0]

    await handlers.help_cmd(update, context)
    assert update.message.reply_html.await_count == 2
    assert "Commands" in update.message.reply_html.await_args.args[0]


async def test_old_download_button_cannot_bypass_active_user_limit():
    context, jobs = make_context(content=make_file(), transfer_run=AsyncMock())
    active = jobs.create(user_id=7, chat_id=50, status_message_id=1)
    active.running = True
    old = jobs.create(user_id=7, chat_id=50, status_message_id=2)
    old.content = make_file()
    query = SimpleNamespace(data=f"dl:{old.id}", from_user=SimpleNamespace(id=7), answer=AsyncMock())
    await handlers.download_cb(SimpleNamespace(callback_query=query), context)
    assert "already have a download" in query.answer.await_args.kwargs["text"]
    context.application.bot_data["transfer"].run.assert_not_awaited()


async def test_button_reserves_user_before_answering():
    context, jobs = make_context(content=make_file(), transfer_run=AsyncMock())
    job = jobs.create(user_id=7, chat_id=50, status_message_id=1)
    job.content = make_file()

    async def answer(**kwargs):
        assert jobs.active_for_user(7) is job

    query = SimpleNamespace(data=f"dl:{job.id}", from_user=SimpleNamespace(id=7), answer=answer)
    await handlers.download_cb(SimpleNamespace(callback_query=query), context)


class _FakeUsage:
    def __init__(self, *, banned: bool = False, downloads: int = 0):
        self.banned = banned
        self.downloads = downloads
        self.touched: list[tuple[int, str | None]] = []

    async def touch_user(self, user_id, *, username=None, first_name=None, command=None):
        self.touched.append((user_id, command))

    async def user(self, user_id):
        from gofile_bot.services.usage import UserStats

        return UserStats(user_id=user_id, downloads=self.downloads, bytes=2048)

    def is_banned(self, user_id):
        return self.banned


async def test_me_command_reports_usage():
    from gofile_bot.services.usage import UserStats  # noqa: F401  (documented type)

    context, _jobs_unused = make_context()
    context.application.bot_data["usage"] = _FakeUsage(downloads=3)
    update = make_update("/me")

    await handlers.me_cmd(update, context)

    text = update.message.reply_html.await_args.args[0]
    assert "Downloads: 3" in text
    assert "Your usage" in text
    usage = context.application.bot_data["usage"]
    assert (7, "me") in usage.touched


async def test_start_is_blocked_for_banned_users():
    context, _jobs_unused = make_context()
    context.application.bot_data["usage"] = _FakeUsage(banned=True)
    update = make_update("/start")

    await handlers.start_cmd(update, context)

    update.message.reply_text.assert_awaited()
    assert "blocked" in update.message.reply_text.await_args.args[0]
    update.message.reply_html.assert_not_awaited()


async def test_menu_callback_opens_help():
    context, _jobs_unused = make_context()
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_message=AsyncMock(),
    )
    context.application.bot = bot
    query = SimpleNamespace(
        data="rc:help",
        from_user=SimpleNamespace(id=7),
        message=SimpleNamespace(chat_id=50, message_id=99),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_message=None,
        message=None,
    )

    await handlers.menu_cb(update, context)

    query.answer.assert_awaited()
    bot.edit_message_text.assert_awaited()
    assert "Commands" in bot.edit_message_text.await_args.kwargs["text"]


async def test_status_command_renders_menu():
    from gofile_bot.config import MAX_FILE_BYTES

    context, _jobs_unused = make_context()
    context.application.bot_data["settings"] = SimpleNamespace(
        enable_mtproto=False,
        telegram_api_url=None,
        max_file_bytes=MAX_FILE_BYTES,
    )
    context.application.bot_data["transfer"] = SimpleNamespace(
        snapshot=lambda job=None: {
            "active": 0,
            "queued": 0,
            "position": None,
            "capacity": 8,
        }
    )
    update = make_update("/status")

    await handlers.status_cmd(update, context)

    text = update.message.reply_html.await_args.args[0]
    assert "Active: 0" in text
    assert "Capacity: 8 jobs" in text
    assert "no queued/running transfer" in text
