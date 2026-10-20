"""MTProto path tests: all Telegram traffic is mocked, no credentials needed."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError
from telethon.sessions import MemorySession

from gofile_bot import app as app_module
from gofile_bot.app import build_application
from gofile_bot.config import LARGE_MAX_FILE_BYTES, Settings, load_settings
from gofile_bot.errors import FileTooLargeError, JobCancelled, TelegramUploadError
from gofile_bot.services.mtproto import MTProtoUploader
from gofile_bot.services.transfer import TransferService
from tests.test_transfer import FakeBot, FakeGofile, make_file, make_job


HASH = "a" * 32  # Test fixture, not a real API credential.


def mtproto_settings(**overrides):
    values = dict(bot_token="123:TEST", enable_mtproto=True, api_id=12345, api_hash=HASH)
    values.update(overrides)
    return Settings(**values)


def fake_client():
    return SimpleNamespace(
        connect=AsyncMock(), disconnect=AsyncMock(), sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(bot=True, id=123)),
        get_input_entity=AsyncMock(return_value=SimpleNamespace(user_id=456)),
        send_file=AsyncMock(),
    )


def test_mtproto_env_enables_large_cap(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("ENABLE_MTPROTO", "true")
    monkeypatch.setenv("API_ID", "12345")
    monkeypatch.setenv("API_HASH", HASH)
    monkeypatch.delenv("TELEGRAM_API_URL", raising=False)
    settings = load_settings(env_file=None)
    assert settings.enable_mtproto
    assert settings.api_id == 12345
    assert settings.max_file_bytes == LARGE_MAX_FILE_BYTES
    assert settings.max_concurrent_downloads == 1
    assert settings.job_timeout == 21600
    assert HASH not in repr(settings)
    assert settings.bot_token not in repr(settings)


@pytest.mark.parametrize("fields", [
    {"api_id": None}, {"api_id": 0}, {"api_id": -1}, {"api_id": True},
    {"api_hash": None}, {"api_hash": "invalid"}, {"api_hash": "z" * 32},
    {"telegram_api_url": "http://api-server:8081"},
])
def test_mtproto_rejects_incomplete_or_conflicting_config(fields):
    with pytest.raises(ValueError):
        mtproto_settings(**fields)


def test_invalid_api_id_environment_is_friendly(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("API_ID", "not-a-number")
    with pytest.raises(ValueError, match="API_ID must be a positive integer"):
        load_settings(env_file=None)


def test_credentials_alone_do_not_enable_mtproto():
    settings = mtproto_settings(enable_mtproto=False)
    assert not settings.large_files_enabled
    assert settings.max_concurrent_downloads == 1


def test_application_wires_uploader_without_connecting():
    app = build_application(mtproto_settings())
    uploader = app.bot_data["mtproto"]
    assert isinstance(uploader, MTProtoUploader)
    assert uploader._client is None
    assert app.bot_data["transfer"]._mtproto is uploader
    assert app.bot.local_mode is False  # No separate Bot API server needed.


async def test_login_uses_only_bot_token():
    client = fake_client()
    uploader = MTProtoUploader(mtproto_settings(), client=client)
    await uploader.start(expected_bot_id=123)
    client.sign_in.assert_awaited_once_with(bot_token="123:TEST")
    assert uploader._started
    await uploader.close()
    client.disconnect.assert_awaited_once()
    assert not uploader._started


async def test_real_client_uses_memory_session(monkeypatch):
    client = fake_client()
    created = []

    def factory(session, api_id, api_hash, **kwargs):
        assert isinstance(session, MemorySession)
        assert (api_id, api_hash) == (12345, HASH)
        assert kwargs["receive_updates"] is True
        assert kwargs["flood_sleep_threshold"] == 0
        created.append(True)
        return client

    monkeypatch.setattr("gofile_bot.services.mtproto.TelegramClient", factory)
    uploader = MTProtoUploader(mtproto_settings())
    await uploader.start(expected_bot_id=123)
    assert created
    await uploader.close()


@pytest.mark.parametrize("me", [SimpleNamespace(bot=False, id=123), SimpleNamespace(bot=True, id=999), None])
async def test_wrong_identity_is_rejected_and_disconnected(me):
    client = fake_client()
    client.get_me.return_value = me
    uploader = MTProtoUploader(mtproto_settings(), client=client)
    with pytest.raises(RuntimeError, match="MTProto bot login failed"):
        await uploader.start(expected_bot_id=123)
    client.disconnect.assert_awaited_once()
    assert not uploader._started


async def test_failed_login_disconnects_without_phone_prompt():
    client = fake_client()
    client.sign_in.side_effect = OSError("network unavailable")
    uploader = MTProtoUploader(mtproto_settings(), client=client)
    with pytest.raises(RuntimeError, match="No phone login is needed"):
        await uploader.start(expected_bot_id=123)
    client.disconnect.assert_awaited_once()


async def test_unknown_peer_asks_for_a_new_message_before_downloading():
    client = fake_client()
    client.get_input_entity.side_effect = ValueError("unknown peer")
    uploader = MTProtoUploader(mtproto_settings(), client=client)
    await uploader.start(expected_bot_id=123)
    bot, source = FakeBot(), FakeGofile()
    await TransferService(bot, source, mtproto_uploader=uploader).run(make_job(make_file()))
    assert not source.downloads
    assert any("Send /start" in text for text in bot.edits)
    await uploader.close()


async def test_send_uses_file_path_chunks_and_progress(tmp_path):
    path = tmp_path / "file.bin"
    path.write_bytes(b"payload")
    client = fake_client()
    uploader = MTProtoUploader(mtproto_settings(), client=client)
    await uploader.start(expected_bot_id=123)
    progress = AsyncMock()

    async def send(peer, **kwargs):
        assert peer is client.get_input_entity.return_value
        assert kwargs["file"] == str(path)
        assert kwargs["force_document"] is True
        assert kwargs["part_size_kb"] == 512
        assert kwargs["attributes"][0].file_name == "safe.bin"
        assert kwargs["caption"] == "<b>caption</b>"
        await kwargs["progress_callback"](7, 7)

    client.send_file.side_effect = send
    await uploader.send_document(
        chat_id=456, path=path, filename="safe.bin", caption="<b>caption</b>",
        progress=progress, cancel_event=asyncio.Event(),
    )
    progress.assert_awaited_once_with(7, 7)
    await uploader.close()


async def test_cancel_during_stalled_upload_stops_task(tmp_path):
    path = tmp_path / "file.bin"
    path.write_bytes(b"payload")
    client = fake_client()
    uploader = MTProtoUploader(mtproto_settings(), client=client)
    await uploader.start(expected_bot_id=123)
    started, stopped, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()  # A blocked network operation.
        finally:
            stopped.set()

    client.send_file.side_effect = send
    task = asyncio.create_task(uploader.send_document(
        chat_id=456, path=path, filename="safe.bin", caption="", cancel_event=cancelled,
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    cancelled.set()
    with pytest.raises(JobCancelled):
        await asyncio.wait_for(task, timeout=1)
    assert stopped.is_set()
    await uploader.close()


async def test_outer_timeout_also_stops_upload(tmp_path):
    path = tmp_path / "file.bin"
    path.write_bytes(b"payload")
    client = fake_client()
    uploader = MTProtoUploader(mtproto_settings(), client=client)
    await uploader.start(expected_bot_id=123)
    stopped = asyncio.Event()

    async def send(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    client.send_file.side_effect = send
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(uploader.send_document(
            chat_id=456, path=path, filename="safe.bin", caption="", cancel_event=asyncio.Event(),
        ), timeout=0.05)
    assert stopped.is_set()
    await uploader.close()


async def test_flood_wait_is_reported_without_duplicate_retries(tmp_path):
    path = tmp_path / "file.bin"
    path.write_bytes(b"payload")
    client = fake_client()
    client.send_file.side_effect = FloodWaitError(request=None, capture=17)
    uploader = MTProtoUploader(mtproto_settings(), client=client)
    await uploader.start(expected_bot_id=123)
    with pytest.raises(TelegramUploadError, match="MTProto flood wait") as error:
        await uploader.send_document(
            chat_id=456, path=path, filename="safe.bin", caption="", cancel_event=asyncio.Event(),
        )
    assert "17 seconds" in error.value.user_message
    client.send_file.assert_awaited_once()
    await uploader.close()


async def test_large_transfer_uses_mtproto_not_cloud_and_cleans_disk(tmp_path):
    uploader = SimpleNamespace(ensure_chat=AsyncMock(), send_document=AsyncMock())
    bot, source = FakeBot(), FakeGofile()
    # Large metadata + a small fake payload avoids producing a multi-GB artifact.
    job = make_job(make_file(size=500 * 1024 * 1024))
    await TransferService(
        bot, source, max_file_bytes=LARGE_MAX_FILE_BYTES, mtproto_uploader=uploader,
        download_dir=str(tmp_path),
    ).run(job)
    uploader.send_document.assert_awaited_once()
    assert not bot.sent
    assert source.downloads == ["doc.pdf"]
    assert not list(tmp_path.iterdir())
    assert any("✅" in edit for edit in bot.edits)


async def test_oversized_file_is_not_sent_to_either_transport():
    uploader = SimpleNamespace(ensure_chat=AsyncMock(), send_document=AsyncMock())
    bot, source = FakeBot(), FakeGofile()
    await TransferService(
        bot, source, max_file_bytes=LARGE_MAX_FILE_BYTES, mtproto_uploader=uploader,
    ).run(make_job(make_file(size=LARGE_MAX_FILE_BYTES + 1)))
    assert not source.downloads
    assert not bot.sent
    uploader.send_document.assert_not_awaited()


async def test_startup_failure_closes_resources(monkeypatch):
    application = SimpleNamespace(
        initialize=AsyncMock(), shutdown=AsyncMock(),
        updater=SimpleNamespace(running=False), running=False,
    )
    close = AsyncMock()
    monkeypatch.setattr(app_module, "build_application", lambda _: application)
    monkeypatch.setattr(app_module, "_startup", AsyncMock(side_effect=RuntimeError("bad login")))
    monkeypatch.setattr(app_module, "_shutdown", close)
    with pytest.raises(RuntimeError, match="bad login"):
        await app_module.run_async(mtproto_settings())
    close.assert_awaited_once_with(application)
    application.shutdown.assert_awaited_once()


async def test_uploader_itself_rejects_oversized_file_without_reading_it():
    uploader = MTProtoUploader(mtproto_settings())
    path = SimpleNamespace(stat=lambda: SimpleNamespace(st_size=LARGE_MAX_FILE_BYTES + 1))
    with pytest.raises(FileTooLargeError):
        await uploader.send_document(
            chat_id=456, path=path, filename="big.bin", caption="", cancel_event=asyncio.Event(),
        )


async def test_cancel_before_upload_does_not_touch_file(tmp_path):
    uploader = MTProtoUploader(mtproto_settings())
    event = asyncio.Event()
    event.set()
    with pytest.raises(JobCancelled):
        await uploader.send_document(
            chat_id=456, path=tmp_path / "missing", filename="missing", caption="", cancel_event=event,
        )
