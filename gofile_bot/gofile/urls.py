"""Strict parsing of user-supplied GoFile links (SSRF protection).

Only genuine ``gofile.io`` share links are accepted. The bot never fetches a
user-supplied host: request targets are always GoFile API endpoints built from
validated content ids, and download URLs returned by GoFile are re-checked with
:func:`is_allowed_download_url` before being requested.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from gofile_bot.errors import InvalidURLError

#: Share links look like https://gofile.io/d/<code> (6-char legacy codes,
#: 8-char current codes, and historical UUID-style ids).
_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,63}$")

#: Absolute share URL, optionally with trailing slash, query or fragment.
_SHARE_URL_RE = re.compile(
    r"^https?://(?:www\.)?gofile\.io/d/([A-Za-z0-9][A-Za-z0-9-]{3,63})"
    r"/?(?:[?#][^\s]*)?$",
    re.IGNORECASE,
)


def parse_share(text: str) -> str:
    """Extract a GoFile content id from a share URL or bare code.

    Accepts ``https://gofile.io/d/<code>``, ``http://``, ``www.``, trailing
    slashes/query strings, and bare content codes. Anything else — including
    other hosts, userinfo tricks, or embedded URLs — raises
    :class:`InvalidURLError`.
    """
    candidate = (text or "").strip()
    if not candidate:
        raise InvalidURLError("empty URL")

    match = _SHARE_URL_RE.match(candidate)
    if match:
        content_id = match.group(1)
        if _CONTENT_ID_RE.match(content_id):
            return content_id
        raise InvalidURLError(f"bad content id in {candidate!r}")

    # Bare content code only (letters/digits/dash, no scheme or slashes).
    if _CONTENT_ID_RE.match(candidate) and "://" not in candidate:
        return candidate

    raise InvalidURLError(f"unsupported URL: {candidate!r}")


def is_allowed_download_url(url: str) -> bool:
    """Return True when *url* is an https URL on a gofile.io host.

    Defence-in-depth: even though download links come from the GoFile API
    response (not from user input), every outbound download request must pass
    this check so the bot can never be steered to an arbitrary host.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password:
        return False

    host = (parsed.hostname or "").lower()
    from gofile_bot.config import (  # local import avoids a cycle at module load
        GOFILE_DOWNLOAD_HOST_SUFFIX,
    )

    return host == "gofile.io" or host.endswith(GOFILE_DOWNLOAD_HOST_SUFFIX)
