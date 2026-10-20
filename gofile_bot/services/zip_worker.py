"""Isolated ZIP worker. Stdlib only; never executes archive contents.

Invoked by zip_extract.py in a separate process. On Linux the OS enforces
memory/CPU/file limits; the parent also enforces a wall-clock deadline.
"""

from __future__ import annotations

import json
import stat
import struct
import sys
import zipfile
from pathlib import Path, PurePosixPath

# Keep this worker independent of Telegram/Telethon imports.
from gofile_bot.config import MAX_ZIP_BYTES, MAX_ZIP_ENTRIES, MAX_ZIP_FILES


class UnsafeZip(ValueError):
    pass


def apply_limits() -> None:
    try:
        import resource
    except ImportError:
        return  # Parent timeout + byte/file limits still apply off POSIX.
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2, 128 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ZIP_BYTES, MAX_ZIP_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))


def check_directory_size(source: Path) -> None:
    """Bound central-directory allocation BEFORE ZipFile constructs ZipInfo objects."""
    size = source.stat().st_size
    if size > MAX_ZIP_BYTES:
        raise UnsafeZip("Archive exceeds 20 MB")
    with source.open("rb") as stream:
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or offset + 22 > len(tail):
        raise UnsafeZip("Invalid ZIP directory")
    (
        _,
        disk,
        directory_disk,
        on_disk,
        count,
        directory_size,
        directory_offset,
        comment,
    ) = struct.unpack_from("<4s4H2LH", tail, offset)
    if (
        disk
        or directory_disk
        or on_disk != count
        or count > MAX_ZIP_ENTRIES
        or directory_size > 512 * 1024
        or directory_offset + directory_size > size
        or offset + 22 + comment != len(tail)
    ):
        raise UnsafeZip("ZIP directory too large, split, or invalid")


def extract_zip(source: Path, destination: Path) -> list[str]:
    check_directory_size(source)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise UnsafeZip("Too many entries")
        files = []
        seen = set()
        declared = 0
        for info in entries:
            raw = info.orig_filename
            normalized = raw.replace("\\", "/")
            parts = normalized.rstrip("/").split("/")
            if (
                not parts
                or normalized.startswith("/")
                or len(raw) > 200
                or len(parts) > 10
                or any(p in ("", ".", "..") or ":" in p for p in parts)
                or any(ord(c) < 32 or ord(c) == 127 for c in raw)
            ):
                raise UnsafeZip("Unsafe archive path")
            key = "/".join(parts).casefold()
            if key in seen:
                raise UnsafeZip("Duplicate archive path")
            seen.add(key)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise UnsafeZip("Links and special files are not allowed")
            if info.flag_bits & 1:
                raise UnsafeZip("Encrypted ZIPs are not supported")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise UnsafeZip("Only stored/deflated ZIPs are supported")
            if info.is_dir():
                continue
            declared += info.file_size
            if declared > MAX_ZIP_BYTES or len(files) >= MAX_ZIP_FILES:
                raise UnsafeZip("Extracted output exceeds 20 MB or 20 files")
            files.append((info, PurePosixPath(*parts).as_posix()))
        if not files:
            raise UnsafeZip("ZIP contains no files")
        written = 0
        names = []
        for info, relative in files:
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            member_bytes = 0
            with archive.open(info) as src, target.open("xb") as dst:
                while chunk := src.read(64 * 1024):
                    written += len(chunk)
                    member_bytes += len(chunk)
                    if written > MAX_ZIP_BYTES or member_bytes > info.file_size:
                        raise UnsafeZip("Expanded ZIP size exceeds the safety limit")
                    dst.write(chunk)
            if member_bytes != info.file_size:
                raise UnsafeZip("Corrupt ZIP member")
            names.append(relative)
        return names


def main() -> int:
    apply_limits()
    try:
        names = extract_zip(Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps({"files": names}))
        return 0
    except (
        ValueError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        NotImplementedError,
        MemoryError,
    ):
        # Never return archive-controlled exception text/paths to the parent.
        print(
            json.dumps(
                {
                    "error": "ZIP rejected: unsafe, corrupt, encrypted, unsupported, or over 20 MB/20 files."
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
