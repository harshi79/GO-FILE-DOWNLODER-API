"""Inline keyboard builders."""

from __future__ import annotations

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from gofile_bot.telegram.formatting import CANCEL_LABEL, DOWNLOAD_LABEL


def download_keyboard(
    job_id: str, can_extract: bool = False, extract_only: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    if not extract_only:
        rows.append(
            [
                InlineKeyboardButton(
                    DOWNLOAD_LABEL, callback_data=f"dl:{job_id}", style="success"
                )
            ]
        )
    if can_extract or extract_only:
        rows.append(
            [
                InlineKeyboardButton(
                    "📦 Extract ZIP · ≤20 MB",
                    callback_data=f"zx:{job_id}",
                    style="primary",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    CANCEL_LABEL, callback_data=f"cx:{job_id}", style="danger"
                )
            ]
        ]
    )


def link_keyboard(url: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("↗ Open download", url=url)]
    if len(url.encode("utf-16-le")) // 2 <= 256:
        row.append(InlineKeyboardButton("📋 Copy link", copy_text=CopyTextButton(url)))
    return InlineKeyboardMarkup([row])
