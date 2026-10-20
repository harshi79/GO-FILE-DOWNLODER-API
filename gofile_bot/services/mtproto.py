"""Optional, bot-token-only MTProto uploads using bounded file chunks.

Bot API polling still handles commands/buttons. Telethon receives updates only
for its peer cache and sends documents; it does not register command handlers.
A MemorySession avoids persisting Telegram authorization keys to disk.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import MemorySession
from telethon.tl.types import DocumentAttributeFilename

from gofile_bot.config import LARGE_MAX_FILE_BYTES, Settings
from gofile_bot.downloaders.base import ProgressCallback
from gofile_bot.errors import FileTooLargeError, JobCancelled, TelegramUploadError


class BoundedMemorySession(MemorySession):
    """Do not retain every Telegram user ever seen by a busy bot."""

    def __init__(self):
        super().__init__()
        self._recent_rows = OrderedDict()

    def process_entities(self, tlo):
        for row in self._entities_to_rows(tlo):
            self._recent_rows[row[0]] = row
            self._recent_rows.move_to_end(row[0])
        while len(self._recent_rows) > 256:
            self._recent_rows.popitem(last=False)
        self._entities = set(self._recent_rows.values())


class MTProtoUploader:
    def __init__(self, settings: Settings, *, client=None) -> None:
        self._settings = settings
        self._client = client
        self._started = False

    async def start(self, *, expected_bot_id: int) -> None:
        """Authorize the same bot, never a phone/user-account login."""
        if self._started:
            return
        if self._client is None:
            self._client = TelegramClient(
                BoundedMemorySession(),
                self._settings.api_id,
                self._settings.api_hash,
                receive_updates=True,
                sequential_updates=True,  # Peer caching only; no per-update task fan-out.
                flood_sleep_threshold=0,
                entity_cache_limit=256,
                request_retries=1,
                connection_retries=3,
                retry_delay=1,
                raise_last_call_error=True,
            )
        try:
            await asyncio.wait_for(self._authorize(expected_bot_id), timeout=90)
        except BaseException as exc:
            await self.close()
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError(
                "MTProto bot login failed. Check API_ID, API_HASH, BOT_TOKEN "
                "and outbound access to Telegram. No phone login is needed."
            ) from exc
        self._started = True

    async def _authorize(self, expected_bot_id: int) -> None:
        await self._client.connect()
        await self._client.sign_in(bot_token=self._settings.bot_token)
        me = await self._client.get_me()
        if me is None or not me.bot or me.id != expected_bot_id:
            raise ValueError(
                "MTProto uploader must use the same bot as Bot API polling"
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
        self._started = False

    async def ensure_chat(self, chat_id: int):
        """Fail before downloading if this session cannot address the chat."""
        if not self._started:
            raise TelegramUploadError(
                "MTProto uploader not started",
                user_message="⚠️ The large-file uploader is offline. Ask the bot owner to check startup logs.",
            )
        try:
            # Resolve before transferring any bytes. Private users/channels need
            # access hashes from MTProto updates, not only numeric Bot API IDs.
            return await self._client.get_input_entity(chat_id)
        except ValueError as exc:
            raise TelegramUploadError(
                "MTProto peer not cached",
                user_message=(
                    "⚠️ The large-file uploader hasn't seen this chat yet. "
                    "Send /start and then send the download link again."
                ),
            ) from exc
        except (RPCError, OSError) as exc:
            raise TelegramUploadError(
                "MTProto couldn't resolve the destination chat"
            ) from exc

    async def send_document(
        self,
        *,
        chat_id: int,
        path: Path,
        filename: str,
        caption: str,
        progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event,
    ) -> None:
        if cancel_event.is_set():
            raise JobCancelled()
        size = path.stat().st_size
        if size > LARGE_MAX_FILE_BYTES:
            raise FileTooLargeError(size=size, limit=LARGE_MAX_FILE_BYTES)
        peer = await self.ensure_chat(chat_id)

        async def on_progress(sent: int, total: int) -> None:
            if cancel_event.is_set():
                raise JobCancelled()
            if progress:
                await progress(sent, total)

        upload = asyncio.create_task(
            self._client.send_file(
                peer,
                file=str(path),
                force_document=True,
                attributes=[DocumentAttributeFilename(filename)],
                caption=caption,
                parse_mode="html",
                part_size_kb=512,
                progress_callback=on_progress,
            )
        )
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                (upload, cancelled), return_when=asyncio.FIRST_COMPLETED
            )
            # If delivery already completed, report success even if Cancel was
            # tapped at the same moment. Telegram may already have the message.
            if upload in done:
                await upload
            else:
                raise JobCancelled()
        except FloodWaitError as exc:
            raise TelegramUploadError(
                "MTProto flood wait",
                user_message=f"⏳ Telegram asked the bot to wait {exc.seconds} seconds. Try again later.",
            ) from exc
        except (RPCError, OSError, ValueError) as exc:
            # Never retry the whole send blindly: a lost acknowledgement can
            # otherwise result in duplicate multi-GB documents.
            raise TelegramUploadError(
                "MTProto upload failed",
                user_message=(
                    "⚠️ Telegram couldn't complete the large-file upload. "
                    "Check whether it arrived before retrying."
                ),
            ) from exc
        finally:
            for task in (upload, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(upload, cancelled, return_exceptions=True)
