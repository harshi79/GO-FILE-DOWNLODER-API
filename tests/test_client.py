"""GoFile client tests against a mocked aiohttp transport (aioresponses)."""

import hashlib
import re
from pathlib import Path

import pytest
from aioresponses import aioresponses

import gofile_bot.gofile.client as client_module
from gofile_bot.errors import (
    ContentNotFoundError,
    DownloadError,
    FileTooLargeError,
    JobCancelled,
    PasswordRequiredError,
    RateLimitedError,
)
from gofile_bot.gofile.client import GoFileClient
from gofile_bot.gofile.models import RemoteFile, RemoteFolder

ACCOUNTS = re.compile(r"https://api\.gofile\.io/accounts")
CONTENTS = re.compile(r"https://api\.gofile\.io/contents/.*")
DOWNLOAD = re.compile(r"https://store-1\.gofile\.io/download/web/.*")

ACCOUNT_OK = {"status": "ok", "data": {"token": "guest-token"}}

FILE_DATA = {
    "id": "fid",
    "type": "file",
    "name": "doc.pdf",
    "size": 1000,
    "mimetype": "application/pdf",
    "link": "https://store-1.gofile.io/download/web/fid/doc.pdf",
    "md5": "m",
}

FOLDER_DATA = {
    "id": "gid",
    "type": "folder",
    "name": "Album",
    "children": {
        "fid": dict(FILE_DATA),
        "subid": {"id": "subid", "type": "folder", "name": "Sub"},
    },
}

SUBFOLDER_DATA = {
    "id": "subid",
    "type": "folder",
    "name": "Sub",
    "children": {"fid2": {**FILE_DATA, "id": "fid2", "name": "inner.txt"}},
}


def make_client(settings):
    return GoFileClient(settings)


async def fetch(settings, mocks, content_id, **kw):
    client = make_client(settings)
    await client.start()
    try:
        return await client.fetch_content(content_id, **kw)
    finally:
        await client.close()


async def test_fetch_file_ok(settings):
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "ok", "data": FILE_DATA})
        content = await fetch(settings, m, "abc")
    assert isinstance(content, RemoteFile)
    assert content.name == "doc.pdf"
    assert content.size == 1000


async def test_fetch_not_found(settings):
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "error-notFound"})
        with pytest.raises(ContentNotFoundError):
            await fetch(settings, m, "abc")


async def test_fetch_password_required(settings):
    data = {**FOLDER_DATA, "password": "x", "passwordStatus": "passwordRequired"}
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "ok", "data": data})
        with pytest.raises(PasswordRequiredError):
            await fetch(settings, m, "abc")


async def test_fetch_rate_limit_backs_off(settings, monkeypatch):
    monkeypatch.setattr(client_module, "RATE_LIMIT_BACKOFF_BASE", 0.01)
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "error-rateLimit"})
        m.get(CONTENTS, payload={"status": "ok", "data": FILE_DATA})
        content = await fetch(settings, m, "abc")
    assert isinstance(content, RemoteFile)


async def test_fetch_rate_limit_exhausted(settings, monkeypatch):
    monkeypatch.setattr(client_module, "RATE_LIMIT_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(client_module, "API_MAX_ATTEMPTS", 2)
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "error-rateLimit"})
        m.get(CONTENTS, payload={"status": "error-rateLimit"})
        with pytest.raises(RateLimitedError):
            await fetch(settings, m, "abc")


async def test_fetch_retries_previous_token_window(settings):
    """error-notPremium on the current window is retried with window_offset=-1."""
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "error-notPremium"})
        m.get(CONTENTS, payload={"status": "ok", "data": FILE_DATA})
        content = await fetch(settings, m, "abc")
    assert isinstance(content, RemoteFile)


async def test_fetch_refreshes_expired_guest_token(settings):
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "error-token"})
        m.post(ACCOUNTS, payload={"status": "ok", "data": {"token": "guest-token-2"}})
        m.get(CONTENTS, payload={"status": "ok", "data": FILE_DATA})
        content = await fetch(settings, m, "abc")
    assert isinstance(content, RemoteFile)


async def test_fetch_folder_with_subfolder(settings):
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "ok", "data": FOLDER_DATA})
        m.get(CONTENTS, payload={"status": "ok", "data": SUBFOLDER_DATA})
        content = await fetch(settings, m, "gid")
    assert isinstance(content, RemoteFolder)
    assert content.file_count == 2
    names = {f.name for f in content.files()}
    assert names == {"doc.pdf", "inner.txt"}


async def test_password_is_hashed_in_request(settings):
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(CONTENTS, payload={"status": "ok", "data": FILE_DATA})
        await fetch(settings, m, "abc", password="secret")

    calls = [
        url
        for (method, url) in m.requests
        if method == "GET" and "/contents/" in str(url)
    ]
    assert calls, "contents request was not recorded"
    expected = hashlib.sha256(b"secret").hexdigest()
    assert expected in str(calls[0])


async def test_configured_token_skips_guest_account(settings):
    from gofile_bot.config import Settings

    premium = Settings(bot_token="t", gofile_api_token="premium-token")
    with aioresponses() as m:
        m.get(CONTENTS, payload={"status": "ok", "data": FILE_DATA})
        content = await fetch(premium, m, "abc")
    assert isinstance(content, RemoteFile)
    assert not any(method == "POST" for (method, _) in m.requests)


async def test_download_writes_file(settings, tmp_path: Path):
    body = b"x" * 1000
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(DOWNLOAD, body=body, headers={"Content-Length": "1000"})
        client = make_client(settings)
        await client.start()
        try:
            file = RemoteFile(
                id="fid",
                name="doc.pdf",
                size=1000,
                mimetype="application/pdf",
                link="https://store-1.gofile.io/download/web/fid/doc.pdf",
            )
            seen: list[tuple[int, int | None]] = []

            async def progress(done, total):
                seen.append((done, total))

            dest = tmp_path / "doc.pdf"
            written = await client.download_file(
                file, dest, max_bytes=10_000, progress=progress
            )
        finally:
            await client.close()

    assert written == 1000
    assert dest.read_bytes() == body
    assert seen[-1] == (1000, 1000)
    assert not dest.with_suffix(".pdf.part").exists()


async def test_download_rejects_foreign_url(settings, tmp_path: Path):
    client = make_client(settings)
    await client.start()
    try:
        file = RemoteFile(
            id="f", name="x", size=1, mimetype="x", link="https://evil.com/x"
        )
        with pytest.raises(DownloadError):
            await client.download_file(file, tmp_path / "x", max_bytes=100)
    finally:
        await client.close()


async def test_download_too_large_from_headers(settings, tmp_path: Path):
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(DOWNLOAD, body=b"x" * 500, headers={"Content-Length": "500"})
        client = make_client(settings)
        await client.start()
        try:
            file = RemoteFile(
                id="fid",
                name="doc.pdf",
                size=500,
                mimetype="application/pdf",
                link="https://store-1.gofile.io/download/web/fid/doc.pdf",
            )
            with pytest.raises(FileTooLargeError):
                await client.download_file(file, tmp_path / "d", max_bytes=100)
        finally:
            await client.close()


async def test_download_too_large_from_metadata(settings, tmp_path: Path):
    client = make_client(settings)
    await client.start()
    try:
        file = RemoteFile(
            id="fid",
            name="big.bin",
            size=10_000,
            mimetype="application/octet-stream",
            link="https://store-1.gofile.io/download/web/fid/big.bin",
        )
        with pytest.raises(FileTooLargeError):
            await client.download_file(file, tmp_path / "b", max_bytes=100)
    finally:
        await client.close()


async def test_download_cancel(settings, tmp_path: Path):
    import asyncio

    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(DOWNLOAD, body=b"x" * 100, headers={"Content-Length": "100"})
        client = make_client(settings)
        await client.start()
        try:
            file = RemoteFile(
                id="fid",
                name="doc.pdf",
                size=100,
                mimetype="application/pdf",
                link="https://store-1.gofile.io/download/web/fid/doc.pdf",
            )
            event = asyncio.Event()
            event.set()
            with pytest.raises(JobCancelled):
                await client.download_file(
                    file, tmp_path / "c", max_bytes=1000, cancel_event=event
                )
        finally:
            await client.close()


async def test_download_http_error(settings, tmp_path: Path):
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(DOWNLOAD, status=404, body=b"nope")
        client = make_client(settings)
        await client.start()
        try:
            file = RemoteFile(
                id="fid",
                name="doc.pdf",
                size=1,
                mimetype="application/pdf",
                link="https://store-1.gofile.io/download/web/fid/doc.pdf",
            )
            with pytest.raises(DownloadError):
                await client.download_file(file, tmp_path / "e", max_bytes=1000)
        finally:
            await client.close()


async def test_download_rejects_redirect_to_foreign_host(settings, tmp_path):
    """Never forward an accountToken cookie outside the GoFile allowlist."""
    with aioresponses() as m:
        m.post(ACCOUNTS, payload=ACCOUNT_OK)
        m.get(DOWNLOAD, status=302, headers={"Location": "https://evil.com/steal"})
        client = make_client(settings)
        await client.start()
        try:
            file = RemoteFile(id="fid", name="doc.pdf", size=1, mimetype="application/pdf", link="https://store-1.gofile.io/download/web/fid/doc.pdf")
            with pytest.raises(DownloadError):
                await client.download_file(file, tmp_path / "file", max_bytes=100)
            assert not any(str(url).startswith("https://evil.com") for _, url in m.requests)
        finally:
            await client.close()
