"""Downloader provider tests: routing, parsing, SSRF guard, streaming.

All network access is mocked (aioresponses) or replaced with fake resolvers;
no live calls to any file host.
"""

import re
from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses

from gofile_bot.config import Settings
from gofile_bot.downloaders import build_registry
from gofile_bot.downloaders.archive_org import (
    extract_item_ref,
    is_system_file,
    parse_metadata,
)
from gofile_bot.downloaders.base import host_matches
from gofile_bot.downloaders.direct import DirectDownloader, is_web_page
from gofile_bot.downloaders.dropbox import to_download_url
from gofile_bot.downloaders.gdrive import (
    export_download_url,
    extract_drive_ref,
    parse_confirm_form,
)
from gofile_bot.downloaders.mediafire import (
    extract_file_key,
    extract_from_html,
    normalize_page_url,
)
from gofile_bot.downloaders.net import (
    filename_from_disposition,
    parse_size_text,
    stream_to_file,
    validate_public_url,
)
from gofile_bot.downloaders.onedrive import (
    download_candidates,
    encode_sharing_url,
    graph_content_url,
    live_download_url,
)
from gofile_bot.downloaders.pixeldrain import (
    extract_pixeldrain_ref,
    parse_file_info,
    parse_list_info,
)
from gofile_bot.errors import RemoteDownloadError, ResolveError
from gofile_bot.gofile.models import RemoteFile, RemoteFolder


async def _public_resolver(host: str):
    return ["93.184.216.34"]


async def _private_resolver(host: str):
    return ["10.0.0.7"]


# ---------------------------------------------------------------------------
# Registry routing
# ---------------------------------------------------------------------------
class _FakeGoFile:
    async def fetch_content(self, content_id, password=None):  # pragma: no cover
        raise NotImplementedError

    async def download_file(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def _registry(**settings_kw):
    return build_registry(Settings(bot_token="t", **settings_kw), _FakeGoFile())


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://gofile.io/d/AbCdEf12", "gofile"),
        ("https://drive.google.com/file/d/1AbC_dEf-GhI/view?usp=sharing", "gdrive"),
        ("https://docs.google.com/document/d/1AbC_dEf-GhI/edit", "gdrive"),
        ("https://www.dropbox.com/scl/fi/abc123/file.zip?rlkey=x&dl=0", "dropbox"),
        ("https://1drv.ms/b/s!AexampleShareId123", "onedrive"),
        ("https://contoso-my.sharepoint.com/:b:/g/personal/x/abc", "onedrive"),
        ("https://pixeldrain.com/u/abc12345", "pixeldrain"),
        ("https://pixeldrain.com/l/abc12345", "pixeldrain"),
        ("https://www.mediafire.com/file/abc123xyz/report.pdf/file", "mediafire"),
        ("https://archive.org/details/some-item", "archive"),
        ("https://example.com/files/archive.zip", "direct"),
        ("https://github.com/owner/repo/releases/download/v1/app.apk", "direct"),
    ],
)
def test_registry_routing(url, provider):
    found = _registry().find(url)
    assert found is not None and found.name == provider


def test_registry_direct_can_be_disabled():
    registry = _registry(enable_direct_downloads=False)
    assert registry.find("https://example.com/file.zip") is None
    assert registry.get("gofile") is not None


def test_host_matches():
    assert host_matches("https://www.gofile.io/d/x", ("gofile.io",))
    assert host_matches("https://gofile.io/d/x", ("gofile.io",))
    assert not host_matches("https://gofile.io.evil.com/d/x", ("gofile.io",))
    assert not host_matches("https://evil.com/?gofile.io", ("gofile.io",))


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------
async def test_validate_public_url_ok():
    url = "https://example.com/file.zip"
    assert await validate_public_url(url, resolver=_public_resolver) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.5/file",
        "http://192.168.1.1/file",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/file",
        "http://localhost/file",
        "http://internal.local/file",
        "https://user:pass@example.com/file",
        "ftp://example.com/file",
        "file:///etc/passwd",
    ],
)
async def test_validate_public_url_blocks_non_public(url):
    with pytest.raises(ResolveError):
        await validate_public_url(url, resolver=_public_resolver)


async def test_validate_public_url_blocks_private_dns():
    with pytest.raises(ResolveError):
        await validate_public_url(
            "https://sneaky.example.com/file", resolver=_private_resolver
        )


# ---------------------------------------------------------------------------
# Streaming / probing
# ---------------------------------------------------------------------------
async def test_stream_to_file_happy_path(tmp_path: Path):
    dest = tmp_path / "out.bin"
    with aioresponses() as m:
        m.get(
            "https://example.com/file.bin",
            body=b"x" * 5000,
            headers={"Content-Length": "5000"},
        )
        async with aiohttp.ClientSession() as session:
            result = await stream_to_file(
                session,
                "https://example.com/file.bin",
                dest,
                max_bytes=10_000,
                public_only=True,
                resolver=_public_resolver,
            )
    assert result == dest
    assert dest.read_bytes() == b"x" * 5000


async def test_stream_to_file_redirect_to_private_blocked(tmp_path: Path):
    dest = tmp_path / "out.bin"
    with aioresponses() as m:
        m.get(
            "https://example.com/jump",
            status=302,
            headers={"Location": "http://169.254.169.254/secret"},
        )
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ResolveError):
                await stream_to_file(
                    session,
                    "https://example.com/jump",
                    dest,
                    max_bytes=10_000,
                    public_only=True,
                    resolver=_public_resolver,
                )
    assert not dest.exists()


async def test_stream_to_file_allowlist_blocks_other_hosts(tmp_path: Path):
    dest = tmp_path / "out.bin"
    async with aiohttp.ClientSession() as session:
        with pytest.raises(RemoteDownloadError):
            await stream_to_file(
                session,
                "https://evil.example.com/file.bin",
                dest,
                max_bytes=10_000,
                allowed_hosts=("pixeldrain.com",),
            )


# ---------------------------------------------------------------------------
# Direct provider
# ---------------------------------------------------------------------------
async def test_direct_resolve_file():
    provider = DirectDownloader(resolver=_public_resolver)
    try:
        with aioresponses() as m:
            m.head(
                "https://example.com/files/archive.zip",
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": "1234",
                },
            )
            content = await provider.resolve("https://example.com/files/archive.zip")
    finally:
        await provider.close()
    assert isinstance(content, RemoteFile)
    assert content.name == "archive.zip"
    assert content.size == 1234
    assert content.mimetype == "application/zip"


async def test_direct_resolve_rejects_web_page():
    provider = DirectDownloader(resolver=_public_resolver)
    try:
        with aioresponses() as m:
            m.head(
                "https://example.com/article",
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
            with pytest.raises(ResolveError) as exc_info:
                await provider.resolve("https://example.com/article")
    finally:
        await provider.close()
    assert "web page" in exc_info.value.user_message


def test_is_web_page():
    assert is_web_page({"Content-Type": "text/html"})
    assert not is_web_page(
        {"Content-Type": "text/html", "Content-Disposition": "attachment"}
    )
    assert not is_web_page({"Content-Type": "application/zip"})


# ---------------------------------------------------------------------------
# Google Drive parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://drive.google.com/file/d/1AbC_dEf-GhIjK/view?usp=sharing", "file"),
        ("https://drive.google.com/open?id=1AbC_dEf-GhIjK", "file"),
        ("https://drive.google.com/uc?export=download&id=1AbC_dEf-GhIjK", "file"),
        ("https://drive.google.com/drive/folders/1AbC_dEf-GhIjK", "folder"),
        ("https://docs.google.com/document/d/1AbC_dEf-GhIjK/edit", "document"),
        ("https://docs.google.com/spreadsheets/d/1AbC_dEf-GhIjK/edit", "spreadsheets"),
        ("https://docs.google.com/presentation/d/1AbC_dEf-GhIjK/edit", "presentation"),
    ],
)
def test_extract_drive_ref(url, kind):
    ref = extract_drive_ref(url)
    assert ref is not None and ref[0] == kind and ref[1] == "1AbC_dEf-GhIjK"


def test_extract_drive_ref_rejects_others():
    assert extract_drive_ref("https://example.com/file/d/1AbC_dEf-GhIjK") is None
    assert extract_drive_ref("https://drive.google.com/drive/my-drive") is None


def test_drive_export_url():
    assert export_download_url("document", "X").endswith("/export?format=pdf")
    assert export_download_url("spreadsheets", "X").endswith("/export?format=xlsx")


def test_parse_confirm_form():
    html = """
    <form id="download-form" action="https://drive.usercontent.google.com/download">
      <input type="hidden" name="id" value="FILEID">
      <input type="hidden" name="export" value="download">
      <input type="hidden" name="confirm" value="t">
      <input type="hidden" name="uuid" value="abc-123">
    </form>
    """
    url = parse_confirm_form(html)
    assert url is not None
    assert url.startswith("https://drive.usercontent.google.com/download?")
    assert "id=FILEID" in url and "confirm=t" in url and "uuid=abc-123" in url
    assert parse_confirm_form("<html>no form</html>") is None


# ---------------------------------------------------------------------------
# Dropbox / OneDrive transforms
# ---------------------------------------------------------------------------
def test_dropbox_to_download_url():
    assert to_download_url(
        "https://www.dropbox.com/scl/fi/abc/f.zip?rlkey=x&dl=0"
    ) == "https://www.dropbox.com/scl/fi/abc/f.zip?rlkey=x&dl=1"
    assert to_download_url("https://www.dropbox.com/s/abc/f.zip").endswith("?dl=1")
    direct = "https://dl.dropboxusercontent.com/s/abc/f.zip"
    assert to_download_url(direct) == direct


def test_onedrive_sharing_token_and_candidates():
    token = encode_sharing_url("https://1drv.ms/b/s!ABC")
    assert token.startswith("u!")
    assert "=" not in token
    assert graph_content_url("https://1drv.ms/b/s!ABC").endswith("/driveItem/content")

    view = "https://onedrive.live.com/?cid=CID123&id=RESID456&authkey=!KEY"
    assert live_download_url(view) == (
        "https://onedrive.live.com/download?cid=CID123&resid=RESID456&authkey=!KEY"
    )
    candidates = download_candidates("https://1drv.ms/b/s!ABC", view)
    assert candidates[0].startswith("https://graph.microsoft.com/")
    assert any("onedrive.live.com/download" in c for c in candidates)
    assert live_download_url("https://example.com/?cid=x&id=y") is None


async def test_onedrive_folder_rejected_without_network():
    from gofile_bot.downloaders.onedrive import OneDriveDownloader

    provider = OneDriveDownloader()
    with pytest.raises(ResolveError):
        await provider.resolve("https://contoso.sharepoint.com/:f:/g/personal/x/abc")


# ---------------------------------------------------------------------------
# MediaFire / Pixeldrain / Archive parsing
# ---------------------------------------------------------------------------
def test_mediafire_extract():
    html = """
    <html><body>
      <div class="filename">my report.pdf</div>
      <a id="downloadButton"
         href="https://download1234.mediafire.com/xyz/my+report.pdf">Download (2.5 MB)</a>
    </body></html>
    """
    page = "https://www.mediafire.com/file/abc123xyz/my_report.pdf/file"
    file = extract_from_html(html, page)
    assert file.name == "my report.pdf"
    assert file.link.startswith("https://download1234.mediafire.com/")
    assert file.size == int(2.5 * 1024 * 1024)
    assert extract_file_key(page) == "abc123xyz"


def test_mediafire_view_links_normalize_to_file_page():
    view = "https://www.mediafire.com/view/abc123xyz/picture.jpeg/file"
    assert extract_file_key(view) == "abc123xyz"
    assert normalize_page_url(view) == (
        "https://www.mediafire.com/file/abc123xyz/picture.jpeg/file"
    )
    # Regular file links are left untouched.
    regular = "https://www.mediafire.com/file/abc123xyz/picture.jpeg/file"
    assert normalize_page_url(regular) == regular


def test_mediafire_extract_deleted():
    from gofile_bot.errors import ContentNotFoundError

    with pytest.raises(ContentNotFoundError):
        extract_from_html(
            "<html>Invalid or Deleted File</html>",
            "https://www.mediafire.com/file/abc/x/file",
        )


def test_pixeldrain_parse():
    assert extract_pixeldrain_ref("https://pixeldrain.com/u/abc12345") == (
        "file",
        "abc12345",
    )
    assert extract_pixeldrain_ref("https://pixeldrain.com/l/xyz98765") == (
        "list",
        "xyz98765",
    )
    file = parse_file_info(
        {"id": "abc12345", "name": "a.zip", "size": 42, "mime_type": "application/zip"}
    )
    assert file.name == "a.zip" and file.size == 42
    assert file.link == "https://pixeldrain.com/api/file/abc12345?download"

    folder = parse_list_info(
        {
            "id": "xyz98765",
            "title": "My list",
            "files": [
                {"id": "f1", "name": "a.txt", "size": 1},
                {"id": "f2", "name": "b.txt", "size": 2},
            ],
        }
    )
    assert isinstance(folder, RemoteFolder)
    assert folder.file_count == 2


async def test_pixeldrain_resolve_via_api():
    from gofile_bot.downloaders.pixeldrain import PixeldrainDownloader

    provider = PixeldrainDownloader()
    try:
        with aioresponses() as m:
            m.get(
                re.compile(r"https://pixeldrain\.com/api/file/abc12345/info"),
                payload={
                    "success": True,
                    "id": "abc12345",
                    "name": "song.mp3",
                    "size": 999,
                    "mime_type": "audio/mpeg",
                },
            )
            content = await provider.resolve("https://pixeldrain.com/u/abc12345")
    finally:
        await provider.close()
    assert isinstance(content, RemoteFile)
    assert content.name == "song.mp3"


def test_archive_parse():
    assert extract_item_ref("https://archive.org/details/myitem") == ("myitem", None)
    assert extract_item_ref("https://archive.org/download/myitem/book.pdf") == (
        "myitem",
        "book.pdf",
    )
    assert extract_item_ref("https://example.org/details/myitem") is None
    assert is_system_file("myitem_meta.xml")
    assert not is_system_file("book.pdf")

    data = {
        "metadata": {"identifier": "myitem", "title": "My Item"},
        "files": [
            {"name": "book.pdf", "size": "5000"},
            {"name": "myitem_meta.xml", "size": "100"},
            {"name": "song.mp3", "size": "7000"},
        ],
    }
    folder = parse_metadata(data)
    assert isinstance(folder, RemoteFolder)
    assert [f.name for f in folder.files()] == ["book.pdf", "song.mp3"]

    single = parse_metadata(data, "book.pdf")
    assert isinstance(single, RemoteFile)
    assert single.link == "https://archive.org/download/myitem/book.pdf"


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------
def test_filename_from_disposition():
    assert filename_from_disposition('attachment; filename="a b.zip"') == "a b.zip"
    assert (
        filename_from_disposition("attachment; filename*=UTF-8''caf%C3%A9.pdf")
        == "café.pdf"
    )
    assert filename_from_disposition(None) is None
    assert filename_from_disposition("inline") is None


def test_parse_size_text():
    assert parse_size_text("2.5 MB") == int(2.5 * 1024**2)
    assert parse_size_text("900 KB") == 900 * 1024
    assert parse_size_text("no size here") == 0
    assert parse_size_text(None) == 0
