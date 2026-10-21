"""Public Pasteview pastes exported as UTF-8 text, not followed as links."""

from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback
from gofile_bot.errors import ContentNotFoundError, FileTooLargeError, JobCancelled, ResolveError
from gofile_bot.gofile.models import RemoteFile
from gofile_bot.metadata import read_text
from gofile_bot.services.resources import disk_io


def paste_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https")
                or parsed.hostname not in ("pasteview.com", "www.pasteview.com")
                or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
            return None
        match = re.fullmatch(r"/([A-Za-z0-9]{7})/?", parsed.path)
        return match[1] if match else None
    except ValueError:
        return None


class _PasteParser(HTMLParser):
    """Only export the paste's code block; never navigation or adverts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            if not self.depth:
                self.parts = []
            self.depth += 1
        elif self.depth and tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "pre" and self.depth:
            self.depth -= 1
            if not self.depth:
                self.blocks.append("".join(self.parts))

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def extract_paste(page: str) -> str:
    parser = _PasteParser()
    parser.feed(page)
    if len(parser.blocks) != 1:
        raise ResolveError(
            "Pasteview did not expose a single public paste block",
            user_message="⚠️ Pasteview didn't expose this paste as public text. It may be private, expired, or require a browser.",
        )
    return parser.blocks[0]


class PasteviewDownloader(HTTPDownloader):
    name = "pasteview"
    label = "Pasteview"
    description = "Public text pastes exported as UTF-8 .txt files"
    examples = ("https://pasteview.com/AbCd123",)

    def matches(self, url: str) -> bool:
        return paste_id(url) is not None

    async def _text(self, url: str) -> tuple[str, bytes]:
        key = paste_id(url)
        if key is None:
            raise ResolveError("Invalid Pasteview paste URL")
        # Fixed origin and no redirects: user input cannot select another host.
        async with self.session.get(
            f"https://pasteview.com/{key}", allow_redirects=False,
        ) as response:
            if response.status in (404, 410):
                raise ContentNotFoundError("Pasteview paste not found")
            if response.status != 200:
                raise ResolveError(f"Pasteview answered HTTP {response.status}")
            text = extract_paste(await read_text(response))
        return key, text.encode("utf-8")

    async def resolve(self, url: str, password: str | None = None) -> RemoteFile:
        key, data = await self._text(url)
        canonical = f"https://pasteview.com/{key}"
        return RemoteFile(
            id=key, name=f"pasteview-{key}.txt", size=len(data),
            mimetype="text/plain", link=canonical, webpage_url=canonical,
        )

    async def download(
        self, file: RemoteFile, dest: Path, *, max_bytes: int,
        progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None, options: dict | None = None,
    ) -> Path:
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        _, data = await self._text(file.link)
        if len(data) > max_bytes:
            raise FileTooLargeError(size=len(data), limit=max_bytes)
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled()
        await disk_io(dest.write_bytes, data)
        if progress:
            await progress(len(data), len(data))
        return dest
