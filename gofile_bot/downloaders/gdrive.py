"""Google Drive provider for *public* links.

Supported:

* Shared files — ``drive.google.com/file/d/<id>/view``, ``open?id=``, ``uc?id=``
  (including the large-file "can't scan for viruses" confirm form).
* Google Docs / Sheets / Slides — exported as PDF / XLSX / PPTX.

Not supported: Drive *folders* and anything that isn't shared publicly
("anyone with the link"). Those need the Drive API with user OAuth, which a
public bot cannot do on a user's behalf — and this bot never asks for Google
credentials.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp

from gofile_bot.metadata import read_text

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback, host_matches
from gofile_bot.downloaders.net import (
    filename_from_disposition,
    stream_to_file,
)
from gofile_bot.errors import (
    ContentNotFoundError,
    PrivateContentError,
    ResolveError,
)
from gofile_bot.gofile.models import Content, RemoteFile

DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
DOWNLOAD_HOSTS = ("drive.google.com", "docs.google.com", "googleusercontent.com")

_FILE_PATH_RE = re.compile(r"/file/d/([A-Za-z0-9_-]{10,})")
_FOLDER_PATH_RE = re.compile(r"/(?:drive/folders|folderview)/?([A-Za-z0-9_-]{10,})?")
_DOC_PATH_RE = re.compile(r"/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{10,})")
_FORM_FIELD_RE = re.compile(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', re.IGNORECASE)

_EXPORT_FORMATS = {
    "document": ("pdf", "document.pdf", "application/pdf"),
    "spreadsheets": (
        "xlsx",
        "spreadsheet.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "presentation": (
        "pptx",
        "presentation.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
}


def extract_drive_ref(url: str) -> tuple[str, str] | None:
    """Return ``(kind, id)`` for a Drive/Docs URL.

    *kind* is ``"file"``, ``"folder"`` or one of the Docs kinds
    (``"document"`` / ``"spreadsheets"`` / ``"presentation"``).
    """
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    if not host_matches(url, DRIVE_HOSTS):
        return None

    match = _DOC_PATH_RE.search(parsed.path)
    if match:
        return match.group(1), match.group(2)
    match = _FILE_PATH_RE.search(parsed.path)
    if match:
        return "file", match.group(1)
    if "/drive/folders/" in parsed.path or "folder" in parsed.path:
        match = _FOLDER_PATH_RE.search(parsed.path)
        return "folder", (match.group(1) if match and match.group(1) else "")

    query_id = (parse_qs(parsed.query).get("id") or [""])[0]
    if query_id and re.fullmatch(r"[A-Za-z0-9_-]{10,}", query_id):
        return "file", query_id
    return None


def file_download_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def export_download_url(kind: str, doc_id: str) -> str:
    fmt = _EXPORT_FORMATS[kind][0]
    return f"https://docs.google.com/{kind}/d/{doc_id}/export?format={fmt}"


def parse_confirm_form(page_html: str) -> str | None:
    """Build the confirmed download URL from Drive's virus-scan warning page."""
    fields = dict(_FORM_FIELD_RE.findall(page_html or ""))
    file_id = fields.get("id")
    if not file_id:
        return None
    parts = [
        f"id={file_id}",
        f"export={fields.get('export', 'download')}",
        f"confirm={fields.get('confirm', 't')}",
    ]
    if fields.get("uuid"):
        parts.append(f"uuid={fields['uuid']}")
    return "https://drive.usercontent.google.com/download?" + "&".join(parts)


class GoogleDriveDownloader(HTTPDownloader):
    name = "gdrive"
    label = "Google Drive"
    description = "Public Drive files + Docs/Sheets/Slides export (no folders)"
    examples = ("https://drive.google.com/file/d/<id>/view",)

    def matches(self, url: str) -> bool:
        return extract_drive_ref(url) is not None

    async def _final_download(self, start_url: str) -> tuple[str, object, int]:
        """Follow Drive's flow to the real file; return (url, headers, status).

        Performs a GET and *closes* the response after reading headers (or the
        small HTML confirm page), so no file body is transferred here. The
        actual body is streamed later by :func:`stream_to_file`.
        """
        current = start_url
        for _ in range(3):
            try:
                async with self.session.get(current, allow_redirects=True) as resp:
                    status = resp.status
                    headers = resp.headers
                    final = str(resp.url)
                    ctype = headers.get("Content-Type", "")
                    if "text/html" in ctype:
                        body = await read_text(resp)
                        confirmed = parse_confirm_form(body)
                        if confirmed:
                            current = confirmed
                            continue
                        lowered = body.lower()
                        if "quota" in lowered or "too many users" in lowered:
                            raise ResolveError(
                                "Drive download quota exceeded",
                                user_message=(
                                    "⚠️ Google says this file's download quota is "
                                    "exhausted right now. Try again later."
                                ),
                            )
                        if "sign in" in lowered or "accounts.google.com" in final:
                            raise PrivateContentError("Drive asks for sign-in")
                        raise ResolveError(
                            "Drive returned an HTML interstitial",
                            user_message=(
                                "⚠️ Google Drive didn't hand over that file. Make "
                                "sure it's shared as “Anyone with the link”."
                            ),
                        )
                    return final, headers, status
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise ResolveError(str(exc)) from exc
        raise ResolveError("Drive confirm loop did not resolve")

    async def resolve(self, url: str, password: str | None = None) -> Content:
        ref = extract_drive_ref(url)
        if ref is None:
            raise ContentNotFoundError(f"not a Drive link: {url!r}")
        kind, ref_id = ref

        if kind == "folder":
            raise ResolveError(
                "Drive folders need API access",
                user_message=(
                    "⚠️ Google Drive *folders* aren't supported — send a link to "
                    "a single file inside the folder instead."
                ),
            )

        if kind in _EXPORT_FORMATS:
            start = export_download_url(kind, ref_id)
            _fmt, default_name, default_mime = _EXPORT_FORMATS[kind]
        else:
            start = file_download_url(ref_id)
            default_name, default_mime = "drive-file", "application/octet-stream"

        _final, headers, status = await self._final_download(start)
        if status == 404:
            raise ContentNotFoundError(f"Drive HTTP 404 for {url}")
        if status >= 400:
            raise ResolveError(f"Drive HTTP {status} for {url}")

        name = (
            filename_from_disposition(headers.get("Content-Disposition", ""))  # type: ignore[attr-defined]
            or default_name
        )
        size = 0
        content_length = headers.get("Content-Length", "")  # type: ignore[attr-defined]
        if str(content_length).isdigit():
            size = int(content_length)
        mimetype = (
            str(headers.get("Content-Type", "")).split(";")[0].strip()  # type: ignore[attr-defined]
            or mimetypes.guess_type(name)[0]
            or default_mime
        )

        return RemoteFile(
            id=ref_id,
            name=name,
            size=size,
            mimetype=mimetype,
            link=start,
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
        final, _headers, _status = await self._final_download(file.link)
        return await stream_to_file(
            self.session,
            final,
            dest,
            max_bytes=max_bytes,
            progress=progress,
            cancel_event=cancel_event,
            allowed_hosts=DOWNLOAD_HOSTS,
            expected_size=file.size or None,
        )
