"""Bounded background work: handlers remain short and Cancel stays responsive."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from gofile_bot.errors import JobBusyError, ServiceBusyError


async def cancelable(awaitable, event: asyncio.Event, timeout: float | None = None):
    """Cancel and join work on user cancellation, timeout, or caller shutdown."""
    from gofile_bot.errors import JobCancelled

    work = asyncio.ensure_future(awaitable)
    stop = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            (work, stop), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if work in done:
            return await work
        if stop in done:
            raise JobCancelled()
        raise TimeoutError()
    finally:
        for task in (work, stop):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, stop, return_exceptions=True)


async def disk_io(function, *args, **kwargs):
    """Keep filesystem writes off the loop; join an in-flight write before cleanup."""
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await work
        raise


class MetadataTasks:
    """No waiting tasks: two small metadata requests, then explicit backpressure."""

    def __init__(self, capacity: int = 2) -> None:
        self.capacity = capacity
        self.users: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    def submit(self, user_id: int, factory: Callable, create_task: Callable) -> None:
        if self._closing:
            raise ServiceBusyError()
        if user_id in self.users:
            raise JobBusyError()
        if len(self.users) >= self.capacity:
            raise ServiceBusyError()
        self.users.add(user_id)

        async def run():
            try:
                async with asyncio.timeout(45):
                    await factory()
            finally:
                self.users.discard(user_id)

        coroutine = run()
        try:
            task = create_task(coroutine)
            if isinstance(task, asyncio.Task):
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except BaseException:
            coroutine.close()
            self.users.discard(user_id)
            raise

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.users.clear()
