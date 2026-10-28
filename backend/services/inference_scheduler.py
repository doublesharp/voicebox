"""Serialize TTS singleton state until synchronous inference really finishes.

Cancelling an await on ``asyncio.to_thread`` cannot stop its thread. The
request owns the TTS resource until its shielded operation drains; chunk
boundaries observe cancellation before starting more work. MLX retains its
own executor and thread affinity inside this boundary.
"""

from __future__ import annotations

import asyncio
import inspect
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Awaitable, Callable, TypeVar
from weakref import WeakKeyDictionary

T = TypeVar("T")


@dataclass
class InferenceScope:
    cancelled: bool = False


_scope: ContextVar[InferenceScope | None] = ContextVar("tts_inference_scope", default=None)
# Locks belong to an event loop, including isolated test/app lifecycles.
_locks: WeakKeyDictionary = WeakKeyDictionary()


def check_inference_cancelled() -> None:
    scope = _scope.get()
    if scope is not None and scope.cancelled:
        raise asyncio.CancelledError()


async def run_serialized_inference(operation: Callable[[], Awaitable[T]]) -> T:
    """Run one TTS operation, draining its worker before releasing ownership.

    All TTS engines share this resource because model singleton state and
    PyTorch's RNG are process-global, even when engines use different devices.
    Nested calls reuse ownership. CPU encoding and unrelated requests can run
    separately; STT/LLM retain their existing runtime policies.
    """
    if _scope.get() is not None:
        check_inference_cancelled()
        return await operation()

    loop = asyncio.get_running_loop()
    lock = _locks.setdefault(loop, asyncio.Lock())
    async with lock:
        scope = InferenceScope()
        token = _scope.set(scope)
        try:
            worker = asyncio.create_task(operation())
        finally:
            _scope.reset(token)

        cancellation_received = False
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancellation_received = True
                scope.cancelled = True
                if worker.done():
                    # Retrieve any exception so a disconnected caller cannot
                    # leave an unobserved worker failure behind.
                    if not worker.cancelled():
                        worker.exception()
                    raise
                # Repeated cancellation must still not free a live thread.
            except BaseException:
                if cancellation_received:
                    raise asyncio.CancelledError() from None
                raise
        if cancellation_received:
            raise asyncio.CancelledError()
        return result


def serialized_inference(function):
    """Decorate internal async orchestration, leaving HTTP signatures intact."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        return await run_serialized_inference(lambda: function(*args, **kwargs))
    wrapped.__signature__ = inspect.signature(function, eval_str=True)
    return wrapped
