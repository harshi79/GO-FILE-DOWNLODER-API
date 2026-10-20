"""Message text builders (HTML) and small formatting helpers."""

from __future__ import annotations

import html
import mimetypes

from gofile_bot.gofile.models import Content, RemoteFile, RemoteFolder

DOWNLOAD_LABEL = "「 DOWNLOAD 」"
CANCEL_LABEL = "❌ Cancel"

#: Credit tag shown in bot messages and file captions.
MADE_BY = "Made By @Yorichiiprime"


def esc(text: str) -> str:
    """HTML-escape user/file-controlled text."""
    return html.escape(str(text), quote=False)


def human_size(num_bytes: float) -> str:
    """1234567 -> '1.2 MB'."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"  # pragma: no cover - unreachable


def file_type_label(file: RemoteFile) -> str:
    if file.mimetype and file.mimetype != "application/octet-stream":
        return file.mimetype
    guessed, _ = mimetypes.guess_type(file.name)
    return guessed or "application/octet-stream"


def fetch_status_text(source: str | None = None) -> str:
    if source:
        return f"⌕ Fetching file information from {source}..."
    return "⌕ Fetching file information..."


def _size_label(size: int) -> str:
    return human_size(size) if size > 0 else "unknown"


def info_text(content: Content, source_label: str | None = None) -> str:
    """Result card shown after metadata lookup."""
    source = f"\n🌐 Source: {esc(source_label)}" if source_label else ""
    if isinstance(content, RemoteFile):
        return (
            f"📄 <b>{esc(content.name)}</b>\n"
            f"💾 Size: {_size_label(content.size)}\n"
            f"🗂 Type: {esc(file_type_label(content))}"
            f"{source}"
        )
    files = content.files()
    total = sum(f.size for f in files)
    return (
        f"📁 <b>{esc(content.name)}</b>\n"
        f"🗂 Type: Folder\n"
        f"📄 {len(files)} file{'s' if len(files) != 1 else ''} · "
        f"💾 {_size_label(total)} total"
        f"{source}"
    )


def progress_text(
    *,
    filename: str,
    received: int,
    total: int | None,
    file_index: int | None = None,
    file_count: int | None = None,
    elapsed: float | None = None,
) -> str:
    prefix = ""
    if file_index is not None and file_count is not None:
        prefix = f"📎 File {file_index}/{file_count}\n"
    if total:
        percent = min(100, int(received * 100 / total))
        bar_len = 12
        filled = int(bar_len * percent / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        detail = f"{human_size(received)} / {human_size(total)}"
        if elapsed and elapsed >= 1 and received > 0:
            speed = received / elapsed
            seconds = max(0, int((total - received) / speed))
            detail += f"\n⚡ {human_size(speed)}/s · ETA {seconds // 60}m {seconds % 60}s"
        return (
            f"⏳ Downloading… {percent}%\n"
            f"<code>{bar}</code>\n"
            f"{prefix}📄 {esc(filename)}\n"
            f"💾 {detail}"
        )
    return (
        f"⏳ Downloading… {human_size(received)}\n"
        f"{prefix}📄 {esc(filename)}"
    )


def uploading_text(
    filename: str, *, file_index: int | None = None, file_count: int | None = None
) -> str:
    prefix = ""
    if file_index is not None and file_count is not None:
        prefix = f"📎 File {file_index}/{file_count}\n"
    return f"📤 Uploading to Telegram…\n{prefix}📄 {esc(filename)}"


def upload_progress_text(*, filename: str, sent: int, total: int | None) -> str:
    return progress_text(filename=filename, received=sent, total=total).replace(
        "⏳ Downloading…", "📤 Uploading…", 1
    )


def success_text(content: Content, sent: list[str] | None = None) -> str:
    if isinstance(content, RemoteFile):
        return f"✅ Sent <b>{esc(content.name)}</b> ({human_size(content.size)})"
    folder = content.name
    count = len(sent) if sent is not None else content.file_count
    return (
        f"✅ Sent {count} file{'s' if count != 1 else ''} "
        f"from <b>{esc(folder)}</b>."
    )


def too_big_note(size: int, limit: int) -> str:
    """Explain an upload-limit rejection before the user taps a dead button."""
    return (
        f"\n\n⚠️ This file is <b>{human_size(size)}</b>, larger than this bot's "
        f"<b>{human_size(limit)}</b> Telegram upload limit.\n"
        "Use <code>/links &lt;your link&gt;</code> to get the host's download "
        "link, or ask the bot owner to enable MTProto / a Local Bot API server "
        "(up to 2 GB)."
    )


def folder_note_text(content: RemoteFolder, *, skipped: list[str]) -> str:
    if not skipped:
        return ""
    names = ", ".join(esc(n) for n in skipped[:5])
    more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
    return f"\n⚠️ Skipped (too big for Telegram): {names}{more}"


def caption_for(file: RemoteFile) -> str:
    return (
        f"💾 {human_size(file.size)} · 🗂 {esc(file_type_label(file))}\n"
        f"{MADE_BY}"
    )


# ---------------------------------------------------------------------------
# Rich (Bot API 10.1+) counterparts
# ---------------------------------------------------------------------------
def human_duration(seconds: float) -> str:
    """75 -> '1m 15s'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _progress_rich(
    *,
    verb: str,
    filename: str,
    received: int,
    total: int | None,
    file_index: int | None = None,
    file_count: int | None = None,
    elapsed: float | None = None,
):
    """Shared rich progress card for downloads and uploads."""
    from gofile_bot.telegram.rich import (
        RichMessage,
        footer,
        heading,
        pre,
        table,
    )

    rows: list[list[str]] = [["📄 File", filename]]
    if file_index is not None and file_count is not None:
        rows.append(["📎 Part", f"{file_index}/{file_count}"])
    if total:
        percent = min(100, int(received * 100 / total))
        bar_len = 12
        filled = int(bar_len * percent / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        rows.append(["💾 Size", f"{human_size(received)} / {human_size(total)}"])
        if elapsed and elapsed >= 1 and received > 0:
            speed = received / elapsed
            remaining = max(0, int((total - received) / speed))
            rows.append(["⚡ Speed", f"{human_size(speed)}/s"])
            rows.append(["⏱ ETA", human_duration(remaining)])
        title = f"⏳ {verb}… {percent}%"
    else:
        bar = "█" * (int(elapsed or 0) % 12 + 1) + "░" * (11 - int(elapsed or 0) % 12)
        rows.append(["💾 Received", human_size(received)])
        title = f"⏳ {verb}…"

    rich = RichMessage().add(
        heading(title, 3),
        pre(bar),
        table(["Field", "Value"], rows, compact=True, aligns=["left", "left"]),
        footer("Use /cancel to stop · /status for the queue"),
    )
    return rich


def progress_rich(
    *,
    filename: str,
    received: int,
    total: int | None,
    file_index: int | None = None,
    file_count: int | None = None,
    elapsed: float | None = None,
):
    return _progress_rich(
        verb="Downloading",
        filename=filename,
        received=received,
        total=total,
        file_index=file_index,
        file_count=file_count,
        elapsed=elapsed,
    )


def upload_progress_rich(
    *, filename: str, sent: int, total: int | None, elapsed: float | None = None
):
    return _progress_rich(
        verb="Uploading to Telegram",
        filename=filename,
        received=sent,
        total=total,
        elapsed=elapsed,
    )


def uploading_rich(
    filename: str, *, file_index: int | None = None, file_count: int | None = None
):
    from gofile_bot.telegram.rich import RichMessage, footer, heading, table

    rows = [["📄 File", filename]]
    if file_index is not None and file_count is not None:
        rows.append(["📎 Part", f"{file_index}/{file_count}"])
    return RichMessage().add(
        heading("📤 Uploading to Telegram…", 3),
        table(["Field", "Value"], rows, compact=True, aligns=["left", "left"]),
        footer("You can cancel while the upload runs."),
    )


def info_rich(content: Content, source_label: str | None = None, note: str = ""):
    """Rich version of :func:`info_text` (file/folder card)."""
    from gofile_bot.telegram.rich import RichMessage, footer, heading, paragraph, table

    if isinstance(content, RemoteFile):
        rows = [
            ["💾 Size", _size_label(content.size)],
            ["🗂 Type", file_type_label(content)],
        ]
        if source_label:
            rows.append(["🌐 Source", source_label])
        rich = RichMessage().add(
            heading(f"📄 {content.name}", 3),
            table(["Field", "Value"], rows, compact=True, aligns=["left", "left"]),
        )
    else:
        files = content.files()
        total = sum(file.size for file in files)
        rows = [
            ["📄 Files", str(len(files))],
            ["💾 Total size", _size_label(total)],
        ]
        if source_label:
            rows.append(["🌐 Source", source_label])
        rich = RichMessage().add(
            heading(f"📁 {content.name}", 3),
            paragraph(f"Folder · {len(files)} file(s)"),
            table(["Field", "Value"], rows, compact=True, aligns=["left", "left"]),
        )
    if note:
        rich.add(paragraph(note))
    rich.add(footer(MADE_BY))
    return rich


def success_rich(content: Content, sent: list[str] | None = None, note: str = ""):
    """Rich version of :func:`success_text`."""
    from gofile_bot.telegram.rich import RichMessage, bold, bullets, footer, heading, paragraph

    if isinstance(content, RemoteFile):
        body = paragraph([bold(content.name), f" · {human_size(content.size)}"])
        rich = RichMessage().add(heading("✅ Delivered", 3), body)
    else:
        count = len(sent) if sent is not None else content.file_count
        rich = RichMessage().add(
            heading("✅ Folder delivered", 3),
            paragraph([f"{count} file(s) from ", bold(content.name)]),
        )
        if sent:
            rich.add(bullets(*sent[:10]))
    if note:
        rich.add(paragraph(note))
    rich.add(footer(MADE_BY))
    return rich
