"""OneDrive / SharePoint provider for public shared links.

Microsoft retired the old anonymous ``api.onedrive.com/v1.0/shares/...``
direct-download endpoint (it now answers 401), and the classic
``onedrive.live.com/download?cid=…`` form stopped working for the newer
``1drv.ms/...`` share links.  This provider therefore uses the same public,
logged-out flow the OneDrive web client itself uses:

1. **Personal OneDrive / 1drv.ms** — the share page is rendered by an internal
   *"Badger"* token combined with ``my.microsoftpersonalcontent.com``'s
   ``_api/v2.0/shares/…/driveitem`` endpoint.  The response's
   ``@content.downloadUrl`` is the file's pre-authenticated CDN URL.
   No Microsoft account is involved: the token is anonymous, exactly like a
   visitor opening the share link in a private browser window.
2. **SharePoint / OneDrive for Business** — ``?download=1`` appended to the
   sharing link (the documented anonymous force-download form), plus the
   tenant's own ``_api/v2.0/shares/…/driveitem`` endpoint as a fallback.
3. **Last resorts** — scraping ``downloadUrl`` out of the share page HTML, the
   Graph ``shares`` endpoint and the legacy live.com download form.

Only links shared with "anyone with the link" work; the bot has no Microsoft
account and never asks for one.  Shared *folders* are not supported.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_module
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

import aiohttp

from gofile_bot.config import DIRECT_MAX_REDIRECTS, METADATA_TIMEOUT
from gofile_bot.downloaders.base import HTTPDownloader, ProgressCallback, host_matches
from gofile_bot.downloaders.direct import is_web_page, size_from_headers
from gofile_bot.downloaders.net import (
    ProbeResult,
    content_type_of,
    filename_from_disposition,
    filename_from_url,
    probe_url,
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
from gofile_bot.metadata import read_json, read_text

logger = logging.getLogger(__name__)

#: Hosts a OneDrive download may legitimately bounce through.  Redirect hops
#: are re-validated against this list by :mod:`gofile_bot.downloaders.net`.
ONEDRIVE_HOSTS = (
    "1drv.ms",
    "1drv.com",
    "onedrive.live.com",
    "onedrive.com",
    "sharepoint.com",
    "microsoftpersonalcontent.com",
    "svc.ms",
    "live.com",
    "office.net",
    "graph.microsoft.com",
    "api.onedrive.com",
)

#: Personal (consumer) OneDrive share hosts.
_PERSONAL_HOSTS = ("1drv.ms", "1drv.com", "onedrive.live.com", "onedrive.com")
_BUSINESS_HOSTS = ("sharepoint.com",)

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_HTML_TYPES = ("text/html", "application/xhtml+xml")

#: The anonymous token service used by the OneDrive web client.  The app id is
#: Microsoft's own public web-client id (the same one a browser sends); it is
#: not a secret and grants no account access.
BADGER_TOKEN_URL = "https://api-badgerp.svc.ms/v1.0/token"
BADGER_APP_ID = "5cbed6ac-a083-4e14-b191-b4ba07653de2"
BADGER_APP_HEADER = "1141147648"
BADGER_TOKEN_TTL = 60 * 60  # cache well below the token's own lifetime

PERSONAL_API_BASE = "https://my.microsoftpersonalcontent.com/_api/v2.0"
_SHARE_SELECT = "@content.downloadUrl,name,size,id,file,folder"

_JSON_DOWNLOAD_RE = re.compile(
    r'"(?:@content\.downloadUrl|@microsoft\.graph\.downloadUrl|downloadUrl)"\s*:\s*"([^"\\]+(?:\\.[^"\\]*)*)"'
)
_RAW_DOWNLOAD_RE = re.compile(
    r"https?://[^\"'<>\s\\]*(?:download\.aspx|/_layouts/15/download\.aspx)[^\"'<>\s\\]*",
    re.IGNORECASE,
)
_FOLDER_PATH_RE = re.compile(r"^/(?:f|folder)(?:/|$)", re.IGNORECASE)


def is_onedrive_link(url: str) -> bool:
    return host_matches(
        url,
        ("1drv.ms", "1drv.com", "onedrive.live.com", "onedrive.com", "sharepoint.com"),
    )


def is_personal_link(url: str) -> bool:
    return host_matches(url, _PERSONAL_HOSTS)


def encode_share_token(url: str) -> str:
    """Base64url encode a sharing URL the way Graph/OneDrive expect."""
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def encode_sharing_url(url: str) -> str:
    """Encode a sharing URL as a Graph ``u!`` share token."""
    return f"u!{encode_share_token(url)}"


def graph_content_url(sharing_url: str) -> str:
    return (
        "https://graph.microsoft.com/v1.0/shares/"
        f"{encode_sharing_url(sharing_url)}/driveItem/content"
    )


def legacy_content_url(sharing_url: str) -> str:
    """Old anonymous endpoint; kept as a last-resort fallback."""
    return (
        "https://api.onedrive.com/v1.0/shares/"
        f"{encode_sharing_url(sharing_url)}/root/content"
    )


def share_api_item_url(api_base: str, sharing_url: str) -> str:
    """Metadata endpoint that returns ``@content.downloadUrl`` for a share."""
    return f"{api_base.rstrip('/')}/shares/{encode_sharing_url(sharing_url)}/driveitem"


def download_param_url(url: str) -> str:
    """Append ``download=1`` to a sharing URL (force-download form)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if re.search(r"(?:^|&)download=1(?:&|$)", parsed.query, re.IGNORECASE):
        return url
    base, _, fragment = url.partition("#")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}download=1" + (f"#{fragment}" if fragment else "")


def replace_query_download_url(url: str) -> str:
    """``…?e=xyz`` -> ``…?download=1`` (the other SharePoint force-download form)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    return urlunparse(parsed._replace(query="download=1", fragment=""))


def share_content_url(api_base: str, sharing_url: str) -> str:
    """Share/tenant content endpoint (302s to the file when anonymous access is on)."""
    return f"{api_base.rstrip('/')}/shares/{encode_sharing_url(sharing_url)}/root/content"


def live_download_url(view_url: str) -> str | None:
    """Classic download form of an ``onedrive.live.com`` view URL."""
    try:
        parsed = urlparse(view_url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in (
        "onedrive.live.com",
        "onedrive.com",
        "www.onedrive.com",
    ):
        return None
    query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
    cid = query.get("cid")
    resid = query.get("resid") or query.get("id")
    if not resid:
        return None
    if not cid and "!" in resid:
        cid = resid.split("!", 1)[0]
    if not cid:
        return None
    out = f"https://onedrive.live.com/download?cid={cid}&resid={resid}"
    if query.get("authkey"):
        out += f"&authkey={query['authkey']}"
    return out


def download_candidates(sharing_url: str, resolved_url: str | None = None) -> list[str]:
    """Direct-ish download URLs to try, most reliable first.

    Kept for backwards compatibility and for embedding/legacy callers; the
    provider itself now uses the richer flow in :class:`OneDriveDownloader`.
    """
    candidates: list[str] = [graph_content_url(sharing_url)]
    for base in (resolved_url, sharing_url):
        if not base:
            continue
        for candidate in (
            download_param_url(base),
            replace_query_download_url(base),
            live_download_url(base),
        ):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    candidates.append(legacy_content_url(sharing_url))
    return list(dict.fromkeys(candidates))


def looks_like_folder(url: str) -> bool:
    """True for obvious OneDrive/SharePoint folder links (no network needed)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    path = parsed.path or ""
    lowered = path.lower()
    if "/:f:/" in lowered or "/:folder:/" in lowered:
        return True
    if re.search(r"/folder(?:/|$)", lowered):
        return True
    host = (parsed.hostname or "").lower()
    if host.endswith("1drv.ms") and _FOLDER_PATH_RE.match(path):
        return True
    # Personal folder shares carry "&parId=…&o=OneUp" on a folder page, but
    # that also appears on files.  Only reject the unambiguous forms above.
    return False


def _looks_like_folder(url: str) -> bool:  # backwards-compatible alias
    return looks_like_folder(url)


def _decode_js_string(value: str) -> str:
    """Decode the escapes used in OneDrive/SharePoint inline JSON."""
    value = html_module.unescape(value)
    for escaped, plain in (
        ("\\u0026", "&"),
        ("\\u003d", "="),
        ("\\u002F", "/"),
        ("\\u002f", "/"),
        ("\\/", "/"),
    ):
        value = value.replace(escaped, plain)
    return value.strip()


def extract_download_urls(page_html: str) -> list[str]:
    """Pull candidate download URLs out of a shared-page HTML document."""
    page_html = page_html or ""
    found: list[str] = []
    for match in _JSON_DOWNLOAD_RE.finditer(page_html):
        found.append(_decode_js_string(match.group(1)))
    for match in _RAW_DOWNLOAD_RE.finditer(page_html):
        found.append(_decode_js_string(match.group(0)))

    unique: list[str] = []
    for candidate in found:
        if not candidate.startswith(("http://", "https://")):
            continue
        try:
            parsed = urlparse(candidate)
        except ValueError:
            continue
        if parsed.username or parsed.password:
            continue
        if not host_matches(candidate, ONEDRIVE_HOSTS):
            continue
        if candidate not in unique:
            unique.append(candidate)
    return unique


@dataclass
class _ViewResult:
    final_url: str
    status: int
    headers: object = field(default_factory=dict)
    body: str | None = None


def _is_html(headers) -> bool:
    content_type = str(headers.get("Content-Type", "")).split(";")[0].strip().lower()
    return content_type in _HTML_TYPES


def _int_or_zero(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _item_is_folder(item: dict) -> bool:
    return bool(item.get("folder"))


def _file_from_item(original: str, item: dict) -> RemoteFile | None:
    download_url = str(
        item.get("@content.downloadUrl") or item.get("@microsoft.graph.downloadUrl") or ""
    ).strip()
    if not download_url:
        return None
    file_meta = item.get("file") if isinstance(item.get("file"), dict) else {}
    name = str(item.get("name") or "").strip() or (
        filename_from_url(download_url) or "onedrive-file"
    )
    mimetype = str(file_meta.get("mimeType") or "").strip() or (
        mimetypes.guess_type(name)[0] or "application/octet-stream"
    )
    return RemoteFile(
        id=str(item.get("id") or hashlib.sha256(original.encode("utf-8")).hexdigest()[:16]),
        name=name,
        size=_int_or_zero(item.get("size")),
        mimetype=mimetype,
        link=download_url,
        webpage_url=original,
    )


class OneDriveDownloader(HTTPDownloader):
    name = "onedrive"
    label = "OneDrive"
    description = "OneDrive / SharePoint public shared files (no folders)"
    examples = ("https://1drv.ms/b/s!ExampleShareId",)

    def __init__(self, *, session: aiohttp.ClientSession | None = None) -> None:
        super().__init__(session=session)
        self._badger_token: str | None = None
        self._badger_expires = 0.0
        self._last_status = 0

    def matches(self, url: str) -> bool:
        return is_onedrive_link(url)

    # -- low level helpers ------------------------------------------------
    @staticmethod
    def _timeout(total: float = METADATA_TIMEOUT) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=total, connect=15)

    async def _fetch_view(self, url: str) -> _ViewResult | None:
        """GET a share page, following redirects manually (allowlist-checked)."""
        current = url
        for _hop in range(DIRECT_MAX_REDIRECTS + 1):
            try:
                async with self.session.get(
                    current,
                    allow_redirects=False,
                    timeout=self._timeout(),
                    headers={"Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
                ) as resp:
                    self._last_status = resp.status
                    if resp.status in _REDIRECT_STATUSES:
                        location = resp.headers.get("Location")
                        if not location:
                            return _ViewResult(current, resp.status, resp.headers)
                        current = urljoin(current, location)
                        if host_matches(current, ("microsoftonline.com",)):
                            raise PrivateContentError(
                                "OneDrive redirected to a Microsoft sign-in page",
                                user_message=(
                                    "🔒 This OneDrive link needs a Microsoft sign-in, "
                                    "so the bot can't open it.\n\n"
                                    "Ask the owner to share it as “Anyone with the link” "
                                    "and send the new link."
                                ),
                            )
                        if not host_matches(current, ONEDRIVE_HOSTS):
                            raise ResolveError(
                                f"OneDrive redirected to a non-Microsoft host: {current}"
                            )
                        continue
                    body = None
                    if resp.status == 200 and _is_html(resp.headers):
                        try:
                            body = await read_text(resp)
                        except ResolveError:
                            body = None
                    return _ViewResult(current, resp.status, resp.headers, body)
            except (TimeoutError, aiohttp.ClientError):
                logger.info("OneDrive page request failed for %s", current, exc_info=True)
                return None
        raise ResolveError(
            f"too many OneDrive redirects for {url}",
            user_message=(
                "⚠️ That OneDrive link redirected too many times. "
                "Open it in a browser and share the file again."
            ),
        )

    async def _get_badger_token(self) -> str | None:
        now = time.monotonic()
        if self._badger_token and now < self._badger_expires:
            return self._badger_token
        try:
            async with self.session.post(
                BADGER_TOKEN_URL,
                json={"appId": BADGER_APP_ID},
                headers={
                    "AppId": BADGER_APP_HEADER,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout(20),
            ) as resp:
                if resp.status != 200:
                    logger.info("OneDrive token endpoint answered HTTP %s", resp.status)
                    return None
                payload = await read_json(resp)
        except (ResolveError, TimeoutError, aiohttp.ClientError):
            logger.info("OneDrive anonymous token request failed", exc_info=True)
            return None
        token = str(payload.get("token") or "").strip()
        if not token:
            return None
        self._badger_token = token
        self._badger_expires = now + BADGER_TOKEN_TTL
        return token

    def _forget_badger_token(self) -> None:
        self._badger_token = None
        self._badger_expires = 0.0

    async def _share_item(
        self,
        api_base: str,
        sharing_links: list[str],
        *,
        token: str | None = None,
    ) -> dict | None:
        """Ask a shares API for a drive item (JSON with downloadUrl)."""
        headers = {"Accept": "application/json", "Prefer": "autoredeem"}
        if token:
            headers["Authorization"] = f"Badger {token}"
        for sharing_url in sharing_links:
            url = (
                f"{share_api_item_url(api_base, sharing_url)}"
                f"?select={_SHARE_SELECT}"
            )
            try:
                async with self.session.get(
                    url,
                    headers=headers,
                    timeout=self._timeout(20),
                ) as resp:
                    self._last_status = resp.status
                    if resp.status == 200:
                        try:
                            payload = await read_json(resp)
                        except ResolveError:
                            continue
                        if payload:
                            logger.info("OneDrive shares API resolved %s", sharing_url)
                            return payload
                    elif resp.status in (401, 403):
                        logger.info(
                            "OneDrive shares API answered HTTP %s for %s",
                            resp.status,
                            sharing_url,
                        )
            except (TimeoutError, aiohttp.ClientError):
                logger.info("OneDrive shares API request failed", exc_info=True)
        return None

    async def _personal_item(self, sharing_links: list[str]) -> dict | None:
        token = await self._get_badger_token()
        if not token:
            return None
        item = await self._share_item(PERSONAL_API_BASE, sharing_links, token=token)
        if item is None and self._badger_token is not None:
            # The cached token may have been revoked early; refresh once.
            self._forget_badger_token()
            token = await self._get_badger_token()
            if token:
                item = await self._share_item(PERSONAL_API_BASE, sharing_links, token=token)
        return item

    async def _drives_item(self, view_url: str | None) -> dict | None:
        """Personal ``/drives/{cid}/items/{id}`` metadata (older share URLs).

        Newer links redirect to a page carrying ``cid`` + ``id``; that pair is
        usable anonymously (the gist-style OneDrive API flow) on deployments
        where it has not been retired.  Failures just fall through.
        """
        if not view_url:
            return None
        try:
            query = parse_qs(urlparse(view_url).query)
        except ValueError:
            return None
        cid = (query.get("cid") or [""])[0]
        resid = (query.get("resid") or query.get("id") or [""])[0]
        if not cid or not resid:
            return None
        url = (
            f"https://api.onedrive.com/v1.0/drives/{quote(cid, safe='')}"
            f"/items/{quote(resid, safe='!')}"
        )
        if query.get("authkey"):
            url += f"?authkey={quote(query['authkey'][0], safe='!')}"
        headers = {"Accept": "application/json"}
        try:
            async with self.session.get(
                url, headers=headers, timeout=self._timeout(20)
            ) as resp:
                if resp.status != 200:
                    return None
                return await read_json(resp)
        except (ResolveError, TimeoutError, aiohttp.ClientError):
            return None

    async def _business_item(
        self, sharing_links: list[str], view_url: str | None
    ) -> dict | None:
        host = ""
        for candidate in ([view_url] if view_url else []) + sharing_links:
            try:
                parsed = urlparse(candidate)
            except ValueError:
                continue
            if host_matches(candidate, _BUSINESS_HOSTS):
                host = (parsed.hostname or "").lower()
                break
        if not host:
            return None
        # SharePoint's own v2.0 API accepts anonymous share tokens for
        # "anyone with the link" items; it simply 401s for restricted ones.
        return await self._share_item(f"https://{host}/_api/v2.0", sharing_links)

    async def _probe_candidate(self, candidate: str) -> ProbeResult | None:
        try:
            return await probe_url(
                self.session,
                candidate,
                allowed_hosts=ONEDRIVE_HOSTS,
                headers={"Accept": "application/octet-stream,*/*;q=0.8"},
            )
        except (ResolveError, RemoteDownloadError):
            logger.info("OneDrive candidate failed: %s", candidate, exc_info=True)
            return None

    def _file_from_probe(self, original: str, link: str, probe: ProbeResult) -> RemoteFile:
        mimetype = content_type_of(probe.headers)
        if mimetype in ("", "binary/octet-stream", "application/octet-stream"):
            mimetype = ""
        name = (
            filename_from_disposition(str(probe.headers.get("Content-Disposition", "")))
            or filename_from_url(probe.final_url)
            or filename_from_url(original)
            or "onedrive-file"
        )
        if name.lower().endswith("download.aspx") or name.lower() in ("download", "view"):
            name = "onedrive-file"
        name = with_extension_for_mimetype(name, mimetype or None)
        return RemoteFile(
            id=hashlib.sha256(original.encode("utf-8")).hexdigest()[:16],
            name=name,
            size=size_from_headers(probe.headers),
            mimetype=mimetype or mimetypes.guess_type(name)[0] or "application/octet-stream",
            link=link,
            webpage_url=original,
        )

    @staticmethod
    def _is_success(probe: ProbeResult) -> bool:
        if probe.status not in (200, 206) or is_web_page(probe.headers):
            return False
        # A metadata/API response is never a file, even when it is HTTP 200.
        return content_type_of(probe.headers) not in ("application/json", "text/json")

    @staticmethod
    def _needs_login(result: _ViewResult) -> bool:
        if result.status in (401, 403):
            return True
        if host_matches(result.final_url, ("login.live.com", "microsoftonline.com")):
            return True
        body = (result.body or "").lower()
        return (
            "login.microsoftonline.com" in body
            or "sign in to your account" in body
            or "accounts.microsoft.com" in body
        )

    # -- provider interface ----------------------------------------------
    async def resolve(self, url: str, password: str | None = None) -> Content:
        if looks_like_folder(url):
            raise ResolveError(
                "OneDrive folder link",
                user_message=(
                    "⚠️ OneDrive/SharePoint *folders* aren't supported — send a "
                    "link to a single file inside the folder."
                ),
            )

        # Follow the short link to the real share page.  This also yields the
        # page HTML, which newer share links use to boot-strap the download.
        view = await self._fetch_view(url)
        if view is None:
            raise ResolveError(
                f"OneDrive is unreachable for {url}",
                user_message=(
                    "⚠️ Couldn't reach OneDrive. Check the link and try again shortly."
                ),
            )
        self._last_status = view.status
        if view.status == 404:
            raise ContentNotFoundError(f"OneDrive HTTP 404 for {url}")

        if (
            view.status in (200, 206)
            and not _is_html(view.headers)
            and content_type_of(view.headers) not in ("application/json", "text/json")
        ):
            # A share link (or its redirect) already pointed straight at a file.
            return self._file_from_probe(
                url, view.final_url, ProbeResult(view.final_url, view.status, view.headers)
            )

        if looks_like_folder(view.final_url):
            raise ResolveError(
                "OneDrive folder link",
                user_message=(
                    "⚠️ OneDrive/SharePoint *folders* aren't supported — send a "
                    "link to a single file inside the folder."
                ),
            )

        sharing_links = list(dict.fromkeys([url, view.final_url]))
        item: dict | None = None
        if is_personal_link(url) or is_personal_link(view.final_url):
            item = await self._personal_item(sharing_links)
        if item is None:
            item = await self._business_item(sharing_links, view.final_url)
        if item is None:
            item = await self._drives_item(view.final_url) or await self._drives_item(url)

        if item is not None:
            file = _file_from_item(url, item)
            if file is not None:
                return file
            if _item_is_folder(item):
                raise ResolveError(
                    "OneDrive folder link",
                    user_message=(
                        "⚠️ OneDrive/SharePoint *folders* aren't supported — send a "
                        "link to a single file inside the folder."
                    ),
                )

        # Some links only expose the download URL in the rendered page.
        for page in (view.body,):
            for candidate in extract_download_urls(page or ""):
                probe = await self._probe_candidate(candidate)
                if probe is not None and self._is_success(probe):
                    return self._file_from_probe(url, candidate, probe)

        # Force-download forms (works for SharePoint shares; harmless for
        # personal links, which ignore the parameter).  Both documented
        # spellings are tried: appending to the existing query and replacing it.
        for base in (view.final_url, url):
            variants = [download_param_url(base)]
            if host_matches(base, _BUSINESS_HOSTS):
                # SharePoint documents both appending and replacing the query;
                # replacing would destroy a personal link's redeem/cid token.
                variants.append(replace_query_download_url(base))
            for candidate in variants:
                probe = await self._probe_candidate(candidate)
                if probe is not None:
                    self._last_status = probe.status
                    if self._is_success(probe):
                        return self._file_from_probe(url, candidate, probe)

        # Legacy/OAuth-required endpoints.  These usually answer 401 now, but
        # keeping them means older tenants and re-enabled endpoints still work.
        sharepoint_host = ""
        for base in (view.final_url, url):
            if host_matches(base, _BUSINESS_HOSTS):
                sharepoint_host = (urlparse(base).hostname or "").lower()
                break
        candidates = [
            graph_content_url(url),
            legacy_content_url(url),
            live_download_url(view.final_url),
            live_download_url(url),
        ]
        if sharepoint_host:
            candidates.append(
                share_content_url(f"https://{sharepoint_host}/_api/v2.0", url)
            )
        for candidate in candidates:
            if not candidate:
                continue
            probe = await self._probe_candidate(candidate)
            if probe is not None:
                self._last_status = probe.status
                if self._is_success(probe):
                    return self._file_from_probe(url, candidate, probe)

        if self._needs_login(view) or self._last_status in (401, 403):
            raise PrivateContentError(
                f"OneDrive asks for a sign-in for {url} (last HTTP {self._last_status})",
                user_message=(
                    "🔒 This OneDrive link needs a Microsoft sign-in, so the bot "
                    "can't open it.\n\n"
                    "Ask the owner to share it as “Anyone with the link” and send "
                    "the new link."
                ),
            )
        raise ResolveError(
            f"no OneDrive download candidate worked for {url} (last HTTP {self._last_status})",
            user_message=(
                "⚠️ OneDrive didn't hand over that file. Make sure it's shared "
                "as “Anyone with the link”, isn't a folder, and hasn't expired."
            ),
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

