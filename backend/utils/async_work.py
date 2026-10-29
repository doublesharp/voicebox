"""Run blocking resource owners off-loop and drain them before cancellation cleanup."""

import asyncio
from functools import partial


async def run_blocking(operation, *args, _on_cancel=None, **kwargs):
    """Cancellation cannot stop a thread; retain ownership until it has returned."""
    return await run_owned(asyncio.to_thread(partial(operation, *args, **kwargs)), _on_cancel=_on_cancel)


async def run_owned(operation, *, _on_cancel=None):
    """Drain an async resource owner before its caller handles cancellation."""
    worker = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            cancelled = True
            if _on_cancel is not None:
                _on_cancel()
            if worker.done():
                if not worker.cancelled():
                    worker.exception()
                raise
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
    if cancelled:
        raise asyncio.CancelledError()
    return result
