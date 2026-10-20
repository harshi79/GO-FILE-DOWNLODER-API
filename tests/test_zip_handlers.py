"""Extraction UI, responsive metadata dispatch and Telegram ZIP streaming."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aioresponses import aioresponses

from gofile_bot.config import MAX_ZIP_BYTES
from gofile_bot.downloaders.telegram_zip import TelegramZipDownloader
from gofile_bot.errors import FileTooLargeError, ResolveError
from gofile_bot.gofile.models import RemoteFile
from gofile_bot.services.resources import MetadataTasks
from gofile_bot.telegram import handlers
from gofile_bot.telegram.keyboards import link_keyboard
from tests.test_universal_handlers import make_context, make_update


async def test_extract_link_has_extract_only_confirmation():
    context, _ = make_context()
    await handlers.extract_cmd(
        make_update("/extract https://fakehost.example/file"), context
    )
    job = next(iter(context.application.bot_data["jobs"]._jobs.values()))
    assert job.action == "extract"
    assert job.content is not None
    markup = context.application.bot.edit_message_text.await_args.kwargs["reply_markup"]
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].callback_data == f"zx:{job.id}"


async def test_zip_link_has_both_download_and_extract_buttons():
    context, _ = make_context()
    await handlers.download_cmd(
        make_update("/download https://fakehost.example/file"), context
    )
    markup = context.application.bot.edit_message_text.await_args.kwargs["reply_markup"]
    assert [row[0].callback_data[:3] for row in markup.inline_keyboard] == [
        "dl:",
        "zx:",
    ]


async def test_oversized_extract_link_never_gets_a_button():
    context, provider = make_context()
    provider.resolve = AsyncMock(
        return_value=RemoteFile(
            id="zip",
            name="large.zip",
            size=MAX_ZIP_BYTES + 1,
            mimetype="application/zip",
            link="https://fakehost.example/file",
        )
    )
    await handlers.extract_cmd(
        make_update("/extract https://fakehost.example/file"), context
    )
    edit = context.application.bot.edit_message_text.await_args.kwargs
    assert "20 MB" in edit["text"]
    assert edit["reply_markup"] is None


async def test_metadata_does_not_block_ping_and_can_be_cancelled():
    context, provider = make_context()
    context.application.bot_data["metadata_tasks"] = MetadataTasks()
    tasks = []

    def spawn(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    context.application.create_task = spawn
    started, stopped = asyncio.Event(), asyncio.Event()

    async def resolve(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    provider.resolve = resolve
    await handlers.download_cmd(
        make_update("/download https://fakehost.example/file"), context
    )
    await asyncio.wait_for(started.wait(), 1)
    ping = make_update("/ping")
    await asyncio.wait_for(handlers.ping_cmd(ping, context), 0.2)
    assert "Pong" in ping.message.reply_text.await_args.args[0]
    await handlers.cancel_cmd(make_update("/cancel"), context)
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert stopped.is_set()
    assert not context.application.bot_data["metadata_tasks"].users
    assert context.application.bot_data["jobs"].active_for_user(7) is None


async def test_extract_callback_requires_matching_user():
    context, _ = make_context()
    await handlers.extract_cmd(
        make_update("/extract https://fakehost.example/file"), context
    )
    job = next(iter(context.application.bot_data["jobs"]._jobs.values()))
    query = SimpleNamespace(
        data=f"zx:{job.id}", from_user=SimpleNamespace(id=999), answer=AsyncMock()
    )
    await handlers.extract_cb(SimpleNamespace(callback_query=query), context)
    assert "isn't for you" in query.answer.await_args.kwargs["text"]
    context.application.bot_data["transfer"].run.assert_not_awaited()


async def test_telegram_zip_download_streams_to_disk(tmp_path):
    bot = SimpleNamespace(
        get_file=AsyncMock(
            return_value=SimpleNamespace(
                file_path="https://api.telegram.org/file/botTEST/archive.zip",
                file_size=5,
            )
        )
    )
    provider = TelegramZipDownloader(bot)
    file = RemoteFile(
        id="id", name="archive.zip", size=5, mimetype="application/zip", link=""
    )
    try:
        with aioresponses() as responses:
            responses.get(
                "https://api.telegram.org/file/botTEST/archive.zip", body=b"hello"
            )
            path = await provider.download(
                file, tmp_path / "archive.zip", max_bytes=MAX_ZIP_BYTES
            )
        assert path.read_bytes() == b"hello"
        bot.get_file.assert_awaited_once_with("id")
    finally:
        await provider.close()


async def test_telegram_size_check_runs_before_downloading(tmp_path):
    bot = SimpleNamespace(
        get_file=AsyncMock(
            return_value=SimpleNamespace(
                file_path="https://api.telegram.org/file/botTEST/archive.zip",
                file_size=MAX_ZIP_BYTES + 1,
            )
        )
    )
    provider = TelegramZipDownloader(bot)
    file = RemoteFile(
        id="id", name="archive.zip", size=0, mimetype="application/zip", link=""
    )
    with pytest.raises(FileTooLargeError):
        await provider.download(file, tmp_path / "archive.zip", max_bytes=10**12)
    assert not list(tmp_path.iterdir())


async def test_telegram_download_errors_do_not_expose_bot_token(tmp_path):
    bot = SimpleNamespace(
        get_file=AsyncMock(
            return_value=SimpleNamespace(
                file_path="https://api.telegram.org/file/botSECRET_TOKEN/archive.zip",
                file_size=5,
            )
        )
    )
    provider = TelegramZipDownloader(bot)
    file = RemoteFile(
        id="id", name="archive.zip", size=5, mimetype="application/zip", link=""
    )
    try:
        with aioresponses() as responses:
            responses.get(
                "https://api.telegram.org/file/botSECRET_TOKEN/archive.zip", status=403
            )
            with pytest.raises(ResolveError) as error:
                await provider.download(
                    file, tmp_path / "archive.zip", max_bytes=MAX_ZIP_BYTES
                )
        assert "SECRET_TOKEN" not in error.value.detail
        assert "SECRET_TOKEN" not in error.value.user_message
    finally:
        await provider.close()


def test_copy_link_button_respects_telegram_utf16_length_limit():
    markup = link_keyboard("https://example.com/file")
    assert markup.inline_keyboard[0][1].copy_text.text == "https://example.com/file"
    markup = link_keyboard("https://example.com/" + "😀" * 130)
    assert len(markup.inline_keyboard[0]) == 1
