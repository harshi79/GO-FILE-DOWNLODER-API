"""Provider interface every downloader implements.

A downloader turns a user link into :class:`~gofile_bot.gofile.models.Content`
(``resolve``) and streams one resolved file to disk (``download``). The
transfer service and the Telegram handlers only ever talk to this interface,
so adding a new site means adding one provider module and registering it in
:func:`gofile_bot.downloaders.build_registry` — nothing else changes.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from gofile_bot.config import GENERIC_USER_AGENT, METADATA_TIMEOUT
from gofile_bot.gofile.models import Content, RemoteFile

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int | None], Awaitable[None]]


def host_of(url: str) -> str:
    """Lower-cased hostname of *url* (empty string when unparseable)."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(url: str, domains: tuple[str, ...]) -> bool:
    """True when *url*'s host is one of *domains* or a subdomain of one."""
    host = host_of(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


class Downloader(abc.ABC):
    """One download source (GoFile, Google Drive, YouTube, …)."""

    #: Short stable identifier stored on jobs (e.g. ``"gofile"``).
    name: str = "base"
    #: Human-readable label shown to users (e.g. ``"Google Drive"``).
    label: str = "Downloader"
    #: One-line description for /supported.
    description: str = ""
    #: Example links for /supported and /help.
    examples: tuple[str, ...] = ()

    @abc.abstractmethod
    def matches(self, url: str) -> bool:
        """Return True when this provider can handle *url*."""

    @abc.abstractmethod
    async def resolve(self, url: str, password: str | None = None) -> Content:
        """Fetch metadata for *url* and return the resolved content."""

    @abc.abstractmethod
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
        """Download *file* and return the path of the finished file.

        Implementations stream to *dest* in small chunks (never loading the
        file into memory) and must honour *max_bytes*, *progress* and
        *cancel_event*.
        """

    async def start(self) -> None:
        """Optional startup hook (default: nothing)."""

    async def close(self) -> None:
        """Optional shutdown hook (default: nothing)."""


class HTTPDownloader(Downloader):
    """Base for providers that need a plain aiohttp session."""

    def __init__(self, *, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": GENERIC_USER_AGENT, "Accept": "*/*"},
                timeout=aiohttp.ClientTimeout(total=METADATA_TIMEOUT),
            )
            self._owns_session = True
        return self._session

    async def start(self) -> None:
        _ = self.session

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None


class DownloaderRegistry:
    """Ordered set of providers; the first match wins."""

    def __init__(self, providers: list[Downloader]) -> None:
        self._providers = list(providers)
        self._by_name = {p.name: p for p in self._providers}

    def find(self, url: str) -> Downloader | None:
        for provider in self._providers:
            try:
                if provider.matches(url):
                    return provider
            except Exception:  # pragma: no cover - a bad matcher must not kill routing
                logger.exception("provider %s crashed in matches()", provider.name)
        return None

    def get(self, name: str) -> Downloader | None:
        return self._by_name.get(name)

    def all(self) -> list[Downloader]:
        return list(self._providers)

    async def start(self) -> None:
        for provider in self._providers:
            await provider.start()

    async def close(self) -> None:
        for provider in self._providers:
            try:
                await provider.close()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.exception("provider %s failed to close cleanly", provider.name)
