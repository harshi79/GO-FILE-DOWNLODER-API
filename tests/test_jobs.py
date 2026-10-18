"""Job store tests."""

import time

from gofile_bot.services.jobs import JobStore


def test_create_and_get():
    store = JobStore()
    job = store.create(
        user_id=1,
        chat_id=2,
        status_message_id=3,
        content_id="abc",
        password=None,
    )
    assert store.get(job.id) is job
    assert job.user_id == 1
    assert job.content is None
    assert not job.running


def test_unknown_job():
    store = JobStore()
    assert store.get("nope") is None


def test_active_for_user():
    store = JobStore()
    job = store.create(
        user_id=1, chat_id=2, status_message_id=3, content_id="abc", password=None
    )
    assert store.active_for_user(1) is None
    job.running = True
    assert store.active_for_user(1) is job
    assert store.active_for_user(2) is None
    job.running = False
    assert store.active_for_user(1) is None


def test_ttl_expiry():
    store = JobStore(ttl=0.05)
    job = store.create(
        user_id=1, chat_id=2, status_message_id=3, content_id="abc", password=None
    )
    time.sleep(0.06)
    assert store.get(job.id) is None
    assert len(store) == 0


def test_purge():
    store = JobStore(ttl=0.05)
    store.create(
        user_id=1, chat_id=2, status_message_id=3, content_id="abc", password=None
    )
    time.sleep(0.06)
    store.purge_expired()
    assert len(store) == 0
