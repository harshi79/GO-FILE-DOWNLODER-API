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
    assert "Hi!" in update.message.reply_text.await_args.args[0]

    await handlers.help_cmd(update, context)
    update.message.reply_html.assert_awaited()
    assert "Commands" in update.message.reply_html.await_args.args[0]
