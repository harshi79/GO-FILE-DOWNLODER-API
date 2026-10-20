"""Throttled editing of a single Telegram status message.

All user-visible progress lands in the original status message. Edits are
rate-limited and tolerant of Telegram's benign errors (``Message is not
modified``, vanishing messages).
"""

from __future__ import annotations

import logging
import time

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError, RetryAfter

from gofile_bot.config import PROGRESS_EDIT_INTERVAL

logger = logging.getLogger(__name__)


class StatusMessage:
    """Wraps one sent message and coalesces edits to it."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message_id: int,
        *,
        min_interval: float = PROGRESS_EDIT_INTERVAL,
    ) -> None:
        self._bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self._min_interval = min_interval
        self._last_edit = 0.0
        self._last_text: str | None = None
        self._blocked_until = 0.0

    async def edit(
        self,
        text: str,
        keyboard: InlineKeyboardMarkup | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """Edit the status message. Returns True when an edit was sent.

        Identical text is skipped. Non-forced edits are throttled to at most
        one per ``min_interval`` seconds.
        """
        now = time.monotonic()
        if now < self._blocked_until:
            return False
        if text == self._last_text and keyboard is None:
            return False
        if not force and now - self._last_edit < self._min_interval:
            return False

        self._last_edit = now  # Also throttle failures, not just successful edits.
        try:
            await self._bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
            self._blocked_until = now + seconds
            return False
        except BadRequest as exc:
            msg = str(exc).lower()
            if "message is not modified" in msg:
                return False
            if "message to edit not found" in msg or "message can't be edited" in msg:
                logger.debug("status message vanished: %s", exc)
                return False
            logger.warning("could not edit status message: %s", exc)
            return False
        except (Forbidden, TelegramError) as exc:
            logger.debug("could not edit status message: %s", exc)
            return False

        self._last_edit = now
        self._last_text = text
        return True

    async def edit_reply_markup(
        self, keyboard: InlineKeyboardMarkup | None
    ) -> None:
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=self.chat_id,
                message_id=self.message_id,
                reply_markup=keyboard,
            )
        except TelegramError as exc:
            logger.debug("could not edit reply markup: %s", exc)
