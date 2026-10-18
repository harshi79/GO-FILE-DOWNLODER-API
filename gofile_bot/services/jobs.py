"""In-memory job registry.

Jobs are deliberately ephemeral: no database, no persistence. They exist only
to link an inline button press back to the share it was created for, and to
enforce one active download per user. Everything expires after
:data:`~gofile_bot.config.JOB_TTL`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from gofile_bot.config import JOB_TTL
from gofile_bot.gofile.models import Content


@dataclass
class Job:
    """A single download request (any provider) and its download state."""

    id: str
    user_id: int
    chat_id: int
    status_message_id: int
    content_id: str
    password: str | None = None
    #: Registry name of the provider handling this job (e.g. ``"gofile"``).
    provider: str = "gofile"
    #: Original link the user sent.
    source_url: str = ""
    content: Content | None = None
    created_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    running: bool = False

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class JobStore:
    """Thread-of-execution safe (single event loop) job storage."""

    def __init__(self, ttl: float = JOB_TTL) -> None:
        self._ttl = ttl
        self._jobs: dict[str, Job] = {}

    def create(
        self,
        *,
        user_id: int,
        chat_id: int,
        status_message_id: int,
        content_id: str = "",
        password: str | None = None,
        provider: str = "gofile",
        source_url: str = "",
    ) -> Job:
        self.purge_expired()
        job = Job(
            id=uuid.uuid4().hex,
            user_id=user_id,
            chat_id=chat_id,
            status_message_id=status_message_id,
            content_id=content_id,
            password=password,
            provider=provider,
            source_url=source_url,
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if time.time() - job.created_at > self._ttl:
            self._jobs.pop(job_id, None)
            return None
        return job

    def active_for_user(self, user_id: int) -> Job | None:
        for job in self._jobs.values():
            if job.user_id == user_id and job.running:
                return job
        return None

    def purge_expired(self) -> None:
        now = time.time()
        stale = [jid for jid, job in self._jobs.items() if now - job.created_at > self._ttl]
        for jid in stale:
            self._jobs.pop(jid, None)

    def __len__(self) -> int:
        return len(self._jobs)
