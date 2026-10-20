"""Single-poller lifecycle: 409 Conflict handling and the instance lock."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Conflict, InvalidToken, TimedOut

import gofile_bot.app as app_module
from gofile_bot.app import ALLOWED_UPDATES
from gofile_bot.polling import (
    CONFLICT_ESCALATE_AFTER,
    CONFLICT_STREAK_RESET,
    CONFLICT_WARN_INTERVAL,
    InstanceLock,
    InstanceLockError,
    PollingConflictGuard,
    start_guarded_polling,
)

CONFLICT_MESSAGE = (
    "Conflict: terminated by other getUpdates request; make sure that only "
    "one bot instance is running"
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def guard(clock, caplog) -> PollingConflictGuard:
    caplog.set_level(logging.DEBUG, logger="gofile_bot.polling")
    return PollingConflictGuard(clock=clock)


def _levels(caplog) -> list[int]:
    return [record.levelno for record in caplog.records]


def test_conflict_logs_one_warning_and_no_traceback(guard, caplog):
    for _ in range(5):
        guard(Conflict(CONFLICT_MESSAGE))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Another process is polling this bot" in warnings[0].getMessage()
    assert "only one getupdates consumer" in warnings[0].getMessage().lower()
    assert logging.ERROR not in _levels(caplog)
    assert all(record.exc_info is None for record in caplog.records)
    assert guard.conflicts == 5


def test_quiet_retries_are_debug_and_periodic_reminder_is_a_warning(guard, clock, caplog):
    guard(Conflict(CONFLICT_MESSAGE))  # first warning
    clock.advance(10)
    guard(Conflict(CONFLICT_MESSAGE))  # quiet retry
    assert not [r for r in caplog.records if r.levelno == logging.WARNING][1:]

    clock.advance(CONFLICT_WARN_INTERVAL)
    guard(Conflict(CONFLICT_MESSAGE))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "Still waiting for the other bot instance" in warnings[1].getMessage()
    assert "3 rejected getUpdates calls" in warnings[1].getMessage()


def test_conflict_streak_resets_after_a_quiet_gap(guard, clock, caplog):
    guard(Conflict(CONFLICT_MESSAGE))
    clock.advance(CONFLICT_STREAK_RESET + 10)
    guard(Conflict(CONFLICT_MESSAGE))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # A new streak starts with its own first warning.
    assert "Another process is polling this bot" in warnings[1].getMessage()
    assert guard.conflicts == 1


def test_persistent_conflicts_escalate_once(guard, clock, caplog):
    for _ in range(int(CONFLICT_ESCALATE_AFTER // 30) + 2):
        guard(Conflict(CONFLICT_MESSAGE))
        clock.advance(30)

    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1
    assert "unable to poll" in criticals[0].getMessage()
    assert "another process using the same BOT_TOKEN" in criticals[0].getMessage()
    assert logging.ERROR not in _levels(caplog)


def test_other_polling_errors_stay_visible(guard, caplog):
    guard(TimedOut("long poll timed out"))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "long poll timed out" in errors[0].getMessage()


def test_invalid_token_is_critical(guard, caplog):
    guard(InvalidToken("bad token"))

    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1
    assert "BOT_TOKEN" in criticals[0].getMessage()


async def test_start_guarded_polling_installs_the_guard():
    updater = SimpleNamespace(start_polling=AsyncMock())
    guard = PollingConflictGuard()

    await start_guarded_polling(
        updater, allowed_updates=ALLOWED_UPDATES, guard=guard,
    )

    kwargs = updater.start_polling.await_args.kwargs
    assert kwargs["error_callback"] is guard
    assert kwargs["allowed_updates"] == ALLOWED_UPDATES
    assert kwargs["drop_pending_updates"] is True


def test_instance_lock_allows_only_one_poller_per_token(tmp_path):
    first = InstanceLock("123456:SECRET", tmp_path)
    second = InstanceLock("123456:SECRET", tmp_path)

    assert first.acquire()
    try:
        assert not second.acquire()
        assert not second.held
    finally:
        first.release()

    assert second.acquire()
    second.release()


def test_instance_lock_is_per_token_and_hides_the_secret(tmp_path):
    first = InstanceLock("123456:SECRET", tmp_path)
    other_bot = InstanceLock("654321:OTHER", tmp_path)
    assert first.path != other_bot.path
    assert "SECRET" not in first.path.name

    assert first.acquire()
    try:
        assert other_bot.acquire()  # A different bot is a different poller.
    finally:
        other_bot.release()
        first.release()


async def test_run_async_refuses_a_second_instance(monkeypatch, tmp_path):
    class HeldLock:
        path = tmp_path / "held.lock"

        def acquire(self) -> bool:
            return False

        def release(self) -> None:  # pragma: no cover - never reached
            raise AssertionError("a lock that was not acquired must not be released")

        def busy_message(self) -> str:
            return "Another bot process is already polling with this BOT_TOKEN"

    monkeypatch.setattr(app_module, "InstanceLock", lambda token: HeldLock())
    monkeypatch.setattr(
        app_module, "build_application",
        lambda _: pytest.fail("must not build an application without the lock"),
    )

    with pytest.raises(InstanceLockError, match="already polling"):
        await app_module.run_async(SimpleNamespace(bot_token="123:TEST"))
