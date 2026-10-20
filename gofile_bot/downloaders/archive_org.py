"""Internet Archive provider (archive.org), via its public metadata API.

* Item metadata: ``GET https://archive.org/metadata/{identifier}``
* File download: ``https://archive.org/download/{identifier}/{file}``
  (redirects to an ``*.us.archive.org`` storage host)

An item page resolves to a folder of its files (system/metadata files are
filtered out; the usual folder caps apply). A link that names a specific
file resolves to just that file.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import aiohttp

from gofile_bot.metadata import read_json

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback, host_matches
from gofile_bot.downloaders.net import stream_to_file
from gofile_bot.errors import ContentNotFoundError, ResolveError
from gofile_bot.gofile.models import Content, RemoteFile, RemoteFolder

ARCHIVE_HOSTS = ("archive.org",)
METADATA_URL = "https://archive.org/metadata/{identifier}"

_ITEM_RE = re.compile(r"^/(?:details|download|embed)/([^/]+)(?:/(.*))?$")
_SYSTEM_SUFFIXES = ("_meta.xml", "_meta.sqlite", "_files.xml", "_reviews.xml")
_SYSTEM_NAMES = {"__ia_thumb.jpg"}


def extract_item_ref(url: str) -> tuple[str, str | None] | None:
    """Return ``(identifier, filename_or_None)`` for an archive.org URL."""
    if not host_matches(url, ARCHIVE_HOSTS):
        return None
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    match = _ITEM_RE.match(path)
    if not match:
        return None
    identifier = unquote(match.group(1))
    filename = unquote(match.group(2)) if match.group(2) else None
    # /details/<id>/<page-ish things> — only treat the tail as a file when it
    # looks like one (has an extension); otherwise it's a view of the item.
    if filename and "." not in filename.rsplit("/", 1)[-1]:
        filename = None
    return identifier, filename


def is_system_file(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base in _SYSTEM_NAMES or base.endswith(_SYSTEM_SUFFIXES)


def file_download_url(identifier: str, filename: str) -> str:
    return f"https://archive.org/download/{quote(identifier)}/{quote(filename)}"


def _parse_file_entry(identifier: str, entry: dict) -> RemoteFile | None:
    name = str(entry.get("name") or "")
    if not name or is_system_file(name):
        return None
    try:
        size = int(entry.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return RemoteFile(
        id=f"{identifier}/{name}",
        name=name.rsplit("/", 1)[-1],
        size=size,
        mimetype=mimetypes.guess_type(name)[0] or "application/octet-stream",
        link=file_download_url(identifier, name),
        webpage_url=f"https://archive.org/details/{identifier}",
    )


def parse_metadata(data: dict, wanted_file: str | None = None) -> Content:
    """Turn a ``/metadata`` payload into a file or folder."""
    identifier = str((data.get("metadata") or {}).get("identifier") or "")
    entries = [e for e in (data.get("files") or []) if isinstance(e, dict)]

    if wanted_file:
        for entry in entries:
            if str(entry.get("name") or "") == wanted_file:
                parsed = _parse_file_entry(identifier, entry)
                if parsed is not None:
                    return parsed
        raise ContentNotFoundError(f"{wanted_file!r} not in item {identifier!r}")

    files = tuple(
        parsed
        for entry in entries
        if (parsed := _parse_file_entry(identifier, entry)) is not None
    )
    title = str((data.get("metadata") or {}).get("title") or identifier or "Archive item")
    return RemoteFolder(id=identifier, name=title, children=files)


class ArchiveOrgDownloader(HTTPDownloader):
    name = "archive"
    label = "Internet Archive"
    description = "archive.org items and files"
    examples = ("https://archive.org/details/<identifier>",)

    def matches(self, url: str) -> bool:
        return extract_item_ref(url) is not None

    async def resolve(self, url: str, password: str | None = None) -> Content:
        ref = extract_item_ref(url)
        if ref is None:
            raise ContentNotFoundError(f"not an archive.org item link: {url!r}")
        identifier, wanted_file = ref
        try:
            async with self.session.get(
                METADATA_URL.format(identifier=quote(identifier))
            ) as resp:
                if resp.status == 404:
                    raise ContentNotFoundError(f"archive.org 404 for {identifier}")
                if resp.status != 200:
                    raise ResolveError(f"archive.org HTTP {resp.status}")
                data = await read_json(resp)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ResolveError(str(exc)) from exc
        if not isinstance(data, dict) or not data.get("metadata"):
            raise ContentNotFoundError(f"empty archive.org item {identifier!r}")
        return parse_metadata(data, wanted_file)

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
            allowed_hosts=ARCHIVE_HOSTS,
            expected_size=file.size or None,
        )
