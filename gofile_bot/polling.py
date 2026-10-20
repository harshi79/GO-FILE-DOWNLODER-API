"""Single-poller lifecycle: process lock and Conflict-aware polling errors.

Telegram allows exactly **one** ``getUpdates`` consumer per bot token. When a
second process polls with the same token, Telegram terminates the long poll
that was already waiting and answers it with::

    409 Conflict: terminated by other getUpdates request

That is a *routing* condition, not a crash: the usual cause is a zero-downtime
deploy that runs the old and the new container side by side for a few seconds
(the new one wins, the old one is about to be stopped), but it also happens
when a stale container ignores SIGTERM or when a local ``python -m gofile_bot``
shares ``BOT_TOKEN`` with the deployed instance.

python-telegram-bot already retries forever. Its default callback, however,
renders every single attempt as an ``ERROR`` with a full traceback, so an
expected deploy overlap looks like a failure. :class:`PollingConflictGuard`
replaces that noise with one actionable warning, quiet retries, a periodic
reminder while the duplicate persists, and a single critical message when it
never clears.

:class:`InstanceLock` covers the other half: two bot processes on the *same*
filesystem must never poll at the same time, so the second one fails fast with
a clear message instead of fighting over updates. Container filesystems are
isolated, so a duplicate *deployment* must still be stopped on the platform.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from telegram.error import Conflict, InvalidToken, TelegramError
from telegram.ext import Updater

try:  # POSIX only; Windows has no ``fcntl`` module.
    import fcntl
except ImportError:  # pragma: no cover - only reached on Windows
    fcntl = None

logger = logging.getLogger(__name__)

#: Re-warn while conflicts persist at most this often (seconds).
CONFLICT_WARN_INTERVAL = 60.0

#: A conflict-free gap this long means the duplicate is gone and the streak
#: ended. python-telegram-bot backs off to at most 30 s between attempts, so
#: 120 s is comfortably longer than any retry inside one conflict streak.
CONFLICT_STREAK_RESET = 120.0

#: Escalate to CRITICAL when polling keeps losing for this long (seconds).
CONFLICT_ESCALATE_AFTER = 300.0

_CONFLICT_HINT = (
    "Telegram allows only one getUpdates consumer per bot token. This instance "
    "is on standby and retries automatically. If it does not take over within "
    "a minute, stop the duplicate: a second deployment/service, an old "
    "container that ignored shutdown, or a local copy of the bot using the "
    "same BOT_TOKEN."
)


class PollingConflictGuard:
    """``error_callback`` for :meth:`telegram.ext.Updater.start_polling`.

    * ``Conflict`` (409) - expected during deploy overlap. Logged once as a
      warning, then at ``DEBUG`` level, with a reminder every
      :data:`CONFLICT_WARN_INTERVAL` seconds and one ``CRITICAL`` message if it
      lasts longer than :data:`CONFLICT_ESCALATE_AFTER`.
    * ``InvalidToken`` - always critical: no retry can fix a wrong token.
    * Any other ``TelegramError`` - logged as ``ERROR`` with the traceback, so
      genuine network/API problems stay visible.

    The callback must never raise: python-telegram-bot aborts its retry loop
    when an error callback fails.
    """

    def __init__(
        self,
        *,
        log: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
        warn_interval: float = CONFLICT_WARN_INTERVAL,
        streak_reset: float = CONFLICT_STREAK_RESET,
        escalate_after: float = CONFLICT_ESCALATE_AFTER,
    ) -> None:
        self._log = log or logger
        self._clock = clock
        self._warn_interval = warn_interval
        self._streak_reset = streak_reset
        self._escalate_after = escalate_after
        self._streak_started: float | None = None
        self._last_conflict: float | None = None
        self._last_warning: float | None = None
        self._conflicts = 0
        self._escalated = False

    @property
    def conflicts(self) -> int:
        """Number of ``409 Conflict`` replies seen in the current streak."""
        return self._conflicts

    def reset(self) -> None:
        """Forget the current conflict streak and start counting afresh."""
        self._streak_started = None
        self._last_conflict = None
        self._last_warning = None
        self._conflicts = 0
        self._escalated = False

    def __call__(self, error: TelegramError) -> None:
        if isinstance(error, Conflict):
            self._on_conflict()
        elif isinstance(error, InvalidToken):
            self._log.critical(
                "Telegram rejected BOT_TOKEN, so polling cannot start. "
                "Check the token from @BotFather (%s).", error,
            )
        else:
            self._log.error("Polling for updates failed: %s", error, exc_info=error)

    def _on_conflict(self) -> None:
        now = self._clock()
        if (
            self._last_conflict is not None
            and now - self._last_conflict >= self._streak_reset
        ):
            self.reset()
        if self._streak_started is None:
            self._streak_started = now

        self._last_conflict = now
        self._conflicts += 1
        elapsed = now - self._streak_started

        if (
            self._last_warning is not None
            and now - self._last_warning < self._warn_interval
        ):
            self._log.debug(
                "getUpdates conflict #%d after %.0f s (another instance is polling)",
                self._conflicts, elapsed,
            )
            return
        self._last_warning = now

        if self._escalated:
            self._log.debug(
                "getUpdates conflict #%d after %.0f s (still waiting for the "
                "other instance to stop)", self._conflicts, elapsed,
            )
        elif elapsed >= self._escalate_after:
            self._escalated = True
            self._log.critical(
                "This instance has been unable to poll for %.0f s: it lost %d "
                "getUpdates calls to another process using the same BOT_TOKEN. "
                "It is healthy but idle and will take over as soon as the other "
                "one stops. Find and stop the duplicate deployment, container, "
                "or local bot process.", elapsed, self._conflicts,
            )
        elif self._conflicts == 1:
            self._log.warning(
                "Another process is polling this bot (Telegram 409 Conflict). %s",
                _CONFLICT_HINT,
            )
        else:
            self._log.warning(
                "Still waiting for the other bot instance after %.0f s (%d "
                "rejected getUpdates calls). %s",
                elapsed, self._conflicts, _CONFLICT_HINT,
            )


async def start_guarded_polling(
    updater: Updater,
    *,
    allowed_updates: list[str] | None,
    drop_pending_updates: bool = True,
    guard: PollingConflictGuard | None = None,
) -> None:
    """Start long polling with a Conflict-aware error callback."""
    await updater.start_polling(
        allowed_updates=allowed_updates,
        drop_pending_updates=drop_pending_updates,
        error_callback=guard or PollingConflictGuard(),
    )


class InstanceLockError(RuntimeError):
    """Another live process on this host is already polling this bot token."""


class InstanceLock:
    """Advisory ``flock`` lock that keeps one poller per token per host.

    ``flock`` is released by the kernel when the process exits - even after
    ``SIGKILL`` or a crash - so a leftover lock file is harmless and never
    needs manual cleanup. The file name is a hash of the token, so the secret
    itself never appears on disk. Where ``fcntl`` is unavailable (Windows) or
    the temp directory is read-only, the guard degrades to a no-op instead of
    blocking startup.
    """

    def __init__(self, bot_token: str, directory: str | Path | None = None) -> None:
        digest = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:16]
        base = Path(directory) if directory is not None else Path(tempfile.gettempdir())
        self.path = base / f"gofile-bot-{digest}.lock"
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Try to become the only poller; ``False`` means another one is live."""
        if fcntl is None:  # pragma: no cover - Windows
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+")
        except OSError:  # pragma: no cover - read-only temp dir
            logger.warning(
                "could not create the single-instance lock at %s; continuing "
                "without it", self.path, exc_info=True,
            )
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        try:  # Best effort: helpful when inspecting /tmp by hand.
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:  # pragma: no cover - still locked, pid is cosmetic
            pass
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def busy_message(self) -> str:
        return (
            "Another bot process is already polling with this BOT_TOKEN on this "
            f"host (lock: {self.path}). Telegram delivers updates to only one "
            "consumer per token, so this process will not start. Stop the other "
            "copy (for example a local `python -m gofile_bot`) and try again."
        )
