"""Fetch uploaded Telegram ZIPs without PTB's whole-file bytearray buffer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import unquote, urlparse

from gofile_bot.config import DOWNLOAD_CHUNK_SIZE, MAX_ZIP_BYTES
from gofile_bot.downloaders.base import HTTPDownloader, host_of
from gofile_bot.downloaders.net import stream_to_file
from gofile_bot.errors import (
    FileTooLargeError,
    JobCancelled,
    RemoteDownloadError,
    ResolveError,
)
from gofile_bot.services.resources import disk_io


class TelegramZipDownloader(HTTPDownloader):
    name = "telegram"
    label = "Uploaded ZIP"
    description = "Send a ZIP document (max 20 MB archive and extracted output)"

    def __init__(self, bot, *, local_api_url: str | None = None):
        super().__init__()
        self._bot = bot
        self._local_api_url = local_api_url

    def matches(self, url: str) -> bool:
        return False  # Only trusted file IDs from document updates enter here.

    async def resolve(self, url: str, password=None):
        raise ResolveError("Send a ZIP document to the bot instead")

    async def download(
        self, file, dest, *, max_bytes, progress=None, cancel_event=None, options=None
    ):
        limit = min(max_bytes, MAX_ZIP_BYTES)
        if file.size > limit:
            raise FileTooLargeError(size=file.size, limit=limit)
        remote = await self._bot.get_file(file.id)
        if remote.file_size and remote.file_size > limit:
            raise FileTooLargeError(size=remote.file_size, limit=limit)
        path = str(remote.file_path or "")
        if not path:
            raise ResolveError("Telegram returned no file path")
        if self._local_api_url and (path.startswith("/") or path.startswith("file://")):
            # Path supplied by the operator-owned Local API, never user input.
            source = Path(
                unquote(urlparse(path).path) if path.startswith("file://") else path
            )
            part = dest.with_suffix(".part")
            written = 0
            try:
                with source.open("rb") as src, part.open("wb") as dst:
                    while chunk := await disk_io(src.read, DOWNLOAD_CHUNK_SIZE):
                        if cancel_event and cancel_event.is_set():
                            raise JobCancelled()
                        written += len(chunk)
                        if written > limit:
                            raise FileTooLargeError(size=written, limit=limit)
                        await disk_io(dst.write, chunk)
                        if progress:
                            await progress(written, remote.file_size)
                        await asyncio.sleep(0)
                part.replace(dest)
                return dest
            finally:
                part.unlink(missing_ok=True)
        try:
            return await stream_to_file(
                self.session,
                path,
                dest,
                max_bytes=limit,
                progress=progress,
                cancel_event=cancel_event,
                expected_size=remote.file_size,
                allowed_hosts=(
                    host_of(self._local_api_url or "https://api.telegram.org"),
                ),
            )
        except RemoteDownloadError as exc:
            # Telegram file URLs contain the bot token. Don't copy one into
            # transfer logs or user-facing errors.
            raise ResolveError("Telegram ZIP download failed") from exc
