import aiohttp
import pytest
from aioresponses import aioresponses

from gofile_bot.downloaders.pasteview import PasteviewDownloader, extract_paste, paste_id
from gofile_bot.errors import ContentNotFoundError, FileTooLargeError, JobCancelled, ResolveError


@pytest.mark.parametrize('url', [
    'https://pasteview.com/AbCd123', 'https://www.pasteview.com/AbCd123/',
])
def test_urls(url):
    assert paste_id(url) == 'AbCd123'


@pytest.mark.parametrize('url', [
    'https://pasteview.com.evil.test/AbCd123', 'https://evil.pasteview.com/AbCd123',
    'https://pasteview.com/discover', 'https://pasteview.com/',
    'https://user@pasteview.com/AbCd123', 'ftp://pasteview.com/AbCd123',
    'https://pasteview.com:8080/AbCd123',
])
def test_invalid_urls(url):
    assert paste_id(url) is None


def test_extract():
    assert extract_paste('<nav>Ignore</nav><pre><code>hello &amp; world\nλ</code></pre>') == 'hello & world\nλ'
    for page in ('<html>Login required</html>', '<pre>a</pre><pre>b</pre>'):
        with pytest.raises(ResolveError):
            extract_paste(page)


@pytest.mark.asyncio
async def test_resolve_download(tmp_path):
    url = 'https://pasteview.com/AbCd123'
    async with aiohttp.ClientSession() as session:
        provider = PasteviewDownloader(session=session)
        with aioresponses() as mock:
            mock.get(url, body='<pre>hello &amp; λ\n</pre>', repeat=True)
            file = await provider.resolve(url)
            assert file.name == 'pasteview-AbCd123.txt'
            assert file.size == len('hello & λ\n'.encode())
            dest = tmp_path / file.name
            await provider.download(file, dest, max_bytes=100)
            assert dest.read_text() == 'hello & λ\n'
            with pytest.raises(FileTooLargeError):
                await provider.download(file, tmp_path / 'too-large', max_bytes=1)
            import asyncio
            event = asyncio.Event()
            event.set()
            with pytest.raises(JobCancelled):
                await provider.download(file, dest, max_bytes=100, cancel_event=event)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error', [(404, ContentNotFoundError), (302, ResolveError), (403, ResolveError)])
async def test_http_errors(status, error):
    url = 'https://pasteview.com/AbCd123'
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.get(url, status=status, headers={'Location': 'http://127.0.0.1/'})
            with pytest.raises(error):
                await PasteviewDownloader(session=session).resolve(url)


def test_registry():
    from gofile_bot.config import Settings
    from gofile_bot.downloaders import build_registry
    from gofile_bot.telegram.handlers import extract_first_url

    registry = build_registry(Settings(bot_token='t', enable_direct_downloads=False), None)
    assert registry.find('https://pasteview.com/AbCd123').name == 'pasteview'
    assert extract_first_url('pasteview.com/AbCd123') == 'https://pasteview.com/AbCd123'
