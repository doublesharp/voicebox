"""Serialize TTS singleton state until synchronous inference really finishes.

Cancelling an await on ``asyncio.to_thread`` cannot stop its thread. The
request owns the TTS resource until its shielded operation drains; chunk
boundaries observe cancellation before starting more work. MLX retains its
own executor and thread affinity inside this boundary.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import TypeVar
from weakref import WeakKeyDictionary, WeakSet

T = TypeVar("T")


@dataclass
class InferenceScope:
    cancelled: bool = False


_scope: ContextVar[InferenceScope | None] = ContextVar("tts_inference_scope", default=None)
# Locks belong to an event loop, including isolated test/app lifecycles.
_locks: WeakKeyDictionary = WeakKeyDictionary()
_closing: WeakSet = WeakSet()


def check_inference_cancelled() -> None:
    scope = _scope.get()
    if scope is not None and scope.cancelled:
        raise asyncio.CancelledError()


async def run_serialized_inference(
    operation: Callable[[], Awaitable[T]], *, _allow_closing: bool = False
) -> T:
    """Run one TTS operation, draining its worker before releasing ownership.

    All TTS engines share this resource because model singleton state and
    PyTorch's RNG are process-global, even when engines use different devices.
    Nested calls reuse ownership. CPU encoding and unrelated requests can run
    separately; STT/LLM retain their existing runtime policies.
    """
    if _scope.get() is not None:
        check_inference_cancelled()
        result = await operation()
        check_inference_cancelled()
        return result

    loop = asyncio.get_running_loop()
    if loop in _closing and not _allow_closing:
        raise RuntimeError("Inference service is shutting down")
    lock = _locks.setdefault(loop, asyncio.Lock())
    async with lock:
        if loop in _closing and not _allow_closing:
            raise RuntimeError("Inference service is shutting down")
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


def start_inference() -> None:
    """Open admission for a new application lifecycle on this event loop."""
    _closing.discard(asyncio.get_running_loop())


async def shutdown_inference(unload: Callable[[], Awaitable[T]]) -> T:
    """Reject queued/new work, drain the physical owner, then unload models."""
    _closing.add(asyncio.get_running_loop())
    return await run_serialized_inference(unload, _allow_closing=True)
