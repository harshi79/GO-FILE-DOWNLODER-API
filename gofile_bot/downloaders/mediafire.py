"""MediaFire provider for public file pages.

MediaFire has no public download API for share pages, so this reads the same
download button a logged-out visitor sees on the file page. Only the free,
public download link is used — no premium/wait-time bypassing: if MediaFire
doesn't offer the button, resolution fails with a friendly error.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote

import aiohttp

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback, host_matches
from gofile_bot.downloaders.net import parse_size_text, stream_to_file
from gofile_bot.errors import ContentNotFoundError, RemoteDownloadError, ResolveError
from gofile_bot.gofile.models import Content, RemoteFile

MEDIAFIRE_HOSTS = ("mediafire.com",)

_FILE_KEY_RE = re.compile(
    r"mediafire\.com/(?:file|file_premium|download|view)/([A-Za-z0-9]+)", re.IGNORECASE
)
_VIEW_PATH_RE = re.compile(r"(mediafire\.com/)view/", re.IGNORECASE)
_DOWNLOAD_URL_RE = re.compile(
    r"""href="(https?://download\d*\.mediafire\.com/[^"]+)\"""", re.IGNORECASE
)
_FILENAME_RE = re.compile(r'<div class="filename"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL)
_SIZE_RE = re.compile(r"(?:File size|Download)\s*\(?([\d.,]+\s*[KMGT]?B)\)?", re.IGNORECASE)
_DELETED_MARKERS = (
    "invalid or deleted file",
    "file removed",
    "this file has been deleted",
    "dangerous file blocked",
)


def extract_file_key(url: str) -> str | None:
    match = _FILE_KEY_RE.search(url or "")
    return match.group(1) if match else None


def normalize_page_url(url: str) -> str:
    """``/view/<key>/…`` pages (image previews) -> canonical ``/file/…``."""
    return _VIEW_PATH_RE.sub(r"\1file/", url or "", count=1)


def _clean_text(raw: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def extract_from_html(page_html: str, page_url: str) -> RemoteFile:
    """Pull the direct download link, name and size out of a file page."""
    lowered = page_html.lower()
    if any(marker in lowered for marker in _DELETED_MARKERS):
        raise ContentNotFoundError("MediaFire reports the file as deleted/blocked")

    match = _DOWNLOAD_URL_RE.search(page_html)
    if not match:
        raise ResolveError(
            "no download button found on MediaFire page",
            user_message=(
                "⚠️ MediaFire didn't offer a download for that link (it may "
                "need a password, a premium account, or a CAPTCHA in a browser)."
            ),
        )
    link = html_lib.unescape(match.group(1))

    name = ""
    name_match = _FILENAME_RE.search(page_html)
    if name_match:
        name = _clean_text(name_match.group(1))
    if not name:
        name = unquote(link.rsplit("/", 1)[-1]) or "file"

    size = 0
    size_match = _SIZE_RE.search(page_html)
    if size_match:
        size = parse_size_text(size_match.group(1))

    return RemoteFile(
        id=extract_file_key(page_url) or "",
        name=name,
        size=size,
        mimetype=mimetypes.guess_type(name)[0] or "application/octet-stream",
        link=link,
        webpage_url=page_url,
    )


class MediaFireDownloader(HTTPDownloader):
    name = "mediafire"
    label = "MediaFire"
    description = "MediaFire public file pages"
    examples = ("https://www.mediafire.com/file/abc123xyz/report.pdf/file",)

    def matches(self, url: str) -> bool:
        return host_matches(url, MEDIAFIRE_HOSTS) and extract_file_key(url) is not None

    async def _fetch_page(self, url: str) -> str:
        try:
            async with self.session.get(url) as resp:
                if resp.status == 404:
                    raise ContentNotFoundError(f"HTTP 404 for {url}")
                if resp.status != 200:
                    raise ResolveError(f"MediaFire HTTP {resp.status}")
                return await resp.text(errors="replace")
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ResolveError(str(exc)) from exc

    async def resolve(self, url: str, password: str | None = None) -> Content:
        if extract_file_key(url) is None:
            raise ContentNotFoundError(f"not a MediaFire file link: {url!r}")
        if "/folder/" in url:
            raise ResolveError(
                "MediaFire folders are not supported",
                user_message=(
                    "⚠️ MediaFire *folders* aren't supported yet — send a link "
                    "to a single file inside the folder."
                ),
            )
        page_url = normalize_page_url(url)
        return extract_from_html(await self._fetch_page(page_url), page_url)

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
        try:
            return await self._stream(file.link, file, dest, max_bytes, progress, cancel_event)
        except RemoteDownloadError as exc:
            # MediaFire download URLs expire; re-read the page once for a fresh one.
            if "HTTP 403" not in str(exc) and "HTTP 410" not in str(exc):
                raise
            if not file.webpage_url:
                raise
            fresh = extract_from_html(await self._fetch_page(file.webpage_url), file.webpage_url)
            return await self._stream(fresh.link, file, dest, max_bytes, progress, cancel_event)

    async def _stream(self, link, file, dest, max_bytes, progress, cancel_event) -> Path:
        return await stream_to_file(
            self.session,
            link,
            dest,
            max_bytes=max_bytes,
            progress=progress,
            cancel_event=cancel_event,
            allowed_hosts=MEDIAFIRE_HOSTS,
            expected_size=file.size or None,
        )
