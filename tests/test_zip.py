"""Real isolated ZIP worker tests, including malicious archives."""

import asyncio
import stat
import struct
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gofile_bot.config import MAX_ZIP_BYTES, MAX_ZIP_FILES
from gofile_bot.errors import ZipSafetyError
from gofile_bot.services.transfer import TransferService
from gofile_bot.services.zip_extract import extract_safely
from gofile_bot.telegram import handlers
from tests.test_transfer import FakeBot, FakeGofile, make_file, make_job
from tests.test_universal_handlers import make_context, make_update


def make_zip(path, names=("one.txt", "nested/two.txt")):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in names:
            z.writestr(name, b"hello")
    return path


async def test_real_worker_extracts_files_not_nested_archives(tmp_path):
    source = make_zip(tmp_path / "archive.zip", ("a.txt", "nested/archive.zip"))
    paths = await extract_safely(source, tmp_path / "output", asyncio.Event())
    assert [p.relative_to(tmp_path / "output").as_posix() for p in paths] == [
        "a.txt",
        "nested/archive.zip",
    ]
    assert all(p.read_bytes() == b"hello" for p in paths)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:\\windows.txt",
        "..\\escape",
        "a/../escape",
        "a//b",
        "a/./b",
    ],
)
async def test_worker_rejects_zip_slip_paths(tmp_path, name):
    source = make_zip(tmp_path / "archive.zip", (name,))
    with pytest.raises(ZipSafetyError):
        await extract_safely(source, tmp_path / "output", asyncio.Event())
    assert not (tmp_path.parent / "escape").exists()


async def test_worker_rejects_symlinks(tmp_path):
    source = tmp_path / "archive.zip"
    with zipfile.ZipFile(source, "w") as z:
        info = zipfile.ZipInfo("symlink")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "../../outside")
    with pytest.raises(ZipSafetyError):
        await extract_safely(source, tmp_path / "output", asyncio.Event())


async def test_worker_rejects_duplicate_casefold_paths(tmp_path):
    source = make_zip(tmp_path / "archive.zip", ("A.txt", "a.txt"))
    with pytest.raises(ZipSafetyError):
        await extract_safely(source, tmp_path / "output", asyncio.Event())


async def test_worker_rejects_too_many_files(tmp_path):
    source = make_zip(
        tmp_path / "archive.zip", tuple(f"f{i}" for i in range(MAX_ZIP_FILES + 1))
    )
    with pytest.raises(ZipSafetyError):
        await extract_safely(source, tmp_path / "output", asyncio.Event())


async def test_worker_rejects_directory_bomb_before_zipinfo_allocation(tmp_path):
    source = make_zip(tmp_path / "archive.zip")
    body = bytearray(source.read_bytes())
    eocd = body.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", body, eocd + 8, 65535, 65535)
    source.write_bytes(body)
    with pytest.raises(ZipSafetyError):
        await extract_safely(source, tmp_path / "output", asyncio.Event())


async def test_small_compressed_zip_cannot_expand_over_20_mb(tmp_path):
    source = tmp_path / "bomb.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as z:
        with z.open("large.txt", "w") as out:
            block = b"x" * 65536
            remaining = MAX_ZIP_BYTES + 1
            while remaining:
                part = block[:remaining]
                out.write(part)
                remaining -= len(part)
    assert source.stat().st_size < 100000
    with pytest.raises(ZipSafetyError):
        await extract_safely(source, tmp_path / "output", asyncio.Event())
    assert not (tmp_path / "output" / "large.txt").exists()


async def test_exactly_20_mb_expanded_is_allowed(tmp_path):
    source = tmp_path / "archive.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as z:
        with z.open("large.txt", "w") as out:
            remaining = MAX_ZIP_BYTES
            while remaining:
                block = b"x" * min(65536, remaining)
                out.write(block)
                remaining -= len(block)
    paths = await extract_safely(source, tmp_path / "output", asyncio.Event())
    assert paths[0].stat().st_size == MAX_ZIP_BYTES


async def test_rejects_oversized_input_before_spawning(tmp_path, monkeypatch):
    source = tmp_path / "large.zip"
    with source.open("wb") as out:
        out.truncate(MAX_ZIP_BYTES + 1)  # Sparse fixture: no large RAM buffer.
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ZipSafetyError):
        await extract_safely(source, tmp_path / "output", asyncio.Event())
    spawn.assert_not_awaited()


async def test_corrupt_zip_is_rejected(tmp_path):
    source = tmp_path / "not.zip"
    source.write_bytes(b"not a zip")
    with pytest.raises(ZipSafetyError):
        await extract_safely(source, tmp_path / "output", asyncio.Event())


async def test_worker_deadline_kills_and_reaps_process(tmp_path, monkeypatch):
    from gofile_bot.services import zip_extract

    source = make_zip(tmp_path / "archive.zip")
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def tracked(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(zip_extract, "ZIP_TIMEOUT", 0.000001)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    with pytest.raises(ZipSafetyError, match="time limit"):
        await extract_safely(source, tmp_path / "output", asyncio.Event())
    assert processes and processes[0].returncode is not None


async def test_extract_job_sends_files_and_cleans_all_temp_files(tmp_path):
    source = make_zip(tmp_path / "archive.zip")
    downloads = tmp_path / "jobs"
    bot = FakeBot()
    job = make_job(make_file(name="archive.zip", size=source.stat().st_size))
    job.action = "extract"
    await TransferService(
        bot, FakeGofile(payload=source.read_bytes()), download_dir=str(downloads)
    ).run(job)
    assert [name for name, _ in bot.sent] == ["one.txt", "two.txt"]
    assert any("Extracted and sent 2" in text for text in bot.edits)
    assert not list(downloads.iterdir())


async def test_extraction_failure_cleans_temp_files(tmp_path):
    bot = FakeBot()
    job = make_job(make_file(name="bad.zip", size=3))
    job.action = "extract"
    await TransferService(
        bot, FakeGofile(payload=b"bad"), download_dir=str(tmp_path)
    ).run(job)
    assert not bot.sent
    assert not list(tmp_path.iterdir())
    assert any("ZIP rejected" in text for text in bot.edits)


async def test_uploaded_zip_shows_extract_only_button():
    context, _ = make_context()
    update = make_update("")
    update.message.document = SimpleNamespace(
        file_id="telegram-file",
        file_name="archive.zip",
        file_size=100,
        mime_type="application/zip",
    )
    await handlers.uploaded_zip(update, context)
    job = next(iter(context.application.bot_data["jobs"]._jobs.values()))
    assert job.provider == "telegram" and job.action == "extract"
    keyboard = context.application.bot.edit_message_text.await_args.kwargs[
        "reply_markup"
    ]
    assert keyboard.inline_keyboard[0][0].callback_data == f"zx:{job.id}"
    assert len(keyboard.inline_keyboard) == 1


async def test_oversized_uploaded_zip_never_creates_job():
    context, _ = make_context()
    update = make_update("")
    update.message.document = SimpleNamespace(
        file_id="file",
        file_name="archive.zip",
        file_size=MAX_ZIP_BYTES + 1,
        mime_type="application/zip",
    )
    await handlers.uploaded_zip(update, context)
    assert not context.application.bot_data["jobs"]._jobs
    assert "20 MB" in update.message.reply_text.await_args.args[0]
