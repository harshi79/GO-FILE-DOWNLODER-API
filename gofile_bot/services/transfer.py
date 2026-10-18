"""Download + upload orchestration for a job.

Runs entirely on the event loop (aiohttp streaming for GoFile, python-telegram-
bot's async client for uploads), reports progress into the job's status
message, and always cleans up temporary files afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from gofile_bot.config import (
    JOB_TIMEOUT,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_FILE_BYTES,
    MAX_FOLDER_FILES,
    MAX_FOLDER_TOTAL_BYTES,
    TMP_DIR_PREFIX,
    UPLOAD_MAX_ATTEMPTS,
)
from gofile_bot.downloaders.base import Downloader, DownloaderRegistry
from gofile_bot.errors import (
    BotError,
    DownloadError,
    DownloadTimeoutError,
    FileTooLargeError,
    JobCancelled,
    RemoteDownloadError,
    TelegramUploadError,
)
from gofile_bot.gofile.models import Content, RemoteFile, RemoteFolder, collect_files
from gofile_bot.services.jobs import Job
from gofile_bot.services.status import StatusMessage
from gofile_bot.telegram import formatting as fmt

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_NAME_LEN = 120


def safe_filename(name: str) -> str:
    """Sanitize a remote file name for local storage and Telegram."""
    base = os.path.basename(str(name).replace("\\", "/"))
    base = _UNSAFE_CHARS.sub("", base).strip().lstrip(".")
    if not base:
        base = "file"
    if len(base) > _MAX_NAME_LEN:
        stem, dot, ext = base.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            base = stem[: _MAX_NAME_LEN - len(ext) - 1] + "." + ext
        else:
            base = base[:_MAX_NAME_LEN]
    return base


def make_job_tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix=TMP_DIR_PREFIX))


def cleanup_stale_tmpdirs(max_age_seconds: float = 3600) -> int:
    """Best-effort sweep of tmp dirs left behind by a crash. Returns count."""
    removed = 0
    tmp_root = Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_seconds
    for entry in tmp_root.glob(f"{TMP_DIR_PREFIX}*"):
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:  # pragma: no cover - best effort
            continue
    return removed


class TransferService:
    """Performs the actual file transfer for a :class:`Job`.

    Downloads are dispatched to the provider named on the job. A global
    semaphore caps how many transfers run at once across all users
    (:data:`~gofile_bot.config.MAX_CONCURRENT_DOWNLOADS`) so a small
    512 MB instance never gets swamped.
    """

    def __init__(
        self,
        bot,
        downloaders,
        *,
        max_concurrent: int = MAX_CONCURRENT_DOWNLOADS,
    ) -> None:
        self._bot = bot
        self._registry = self._as_registry(downloaders)
        self._semaphore = asyncio.Semaphore(max_concurrent)

    @staticmethod
    def _as_registry(downloaders) -> DownloaderRegistry:
        """Accept a registry, or legacy GoFile-client-like objects."""
        if isinstance(downloaders, DownloaderRegistry):
            return downloaders
        if isinstance(downloaders, Downloader):
            return DownloaderRegistry([downloaders])
        # Legacy: a GoFileClient (or test fake) with fetch/download methods.
        from gofile_bot.downloaders.gofile_provider import GoFileDownloader

        return DownloaderRegistry([GoFileDownloader(downloaders)])

    def _downloader_for(self, job: Job) -> Downloader:
        provider = self._registry.get(job.provider) or self._registry.get("gofile")
        if provider is None:  # pragma: no cover - registry always has gofile
            raise RemoteDownloadError(f"no downloader for provider {job.provider!r}")
        return provider

    async def run(self, job: Job) -> None:
        """Download and deliver the job's content; never raises."""
        status = StatusMessage(self._bot, job.chat_id, job.status_message_id)
        tmpdir = make_job_tmpdir()
        try:
            if self._semaphore.locked():
                await status.edit(
                    "⏳ Queued — waiting for a free download slot…",
                    self._cancel_markup(job),
                    force=True,
                )
            async with self._semaphore:
                await asyncio.wait_for(self._run(job, status, tmpdir), JOB_TIMEOUT)
        except JobCancelled:
            await status.edit("❌ Download cancelled.", None, force=True)
        except TimeoutError:
            logger.warning("job %s timed out", job.id)
            await status.edit(DownloadTimeoutError.user_message, None, force=True)
        except FileTooLargeError as exc:
            await status.edit(exc.user_message, None, force=True)
        except TelegramUploadError as exc:
            await status.edit(exc.user_message, None, force=True)
        except BotError as exc:
            logger.info("job %s failed: %s", job.id, exc.detail or exc)
            await status.edit(exc.user_message, None, force=True)
        except Exception:
            logger.exception("job %s failed unexpectedly", job.id)
            await status.edit(
                "⚠️ Something went wrong while transferring the file. "
                "Please try again.",
                None,
                force=True,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            job.running = False

    # ------------------------------------------------------------------
    async def _run(self, job: Job, status: StatusMessage, tmpdir: Path) -> None:
        content: Content = job.content  # type: ignore[assignment]
        if content is None:  # pragma: no cover - handlers set it before running
            raise DownloadError("job has no content")

        files = self._select_files(content)
        if not files:
            if isinstance(content, RemoteFolder) and content.file_count == 0:
                raise RemoteDownloadError(
                    "empty folder",
                    user_message="⚠️ This folder is empty — there is nothing to send.",
                )
            raise FileTooLargeError(
                "nothing deliverable",
                user_message=(
                    "⚠️ None of the files can be sent to Telegram — each file "
                    f"must be at most {fmt.human_size(MAX_FILE_BYTES)}."
                ),
            )

        sent: list[str] = []
        for index, file in enumerate(files, start=1):
            if job.cancelled:
                raise JobCancelled()
            await self._transfer_file(
                job,
                status,
                file,
                tmpdir,
                file_index=index,
                file_count=len(files),
            )
            sent.append(file.name)

        await status.edit(
            fmt.success_text(content, sent) + self._skipped_note(content, files),
            None,
            force=True,
        )

    def _select_files(self, content: Content) -> list[RemoteFile]:
        """Files to deliver, subject to size/count caps; oversized ones drop."""
        candidates = collect_files(content)
        if isinstance(content, RemoteFile):
            return candidates

        selected: list[RemoteFile] = []
        total = 0
        for file in candidates:
            if file.size > MAX_FILE_BYTES:
                continue
            if len(selected) >= MAX_FOLDER_FILES:
                break
            if total + file.size > MAX_FOLDER_TOTAL_BYTES:
                break
            if not file.link:
                continue
            selected.append(file)
            total += file.size
        return selected

    def _skipped_note(self, content: Content, delivered: list[RemoteFile]) -> str:
        if isinstance(content, RemoteFile):
            return ""
        all_files = content.files()
        delivered_ids = {f.id for f in delivered}
        skipped = [f.name for f in all_files if f.id not in delivered_ids]
        return fmt.folder_note_text(content, skipped=skipped)

    # ------------------------------------------------------------------
    async def _transfer_file(
        self,
        job: Job,
        status: StatusMessage,
        file: RemoteFile,
        tmpdir: Path,
        *,
        file_index: int,
        file_count: int,
    ) -> None:
        if job.cancelled:
            raise JobCancelled()

        dest = tmpdir / safe_filename(file.name)

        async def progress(received: int, total: int | None) -> None:
            if job.cancelled:
                raise JobCancelled()
            await status.edit(
                fmt.progress_text(
                    filename=file.name,
                    received=received,
                    total=total,
                    file_index=file_index if file_count > 1 else None,
                    file_count=file_count if file_count > 1 else None,
                ),
                self._cancel_markup(job),
            )

        downloader = self._downloader_for(job)
        actual_path = await downloader.download(
            file,
            dest,
            max_bytes=MAX_FILE_BYTES,
            progress=progress,
            cancel_event=job.cancel_event,
        )

        if job.cancelled:
            raise JobCancelled()

        await status.edit(
            fmt.uploading_text(
                file.name,
                file_index=file_index if file_count > 1 else None,
                file_count=file_count if file_count > 1 else None,
            ),
            None,
            force=True,
        )
        await self._send_document(job, file, Path(actual_path))

    def _cancel_markup(self, job: Job):
        from gofile_bot.telegram.keyboards import cancel_keyboard

        return cancel_keyboard(job.id)

    # ------------------------------------------------------------------
    async def _send_document(self, job: Job, file: RemoteFile, path: Path) -> None:
        last_error: Exception | None = None
        for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
            try:
                with path.open("rb") as fh:
                    await self._bot.send_document(
                        chat_id=job.chat_id,
                        document=fh,
                        filename=safe_filename(file.name),
                        caption=fmt.caption_for(file),
                        parse_mode="HTML",
                        read_timeout=JOB_TIMEOUT,
                        write_timeout=JOB_TIMEOUT,
                    )
                return
            except Exception as exc:
                last_error = exc
                text = str(exc).lower()
                if "too large" in text or "request entity too large" in text:
                    raise TelegramUploadError(str(exc)) from exc
                if "file is too big" in text:
                    raise TelegramUploadError(str(exc)) from exc
                if "wrong file identifier" in text or "invalid file" in text:
                    raise TelegramUploadError(str(exc)) from exc
                logger.warning(
                    "upload attempt %d for %s failed: %s", attempt, file.name, exc
                )
                if attempt < UPLOAD_MAX_ATTEMPTS:
                    await asyncio.sleep(2)
        raise TelegramUploadError(str(last_error)) from last_error
