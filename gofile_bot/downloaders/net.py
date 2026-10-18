"""Shared HTTP helpers for the generic downloaders.

Two jobs:

* :func:`stream_to_file` — stream a URL to disk with size caps, progress,
  cancellation and *manually followed* redirects. Every redirect hop is
  re-validated, so a redirect can never bounce the bot somewhere the provider
  didn't allow.
* :func:`validate_public_url` — SSRF guard for user-supplied (direct) links:
  the host must resolve exclusively to globally-routable addresses. Private,
  loopback, link-local, multicast and reserved ranges are refused, as are
  ``localhost``-style names and URLs with embedded credentials.

Providers for known hosts (Drive, MediaFire, Pixeldrain) instead pin an
``allowed_hosts`` allowlist — same defence GoFile already used.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import mimetypes
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from gofile_bot.config import (
    DIRECT_MAX_REDIRECTS,
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_SOCK_READ_TIMEOUT,
)
from gofile_bot.downloaders.base import ProgressCallback
from gofile_bot.errors import (
    FileTooLargeError,
    JobCancelled,
    RemoteDownloadError,
    ResolveError,
)

logger = logging.getLogger(__name__)

#: Resolves a hostname to IP literal strings. Injectable for tests.
Resolver = Callable[[str], Awaitable[Iterable[str]]]

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home", ".invalid")
_SIZE_TEXT_RE = re.compile(r"([\d.,]+)\s*([KMGT]?B)\b", re.IGNORECASE)
_SIZE_MULTIPLIERS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


async def default_resolver(host: str) -> list[str]:
    """Resolve *host* via the event loop's resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def validate_public_url(url: str, *, resolver: Resolver | None = None) -> str:
    """Return *url* unchanged when it is safe to fetch, else raise.

    Only ``http``/``https`` URLs whose host resolves *only* to public IPs are
    accepted. Raises :class:`ResolveError` with a friendly message otherwise.
    """
    candidate = (url or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise ResolveError(f"unparseable URL: {exc}") from exc

    blocked = ResolveError(
        "blocked non-public URL",
        user_message=(
            "⚠️ That link points to a private or internal address, which the "
            "bot won't fetch. Send a public http(s) link instead."
        ),
    )

    if parsed.scheme not in ("http", "https"):
        raise blocked
    if parsed.username or parsed.password:
        raise blocked

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise blocked

    # IP literal: check directly, no DNS involved.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _is_public_ip(host):
            raise blocked
        return candidate

    resolve = resolver or default_resolver
    try:
        addresses = list(await resolve(host))
    except (OSError, UnicodeError) as exc:
        raise ResolveError(
            f"DNS lookup failed for {host}: {exc}",
            user_message="⚠️ Couldn't resolve that host. Check the link and try again.",
        ) from exc
    if not addresses or not all(_is_public_ip(a) for a in addresses):
        raise blocked
    return candidate


def _check_allowed_host(url: str, allowed_hosts: tuple[str, ...]) -> None:
    from gofile_bot.downloaders.base import host_matches

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise RemoteDownloadError(f"refusing non-http(s) URL: {url!r}")
    if not host_matches(url, allowed_hosts):
        raise RemoteDownloadError(f"refusing download from non-allowlisted host: {url!r}")


async def stream_to_file(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    progress: ProgressCallback | None = None,
    cancel_event: asyncio.Event | None = None,
    headers: dict[str, str] | None = None,
    allowed_hosts: tuple[str, ...] | None = None,
    public_only: bool = False,
    resolver: Resolver | None = None,
    expected_size: int | None = None,
) -> Path:
    """Stream *url* to *dest* following redirects manually; return *dest*.

    Exactly one of *allowed_hosts* (host allowlist) or *public_only* (SSRF
    check) should typically be set; both may be combined.
    """
    timeout = aiohttp.ClientTimeout(
        total=None, connect=30, sock_read=DOWNLOAD_SOCK_READ_TIMEOUT
    )
    current = url
    part_path = dest.with_suffix(dest.suffix + ".part")
    written = 0
    try:
        for _hop in range(DIRECT_MAX_REDIRECTS + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled()
            if allowed_hosts is not None:
                _check_allowed_host(current, allowed_hosts)
            if public_only:
                await validate_public_url(current, resolver=resolver)

            async with session.get(
                current, headers=headers, timeout=timeout, allow_redirects=False
            ) as resp:
                if resp.status in _REDIRECT_STATUSES:
                    location = resp.headers.get("Location")
                    if not location:
                        raise RemoteDownloadError(f"HTTP {resp.status} without Location")
                    current = urljoin(current, location)
                    continue
                if resp.status != 200:
                    raise RemoteDownloadError(f"HTTP {resp.status} for {current}")

                total = expected_size
                content_length = resp.headers.get("Content-Length", "")
                if content_length.isdigit():
                    total = int(content_length)
                    if total > max_bytes:
                        raise FileTooLargeError(size=total, limit=max_bytes)

                with part_path.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                        if cancel_event is not None and cancel_event.is_set():
                            raise JobCancelled()
                        written += len(chunk)
                        if written > max_bytes:
                            raise FileTooLargeError(size=written, limit=max_bytes)
                        fh.write(chunk)
                        if progress is not None:
                            await progress(written, total)
                part_path.replace(dest)
                logger.info("downloaded %s (%d bytes)", dest.name, written)
                return dest
        raise RemoteDownloadError("too many redirects")
    except (TimeoutError, aiohttp.ClientError) as exc:
        raise RemoteDownloadError(str(exc)) from exc
    finally:
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass


class ProbeResult:
    """Outcome of :func:`probe_url` (headers only, no body transferred)."""

    __slots__ = ("final_url", "headers", "status")

    def __init__(self, final_url: str, status: int, headers) -> None:
        self.final_url = final_url
        self.status = status
        self.headers = headers


async def probe_url(
    session: aiohttp.ClientSession,
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | None = None,
    public_only: bool = False,
    resolver: Resolver | None = None,
    headers: dict[str, str] | None = None,
) -> ProbeResult:
    """HEAD *url* (ranged GET fallback) following redirects manually.

    Reads response *headers only* — the body is never downloaded — so probing
    costs almost nothing. Every redirect hop is re-validated with the same
    policy as :func:`stream_to_file`. Raises :class:`ResolveError` on network
    failure; HTTP error statuses are returned, not raised.
    """
    from gofile_bot.config import METADATA_TIMEOUT

    timeout = aiohttp.ClientTimeout(total=METADATA_TIMEOUT)
    status = 0
    final_headers: object = {}
    current = url
    for method, extra in (("HEAD", None), ("GET", {"Range": "bytes=0-0"})):
        current = url
        for _hop in range(DIRECT_MAX_REDIRECTS + 1):
            if allowed_hosts is not None:
                _check_allowed_host(current, allowed_hosts)
            if public_only:
                await validate_public_url(current, resolver=resolver)
            merged = {**(headers or {}), **(extra or {})} or None
            try:
                async with session.request(
                    method,
                    current,
                    headers=merged,
                    timeout=timeout,
                    allow_redirects=False,
                ) as resp:
                    status = resp.status
                    final_headers = resp.headers
                    if status in _REDIRECT_STATUSES and resp.headers.get("Location"):
                        current = urljoin(current, resp.headers["Location"])
                        continue
                    break
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise ResolveError(str(exc)) from exc
        # Some servers reject HEAD outright; retry once with a ranged GET.
        if method == "HEAD" and status in (400, 403, 405, 501):
            continue
        return ProbeResult(current, status, final_headers)
    return ProbeResult(current, status, final_headers)  # pragma: no cover


# ---------------------------------------------------------------------------
# Response-parsing helpers
# ---------------------------------------------------------------------------
def filename_from_disposition(disposition: str | None) -> str | None:
    """Extract a filename from a ``Content-Disposition`` header value."""
    if not disposition:
        return None
    match = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]+)", disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1)).strip().strip('"') or None
    match = re.search(r'filename="([^"]+)"', disposition, re.IGNORECASE)
    if match:
        return match.group(1).strip() or None
    match = re.search(r"filename=([^;]+)", disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1)).strip().strip('"') or None
    return None


def filename_from_url(url: str) -> str | None:
    """Best-effort filename from the last URL path segment."""
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    name = unquote(path.rsplit("/", 1)[-1]).strip()
    return name or None


def with_extension_for_mimetype(name: str, mimetype: str | None) -> str:
    """Append a guessed extension when *name* has none."""
    if "." in name.rsplit("/", 1)[-1] or not mimetype:
        return name
    ext = mimetypes.guess_extension(mimetype.split(";")[0].strip())
    return f"{name}{ext}" if ext else name


def parse_size_text(text: str | None) -> int:
    """Parse human sizes like ``"12.4 MB"`` into bytes (0 when unknown)."""
    if not text:
        return 0
    match = _SIZE_TEXT_RE.search(str(text).replace("\u00a0", " "))
    if not match:
        return 0
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(value * _SIZE_MULTIPLIERS.get(match.group(2).upper(), 1))


def content_type_of(headers) -> str:
    raw = headers.get("Content-Type", "") if headers is not None else ""
    return raw.split(";")[0].strip().lower()
