"""Backpressure, cancellation and absolute byte-limit regressions."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses

from gofile_bot.app import build_application
from gofile_bot.config import (
    HARD_MAX_FILE_BYTES,
    MAX_METADATA_BYTES,
    MAX_UPDATE_QUEUE,
    Settings,
)
from gofile_bot.downloaders.net import stream_to_file
from gofile_bot.errors import (
    FileTooLargeError,
    JobBusyError,
    ResolveError,
    ServiceBusyError,
)
from gofile_bot.metadata import read_json
from gofile_bot.services.jobs import JobStore
from gofile_bot.services.resources import MetadataTasks
from gofile_bot.services.transfer import TransferService
from tests.test_transfer import FakeBot, FakeGofile, make_file, make_job


def test_application_has_bounded_queue_and_no_unbounded_update_task_fanout():
    application = build_application(Settings(bot_token="123:TEST"))
    assert application.update_queue.maxsize == MAX_UPDATE_QUEUE
    assert application.concurrent_updates == 1


def test_job_store_is_bounded_and_preserves_active_jobs():
    store = JobStore(max_jobs=3)
    active = store.create(user_id=1, chat_id=1, status_message_id=1)
    active.running = True
    for i in range(1000):
        store.create(user_id=i + 2, chat_id=1, status_message_id=1)
    assert len(store) == 3
    assert store.get(active.id) is active
    active.created_at = 0
    store.purge_expired()
    assert store.get(active.id) is active  # Long-running jobs remain cancellable.


def test_user_cannot_fill_all_card_slots():
    store = JobStore()
    for _ in range(1000):
        store.create(user_id=1, chat_id=1, status_message_id=1)
    assert len(store) == 3


async def test_metadata_storm_creates_only_two_tasks():
    limiter = MetadataTasks(capacity=2)
    gate = asyncio.Event()
    tasks = []
    spawn = lambda coro: tasks.append(asyncio.create_task(coro))
    for i in range(1000):
        if i < 2:
            limiter.submit(i, gate.wait, spawn)
        else:
            with pytest.raises(ServiceBusyError):
                limiter.submit(i, gate.wait, spawn)
    assert len(tasks) == 2
    with pytest.raises(JobBusyError):
        limiter.submit(0, gate.wait, spawn)
    gate.set()
    await asyncio.gather(*tasks)
    assert not limiter.users


async def test_full_transfer_queue_rejects_without_allocating_tasks(tmp_path):
    service = TransferService(
        FakeBot(), FakeGofile(), max_pending=2, download_dir=str(tmp_path)
    )
    tasks = []
    for i in range(1000):
        job = make_job(make_file())
        job.user_id = i
        if i < 2:
            service.submit(job, lambda coro: tasks.append(asyncio.create_task(coro)))
        else:
            with pytest.raises(ServiceBusyError):
                service.submit(
                    job, lambda coro: tasks.append(asyncio.create_task(coro))
                )
    assert len(tasks) == 2
    await asyncio.gather(*tasks)
    assert service.snapshot()["active"] == service.snapshot()["queued"] == 0
    assert not list(tmp_path.iterdir())


async def test_queued_cancel_is_immediate_and_does_not_take_a_disk_slot(tmp_path):
    gate = asyncio.Event()
    started = asyncio.Event()

    class Slow(FakeGofile):
        async def download_file(self, *args, **kwargs):
            started.set()
            await gate.wait()
            return await super().download_file(*args, **kwargs)

    service = TransferService(FakeBot(), Slow(), download_dir=str(tmp_path))
    first = make_job(make_file())
    second = make_job(make_file())
    second.user_id = 2
    task1 = asyncio.create_task(service.run(first))
    await asyncio.wait_for(started.wait(), 1)
    task2 = asyncio.create_task(service.run(second))
    await asyncio.sleep(0.01)
    assert len(list(tmp_path.iterdir())) == 1
    second.cancel_event.set()
    await asyncio.wait_for(task2, 1)
    assert not second.running and first.running
    assert service.snapshot()["active"] == 1
    gate.set()
    await task1
    assert not list(tmp_path.iterdir())


async def test_running_cancel_interrupts_stalled_network_and_releases_slot(tmp_path):
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Slow(FakeGofile):
        async def download_file(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    service = TransferService(FakeBot(), Slow(), download_dir=str(tmp_path))
    job = make_job(make_file())
    task = asyncio.create_task(service.run(job))
    await asyncio.wait_for(started.wait(), 1)
    job.cancel_event.set()
    await asyncio.wait_for(task, 1)
    assert stopped.is_set()
    assert service.snapshot()["active"] == 0
    assert not list(tmp_path.iterdir())


async def test_hard_2_gb_limit_cannot_be_overridden_by_service_configuration():
    assert HARD_MAX_FILE_BYTES == 2_000_000_000
    source = FakeGofile()
    job = make_job(make_file(size=HARD_MAX_FILE_BYTES + 1))
    await TransferService(FakeBot(), source, max_file_bytes=10**12).run(job)
    assert not source.downloads
    assert job.cancelled


async def test_stream_aborts_unknown_size_at_hard_limit_and_removes_partial(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("gofile_bot.downloaders.net.HARD_MAX_FILE_BYTES", 5)
    with aioresponses() as responses:
        responses.get("https://example.com/file", body=b"123456")
        async with aiohttp.ClientSession() as session:
            with pytest.raises(FileTooLargeError):
                await stream_to_file(
                    session,
                    "https://example.com/file",
                    tmp_path / "file",
                    max_bytes=1000,
                )
    assert not list(tmp_path.iterdir())


async def test_metadata_response_is_bounded():
    with aioresponses() as responses:
        responses.get("https://example.com/info", body=b"x" * (MAX_METADATA_BYTES + 1))
        async with aiohttp.ClientSession() as session:
            async with session.get("https://example.com/info") as response:
                with pytest.raises(ResolveError, match="exceeds 1 MiB"):
                    await read_json(response)


def test_mtproto_peer_session_cache_is_bounded():
    from telethon.tl.types import User

    from gofile_bot.services.mtproto import BoundedMemorySession

    session = BoundedMemorySession()
    for i in range(1000):
        session.process_entities(
            [User(id=i + 1, access_hash=i + 20, first_name="user")]
        )
    assert len(session._entities) == 256
    assert session.get_input_entity(1000).user_id == 1000


async def test_rate_limited_progress_edits_do_not_retry_every_chunk(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    from telegram.error import RetryAfter

    from gofile_bot.services.status import StatusMessage

    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=RetryAfter(10)))
    status = StatusMessage(bot, 1, 1)
    await status.edit("one")
    for i in range(1000):
        await status.edit(str(i), force=True)
    assert bot.edit_message_text.await_count == 1


async def test_shutdown_cancels_work_and_stops_admission():
    service = TransferService(FakeBot(), FakeGofile())
    task_list = []
    job = make_job(make_file())
    service.submit(job, lambda coro: task_list.append(asyncio.create_task(coro)))
    service.begin_shutdown()
    await asyncio.gather(*task_list)
    assert job.cancelled and not job.running
    with pytest.raises(ServiceBusyError):
        service.submit(make_job(make_file()), asyncio.create_task)


async def test_metadata_shutdown_releases_slots_even_before_tasks_start():
    limiter = MetadataTasks()
    limiter.submit(1, asyncio.Event().wait, asyncio.create_task)
    await limiter.close()
    assert not limiter.users
    with pytest.raises(ServiceBusyError):
        limiter.submit(2, asyncio.Event().wait, asyncio.create_task)


def test_memory_backed_mount_detection_uses_longest_matching_mount(tmp_path):
    from gofile_bot.services.transfer import is_memory_backed

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 0 1:0 / / rw - ext4 disk rw\n"
        "2 1 0:1 / /tmp rw - tmpfs tmpfs rw\n"
        "3 1 1:0 / /tmp/disk rw - ext4 disk rw\n"
    )
    assert is_memory_backed(Path("/tmp/ram/job"), mountinfo)
    assert not is_memory_backed(Path("/tmp/disk/job"), mountinfo)


async def test_ram_backed_big_download_is_refused_before_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr("gofile_bot.services.transfer.is_memory_backed", lambda _: True)
    source, bot = FakeGofile(), FakeBot()
    job = make_job(make_file(size=30_000_000))
    await TransferService(bot, source, download_dir=str(tmp_path)).run(job)
    assert not source.downloads
    assert any("disk-backed" in text for text in bot.edits)
    assert not list(tmp_path.iterdir())
