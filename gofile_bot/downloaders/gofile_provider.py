"""GoFile provider — thin adapter over the existing GoFile client.

The original bot was GoFile-only; this adapter lets that battle-tested client
sit behind the generic :class:`Downloader` interface without behaviour change
(including its ``*.gofile.io`` download-host allowlist).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gofile_bot.downloaders.base import Downloader, ProgressCallback, host_matches
from gofile_bot.gofile.client import GoFileClient
from gofile_bot.gofile.models import Content, RemoteFile
from gofile_bot.gofile.urls import parse_share

GOFILE_HOSTS = ("gofile.io",)


class GoFileDownloader(Downloader):
    name = "gofile"
    label = "GoFile"
    description = "GoFile share links (files & folders, passwords supported)"
    examples = ("https://gofile.io/d/AbCdEf12",)

    def __init__(self, client: GoFileClient) -> None:
        self._client = client

    def matches(self, url: str) -> bool:
        return host_matches(url, GOFILE_HOSTS)

    async def resolve(self, url: str, password: str | None = None) -> Content:
        content_id = parse_share(url)
        return await self._client.fetch_content(content_id, password=password)

    async def download(
        self,
        file: RemoteFile,
        dest: Path,
        *,
        max_bytes: int,
        progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        options: dict | None = None,
    ) -> Path:
        await self._client.download_file(
            file,
            dest,
            max_bytes=max_bytes,
            progress=progress,
            cancel_event=cancel_event,
        )
        return dest
