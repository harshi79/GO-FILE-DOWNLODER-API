"""Offline overload/ZIP smoke test under a 512 MiB process address-space limit.

Run: python -m scripts.resource_smoke
Not a live Telegram/bandwidth benchmark. No credentials or external requests.
"""

import resource

resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))

import asyncio
import json
import tempfile
import time
import zipfile
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from gofile_bot.app import build_application
from gofile_bot.config import MAX_ZIP_BYTES, Settings
from gofile_bot.downloaders.base import Downloader, DownloaderRegistry
from gofile_bot.errors import ServiceBusyError
from gofile_bot.gofile.models import RemoteFile
from gofile_bot.health import create_health_app
from gofile_bot.metadata import read_json
from gofile_bot.services.jobs import Job
from gofile_bot.services.resources import MetadataTasks, disk_io
from gofile_bot.services.transfer import TransferService


class Bot:
    sent = 0

    async def edit_message_text(self, **kwargs):
        await asyncio.sleep(0)

    async def send_document(self, **kwargs):
        self.sent += 1
        await asyncio.sleep(0.002)


class Source(Downloader):
    name = "fixture"

    def __init__(self, payload):
        self.payload = payload

    def matches(self, url):
        return True

    async def resolve(self, url, password=None):
        raise NotImplementedError

    async def download(self, file, dest, **kwargs):
        await disk_io(dest.write_bytes, self.payload)
        return dest


class MetadataResponse:
    headers = {}

    def __init__(self, data):
        self.data = data
        self.content = self

    async def iter_chunked(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset : offset + size]
            await asyncio.sleep(0)


async def main():
    # Import/wire the full MTProto-enabled app without network authorization.
    app = build_application(
        Settings(
            bot_token="123:TEST", enable_mtproto=True, api_id=123, api_hash="a" * 32
        )
    )
    tasks = []
    health_samples = []
    peaks = {"active": 0, "queued": 0}
    running = True
    health = TestClient(TestServer(create_health_app()))
    await health.start_server()
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        archive = root / "fixture.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            with z.open("exactly-20-MB.txt", "w") as out:
                remaining = MAX_ZIP_BYTES
                while remaining:
                    chunk = b"x" * min(65536, remaining)
                    out.write(chunk)
                    remaining -= len(chunk)
        source = Source(archive.read_bytes())
        bot = Bot()
        service = TransferService(
            bot, DownloaderRegistry([source]), download_dir=str(root / "jobs")
        )

        async def heartbeat():
            last = time.monotonic()
            while running:
                response = await health.get("/health")
                assert await response.text() == "OK"
                now = time.monotonic()
                health_samples.append(now - last)
                last = now
                state = service.snapshot()
                for key in peaks:
                    peaks[key] = max(peaks[key], state[key])
                await asyncio.sleep(0.01)

        pulse = asyncio.create_task(heartbeat())
        rejected = 0
        for i in range(2000):
            job = Job(
                id=str(i),
                user_id=i,
                chat_id=i,
                status_message_id=1,
                content_id="",
                provider="fixture",
            )
            job.content = RemoteFile(
                id="fixture",
                name="fixture.zip",
                size=len(source.payload),
                mimetype="application/zip",
                link="https://fixture.invalid/file",
            )
            job.action = "extract"
            try:
                service.submit(
                    job, lambda coro: tasks.append(asyncio.create_task(coro))
                )
            except ServiceBusyError:
                rejected += 1
        metadata = MetadataTasks()
        body = json.dumps({"value": "x" * 500000}).encode()
        metadata_rejected = 0

        async def lookup():
            await read_json(MetadataResponse(body))

        for i in range(1000):
            try:
                metadata.submit(
                    i, lookup, lambda coro: tasks.append(asyncio.create_task(coro))
                )
            except ServiceBusyError:
                metadata_rejected += 1
        await asyncio.gather(*tasks)
        running = False
        await pulse
        await health.close()
        assert bot.sent == 8
        assert not list((root / "jobs").iterdir())
        assert peaks["active"] <= 1
        assert rejected == 1992 and metadata_rejected == 998
        assert max(health_samples) < 0.5
        parent = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
        print(
            json.dumps(
                {
                    "process_address_space_limit_mib": 512,
                    "parent_peak_rss_mib": round(parent, 1),
                    "largest_zip_worker_peak_rss_mib": round(child, 1),
                    "conservative_combined_peak_mib": round(parent + child, 1),
                    "transfer_requests": 2000,
                    "accepted_transfers": 8,
                    "busy_rejections": rejected,
                    "metadata_requests": 1000,
                    "metadata_busy_rejections": metadata_rejected,
                    "zip_expanded_bytes_each": MAX_ZIP_BYTES,
                    "documents_sent_to_fake_transport": bot.sent,
                    "peak_active_transfers": peaks["active"],
                    "max_health_heartbeat_gap_ms": round(max(health_samples) * 1000, 1),
                    "live_telegram_upload_tested": False,
                },
                indent=2,
            )
        )
    # Close the HTTPX pools created by application assembly, without logging in.
    for request in app.bot._request:
        await request.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
