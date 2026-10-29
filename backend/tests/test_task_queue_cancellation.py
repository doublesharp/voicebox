import asyncio

import pytest

from backend.services import task_queue


@pytest.mark.asyncio
async def test_cancel_queued_generation_skips_execution():
    task_queue.init_queue(force=True)

    running_started = asyncio.Event()
    release_running = asyncio.Event()
    queued_ran = asyncio.Event()

    async def running_job():
        running_started.set()
        await release_running.wait()

    async def queued_job():
        queued_ran.set()

    task_queue.enqueue_generation("gen-running", running_job())
    await asyncio.wait_for(running_started.wait(), timeout=1)

    task_queue.enqueue_generation("gen-queued", queued_job())
    assert task_queue.cancel_generation("gen-queued") == "queued"

    release_running.set()
    await asyncio.sleep(0.1)

    assert not queued_ran.is_set()


@pytest.mark.asyncio
async def test_cancel_running_generation_cancels_task():
    task_queue.init_queue(force=True)

    running_started = asyncio.Event()
    running_cancelled = asyncio.Event()

    async def running_job():
        running_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            running_cancelled.set()
            raise

    task_queue.enqueue_generation("gen-running", running_job())
    await asyncio.wait_for(running_started.wait(), timeout=1)

    assert task_queue.cancel_generation("gen-running") == "running"
    await asyncio.wait_for(running_cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_cancel_retry_before_original_cancelled_job_is_drained():
    task_queue.init_queue(force=True)
    started, release, retry_ran = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocker():
        started.set()
        await release.wait()

    async def cancelled_job():
        pytest.fail("cancelled generation should not run")

    async def retry():
        retry_ran.set()

    task_queue.enqueue_generation("blocker", blocker())
    await asyncio.wait_for(started.wait(), timeout=2)
    task_queue.enqueue_generation("generation", cancelled_job())
    assert task_queue.cancel_generation("generation") == "queued"
    task_queue.enqueue_generation("generation", retry())
    assert task_queue.cancel_generation("generation") == "queued"
    release.set()
    await asyncio.wait_for(task_queue._generation_queue.join(), timeout=2)
    assert not retry_ran.is_set()


@pytest.mark.asyncio
async def test_cancel_retry_after_original_cancelled_job_is_drained():
    task_queue.init_queue(force=True)
    first_started, release_first = asyncio.Event(), asyncio.Event()
    second_started, release_second = asyncio.Event(), asyncio.Event()
    retry_ran = asyncio.Event()

    async def blocker(started, release):
        started.set()
        await release.wait()

    async def cancelled_job():
        pytest.fail("cancelled generation should not run")

    async def retry():
        retry_ran.set()

    task_queue.enqueue_generation("first", blocker(first_started, release_first))
    await asyncio.wait_for(first_started.wait(), timeout=2)
    task_queue.enqueue_generation("generation", cancelled_job())
    assert task_queue.cancel_generation("generation") == "queued"
    task_queue.enqueue_generation("second", blocker(second_started, release_second))
    task_queue.enqueue_generation("generation", retry())
    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=2)
    try:
        assert task_queue.cancel_generation("generation") == "queued"
    finally:
        release_second.set()
        await asyncio.wait_for(task_queue._generation_queue.join(), timeout=2)
    assert not retry_ran.is_set()


@pytest.mark.asyncio
async def test_duplicate_queued_generation_is_rejected_without_losing_original():
    task_queue.init_queue(force=True)
    started, release, ran = asyncio.Event(), asyncio.Event(), []

    async def blocker():
        started.set()
        await release.wait()

    async def job(label):
        ran.append(label)

    task_queue.enqueue_generation("blocker", blocker())
    await asyncio.wait_for(started.wait(), timeout=2)
    task_queue.enqueue_generation("generation", job("original"))
    duplicate = job("duplicate")
    try:
        with pytest.raises(ValueError, match="already queued or running"):
            task_queue.enqueue_generation("generation", duplicate)
        assert duplicate.cr_frame is None
    finally:
        duplicate.close()
        release.set()
        await asyncio.wait_for(task_queue._generation_queue.join(), timeout=2)
    assert ran == ["original"]


@pytest.mark.asyncio
async def test_force_reset_cancels_old_worker_and_closes_queued_jobs():
    task_queue.init_queue(force=True)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def running():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def queued():
        pytest.fail("discarded job must not run")

    task_queue.enqueue_generation("running", running())
    await asyncio.wait_for(started.wait(), timeout=2)
    discarded = queued()
    task_queue.enqueue_generation("queued", discarded)
    old_queue = task_queue._generation_queue
    old_worker = task_queue._generation_worker_task
    task_queue.init_queue(force=True)
    await asyncio.wait_for(stopped.wait(), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await old_worker
    await asyncio.wait_for(old_queue.join(), timeout=2)
    assert discarded.cr_frame is None
    ran = asyncio.Event()

    async def new_job():
        ran.set()

    task_queue.enqueue_generation("new", new_job())
    await asyncio.wait_for(task_queue._generation_queue.join(), timeout=2)
    assert ran.is_set()
