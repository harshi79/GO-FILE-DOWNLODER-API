"""Persistent usage statistics and operator controls (SQLite, stdlib only).

The bot itself keeps *no* content database — jobs and cards stay in memory.
This store only remembers lightweight counters so an operator can answer
"who is using the bot, how many downloads, how much data, who is banned?"
from the ``/admin`` panel, plus per-user ``/me`` stats.

SQLite is used because it is part of the standard library, crash-safe,
and cheap enough for a single-process bot. Every call is executed in a
worker thread so the event loop never blocks on disk I/O. If the configured
path is not writable (read-only container), the store transparently falls
back to an in-memory database and logs a warning.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    commands    INTEGER NOT NULL DEFAULT 0,
    downloads   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    provider    TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT 'download',
    status      TEXT NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    duration    REAL NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id);
CREATE INDEX IF NOT EXISTS idx_downloads_time ON downloads(created_at);
CREATE TABLE IF NOT EXISTS bans (
    user_id     INTEGER PRIMARY KEY,
    banned_by   INTEGER,
    reason      TEXT,
    created_at  REAL NOT NULL
);
"""


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class UserStats:
    user_id: int
    username: str | None = None
    first_name: str | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    commands: int = 0
    downloads: int = 0
    failed: int = 0
    bytes: int = 0
    banned: bool = False
    ban_reason: str = ""

    @property
    def name(self) -> str:
        """Human label: first name, else @username, else the numeric id."""
        if self.first_name:
            return self.first_name
        if self.username:
            return f"@{self.username}"
        return str(self.user_id)

    @property
    def mention_html(self) -> str:
        import html as html_module

        label = html_module.escape(self.name, quote=False)
        return f'<a href="tg://user?id={self.user_id}">{label}</a>'


@dataclass(frozen=True)
class DownloadRecord:
    user_id: int
    user_name: str
    provider: str
    action: str
    status: str
    bytes: int
    duration: float
    created_at: float


@dataclass
class Overview:
    users: int = 0
    new_users_24h: int = 0
    downloads: int = 0
    failed: int = 0
    bytes: int = 0
    downloads_24h: int = 0
    bytes_24h: int = 0
    active_bans: int = 0
    providers: list[tuple[str, int, int]] = field(default_factory=list)


class UsageStore:
    """Async facade over one SQLite connection (serialized by a lock)."""

    def __init__(self, path: str | Path | None = "data/usage.sqlite3") -> None:
        self.path = str(path) if path else None
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._banned: set[int] = set()
        self._banned_reasons: dict[int, str] = {}
        self._memory_fallback = not bool(path)
        self._closed = False

    # -- lifecycle -------------------------------------------------------
    @property
    def using_memory(self) -> bool:
        """True when statistics are not being persisted to disk."""
        return self._memory_fallback

    async def open(self) -> None:
        async with self._lock:
            if self._conn is not None:
                return
            await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        try:
            if self.path:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            else:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
        except (OSError, sqlite3.Error) as exc:
            logger.warning(
                "usage database %s is unusable (%s); falling back to in-memory stats",
                self.path,
                exc,
            )
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            self._memory_fallback = True
        self._conn = conn
        self._load_bans_sync()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    # -- plumbing --------------------------------------------------------
    async def _execute(self, fn):
        if self._closed:
            return None
        async with self._lock:
            if self._conn is None:
                await asyncio.to_thread(self._open_sync)
            try:
                return await asyncio.to_thread(fn, self._conn)
            except sqlite3.Error:
                logger.exception("usage database operation failed")
                return None

    def _load_bans_sync(self) -> None:
        conn = self._conn
        if conn is None:
            return
        self._banned = {row["user_id"] for row in conn.execute("SELECT user_id FROM bans")}
        self._banned_reasons = {
            row["user_id"]: (row["reason"] or "")
            for row in conn.execute("SELECT user_id, reason FROM bans")
        }

    # -- user tracking ---------------------------------------------------
    async def touch_user(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        command: str | None = None,
    ) -> None:
        """Record activity (and optionally one command) for *user_id*."""

        def run(conn: sqlite3.Connection) -> None:
            now = _now()
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, first_seen, last_seen, commands)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    last_seen = excluded.last_seen,
                    commands = users.commands + excluded.commands
                """,
                (user_id, username, first_name, now, now, 1 if command else 0),
            )
            conn.commit()

        await self._execute(run)

    async def record_download(
        self,
        user_id: int,
        *,
        provider: str = "",
        action: str = "download",
        status: str = "ok",
        bytes: int = 0,
        duration: float = 0.0,
        username: str | None = None,
        first_name: str | None = None,
    ) -> None:
        """Store one finished transfer and update the user's aggregates."""

        def run(conn: sqlite3.Connection) -> None:
            now = _now()
            ok = status in ("ok", "complete", "success")
            conn.execute(
                """
                INSERT INTO downloads
                    (user_id, provider, action, status, bytes, duration, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, provider[:32], action[:16], status[:24], int(bytes), float(duration), now),
            )
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    last_seen = excluded.last_seen
                """,
                (user_id, username, first_name, now, now),
            )
            if ok:
                conn.execute(
                    "UPDATE users SET downloads = downloads + 1, bytes = bytes + ? WHERE user_id = ?",
                    (int(bytes), user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET failed = failed + 1 WHERE user_id = ?", (user_id,)
                )
            conn.commit()

        await self._execute(run)

    # -- reading ---------------------------------------------------------
    async def overview(self) -> Overview:
        def run(conn: sqlite3.Connection) -> Overview:
            day_ago = _now() - 86_400
            row = conn.execute(
                """
                SELECT COUNT(*) AS users,
                       COALESCE(SUM(downloads), 0) AS downloads,
                       COALESCE(SUM(failed), 0) AS failed,
                       COALESCE(SUM(bytes), 0) AS bytes,
                       COALESCE(SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END), 0) AS new_users
                FROM users
                """,
                (day_ago,),
            ).fetchone()
            recent = conn.execute(
                """
                SELECT COUNT(*) AS downloads, COALESCE(SUM(bytes), 0) AS bytes
                FROM downloads WHERE created_at >= ?
                """,
                (day_ago,),
            ).fetchone()
            bans = conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()
            providers = [
                (entry["provider"] or "unknown", entry["n"], entry["bytes"] or 0)
                for entry in conn.execute(
                    """
                    SELECT provider, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS bytes
                    FROM downloads WHERE status IN ('ok', 'complete', 'success')
                    GROUP BY provider ORDER BY n DESC LIMIT 6
                    """
                )
            ]
            return Overview(
                users=row["users"],
                new_users_24h=row["new_users"],
                downloads=row["downloads"],
                failed=row["failed"],
                bytes=row["bytes"],
                downloads_24h=recent["downloads"],
                bytes_24h=recent["bytes"],
                active_bans=bans["n"],
                providers=providers,
            )

        return await self._execute(run) or Overview()

    async def user(self, user_id: int) -> UserStats | None:
        def run(conn: sqlite3.Connection) -> UserStats | None:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                return None
            return self._user_from_row(row)

        return await self._execute(run)

    def _user_from_row(self, row: sqlite3.Row) -> UserStats:
        return UserStats(
            user_id=row["user_id"],
            username=row["username"],
            first_name=row["first_name"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            commands=row["commands"],
            downloads=row["downloads"],
            failed=row["failed"],
            bytes=row["bytes"],
            banned=row["user_id"] in self._banned,
            ban_reason=self._banned_reasons.get(row["user_id"], ""),
        )

    async def list_users(
        self,
        *,
        page: int = 0,
        per_page: int = 8,
        order: str = "downloads",
    ) -> tuple[list[UserStats], int]:
        order_sql = {
            "downloads": "downloads DESC, last_seen DESC",
            "recent": "last_seen DESC",
            "bytes": "bytes DESC, last_seen DESC",
            "new": "first_seen DESC",
        }.get(order, "downloads DESC, last_seen DESC")
        per_page = max(1, min(50, per_page))
        offset = max(0, page) * per_page

        def run(conn: sqlite3.Connection):
            total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM users ORDER BY {order_sql} LIMIT ? OFFSET ?",
                (per_page, offset),
            ).fetchall()
            return [self._user_from_row(row) for row in rows], total

        return await self._execute(run) or ([], 0)

    async def recent_downloads(self, limit: int = 10) -> list[DownloadRecord]:
        limit = max(1, min(50, limit))

        def run(conn: sqlite3.Connection):
            rows = conn.execute(
                """
                SELECT d.*, COALESCE(u.first_name, u.username, '') AS user_name
                FROM downloads d LEFT JOIN users u ON u.user_id = d.user_id
                ORDER BY d.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                DownloadRecord(
                    user_id=row["user_id"],
                    user_name=row["user_name"] or str(row["user_id"]),
                    provider=row["provider"],
                    action=row["action"],
                    status=row["status"],
                    bytes=row["bytes"],
                    duration=row["duration"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

        return await self._execute(run) or []

    # -- bans ------------------------------------------------------------
    def is_banned(self, user_id: int) -> bool:
        """Cached check — safe to call from handlers without awaiting."""
        return user_id in self._banned

    @property
    def banned_ids(self) -> set[int]:
        return set(self._banned)

    async def ban(self, user_id: int, *, banned_by: int = 0, reason: str = "") -> None:
        def run(conn: sqlite3.Connection) -> None:
            now = _now()
            conn.execute(
                "INSERT INTO users (user_id, first_seen, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO bans (user_id, banned_by, reason, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    banned_by = excluded.banned_by,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (user_id, banned_by, reason[:200], now),
            )
            conn.commit()

        await self._execute(run)
        self._banned.add(user_id)
        self._banned_reasons[user_id] = reason[:200]

    async def unban(self, user_id: int) -> bool:
        def run(conn: sqlite3.Connection) -> bool:
            changed = conn.execute("DELETE FROM bans WHERE user_id = ?", (user_id,)).rowcount
            conn.commit()
            return bool(changed)

        changed = await self._execute(run)
        self._banned.discard(user_id)
        self._banned_reasons.pop(user_id, None)
        return bool(changed)

    async def banned(self, *, page: int = 0, per_page: int = 8) -> tuple[list[UserStats], int]:
        per_page = max(1, min(50, per_page))
        offset = max(0, page) * per_page

        def run(conn: sqlite3.Connection):
            total = conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()["n"]
            rows = conn.execute(
                """
                SELECT u.* FROM bans b LEFT JOIN users u ON u.user_id = b.user_id
                ORDER BY b.created_at DESC LIMIT ? OFFSET ?
                """,
                (per_page, offset),
            ).fetchall()
            stats = []
            for row in rows:
                if row["user_id"] is None:
                    continue
                stats.append(self._user_from_row(row))
            return stats, total

        return await self._execute(run) or ([], 0)

    # -- maintenance -----------------------------------------------------
    async def reset_user(self, user_id: int) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE users SET downloads = 0, failed = 0, bytes = 0, commands = 0 "
                "WHERE user_id = ?",
                (user_id,),
            )
            conn.execute("DELETE FROM downloads WHERE user_id = ?", (user_id,))
            conn.commit()

        await self._execute(run)

    async def purge_downloads(self, older_than_days: int = 90) -> int:
        """Drop old per-download rows; aggregate counters stay intact."""
        cutoff = _now() - max(1, older_than_days) * 86_400

        def run(conn: sqlite3.Connection) -> int:
            removed = conn.execute(
                "DELETE FROM downloads WHERE created_at < ?", (cutoff,)
            ).rowcount
            conn.commit()
            return int(removed or 0)

        return int(await self._execute(run) or 0)
