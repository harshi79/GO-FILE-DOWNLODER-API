"""Bounded metadata reads; no unbounded json()/text() response buffering."""

from __future__ import annotations

import asyncio
import json

from gofile_bot.config import MAX_METADATA_BYTES
from gofile_bot.errors import ResolveError


async def read_bytes(response) -> bytes:
    encoding = response.headers.get("Content-Encoding", "identity").lower()
    if encoding not in ("", "identity"):
        raise ResolveError(
            "Compressed metadata refused; server must honor Accept-Encoding: identity"
        )
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        if len(body) + len(chunk) > MAX_METADATA_BYTES:
            raise ResolveError(
                "Metadata response exceeds 1 MiB",
                user_message="⚠️ That share's metadata is too large to process safely. Try a smaller folder or single file.",
            )
        body.extend(chunk)
    return bytes(body)


async def read_json(response) -> dict:
    try:
        payload = await asyncio.to_thread(json.loads, await read_bytes(response))
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, RecursionError) as exc:
        raise ResolveError("Invalid metadata JSON") from exc


async def read_text(response) -> str:
    return (await read_bytes(response)).decode("utf-8", "replace")
