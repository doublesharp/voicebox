import asyncio
import threading

import pytest

from backend.services.inference_scheduler import check_inference_cancelled, run_serialized_inference


@pytest.mark.asyncio
async def test_cancelled_thread_keeps_inference_ownership_until_it_finishes():
    started = threading.Event()
    release = threading.Event()
    second_started = asyncio.Event()

    def blocking_inference():
        started.set()
        release.wait(timeout=3)

    async def first():
        await asyncio.to_thread(blocking_inference)

    async def second():
        second_started.set()

    task = asyncio.create_task(run_serialized_inference(first))
    next_task = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        next_task = asyncio.create_task(run_serialized_inference(second))
        await asyncio.sleep(0.03)
        assert not second_started.is_set(), 'cancelled inference thread still owns the device'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(next_task, 1)
        assert second_started.is_set()
    finally:
        release.set()
        await asyncio.gather(task, *([next_task] if next_task else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_waiting_cancelled_request_never_starts():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def first():
        entered.set()
        await release.wait()

    async def waiting():
        calls.append('started')

    task = asyncio.create_task(run_serialized_inference(first))
    await entered.wait()
    queued = asyncio.create_task(run_serialized_inference(waiting))
    try:
        await asyncio.sleep(0)
        queued.cancel()
        release.set()
        await asyncio.gather(task, queued, return_exceptions=True)
        assert calls == []
    finally:
        release.set()
        await asyncio.gather(task, queued, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_stops_at_next_chunk_boundary_after_thread_returns():
    entered = threading.Event()
    release = threading.Event()
    chunks = []

    def first_chunk():
        entered.set()
        release.wait(timeout=3)

    async def generate():
        await asyncio.to_thread(first_chunk)
        check_inference_cancelled()
        chunks.append('second chunk')

    task = asyncio.create_task(run_serialized_inference(generate))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert chunks == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_nested_inference_reuses_ownership():
    async def inner():
        return 'audio'

    async def outer():
        return await run_serialized_inference(inner)

    assert await asyncio.wait_for(run_serialized_inference(outer), 1) == 'audio'
