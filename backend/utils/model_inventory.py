"""Short-lived, single-flight disk inventory without blocking the API loop."""

import asyncio
import time
from dataclasses import dataclass
from weakref import WeakKeyDictionary


@dataclass
class _Entry:
    key: object
    task: asyncio.Task | None
    expires: float = 0
    result: object = None


class ModelInventory:
    def __init__(self, ttl: float = 10):
        self.ttl = ttl
        self._generation = 0
        self._entries = WeakKeyDictionary()

    def invalidate(self):
        # A worker already scanning may finish, but its result cannot become
        # the inventory for a request made after this mutation.
        self._generation += 1

    async def get(self, key, scan):
        while True:
            generation = self._generation
            result = await self._get((generation, key), scan)
            if generation == self._generation:
                return result
            # A mutation overlapped the scan. Neither this caller nor later
            # callers should observe its obsolete pre-mutation snapshot.

    async def _get(self, key, scan):
        loop = asyncio.get_running_loop()
        entry = self._entries.get(loop)
        if entry is None or entry.key != key or (entry.task is None and entry.expires <= time.monotonic()):
            task = asyncio.create_task(asyncio.to_thread(scan))
            entry = _Entry(key, task)
            self._entries[loop] = entry

            def completed(done):
                if done.cancelled() or done.exception() is not None:
                    if self._entries.get(loop) is entry:
                        self._entries.pop(loop, None)
                else:
                    entry.result = done.result()
                    entry.expires = time.monotonic() + self.ttl
                    # A completed Task owns its event loop. Retain the result
                    # alone so isolated app lifecycles can release their loop.
                    entry.task = None

            task.add_done_callback(completed)
        if entry.task is None:
            return entry.result
        # One disconnected status client must not cancel other clients' scan.
        return await asyncio.shield(entry.task)


model_inventory = ModelInventory()
