"""Async parent for the memory/CPU-limited ZIP subprocess."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

from gofile_bot.config import MAX_ZIP_BYTES, MAX_ZIP_FILES, ZIP_TIMEOUT
from gofile_bot.errors import ZipSafetyError
from gofile_bot.services.resources import cancelable


def is_zip(file) -> bool:
    return file.name.lower().endswith(".zip") or file.mimetype in (
        "application/zip",
        "application/x-zip-compressed",
    )


async def extract_safely(
    source: Path, destination: Path, cancel_event: asyncio.Event
) -> list[Path]:
    if source.stat().st_size > MAX_ZIP_BYTES:
        raise ZipSafetyError()
    if destination.exists():
        raise ZipSafetyError("Extraction destination must be fresh")
    success = False
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "gofile_bot.services.zip_worker",
        str(source),
        str(destination),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=32768,
    )
    try:
        # Output is a bounded JSON manifest (20 names, at most 200 chars each).
        output, _ = await cancelable(process.communicate(), cancel_event, ZIP_TIMEOUT)
        if process.returncode or len(output) > 32768:
            raise ZipSafetyError()
        try:
            names = json.loads(output)["files"]
            if not isinstance(names, list) or not 0 < len(names) <= MAX_ZIP_FILES:
                raise ValueError()
            paths = [(destination / name).resolve() for name in names]
            root = destination.resolve()
            if any(not p.is_relative_to(root) or not p.is_file() for p in paths):
                raise ValueError()
            if sum(p.stat().st_size for p in paths) > MAX_ZIP_BYTES:
                raise ValueError()
            success = True
            return paths
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ZipSafetyError() from exc
    except TimeoutError as exc:
        raise ZipSafetyError(
            user_message="⚠️ ZIP extraction exceeded the safe time limit and was stopped."
        ) from exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if not success:
            await asyncio.to_thread(shutil.rmtree, destination, ignore_errors=True)
