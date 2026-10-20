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
