"""Large-file configuration and upload paths; no giant test artifacts needed."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InputFile

from gofile_bot.app import build_application
from gofile_bot.config import LOCAL_MAX_FILE_BYTES, MAX_FILE_BYTES, Settings, load_settings
from gofile_bot.services.transfer import TransferService, cleanup_stale_tmpdirs, make_job_tmpdir
from tests.test_transfer import FakeBot, FakeGofile, make_file, make_job


def test_cloud_limit_is_not_silently_raised():
    settings = Settings(bot_token="t")
    assert settings.max_file_bytes == MAX_FILE_BYTES
    assert settings.job_timeout == 1800


def test_local_settings_from_environment(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_API_URL", "http://telegram-bot-api:8081/")
    monkeypatch.setenv("DOWNLOAD_DIR", "/downloads")
    settings = load_settings(env_file=None)
    assert settings.max_file_bytes == LOCAL_MAX_FILE_BYTES
    assert settings.telegram_api_url == "http://telegram-bot-api:8081"
    assert settings.download_dir == "/downloads"
    assert settings.job_timeout == 6 * 3600


@pytest.mark.parametrize("url", [
    "ftp://server", "http://user:pass@server", "http://server/bot123",
    "http://server?key=secret", "http://server#fragment", "not-a-url",
])
def test_invalid_local_api_origin(url):
    with pytest.raises(ValueError, match="TELEGRAM_API_URL"):
        Settings(bot_token="t", telegram_api_url=url)


def test_application_uses_local_api_for_all_telegram_requests():
    app = build_application(Settings(
        bot_token="123:token", telegram_api_url="http://telegram-bot-api:8081",
    ))
    assert app.bot.local_mode is True
    assert app.bot.base_url == "http://telegram-bot-api:8081/bot123:token"
    assert app.bot.base_file_url == "http://telegram-bot-api:8081/file/bot123:token"


async def test_cloud_upload_does_not_eagerly_read_entire_file(tmp_path):
    path = tmp_path / "small.bin"
    path.write_bytes(b"payload")
    bot = SimpleNamespace(send_document=AsyncMock())
    service = TransferService(bot, FakeGofile())
    file = make_file()
    await service._send_document(make_job(file), file, path)
    document = bot.send_document.await_args.kwargs["document"]
    assert isinstance(document, InputFile)
    assert not isinstance(document.input_file_content, bytes)


async def test_local_upload_sends_shared_path_and_cleans_up(tmp_path):
    seen = []

    class LocalBot(FakeBot):
        async def send_document(self, **kwargs):
            document = kwargs["document"]
            assert isinstance(document, Path)
            assert document.is_absolute()
            assert document.read_bytes() == b"data"
            seen.append(document)

    gofile = FakeGofile()
    # Metadata exceeds cloud limit; fake payload avoids creating a large fixture.
    job = make_job(make_file(size=60 * 1024 * 1024))
    await TransferService(
        LocalBot(), gofile, local_mode=True, max_file_bytes=LOCAL_MAX_FILE_BYTES,
        download_dir=str(tmp_path),
    ).run(job)
    assert gofile.downloads == ["doc.pdf"]
    assert seen and not seen[0].exists()
    assert not list(tmp_path.iterdir())


async def test_cloud_rejects_known_oversized_file_before_fetching():
    bot, gofile = FakeBot(), FakeGofile()
    job = make_job(make_file(size=MAX_FILE_BYTES + 1))
    await TransferService(bot, gofile).run(job)
    assert not gofile.downloads
    assert not bot.sent
    assert any("too big" in edit for edit in bot.edits)


async def test_local_folder_can_select_a_file_larger_than_old_folder_budget():
    from gofile_bot.gofile.models import RemoteFolder

    service = TransferService(FakeBot(), FakeGofile(), max_file_bytes=LOCAL_MAX_FILE_BYTES)
    large = make_file(size=400 * 1024 * 1024)
    assert service._select_files(RemoteFolder(id="folder", name="folder", children=(large,))) == [large]


async def test_low_disk_space_is_reported_without_download(monkeypatch):
    monkeypatch.setattr("gofile_bot.services.transfer.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    bot, gofile = FakeBot(), FakeGofile()
    await TransferService(bot, gofile).run(make_job(make_file()))
    assert not gofile.downloads
    assert any("disk space" in edit for edit in bot.edits)


def test_custom_download_directory_cleanup(tmp_path):
    directory = make_job_tmpdir(str(tmp_path))
    assert directory.parent == tmp_path
    assert cleanup_stale_tmpdirs(-1, str(tmp_path)) == 1
    assert not directory.exists()
