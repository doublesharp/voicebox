"""Bounded bridge from a synchronous model iterator to asynchronous audio consumers."""

import asyncio
import concurrent.futures
import math
import threading
import time
from contextlib import closing
from numbers import Integral

import numpy as np


def audio_frame(audio, sample_rate):
    """Materialize on the model's thread; never queue device-backed arrays."""
    if not isinstance(sample_rate, Integral) or isinstance(sample_rate, bool) or sample_rate <= 0:
        raise ValueError("Native audio requires a positive integer sample rate")
    samples = np.array(audio, dtype=np.float32, copy=True)
    if samples.ndim != 1 or not np.isfinite(samples).all():
        raise ValueError("Native audio requires finite mono samples")
    return samples, int(sample_rate)


async def drain_worker(future):
    """Cancellation must not release model ownership before its native worker exits."""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not future.cancelled():
        future.exception()


async def iter_native_worker(factory, submit, *, stall_timeout=30.0):
    """Run and close one model iterator on its executor, with bounded backpressure.

    submit must return an asyncio Future for the physical worker. The owner
    retains its operation lock around this iterator, including its final drain.
    """
    if not math.isfinite(stall_timeout) or stall_timeout <= 0:
        raise ValueError("Native stream stall timeout must be positive and finite")
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=2)
    stopped = threading.Event()

    def produce():
        with closing(factory()) as iterator:
            for frame in iterator:
                if stopped.is_set():
                    return
                pending = asyncio.run_coroutine_threadsafe(queue.put(frame), loop)
                deadline = time.monotonic() + stall_timeout
                try:
                    while not stopped.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Native audio consumer stalled")
                        try:
                            pending.result(timeout=min(0.1, remaining))
                            break
                        except concurrent.futures.TimeoutError:
                            continue
                    else:
                        return
                finally:
                    if not pending.done():
                        pending.cancel()

    worker = submit(produce)
    try:
        while True:
            if worker.done() and queue.empty():
                worker.result()  # Propagate a worker error after buffered frames.
                return
            receive = asyncio.create_task(queue.get())
            try:
                ready, _ = await asyncio.wait((worker, receive), return_when=asyncio.FIRST_COMPLETED)
                if receive in ready:
                    yield receive.result()
            finally:
                if not receive.done():
                    receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
    finally:
        stopped.set()
        await drain_worker(worker)
