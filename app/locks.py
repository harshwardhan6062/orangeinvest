from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary


class SessionLocks:
    """One asyncio.Lock per session. An entry disappears once no task holds or
    waits on it, so this doesn't grow with the number of sessions ever seen.
    In-process only: multiple instances need a DB or advisory lock."""

    def __init__(self) -> None:
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        async with lock:
            yield
