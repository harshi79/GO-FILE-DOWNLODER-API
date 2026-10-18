"""Generation of GoFile's rotating client-side website token (``X-Website-Token``).

GoFile's web client derives its website token in the browser as::

    sha256(f"{userAgent}::{language}::{accountToken}::{window}::{salt}")

where ``window = unix_time // 14400`` is a 4-hour bucket and ``salt`` is a
value embedded in GoFile's public client script. The server recomputes the
same value, so the ``User-Agent`` and ``X-BL`` headers sent along with the
token must match the values that were hashed into it.

This reproduces exactly what GoFile's own public web client does for ordinary
visitors of a public share page — it is not a bypass of any authentication:
public shares have none, and password-protected shares still require the
password.
"""

from __future__ import annotations

import hashlib
import time

from gofile_bot.config import (
    GOFILE_LOCALE,
    GOFILE_USER_AGENT,
    GOFILE_WT_WINDOW_SECONDS,
)


def website_token(
    account_token: str,
    *,
    salt: str,
    window_offset: int = 0,
    user_agent: str = GOFILE_USER_AGENT,
    locale: str = GOFILE_LOCALE,
    now: float | None = None,
) -> str:
    """Return the 64-char hex ``X-Website-Token`` for *account_token*.

    ``window_offset`` shifts the 4-hour bucket (use ``-1`` near a bucket
    boundary when the server clock lags). ``now`` exists for tests.
    """
    timestamp = time.time() if now is None else now
    time_window = int(timestamp) // GOFILE_WT_WINDOW_SECONDS + window_offset
    seed = f"{user_agent}::{locale}::{account_token}::{time_window}::{salt}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()
