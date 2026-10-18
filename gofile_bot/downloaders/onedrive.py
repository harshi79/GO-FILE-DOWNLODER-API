"""OneDrive / SharePoint provider for public shared links.

Two well-known ways turn a sharing link into a download, and this provider
tries both (whichever the link supports wins):

1. Microsoft Graph *shares* endpoint — the sharing URL base64url-encoded as a
   ``u!`` token: ``/v1.0/shares/{token}/driveItem/content``.
2. The classic ``onedrive.live.com/download?cid=…&resid=…&authkey=…`` form
   derived from the resolved (post-redirect) view URL.

Only links shared with "anyone with the link" work; the bot has no Microsoft
account and never asks for one. Shared *folders* are not supported.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback, host_matches
from gofile_bot.downloaders.direct import is_web_page, size_from_headers
from gofile_bot.downloaders.net import (
    ProbeResult,
    filename_from_disposition,
    filename_from_url,
    probe_url,
    stream_to_file,
)
from gofile_bot.errors import ContentNotFoundError, ResolveError
from gofile_bot.gofile.models import Content, RemoteFile

#: Hosts a OneDrive download may legitimately bounce through.
ONEDRIVE_HOSTS = (
    "1drv.ms",
    "onedrive.live.com",
    "sharepoint.com",
    "1drv.com",
    "graph.microsoft.com",
    "microsoftpersonalcontent.com",
    "live.com",
)


def is_onedrive_link(url: str) -> bool:
    return host_matches(url, ("1drv.ms", "onedrive.live.com", "sharepoint.com"))


def encode_sharing_url(url: str) -> str:
    """Encode a sharing URL as a Graph ``u!`` share token."""
    token = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"u!{token}"


def graph_content_url(sharing_url: str) -> str:
    return (
        "https://graph.microsoft.com/v1.0/shares/"
        f"{encode_sharing_url(sharing_url)}/driveItem/content"
    )


def live_download_url(view_url: str) -> str | None:
    """Classic download form of an ``onedrive.live.com`` view URL."""
    try:
        parsed = urlparse(view_url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() != "onedrive.live.com":
        return None
    query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
    cid = query.get("cid")
    resid = query.get("resid") or query.get("id")
    if not cid or not resid:
        return None
    out = f"https://onedrive.live.com/download?cid={cid}&resid={resid}"
    if query.get("authkey"):
        out += f"&authkey={query['authkey']}"
    return out


def download_candidates(sharing_url: str, resolved_url: str | None = None) -> list[str]:
    """Download URLs to try for a sharing link, most reliable first."""
    candidates: list[str] = [graph_content_url(sharing_url)]
    for candidate in (
        live_download_url(resolved_url) if resolved_url else None,
        live_download_url(sharing_url),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _looks_like_folder(url: str) -> bool:
    path = urlparse(url).path
    return "/:f:/" in path or path.startswith("/folder")


class OneDriveDownloader(HTTPDownloader):
    name = "onedrive"
    label = "OneDrive"
    description = "OneDrive / SharePoint public shared files (no folders)"
    examples = ("https://1drv.ms/b/s!ExampleShareId",)

    def matches(self, url: str) -> bool:
        return is_onedrive_link(url)

    async def _try_probe(self, url: str) -> ProbeResult | None:
        try:
            return await probe_url(self.session, url, allowed_hosts=ONEDRIVE_HOSTS)
        except ResolveError:
            return None

    async def resolve(self, url: str, password: str | None = None) -> Content:
        if _looks_like_folder(url):
            raise ResolveError(
                "OneDrive folder link",
                user_message=(
                    "⚠️ OneDrive/SharePoint *folders* aren't supported — send a "
                    "link to a single file inside the folder."
                ),
            )

        # Resolve short links (1drv.ms) to their live.com form first; that
        # final URL feeds the classic download transform. The probe reads
        # headers only, so this stays cheap.
        resolved: str | None = None
        initial = await self._try_probe(url)
        if initial is not None:
            if initial.status == 200 and not is_web_page(initial.headers):
                return self._file_from_probe(url, url, initial)
            resolved = initial.final_url

        last_status = initial.status if initial is not None else 0
        for candidate in download_candidates(url, resolved):
            probe = await self._try_probe(candidate)
            if probe is None:
                continue
            last_status = probe.status
            if probe.status == 200 and not is_web_page(probe.headers):
                return self._file_from_probe(url, candidate, probe)

        if last_status == 404:
            raise ContentNotFoundError(f"OneDrive HTTP 404 for {url}")
        raise ResolveError(
            f"no OneDrive download candidate worked for {url} (last HTTP {last_status})",
            user_message=(
                "⚠️ OneDrive didn't hand over that file. Make sure it's shared "
                "as “Anyone with the link” and isn't a folder."
            ),
        )

    @staticmethod
    def _file_from_probe(original: str, link: str, probe: ProbeResult) -> RemoteFile:
        name = (
            filename_from_disposition(
                str(probe.headers.get("Content-Disposition", ""))
            )
            or filename_from_url(probe.final_url)
            or filename_from_url(original)
            or "onedrive-file"
        )
        return RemoteFile(
            id=hashlib.sha256(original.encode("utf-8")).hexdigest()[:16],
            name=name,
            size=size_from_headers(probe.headers),
            mimetype=mimetypes.guess_type(name)[0] or "application/octet-stream",
            link=link,
            webpage_url=original,
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
            allowed_hosts=ONEDRIVE_HOSTS,
            expected_size=file.size or None,
        )
