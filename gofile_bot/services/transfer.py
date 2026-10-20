"""Download + upload orchestration for a job.

Runs entirely on the event loop (aiohttp streaming for GoFile, python-telegram-
bot's async client for uploads), reports progress into the job's status
message, and always cleans up temporary files afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from telegram import InputFile

from gofile_bot.config import (
    HARD_MAX_FILE_BYTES,
    JOB_TIMEOUT,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_FILE_BYTES,
    MAX_FOLDER_FILES,
    MAX_FOLDER_TOTAL_BYTES,
    MAX_PENDING_TRANSFERS,
    MAX_ZIP_BYTES,
    QUEUE_TIMEOUT,
    TMP_DIR_PREFIX,
    UPLOAD_MAX_ATTEMPTS,
)
from gofile_bot.downloaders.base import Downloader, DownloaderRegistry
from gofile_bot.errors import (
    BotError,
    DownloadError,
    DownloadTimeoutError,
    FileTooLargeError,
    JobBusyError,
    JobCancelled,
    RemoteDownloadError,
    ServiceBusyError,
    TelegramUploadError,
    ZipSafetyError,
)
from gofile_bot.gofile.models import (
    Content,
    RemoteFile,
    RemoteFolder,
    bounded_content,
    collect_files,
)
from gofile_bot.services.jobs import Job
from gofile_bot.services.resources import cancelable, disk_io
from gofile_bot.services.status import StatusMessage
from gofile_bot.services.zip_extract import extract_safely, is_zip
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


def is_memory_backed(
    directory: Path, mountinfo: Path = Path("/proc/self/mountinfo")
) -> bool:
    """Detect Linux tmpfs/ramfs so a 2 GB temp file cannot fill a 512 MB RAM mount."""
    target = directory.resolve()
    best_length, filesystem = -1, ""
    try:
        with mountinfo.open() as mounts:
            for line in mounts:
                fields = line.split()
                separator = fields.index("-")
                mount = re.sub(
                    r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]
                )
                if target.is_relative_to(Path(mount)) and len(mount) > best_length:
                    best_length, filesystem = len(mount), fields[separator + 1]
    except (OSError, ValueError, IndexError):
        return (
            False  # Non-Linux deployments must ensure disk-backed storage themselves.
        )
    return filesystem in ("tmpfs", "ramfs", "devtmpfs", "hugetlbfs")


def make_job_tmpdir(directory: str | None = None) -> Path:
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=TMP_DIR_PREFIX, dir=directory)).resolve()


def cleanup_stale_tmpdirs(
    max_age_seconds: float = 3600, directory: str | None = None
) -> int:
    """Best-effort sweep of tmp dirs left behind by a crash. Returns count."""
    removed = 0
    tmp_root = Path(directory or tempfile.gettempdir())
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

    Downloads are dispatched to the provider named on the job. A bounded FIFO
    scheduler caps how many transfers run at once across all users
    (:data:`~gofile_bot.config.MAX_CONCURRENT_DOWNLOADS`) so a small
    512 MB instance sheds excess work instead of allocating unlimited tasks.
    """

    def __init__(
        self,
        bot,
        downloaders,
        *,
        max_concurrent: int = MAX_CONCURRENT_DOWNLOADS,
        max_file_bytes: int = MAX_FILE_BYTES,
        local_mode: bool = False,
        download_dir: str | None = None,
        job_timeout: int = JOB_TIMEOUT,
        mtproto_uploader=None,
        max_pending: int = MAX_PENDING_TRANSFERS,
    ) -> None:
        self._bot = bot
        self._max_file_bytes = min(max_file_bytes, HARD_MAX_FILE_BYTES)
        self._folder_budget = max(MAX_FOLDER_TOTAL_BYTES, self._max_file_bytes)
        self._local_mode = local_mode
        self._download_dir = download_dir
        self._job_timeout = job_timeout
        self._mtproto = mtproto_uploader
        self._registry = self._as_registry(downloaders)
        self._max_concurrent = max_concurrent
        self._max_pending = max_pending
        self._pending: dict[str, Job] = {}
        self._active: set[str] = set()
        self._changed = asyncio.Event()
        self._closing = False

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

    def _admit(self, job: Job) -> None:
        if self._closing:
            raise ServiceBusyError()
        if any(j.user_id == job.user_id for j in self._pending.values()):
            raise JobBusyError()
        if len(self._pending) >= self._max_pending:
            raise ServiceBusyError()
        job.running = True
        job.phase = "queued"
        self._pending[job.id] = job

    def submit(self, job: Job, create_task) -> None:
        """Reserve capacity synchronously, BEFORE allocating a background task."""
        self._admit(job)
        job.cancel_event.clear()
        coroutine = self.run(job, _reserved=True)
        try:
            create_task(coroutine)
        except BaseException:
            coroutine.close()
            self._pending.pop(job.id, None)
            self._changed.set()
            job.running = False
            raise

    def begin_shutdown(self) -> None:
        self._closing = True
        for job in self._pending.values():
            job.cancel_event.set()
        self._changed.set()

    def snapshot(self, job: Job | None = None) -> dict:
        waiting = [j for j in self._pending.values() if j.id not in self._active]
        position = next(
            (i for i, j in enumerate(waiting, 1) if job and j.id == job.id), None
        )
        return {
            "active": len(self._active),
            "queued": len(waiting),
            "position": position,
            "capacity": self._max_pending,
        }

    async def _wait_slot(self, job: Job) -> None:
        while True:
            if job.cancelled:
                raise JobCancelled()
            waiting = [j for j in self._pending.values() if j.id not in self._active]
            if (
                len(self._active) < self._max_concurrent
                and waiting
                and waiting[0].id == job.id
            ):
                self._active.add(job.id)
                self._changed.set()
                return
            self._changed.clear()
            await self._changed.wait()

    async def run(self, job: Job, *, _reserved: bool = False) -> None:
        """Download and deliver the job's content; never raises."""
        status = StatusMessage(self._bot, job.chat_id, job.status_message_id)
        tmpdir = None
        admitted = _reserved
        try:
            if not admitted:
                self._admit(job)
                admitted = True
            position = self.snapshot(job)["position"]
            if self._active or (position and position > 1):
                await status.edit(
                    f"⏳ Queued · position {position or 1}. Use /status or Cancel. "
                    "Your file is not downloaded until a slot is free.",
                    self._cancel_markup(job),
                    force=True,
                )
            await cancelable(self._wait_slot(job), job.cancel_event, QUEUE_TIMEOUT)
            tmpdir = make_job_tmpdir(self._download_dir)
            await cancelable(
                self._run(job, status, tmpdir), job.cancel_event, self._job_timeout
            )
            job.phase = "complete"
        except JobCancelled:
            job.phase = "cancelled"
            await status.edit("❌ Download cancelled.", None, force=True)
        except TimeoutError:
            job.phase = "timed out"
            logger.warning("job %s timed out", job.id)
            text = (
                "⏳ Your queue wait expired after 10 minutes. No file was downloaded; try again later."
                if job.id not in self._active
                else DownloadTimeoutError.user_message
            )
            await status.edit(text, None, force=True)
        except FileTooLargeError as exc:
            job.cancel_event.set()
            job.phase = "rejected: size limit"
            await status.edit(exc.user_message, None, force=True)
        except TelegramUploadError as exc:
            job.phase = "failed"
            await status.edit(exc.user_message, None, force=True)
        except BotError as exc:
            job.phase = "failed"
            logger.info("job %s failed: %s", job.id, exc.detail or exc)
            await status.edit(exc.user_message, None, force=True)
        except Exception:
            job.phase = "failed"
            logger.exception("job %s failed unexpectedly", job.id)
            await status.edit(
                "⚠️ Something went wrong while transferring the file. Please try again.",
                None,
                force=True,
            )
        finally:
            try:
                if tmpdir is not None:
                    await disk_io(shutil.rmtree, tmpdir, ignore_errors=True)
            finally:
                # Keep the slot through cleanup: don't overlap two large temp
                # trees or start a new ZIP process while the old one is reaped.
                if admitted:
                    self._pending.pop(job.id, None)
                    self._active.discard(job.id)
                    self._changed.set()
                job.running = False

    # ------------------------------------------------------------------
    async def _run(self, job: Job, status: StatusMessage, tmpdir: Path) -> None:
        content: Content = job.content  # type: ignore[assignment]
        if content is None:  # pragma: no cover - handlers set it before running
            raise DownloadError("job has no content")

        if job.cancelled:
            raise JobCancelled()
        # Signed/CDN URLs can expire while a button sits in chat or a job queues.
        # Resolve at execution time rather than trusting yesterday's metadata.
        if job.source_url:
            job.phase = "refreshing link"
            content = bounded_content(
                await self._downloader_for(job).resolve(
                    job.source_url, password=job.password
                )
            )
            job.content = content
        if self._mtproto is not None:
            await self._mtproto.ensure_chat(job.chat_id)
        if job.action == "extract":
            if (
                not isinstance(content, RemoteFile)
                or not is_zip(content)
                or content.size > MAX_ZIP_BYTES
            ):
                raise ZipSafetyError()
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
                    f"must be at most {fmt.human_size(self._max_file_bytes)}."
                ),
            )

        sent: list[str] = []
        remaining = self._folder_budget
        for index, file in enumerate(files, start=1):
            if job.cancelled:
                raise JobCancelled()
            transferred = await self._transfer_file(
                job,
                status,
                file,
                tmpdir,
                file_index=index,
                file_count=len(files),
                byte_budget=remaining,
            )
            if job.action == "extract":
                return
            remaining -= transferred
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
            if file.size > self._max_file_bytes:
                continue
            if len(selected) >= MAX_FOLDER_FILES:
                break
            if total + file.size > self._folder_budget:
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
        byte_budget: int | None = None,
    ) -> int:
        if job.cancelled:
            raise JobCancelled()

        limit = (
            min(self._max_file_bytes, byte_budget)
            if byte_budget is not None
            else self._max_file_bytes
        )
        if job.action == "extract":
            limit = min(limit, MAX_ZIP_BYTES)
        if file.size > limit:
            raise FileTooLargeError(size=file.size, limit=limit)
        required = (file.size or limit) + (
            MAX_ZIP_BYTES if job.action == "extract" else 0
        )
        job.phase = "downloading"
        started = time.monotonic()
        if required > MAX_ZIP_BYTES and is_memory_backed(tmpdir):
            raise DownloadError(
                "RAM-backed temporary directory",
                user_message=(
                    "⚠️ Big downloads need disk-backed storage. The bot owner must set "
                    "DOWNLOAD_DIR to a real disk, not tmpfs/RAM."
                ),
            )
        if shutil.disk_usage(tmpdir).free < required + 64 * 1024 * 1024:
            raise DownloadError(
                "insufficient disk space",
                user_message="⚠️ Not enough free disk space on the bot server for this file.",
            )
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
                    elapsed=time.monotonic() - started,
                ),
                self._cancel_markup(job),
            )

        downloader = self._downloader_for(job)
        actual_path = await downloader.download(
            file,
            dest,
            max_bytes=limit,
            progress=progress,
            cancel_event=job.cancel_event,
        )

        if job.cancelled:
            raise JobCancelled()

        if job.action == "extract":
            await self._extract_and_send(job, status, Path(actual_path), tmpdir)
            return Path(actual_path).stat().st_size
        job.phase = "uploading"
        await status.edit(
            fmt.uploading_text(
                file.name,
                file_index=file_index if file_count > 1 else None,
                file_count=file_count if file_count > 1 else None,
            ),
            self._cancel_markup(job) if self._mtproto else None,
            force=True,
        )
        path = Path(actual_path)
        actual_size = path.stat().st_size
        if actual_size > limit:
            raise FileTooLargeError(size=actual_size, limit=limit)

        async def upload_progress(sent: int, total: int | None) -> None:
            await status.edit(
                fmt.upload_progress_text(filename=file.name, sent=sent, total=total),
                self._cancel_markup(job),
            )

        try:
            await self._send_document(job, file, path, progress=upload_progress)
        finally:
            path.unlink(missing_ok=True)  # Don't accumulate a whole folder on disk.
        return actual_size

    async def _extract_and_send(self, job, status, source: Path, tmpdir: Path) -> None:
        job.phase = "extracting"
        await status.edit(
            "📦 Checking and extracting ZIP · 20 MB safety cap…",
            self._cancel_markup(job),
            force=True,
        )
        paths = await extract_safely(source, tmpdir / "extracted", job.cancel_event)
        for index, path in enumerate(paths, 1):
            if job.cancelled:
                raise JobCancelled()
            job.phase = f"sending extracted file {index}/{len(paths)}"
            file = RemoteFile(
                id=f"{job.id}-{index}",
                name=path.name,
                size=path.stat().st_size,
                mimetype=mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                link="",
            )
            await status.edit(
                f"📤 Sending extracted file {index}/{len(paths)}\n📄 {fmt.esc(path.name)}",
                self._cancel_markup(job),
                force=True,
            )
            relative = path.relative_to(tmpdir / "extracted").as_posix()
            await self._send_document(
                job,
                file,
                path,
                caption_override=(f"📁 {fmt.esc(relative)}\n" + fmt.caption_for(file)),
            )
            path.unlink(missing_ok=True)
        await status.edit(
            f"✅ Extracted and sent {len(paths)} file(s). Temporary files will be removed.",
            None,
            force=True,
        )

    def _cancel_markup(self, job: Job):
        from gofile_bot.telegram.keyboards import cancel_keyboard

        return cancel_keyboard(job.id)

    # ------------------------------------------------------------------
    async def _send_document(
        self,
        job: Job,
        file: RemoteFile,
        path: Path,
        *,
        progress=None,
        caption_override=None,
    ) -> None:
        caption = caption_override or fmt.caption_for(file)
        if self._mtproto is not None:
            await self._mtproto.send_document(
                chat_id=job.chat_id,
                path=path,
                filename=safe_filename(file.name),
                caption=caption,
                progress=progress,
                cancel_event=job.cancel_event,
            )
            return
        last_error: Exception | None = None
        for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
            try:
                with path.open("rb") as fh:
                    await self._bot.send_document(
                        chat_id=job.chat_id,
                        # Local API reads a shared absolute path; cloud uploads stream
                        # from the handle instead of copying the entire file into RAM.
                        document=path
                        if self._local_mode
                        else InputFile(
                            fh,
                            filename=safe_filename(file.name),
                            read_file_handle=False,
                        ),
                        filename=safe_filename(file.name),
                        caption=caption,
                        parse_mode="HTML",
                        read_timeout=self._job_timeout,
                        write_timeout=self._job_timeout,
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
