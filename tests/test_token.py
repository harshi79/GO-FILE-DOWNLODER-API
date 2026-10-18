"""Website token generation tests."""

import hashlib

from gofile_bot.config import GOFILE_WT_WINDOW_SECONDS
from gofile_bot.gofile.token import website_token


def test_matches_reference_formula():
    token = website_token(
        "acct-token",
        salt="12af056dacea0b",
        user_agent="Mozilla/5.0",
        locale="en-US",
        now=1_700_000_000,
    )
    window = 1_700_000_000 // GOFILE_WT_WINDOW_SECONDS
    seed = f"Mozilla/5.0::en-US::acct-token::{window}::12af056dacea0b"
    assert token == hashlib.sha256(seed.encode()).hexdigest()
    assert len(token) == 64


def test_deterministic_within_window():
    t1 = website_token("acct", salt="s", now=1_700_000_000)
    t2 = website_token("acct", salt="s", now=1_700_000_000 + 60)
    assert t1 == t2


def test_window_offset_changes_token():
    base = 1_700_000_000
    t0 = website_token("acct", salt="s", window_offset=0, now=base)
    t1 = website_token("acct", salt="s", window_offset=-1, now=base)
    assert t0 != t1


def test_window_boundary_rotates():
    window_start = GOFILE_WT_WINDOW_SECONDS * 100
    before = website_token("acct", salt="s", now=window_start - 1)
    after = website_token("acct", salt="s", now=window_start)
    assert before != after


def test_salt_and_account_change_token():
    a = website_token("acct", salt="s1", now=0)
    b = website_token("acct", salt="s2", now=0)
    c = website_token("other", salt="s1", now=0)
    assert len({a, b, c}) == 3
