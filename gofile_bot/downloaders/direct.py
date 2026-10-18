"""Direct http(s) file-link downloader.

This is the fallback provider: it accepts any public http(s) URL that points
straight at a file (``.zip``, ``.mp4``, GitHub release assets, …). Because the
URL is fully user-controlled, every request — and every redirect hop — goes
through the SSRF guard in :mod:`gofile_bot.downloaders.net`: only
globally-routable addresses are ever contacted.

Web *pages* are rejected at resolve time (``text/html`` without an attachment
disposition) with a hint towards /supported.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback
from gofile_bot.downloaders.net import (
    Resolver,
    content_type_of,
    filename_from_disposition,
    filename_from_url,
    probe_url,
    stream_to_file,
    validate_public_url,
    with_extension_for_mimetype,
)
from gofile_bot.errors import ContentNotFoundError, ResolveError
from gofile_bot.gofile.models import Content, RemoteFile


def size_from_headers(headers) -> int:
    """Total size from Content-Range / Content-Length (0 when unknown)."""
    content_range = str(headers.get("Content-Range", ""))
    if "/" in content_range and content_range.rsplit("/", 1)[-1].isdigit():
        return int(content_range.rsplit("/", 1)[-1])
    content_length = str(headers.get("Content-Length", ""))
    return int(content_length) if content_length.isdigit() else 0


def is_web_page(headers) -> bool:
    """True when the response looks like an HTML page, not a file."""
    disposition = str(headers.get("Content-Disposition", "")).lower()
    if "attachment" in disposition:
        return False
    return content_type_of(headers) in ("text/html", "application/xhtml+xml")


class DirectDownloader(HTTPDownloader):
    name = "direct"
    label = "Direct link"
    description = "Any direct http(s) file link (.zip, .mp4, GitHub assets, …)"
    examples = ("https://example.com/files/archive.zip",)

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        super().__init__(session=session)
        self._resolver = resolver

    def matches(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)

    async def resolve(self, url: str, password: str | None = None) -> Content:
        await validate_public_url(url, resolver=self._resolver)
        probe = await probe_url(
            self.session, url, public_only=True, resolver=self._resolver
        )
        headers = probe.headers

        if probe.status == 404:
            raise ContentNotFoundError(f"HTTP 404 for {url}")
        if probe.status >= 400:
            raise ResolveError(
                f"HTTP {probe.status} for {url}",
                user_message=(
                    f"⚠️ That server answered HTTP {probe.status}. The file may "
                    "be gone or the link may need a login."
                ),
            )
        if is_web_page(headers):
            raise ResolveError(
                f"URL is a web page, not a file: {url}",
                user_message=(
                    "⚠️ That link opens a web page, not a direct file.\n"
                    "See /supported for the file hosts I understand."
                ),
            )

        mimetype = content_type_of(headers) or "application/octet-stream"
        name = (
            filename_from_disposition(str(headers.get("Content-Disposition", "")))
            or filename_from_url(probe.final_url)
            or filename_from_url(url)
            or "download"
        )
        name = with_extension_for_mimetype(name, mimetype)

        return RemoteFile(
            id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            name=name,
            size=size_from_headers(headers),
            mimetype=mimetype,
            link=url,
            webpage_url=url,
        )

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
        return await stream_to_file(
            self.session,
            file.link,
            dest,
            max_bytes=max_bytes,
            progress=progress,
            cancel_event=cancel_event,
            public_only=True,
            resolver=self._resolver,
            expected_size=file.size or None,
        )
