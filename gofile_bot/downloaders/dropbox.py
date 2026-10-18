"""Dropbox provider for shared links.

A Dropbox share link becomes a direct download by setting ``dl=1`` (Dropbox's
own documented trick for forcing a download instead of the preview page). The
bot then streams from Dropbox's content hosts only. Shared *folders* come
down as the single ``.zip`` Dropbox itself offers for them.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback, host_matches
from gofile_bot.downloaders.direct import is_web_page, size_from_headers
from gofile_bot.downloaders.net import (
    filename_from_disposition,
    filename_from_url,
    probe_url,
    stream_to_file,
)
from gofile_bot.errors import ContentNotFoundError, ResolveError
from gofile_bot.gofile.models import Content, RemoteFile

DROPBOX_HOSTS = ("dropbox.com", "dropboxusercontent.com")


def is_dropbox_link(url: str) -> bool:
    if not host_matches(url, DROPBOX_HOSTS):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host == "dl.dropboxusercontent.com":
        return True
    return parsed.path.startswith(("/s/", "/scl/", "/sh/"))


def to_download_url(url: str) -> str:
    """Force-download form of a Dropbox share link (``dl=1``)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "dl.dropboxusercontent.com":
        return url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["dl"] = "1"
    return urlunparse(parsed._replace(query=urlencode(query)))


class DropboxDownloader(HTTPDownloader):
    name = "dropbox"
    label = "Dropbox"
    description = "Dropbox shared links (folders arrive as a .zip)"
    examples = ("https://www.dropbox.com/scl/fi/<id>/file.zip?rlkey=…&dl=0",)

    def matches(self, url: str) -> bool:
        return is_dropbox_link(url)

    async def resolve(self, url: str, password: str | None = None) -> Content:
        download_url = to_download_url(url)
        probe = await probe_url(
            self.session, download_url, allowed_hosts=DROPBOX_HOSTS
        )
        headers = probe.headers

        if probe.status == 404:
            raise ContentNotFoundError(f"Dropbox HTTP 404 for {url}")
        if probe.status >= 400:
            raise ResolveError(
                f"Dropbox HTTP {probe.status} for {url}",
                user_message=(
                    "⚠️ Dropbox refused that link. It may be deleted, or not "
                    "shared publicly (“anyone with the link”)."
                ),
            )
        if is_web_page(headers):
            raise ResolveError(
                f"Dropbox returned a page instead of a file: {url}",
                user_message=(
                    "⚠️ Dropbox didn't hand over a file for that link. It may "
                    "need a password or a Dropbox account."
                ),
            )

        name = (
            filename_from_disposition(str(headers.get("Content-Disposition", "")))
            or filename_from_url(url)
            or "dropbox-file"
        )
        return RemoteFile(
            id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            name=name,
            size=size_from_headers(headers),
            mimetype=mimetypes.guess_type(name)[0] or "application/octet-stream",
            link=download_url,
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
            allowed_hosts=DROPBOX_HOSTS,
            expected_size=file.size or None,
        )
