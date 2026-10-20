"""Google Drive provider for *public* links.

Supported:

* Shared files — ``drive.google.com/file/d/<id>/view``, ``open?id=``, ``uc?id=``
  (including the large-file "can't scan for viruses" confirm form).
* Google Docs / Sheets / Slides — exported as PDF / XLSX / PPTX.

Downloads go through Google's current ``drive.usercontent.google.com``
endpoint with ``confirm=t``, which serves files of every size directly (the
old cookie-based confirm dance is no longer needed).  A ranged request is used
while reading metadata, so resolving a multi-gigabyte file never starts a
multi-gigabyte transfer.

Not supported: Drive *folders* and anything that isn't shared publicly
("anyone with the link"). Those need the Drive API with user OAuth, which a
public bot cannot do on a user's behalf — and this bot never asks for Google
credentials.
"""

from __future__ import annotations

import asyncio
import html as html_module
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp

from gofile_bot.metadata import read_text

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback, host_matches
from gofile_bot.downloaders.direct import size_from_headers
from gofile_bot.downloaders.net import (
    filename_from_disposition,
    filename_from_url,
    stream_to_file,
    with_extension_for_mimetype,
)
from gofile_bot.errors import (
    ContentNotFoundError,
    PrivateContentError,
    RemoteDownloadError,
    ResolveError,
)
from gofile_bot.gofile.models import Content, RemoteFile

DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
DOWNLOAD_HOSTS = (
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
    "googleusercontent.com",
    "google.com",
)

#: Google's direct-download host (2024+).  Kept separate from ``DRIVE_HOSTS``
#: because share links never use it.
USERCONTENT_HOST = "drive.usercontent.google.com"

_FILE_PATH_RE = re.compile(r"/file/d/([A-Za-z0-9_-]{10,})")
_FOLDER_PATH_RE = re.compile(r"/(?:drive/folders|folderview)/?([A-Za-z0-9_-]{10,})?")
_DOC_PATH_RE = re.compile(r"/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{10,})")
_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([A-Za-z_:][\w:.-]*)\s*=\s*"([^"]*)"')
_FORM_ACTION_RE = re.compile(r'<form\b[^>]*\baction\s*=\s*"([^"]+)"', re.IGNORECASE)
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_MAX_CONFIRM_HOPS = 4

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
    """Legacy ``uc?export=download`` URL (kept for compatibility)."""
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def direct_download_url(file_id: str) -> str:
    """Current direct-download URL; ``confirm=t`` skips the virus-scan page."""
    params = urlencode({"id": file_id, "export": "download", "confirm": "t"})
    return f"https://{USERCONTENT_HOST}/download?{params}"


def export_download_url(kind: str, doc_id: str) -> str:
    fmt = _EXPORT_FORMATS[kind][0]
    return f"https://docs.google.com/{kind}/d/{doc_id}/export?format={fmt}"


def parse_confirm_form(page_html: str) -> str | None:
    """Build the confirmed download URL from Drive's virus-scan warning page."""
    fields: dict[str, str] = {}
    for tag in _INPUT_TAG_RE.findall(page_html or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        name = attrs.get("name")
        if name and "value" in attrs:
            fields.setdefault(name, html_module.unescape(attrs["value"]))
    file_id = fields.get("id")
    if not file_id:
        return None

    action_match = _FORM_ACTION_RE.search(page_html or "")
    action = html_module.unescape(action_match.group(1)) if action_match else ""
    if not host_matches(action, DOWNLOAD_HOSTS):
        action = f"https://{USERCONTENT_HOST}/download"

    query = {"id": file_id, "export": fields.get("export", "download")}
    if fields.get("confirm"):
        query["confirm"] = fields["confirm"]
    if fields.get("uuid"):
        query["uuid"] = fields["uuid"]
    return f"{action}{'&' if '?' in action else '?'}{urlencode(query)}"


def _is_html(headers) -> bool:
    content_type = str(headers.get("Content-Type", "")).split(";")[0].strip().lower()
    return content_type in ("text/html", "application/xhtml+xml")


class GoogleDriveDownloader(HTTPDownloader):
    name = "gdrive"
    label = "Google Drive"
    description = "Public Drive files + Docs/Sheets/Slides export (no folders)"
    examples = ("https://drive.google.com/file/d/<id>/view",)

    def matches(self, url: str) -> bool:
        return extract_drive_ref(url) is not None

    async def _fetch(
        self, url: str
    ) -> tuple[str, object, int]:
        """GET *url* with a one-byte range; return (final_url, headers, status).

        Redirects are followed manually and re-validated against Google's host
        allowlist.  Only headers (or the small HTML confirm page) are read, so
        metadata lookups never transfer the file body.
        """
        current = url
        for _hop in range(_MAX_CONFIRM_HOPS):
            if not host_matches(current, DOWNLOAD_HOSTS):
                raise ResolveError(f"Drive redirect left Google's hosts: {current}")
            try:
                async with self.session.get(
                    current,
                    allow_redirects=False,
                    headers={"Range": "bytes=0-0", "Accept": "*/*"},
                    timeout=aiohttp.ClientTimeout(total=30, connect=15),
                ) as resp:
                    status = resp.status
                    headers = resp.headers
                    if status in _REDIRECT_STATUSES:
                        location = headers.get("Location")
                        if not location:
                            return current, headers, status
                        current = urljoin(current, location)
                        continue
                    if _is_html(headers):
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
                        if (
                            "sign in" in lowered
                            or "accounts.google.com" in lowered
                            or "request access" in lowered
                            or "you need permission" in lowered
                        ):
                            raise PrivateContentError("Drive asks for sign-in")
                        if status in (200, 206):
                            raise ResolveError(
                                "Drive returned an HTML interstitial",
                                user_message=(
                                    "⚠️ Google Drive didn't hand over that file. Make "
                                    "sure it's shared as “Anyone with the link”."
                                ),
                            )
                    return current, headers, status
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise ResolveError(str(exc)) from exc
        raise ResolveError("Drive confirm loop did not resolve")

    @staticmethod
    def _start_url(kind: str, ref_id: str) -> str:
        if kind in _EXPORT_FORMATS:
            return export_download_url(kind, ref_id)
        return direct_download_url(ref_id)

    def _download_link(self, kind: str, ref_id: str, final_url: str) -> str:
        """Prefer the resolved direct URL; otherwise rebuild a reliable one."""
        if kind in _EXPORT_FORMATS:
            return final_url
        if host_matches(final_url, (USERCONTENT_HOST,)):
            return final_url
        return direct_download_url(ref_id)

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
            default_name, default_mime = (
                _EXPORT_FORMATS[kind][1],
                _EXPORT_FORMATS[kind][2],
            )
        else:
            default_name, default_mime = "drive-file", "application/octet-stream"

        final, headers, status = await self._fetch(self._start_url(kind, ref_id))
        if status == 404:
            raise ContentNotFoundError(f"Drive HTTP 404 for {url}")
        if status not in (200, 206):
            raise ResolveError(
                f"Drive HTTP {status} for {url}",
                user_message=(
                    f"⚠️ Google Drive answered HTTP {status} for that link. It may "
                    "be deleted, private, or over its download quota."
                ),
            )

        name = (
            filename_from_disposition(str(headers.get("Content-Disposition", "")))
            or filename_from_url(final)
            or default_name
        )
        if name in ("download", "export", "uc", "view") or not name:
            name = default_name
        mimetype = (
            str(headers.get("Content-Type", "")).split(";")[0].strip()
            or mimetypes.guess_type(name)[0]
            or default_mime
        )
        if mimetype in ("application/octet-stream", "binary/octet-stream"):
            guessed = mimetypes.guess_type(name)[0]
            mimetype = guessed or mimetype
        name = with_extension_for_mimetype(name, mimetype)
        size = size_from_headers(headers)

        return RemoteFile(
            id=ref_id,
            name=name,
            size=size,
            mimetype=mimetype,
            link=self._download_link(kind, ref_id, final),
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
        kwargs = dict(
            max_bytes=max_bytes,
            progress=progress,
            cancel_event=cancel_event,
            allowed_hosts=DOWNLOAD_HOSTS,
            expected_size=file.size or None,
        )
        try:
            return await stream_to_file(self.session, file.link, dest, **kwargs)
        except RemoteDownloadError:
            # A confirm/session URL can occasionally expire between the info
            # card and the button press; resolve it once more and retry.
            ref = extract_drive_ref(file.webpage_url)
            if ref is None or ref[0] == "folder":
                raise
            final, _headers, status = await self._fetch(self._start_url(*ref))
            if status not in (200, 206):
                raise
            return await stream_to_file(self.session, final, dest, **kwargs)
