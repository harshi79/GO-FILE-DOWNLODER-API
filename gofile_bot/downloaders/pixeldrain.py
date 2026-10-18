"""Pixeldrain provider, built on Pixeldrain's official public API.

* File info:  ``GET https://pixeldrain.com/api/file/{id}/info``
* List info:  ``GET https://pixeldrain.com/api/list/{id}``
* Download:   ``GET https://pixeldrain.com/api/file/{id}?download``

No API key is required for downloading public files.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path

import aiohttp

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback, host_matches
from gofile_bot.downloaders.net import stream_to_file
from gofile_bot.errors import ContentNotFoundError, ResolveError
from gofile_bot.gofile.models import Content, RemoteFile, RemoteFolder

PIXELDRAIN_HOSTS = ("pixeldrain.com",)
API_BASE = "https://pixeldrain.com/api"

_LINK_RE = re.compile(r"pixeldrain\.com/(u|l)/([A-Za-z0-9]{4,})", re.IGNORECASE)


def extract_pixeldrain_ref(url: str) -> tuple[str, str] | None:
    """Return ``("file"|"list", id)`` for a Pixeldrain URL, else None."""
    match = _LINK_RE.search(url or "")
    if not match:
        return None
    kind = "file" if match.group(1).lower() == "u" else "list"
    return kind, match.group(2)


def file_download_url(file_id: str) -> str:
    return f"{API_BASE}/file/{file_id}?download"


def parse_file_info(data: dict) -> RemoteFile:
    file_id = str(data.get("id") or "")
    name = str(data.get("name") or "file")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    mimetype = str(data.get("mime_type") or "") or (
        mimetypes.guess_type(name)[0] or "application/octet-stream"
    )
    return RemoteFile(
        id=file_id,
        name=name,
        size=size,
        mimetype=mimetype,
        link=file_download_url(file_id),
        webpage_url=f"https://pixeldrain.com/u/{file_id}",
    )


def parse_list_info(data: dict) -> RemoteFolder:
    files = tuple(
        parse_file_info(entry)
        for entry in (data.get("files") or [])
        if isinstance(entry, dict)
    )
    return RemoteFolder(
        id=str(data.get("id") or ""),
        name=str(data.get("title") or "Pixeldrain list"),
        children=files,
    )


class PixeldrainDownloader(HTTPDownloader):
    name = "pixeldrain"
    label = "Pixeldrain"
    description = "Pixeldrain files and lists"
    examples = ("https://pixeldrain.com/u/abc12345", "https://pixeldrain.com/l/abc12345")

    def matches(self, url: str) -> bool:
        return host_matches(url, PIXELDRAIN_HOSTS) and extract_pixeldrain_ref(url) is not None

    async def _get_json(self, url: str) -> dict:
        try:
            async with self.session.get(url) as resp:
                if resp.status == 404:
                    raise ContentNotFoundError(f"HTTP 404 for {url}")
                payload = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ResolveError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise ResolveError(f"unexpected Pixeldrain response for {url}")
        if payload.get("success") is False:
            value = str(payload.get("value") or "")
            if "not_found" in value or "not exist" in value.lower():
                raise ContentNotFoundError(value)
            raise ResolveError(
                value or "Pixeldrain refused the request",
                user_message=(
                    "⚠️ Pixeldrain refused that request (the file may be "
                    "rate-limited or removed). Try again later."
                ),
            )
        return payload

    async def resolve(self, url: str, password: str | None = None) -> Content:
        ref = extract_pixeldrain_ref(url)
        if ref is None:
            raise ContentNotFoundError(f"not a Pixeldrain file/list link: {url!r}")
        kind, ref_id = ref
        if kind == "file":
            return parse_file_info(await self._get_json(f"{API_BASE}/file/{ref_id}/info"))
        return parse_list_info(await self._get_json(f"{API_BASE}/list/{ref_id}"))

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
            file.link or file_download_url(file.id),
            dest,
            max_bytes=max_bytes,
            progress=progress,
            cancel_event=cancel_event,
            allowed_hosts=PIXELDRAIN_HOSTS,
            expected_size=file.size or None,
        )
