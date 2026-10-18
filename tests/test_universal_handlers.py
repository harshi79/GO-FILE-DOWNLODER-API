"""Universal handler tests: /download, pasted links, /supported."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gofile_bot.downloaders.base import Downloader, DownloaderRegistry
from gofile_bot.gofile.models import RemoteFile
from gofile_bot.services.jobs import JobStore
from gofile_bot.telegram import handlers


class FakeProvider(Downloader):
    name = "fakehost"
    label = "FakeHost"
    description = "Fake file host"
    examples = ("https://fakehost.example/f/abc",)

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def matches(self, url: str) -> bool:
        return "fakehost.example" in url

    async def resolve(self, url, password=None):
        self.resolved.append(url)
        return RemoteFile(
            id="f1", name="thing.zip", size=2048,
            mimetype="application/zip", link="https://fakehost.example/dl/f1",
        )

    async def download(self, file, dest, **kw):  # pragma: no cover - not used
        return dest


def make_context(provider=None):
    provider = provider or FakeProvider()
    registry = DownloaderRegistry([provider])
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_document=AsyncMock())
    application = SimpleNamespace(
        bot=bot,
        bot_data={
            "jobs": JobStore(),
            "downloaders": registry,
            "transfer": SimpleNamespace(run=AsyncMock()),
        },
    )
    return SimpleNamespace(application=application), provider


def make_update(text: str, user_id: int = 7):
    message = SimpleNamespace(
        text=text,
        chat_id=50,
        message_id=99,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        reply_html=AsyncMock(),
    )
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=user_id))


async def test_download_cmd_auto_detects_provider():
    context, provider = make_context()
    update = make_update("/download https://fakehost.example/f/abc")

    await handlers.download_cmd(update, context)

    assert provider.resolved == ["https://fakehost.example/f/abc"]
    jobs = context.application.bot_data["jobs"]
    assert len(jobs) == 1
    job = next(iter(jobs._jobs.values()))
    assert job.provider == "fakehost"
    assert job.source_url == "https://fakehost.example/f/abc"
    final_text = context.application.bot.edit_message_text.await_args.kwargs["text"]
    assert "thing.zip" in final_text
    assert "FakeHost" in final_text


async def test_download_cmd_usage_on_missing_url():
    context, _provider = make_context()
    update = make_update("/download")
    await handlers.download_cmd(update, context)
    reply = update.message.reply_text.await_args.args[0]
    assert "/download <url>" in reply


async def test_download_cmd_unsupported_link():
    context, _provider = make_context()
    update = make_update("/download https://unknown.example/page")
    await handlers.download_cmd(update, context)
    reply = update.message.reply_text.await_args.args[0]
    assert "don't know how to download" in reply


async def test_auto_link_starts_flow_without_command():
    context, provider = make_context()
    update = make_update("hey grab this https://fakehost.example/f/abc please")

    await handlers.auto_link(update, context)

    assert provider.resolved == ["https://fakehost.example/f/abc"]


async def test_auto_link_ignores_plain_text():
    context, provider = make_context()
    update = make_update("hello, no links here")
    await handlers.auto_link(update, context)
    assert provider.resolved == []
    update.message.reply_text.assert_not_awaited()


async def test_supported_lists_providers():
    context, _provider = make_context()
    update = make_update("/supported")
    await handlers.supported_cmd(update, context)
    text = update.message.reply_html.await_args.args[0]
    assert "FakeHost" in text
    assert "fakehost.example" in text


async def test_ping():
    context, _provider = make_context()
    update = make_update("/ping")
    await handlers.ping_cmd(update, context)
    assert "Pong" in update.message.reply_text.await_args.args[0]


def test_extract_first_url():
    assert (
        handlers.extract_first_url("see https://example.com/a.zip now")
        == "https://example.com/a.zip"
    )
    assert (
        handlers.extract_first_url("(https://example.com/a.zip)")
        == "https://example.com/a.zip"
    )
    assert (
        handlers.extract_first_url("gofile.io/d/AbCdEf12")
        == "https://gofile.io/d/AbCdEf12"
    )
    assert handlers.extract_first_url("nothing here") is None


def test_looks_like_supported_link():
    registry = DownloaderRegistry([FakeProvider()])
    assert handlers.looks_like_supported_link(
        "https://fakehost.example/f/abc", registry
    )
    assert not handlers.looks_like_supported_link(
        "https://other.example/page", registry
    )

    from gofile_bot.downloaders.direct import DirectDownloader

    direct_registry = DownloaderRegistry([DirectDownloader()])
    # Generic links only auto-start when they look like files.
    assert handlers.looks_like_supported_link(
        "https://example.com/big.zip", direct_registry
    )
    assert not handlers.looks_like_supported_link(
        "https://example.com/some/article", direct_registry
    )


async def test_transfer_dispatches_to_job_provider(tmp_path):
    """TransferService must call the provider named on the job."""
    from gofile_bot.downloaders.base import Downloader as Base
    from gofile_bot.services.transfer import TransferService

    downloaded = []

    class Provider(Base):
        name = "fakehost"
        label = "FakeHost"

        def matches(self, url):
            return True

        async def resolve(self, url, password=None):  # pragma: no cover
            raise NotImplementedError

        async def download(
            self, file, dest, *, max_bytes, progress=None,
            cancel_event=None, options=None,
        ):
            downloaded.append(file.name)
            dest.write_bytes(b"data")
            if progress:
                await progress(4, 4)
            return dest

    class FakeBot:
        def __init__(self):
            self.edits = []
            self.sent = []

        async def edit_message_text(self, **kw):
            self.edits.append(kw.get("text", ""))

        async def send_document(self, **kw):
            self.sent.append(kw.get("filename"))

    bot = FakeBot()
    registry = DownloaderRegistry([Provider()])
    store = JobStore()
    job = store.create(
        user_id=1, chat_id=2, status_message_id=3,
        provider="fakehost", source_url="https://fakehost.example/f/abc",
    )
    job.content = RemoteFile(
        id="f1", name="thing.zip", size=4,
        mimetype="application/zip", link="https://fakehost.example/dl/f1",
    )
    job.running = True

    await TransferService(bot, registry).run(job)

    assert downloaded == ["thing.zip"]
    assert bot.sent == ["thing.zip"]
    assert not job.running


async def test_transfer_global_concurrency_cap():
    """At most N transfers run at once (512 MB protection)."""
    from gofile_bot.downloaders.base import Downloader as Base
    from gofile_bot.services.transfer import TransferService

    gate = asyncio.Event()
    state = {"active": 0, "peak": 0}

    class SlowProvider(Base):
        name = "slow"
        label = "Slow"

        def matches(self, url):
            return True

        async def resolve(self, url, password=None):  # pragma: no cover
            raise NotImplementedError

        async def download(
            self, file, dest, *, max_bytes, progress=None,
            cancel_event=None, options=None,
        ):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await gate.wait()
            state["active"] -= 1
            dest.write_bytes(b"x")
            return dest

    class FakeBot:
        async def edit_message_text(self, **kw):
            return None

        async def send_document(self, **kw):
            return None

    registry = DownloaderRegistry([SlowProvider()])
    service = TransferService(FakeBot(), registry, max_concurrent=2)
    store = JobStore()
    jobs = []
    for i in range(4):
        job = store.create(
            user_id=i, chat_id=2, status_message_id=3, provider="slow"
        )
        job.content = RemoteFile(
            id=f"f{i}", name=f"f{i}.bin", size=1,
            mimetype="application/octet-stream", link="https://x.example/f",
        )
        job.running = True
        jobs.append(job)

    tasks = [asyncio.create_task(service.run(job)) for job in jobs]
    await asyncio.sleep(0.2)
    assert state["peak"] == 2  # only 2 in flight, others queued
    gate.set()
    await asyncio.gather(*tasks)
    assert state["peak"] == 2
    assert all(not job.running for job in jobs)
