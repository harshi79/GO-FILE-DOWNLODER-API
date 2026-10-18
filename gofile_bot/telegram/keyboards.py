"""Inline keyboard builders."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from gofile_bot.telegram.formatting import CANCEL_LABEL, DOWNLOAD_LABEL


def download_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(DOWNLOAD_LABEL, callback_data=f"dl:{job_id}")]]
    )


def cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(CANCEL_LABEL, callback_data=f"cx:{job_id}")]]
    )
