"""One cancellation-safe owner for local TTS model lifecycles.

The server runs inference on one event loop. A cancelled HTTP/queue caller
must not release the model while a native worker is still using it.
"""

import asyncio
from functools import wraps
import weakref


_locks = weakref.WeakKeyDictionary()
_owners = set()
_cancel_requested = set()
_pending = set()
_closing = weakref.WeakSet()


async def run_inference(operation, *, _allow_closing=False):
    current = asyncio.current_task()
    if current in _owners:
        if current in _cancel_requested:
            raise asyncio.CancelledError
        result = await operation()
        if current in _cancel_requested:
            raise asyncio.CancelledError
        return result

    loop = asyncio.get_running_loop()
    if loop in _closing and not _allow_closing:
        raise RuntimeError("Inference service is shutting down")
    lock = _locks.setdefault(loop, asyncio.Lock())
    await lock.acquire()
    if loop in _closing and not _allow_closing:
        lock.release()
        raise RuntimeError("Inference service is shutting down")

    async def owned():
        owner = asyncio.current_task()
        _owners.add(owner)
        try:
            return await operation()
        finally:
            _owners.discard(owner)
            _cancel_requested.discard(owner)
            lock.release()

    task = asyncio.create_task(owned())
    _pending.add(task)

    def finished(completed):
        _pending.discard(completed)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(finished)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        _cancel_requested.add(task)
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        finally:
            _cancel_requested.discard(task)
        raise


def serialized_inference(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        return await run_inference(lambda: function(*args, **kwargs))

    return guarded


def start_inference():
    _closing.discard(asyncio.get_running_loop())


async def shutdown_inference(unload):
    """Reject new owners, drain the current owner, then tear down models."""
    _closing.add(asyncio.get_running_loop())
    return await run_inference(unload, _allow_closing=True)
