"""URL parsing and SSRF-safety tests."""

import pytest

from gofile_bot.errors import InvalidURLError
from gofile_bot.gofile.urls import is_allowed_download_url, parse_share


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://gofile.io/d/AbCdEf12", "AbCdEf12"),
        ("http://gofile.io/d/AbCdEf12", "AbCdEf12"),
        ("https://www.gofile.io/d/AbCdEf12", "AbCdEf12"),
        ("https://gofile.io/d/AbCdEf12/", "AbCdEf12"),
        ("https://gofile.io/d/AbCdEf12?utm=x", "AbCdEf12"),
        ("https://gofile.io/d/AbCdEf12#frag", "AbCdEf12"),
        ("HTTPS://GOFILE.IO/d/AbCdEf12", "AbCdEf12"),
        ("  https://gofile.io/d/AbCdEf12  ", "AbCdEf12"),
        ("https://gofile.io/d/6charCd", "6charCd"),
        (
            "https://gofile.io/d/4991e6d7-5217-46ae-af3d-c9174adae924",
            "4991e6d7-5217-46ae-af3d-c9174adae924",
        ),
        ("AbCdEf12", "AbCdEf12"),  # bare code
    ],
)
def test_parse_share_valid(text, expected):
    assert parse_share(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a url",
        "https://evil.com/d/AbCdEf12",
        "https://gofile.io.evil.com/d/AbCdEf12",
        "https://evil.com/https://gofile.io/d/AbCdEf12",
        "https://gofile.io@evil.com/d/AbCdEf12",
        "https://user:pass@gofile.io/d/AbCdEf12",
        "ftp://gofile.io/d/AbCdEf12",
        "file:///etc/passwd",
        "https://127.0.0.1/d/AbCdEf12",
        "https://gofile.io/f/AbCdEf12",  # wrong path
        "https://gofile.io/d/",  # missing code
        "https://gofile.io/d/ab cd",  # space
        "https://gofile.io/d/..%2f..",  # traversal
        "//gofile.io/d/AbCdEf12",  # protocol-relative
        "gofile.io/d/AbCdEf12",  # no scheme
        "https://evil.com/?u=https://gofile.io/d/AbCdEf12",
    ],
)
def test_parse_share_rejects(text):
    with pytest.raises(InvalidURLError):
        parse_share(text)


@pytest.mark.parametrize(
    "url",
    [
        "https://store-1.gofile.io/download/web/abc/file.pdf",
        "https://store4.gofile.io/download/web/abc/file.pdf",
        "https://gofile.io/download/web/abc/file.pdf",
        "https://a.b.gofile.io/x",
    ],
)
def test_download_url_allowed(url):
    assert is_allowed_download_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://store-1.gofile.io/download/web/abc/f.pdf",  # not https
        "https://evil.com/download/web/abc/f.pdf",
        "https://gofile.io.evil.com/x",
        "https://evilgofile.io/x",
        "https://user@store-1.gofile.io/x",
        "file:///etc/passwd",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://store-1.gofile.io@evil.com/x",
        "not-a-url",
    ],
)
def test_download_url_rejected(url):
    assert is_allowed_download_url(url) is False
