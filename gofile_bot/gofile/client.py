"""Asynchronous GoFile client: metadata lookup and streaming downloads.

Access model
------------
Reading content through the documented API (``GET /contents/{id}``) requires a
Premium account token. For ordinary public shares the GoFile **web client**
uses the same endpoint with a rotating ``X-Website-Token`` derived from a
guest account; that is the supported public way to open a shared link, and it
is what this client reproduces (see :mod:`gofile_bot.gofile.token`). If the
operator supplies ``GOFILE_API_TOKEN`` (e.g. a Premium token) it is used as the
account token instead of a throwaway guest account.

The client never bypasses authentication or access controls: private content
stays inaccessible, password-protected content still requires the password,
and rate limits are honoured with bounded backoff.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp

from gofile_bot.metadata import read_json

from gofile_bot.config import (
    API_CONCURRENCY,
    API_MAX_ATTEMPTS,
    GOFILE_ACCOUNTS_URL,
    GOFILE_CONTENT_URL,
    GOFILE_LOCALE,
    GOFILE_USER_AGENT,
    GOFILE_WEB_BASE,
    METADATA_TIMEOUT,
    MAX_RESOLVED_FILES,
    RATE_LIMIT_BACKOFF_BASE,
    Settings,
)
from gofile_bot.errors import (
    ContentNotFoundError,
    DownloadError,
    FileTooLargeError,
    GoFileTimeoutError,
    GoFileUnavailableError,
    JobCancelled,
    PasswordIncorrectError,
    PasswordRequiredError,
    RateLimitedError,
    TokenRejectedError,
)
from gofile_bot.gofile.models import (
    Content,
    RemoteFile,
    check_password_state,
    parse_child,
    parse_file,
    parse_folder,
)
from gofile_bot.gofile.token import website_token
from gofile_bot.gofile.urls import is_allowed_download_url

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int | None], Awaitable[None]]

# Settings attribute names (kept as literals for robustness).
_API_TOKEN_ATTR = "gofile_api_token"
_WT_SALT_ATTR = "gofile_wt_salt"


def hash_password(password: str) -> str:
    """GoFile expects the SHA-256 hex of the plaintext password."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _raise_for_status(status: str) -> None:
    """Map a GoFile envelope status to a typed exception."""
    if status == "error-notFound":
        raise ContentNotFoundError(status)
    if status in ("error-passwordRequired", "error-password"):
        raise PasswordRequiredError(status)
    if status in ("error-passwordWrong", "error-passwordError"):
        raise PasswordIncorrectError(status)
    if status == "error-rateLimit":
        raise RateLimitedError(status)
    if status == "error-token":
        raise TokenRejectedError(status)
    raise GoFileUnavailableError(f"GoFile API status: {status}")


class GoFileClient:
    """Small aiohttp-based client for the subset of GoFile the bot needs."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._settings = settings
        self._session = session
        self._owns_session = session is None
        self._api_token: str | None = getattr(settings, _API_TOKEN_ATTR, None)
        self._salt: str = getattr(settings, _WT_SALT_ATTR) or "12af056dacea0b"
        self._account_token: str | None = None
        self._token_lock = asyncio.Lock()
        self._api_semaphore = asyncio.Semaphore(API_CONCURRENCY)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                auto_decompress=False,
                skip_auto_headers={"Accept-Encoding"},
                headers={"User-Agent": GOFILE_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"},
                timeout=aiohttp.ClientTimeout(total=METADATA_TIMEOUT),
            )

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("GoFileClient.start() has not been called")
        return self._session

    # ------------------------------------------------------------------
    # Account token
    # ------------------------------------------------------------------
    async def _ensure_account_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            if self._api_token:
                return self._api_token
            if self._account_token and not force_refresh:
                return self._account_token
            self._account_token = await self._create_guest_account()
            return self._account_token

    async def _create_guest_account(self) -> str:
        """Create a throwaway guest account (documented public entry point)."""
        for attempt in range(1, 3):
            try:
                async with self.session.post(GOFILE_ACCOUNTS_URL) as resp:
                    payload = await read_json(resp)
            except (TimeoutError, aiohttp.ClientError) as exc:
                logger.warning("guest account attempt %d failed: %s", attempt, exc)
                if attempt == 2:
                    raise GoFileTimeoutError(str(exc)) from exc
                await asyncio.sleep(1)
                continue

            if payload.get("status") == "ok":
                token = (payload.get("data") or {}).get("token")
                if token:
                    logger.info("created GoFile guest account for this session")
                    return str(token)
            logger.error("unexpected guest account response: %s", payload.get("status"))
            break
        raise GoFileUnavailableError("could not create a GoFile guest account")

    # ------------------------------------------------------------------
    # Headers / website token
    # ------------------------------------------------------------------
    def _api_headers(self, account_token: str, window_offset: int = 0) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {account_token}",
            "X-Website-Token": website_token(
                account_token,
                salt=self._salt,
                window_offset=window_offset,
            ),
            "X-BL": GOFILE_LOCALE,
            "User-Agent": GOFILE_USER_AGENT,
            "Origin": GOFILE_WEB_BASE,
            "Referer": f"{GOFILE_WEB_BASE}/",
        }

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    async def fetch_content(
        self,
        content_id: str,
        *,
        password: str | None = None,
        max_depth: int = 3,
    ) -> Content:
        """Fetch and parse the share behind *content_id*.

        *password* is the plaintext folder password, if any.
        """
        hashed = hash_password(password) if password else None
        data = await self._fetch_content_data(
            content_id, password=hashed, allow_token_refresh=True
        )
        return await self._parse_tree(data, hashed, depth=0, max_depth=max_depth)

    async def _fetch_content_data(
        self,
        content_id: str,
        *,
        password: str | None,
        window_offset: int = 0,
        allow_token_refresh: bool,
    ) -> dict:
        params: dict[str, str | int] = {
            "cache": "true",
            "sortField": "createTime",
            "sortDirection": 1,
            "page": 1,
            "pageSize": 100,
        }
        if password:
            params["password"] = password

        url = GOFILE_CONTENT_URL.format(content_id=content_id)

        for attempt in range(1, API_MAX_ATTEMPTS + 1):
            account_token = await self._ensure_account_token()
            try:
                async with self._api_semaphore, self.session.get(
                    url,
                    params=params,
                    headers=self._api_headers(account_token, window_offset),
                ) as resp:
                    payload = await read_json(resp)
            except (TimeoutError, aiohttp.ClientError) as exc:
                if attempt < API_MAX_ATTEMPTS:
                    logger.warning("metadata fetch attempt %d failed: %s", attempt, exc)
                    await asyncio.sleep(RATE_LIMIT_BACKOFF_BASE)
                    continue
                raise GoFileTimeoutError(str(exc)) from exc

            status = str(payload.get("status") or "unknown")
            if status == "ok":
                return payload.get("data") or {}

            if status == "error-rateLimit" and attempt < API_MAX_ATTEMPTS:
                delay = RATE_LIMIT_BACKOFF_BASE * attempt
                logger.warning("GoFile rate limit; backing off %ss", delay)
                await asyncio.sleep(delay)
                continue

            if status == "error-notPremium" and window_offset == 0:
                # Near a 4-hour bucket boundary the server clock may lag one
                # window behind ours — retry once with the previous window.
                logger.info("website token window mismatch; retrying previous window")
                window_offset = -1
                continue

            if (
                status == "error-token"
                and allow_token_refresh
                and not self._api_token
            ):
                # Guest tokens can expire; mint a fresh one and retry once.
                logger.info("guest token rejected; refreshing")
                await self._ensure_account_token(force_refresh=True)
                allow_token_refresh = False
                continue

            _raise_for_status(status)

        raise RateLimitedError("exhausted retries")

    async def _parse_tree(
        self, data: dict, password: str | None, *, depth: int, max_depth: int, budget: list[int] | None = None
    ) -> Content:
        check_password_state(data, password_provided=password is not None)

        if data.get("type") != "folder":
            return parse_file(data)

        if budget is None:
            budget = [MAX_RESOLVED_FILES]
        children: list[Content] = []
        raw_children = data.get("children") or {}
        entries = (
            list(raw_children.values()) if isinstance(raw_children, dict) else list(raw_children)
        )
        for entry in entries:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "folder" and depth < max_depth:
                try:
                    nested = await self._fetch_content_data(
                        str(entry.get("id") or ""),
                        password=password,
                        allow_token_refresh=False,
                    )
                    child = await self._parse_tree(
                        nested, password, depth=depth + 1, max_depth=max_depth, budget=budget
                    )
                except GoFileUnavailableError:
                    logger.warning("could not expand subfolder %s", entry.get("id"))
                    child = parse_child(entry)
            else:
                child = parse_child(entry)
            children.append(child)

        return parse_folder(data, children=tuple(children))

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------
    async def download_file(
        self,
        file: RemoteFile,
        dest: Path,
        *,
        max_bytes: int,
        progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> int:
        """Stream *file* to *dest*; returns bytes written.

        Raises :class:`FileTooLargeError` if the transfer would exceed
        *max_bytes*, :class:`JobCancelled` if *cancel_event* is set.
        """
        if not file.link or not is_allowed_download_url(file.link):
            raise DownloadError(f"refusing non-GoFile download URL: {file.link!r}")
        if file.size and file.size > max_bytes:
            raise FileTooLargeError(size=file.size, limit=max_bytes)

        account_token = await self._ensure_account_token()
        headers = {
            "Cookie": f"accountToken={account_token}",
            "User-Agent": GOFILE_USER_AGENT,
            "Referer": f"{GOFILE_WEB_BASE}/",
        }
        # Follow redirects ourselves: aiohttp would otherwise forward the
        # explicit account cookie to an arbitrary redirect target.
        from gofile_bot.downloaders.net import stream_to_file
        from gofile_bot.errors import RemoteDownloadError

        try:
            await stream_to_file(
                self.session, file.link, dest, max_bytes=max_bytes,
                progress=progress, cancel_event=cancel_event, headers=headers,
                allowed_hosts=("gofile.io",), expected_size=file.size or None,
            )
        except RemoteDownloadError as exc:
            raise DownloadError(exc.detail) from exc
        return dest.stat().st_size
